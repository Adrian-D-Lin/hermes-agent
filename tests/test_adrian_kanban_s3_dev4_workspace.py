"""Real DEV4 segment merge and retained-workspace retirement orchestration."""

from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tests.test_adrian_kanban_s3_orchestration_checkpoints import (
    _approve,
    _database,
    _seed_initiative,
    _submit,
    commands_module,  # noqa: F401
)
from tests.test_adrian_kanban_s3_dev4_checkpoints import (
    _accept_dev4_2_chain,
    _dev4_3,
    _dev4_4,
    _dev4_5,
    _seed_dev4_tasks,
)
from tests.test_adrian_kanban_s3_initiatives import (
    _approve as _approve_transition,
    _seed_reconciliation,
    _submit as _submit_transition,
)
from tests.test_adrian_kanban_s3_transition_integration import (
    _git,
    _repository_with_origin_main,
)


def _workspace_case(commands_module, tmp_path, monkeypatch):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV4")
    repository, base_sha = _repository_with_origin_main(tmp_path / "git")
    controlled_root = (tmp_path / "segment-workspaces").resolve()
    workspace = importlib.import_module(f"{commands_module.__package__}.workspace")
    registry = workspace._TrustedRepositoryRegistry(
        (
            workspace._RepositoryRegistration(
                repository_identity="repo-1",
                repository_root=str(repository),
                controlled_worktree_root=str(controlled_root),
            ),
        )
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO initiative_segment_projections "
            "(projection_id, projection_version, initiative_card_id, initiative_id, "
            "manifest_path, manifest_sha, content_digest, parsed_segment_definitions, "
            "readiness_refs, validation_result, projected_at) VALUES "
            "('projection-1', 1, ?, 'initiative-1', 'segments.json', ?, ?, ?, ?, "
            "'accepted', 2)",
            (
                card_id,
                "a" * 40,
                "b" * 64,
                json.dumps([{"segment_id": "S1", "ordinal": 1, "dependency_ids": []}]),
                json.dumps({"S1": "ready:S1"}),
            ),
        )
        conn.execute(
            "INSERT INTO segment_workspaces "
            "(workspace_id, initiative_card_id, initiative_id, segment_id, projection_id, "
            "lifecycle_state, controller_binding_ref, active, created_at, updated_at) "
            "VALUES ('initiative-1:S1', ?, 'initiative-1', 'S1', 'projection-1', "
            "'planned', 'adrian-kanban:workspace-controller:v1', 1, 2, 2)",
            (card_id,),
        )
        conn.execute(
            "INSERT INTO segment_workspace_members "
            "(workspace_id, repository_identity, relative_path, branch, required_base_sha, "
            "observed_head, member_state, observed_at) VALUES "
            "('initiative-1:S1', 'repo-1', 'initiative-1/S1/repo-1', "
            "'initiative-1/S1', NULL, NULL, 'planned', 2)"
        )
        conn.commit()
        segment_root = workspace._materialize_segment_workspace(
            conn,
            registry,
            initiative_id="initiative-1",
            segment_id="S1",
            materialized_at=10,
        )
    member_path = Path(segment_root) / "repo-1"
    return path, provider, workspace, registry, repository, member_path, base_sha


def _commit(member_path: Path, name: str, content: str) -> str:
    (member_path / name).write_text(content, encoding="utf-8")
    _git(member_path, "add", name)
    _git(member_path, "commit", "-m", f"add {name}")
    return _git(member_path, "rev-parse", "HEAD")


def test_dev4_merge_coordinator_seals_current_delivery_and_is_recoverable(
    commands_module, tmp_path, monkeypatch
):
    path, _, workspace, registry, repository, member_path, base_sha = _workspace_case(
        commands_module, tmp_path, monkeypatch
    )
    accepted_sha = _commit(member_path, "feature.txt", "accepted\n")
    delivery_sha = _commit(member_path, "dev4-evidence.txt", "closure evidence\n")

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        result = workspace._merge_segment_workspace(
            conn,
            registry,
            initiative_id="initiative-1",
            segment_id="S1",
            accepted_member_heads=(
                {"repository_identity": "repo-1", "accepted_sha": accepted_sha},
            ),
            merged_at=20,
        )
        assert result["workspace_id"] == "initiative-1:S1"
        assert result["member_merges"] == [
            {
                "repository_identity": "repo-1",
                "dev3_accepted_sha": accepted_sha,
                "segment_delivery_sha": delivery_sha,
                "merge_operation_id": "workspace-merge-initiative-1:S1-repo-1-" + delivery_sha,
                "merge_commit_sha": result["member_merges"][0]["merge_commit_sha"],
            }
        ]
        merge_sha = result["member_merges"][0]["merge_commit_sha"]
        assert len(merge_sha) == 40
        assert tuple(conn.execute(
            "SELECT observed_head, member_state FROM segment_workspace_members "
            "WHERE workspace_id='initiative-1:S1' AND repository_identity='repo-1'"
        ).fetchone()) == (delivery_sha, "merged")
        assert conn.execute(
            "SELECT state FROM external_operation_journal WHERE operation_kind='workspace_merge' "
            "ORDER BY event_id DESC LIMIT 1"
        ).fetchone()[0] == "verified"

        # A retry consumes the same verified journal and does not create a second merge.
        assert workspace._merge_segment_workspace(
            conn,
            registry,
            initiative_id="initiative-1",
            segment_id="S1",
            accepted_member_heads=(
                {"repository_identity": "repo-1", "accepted_sha": accepted_sha},
            ),
            merged_at=20,
        ) == result

    assert _git(repository, "rev-parse", "origin/main") == merge_sha
    assert _git(repository, "rev-list", "--parents", "-n", "1", merge_sha).count(" ") == 2
    assert subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", accepted_sha, "origin/main"]
    ).returncode == 0
    assert base_sha != delivery_sha


def test_dev4_merge_coordinator_rejects_unaccepted_or_dirty_delivery_without_merge(
    commands_module, tmp_path, monkeypatch
):
    path, _, workspace, registry, repository, member_path, base_sha = _workspace_case(
        commands_module, tmp_path, monkeypatch
    )
    _commit(member_path, "feature.txt", "accepted\n")
    (member_path / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(workspace._WorkspaceRejected):
            workspace._merge_segment_workspace(
                conn,
                registry,
                initiative_id="initiative-1",
                segment_id="S1",
                accepted_member_heads=(
                    {"repository_identity": "repo-1", "accepted_sha": "f" * 40},
                ),
                merged_at=20,
            )
        assert tuple(conn.execute(
            "SELECT observed_head, member_state FROM segment_workspace_members "
            "WHERE workspace_id='initiative-1:S1'"
        ).fetchone()) == (base_sha, "materialized")
        assert conn.execute(
            "SELECT COUNT(*) FROM external_operation_journal WHERE operation_kind='workspace_merge'"
        ).fetchone()[0] == 0
    assert _git(repository, "rev-parse", "origin/main") == base_sha


def test_dev4_retirement_coordinator_retains_clean_merged_worktree(
    commands_module, tmp_path, monkeypatch
):
    path, _, workspace, registry, _, member_path, _ = _workspace_case(
        commands_module, tmp_path, monkeypatch
    )
    accepted_sha = _commit(member_path, "feature.txt", "accepted\n")
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        workspace._merge_segment_workspace(
            conn,
            registry,
            initiative_id="initiative-1",
            segment_id="S1",
            accepted_member_heads=(
                {"repository_identity": "repo-1", "accepted_sha": accepted_sha},
            ),
            merged_at=20,
        )
        result = workspace._retire_segment_workspace(
            conn,
            registry,
            initiative_id="initiative-1",
            segment_id="S1",
            retired_at=30,
        )
        assert result == {
            "workspace_id": "initiative-1:S1",
            "retired_member_ids": ["repo-1"],
            "retired_at": 30,
        }
        assert workspace._retire_segment_workspace(
            conn,
            registry,
            initiative_id="initiative-1",
            segment_id="S1",
            retired_at=30,
        ) == result
        assert tuple(conn.execute(
            "SELECT lifecycle_state, active FROM segment_workspaces "
            "WHERE workspace_id='initiative-1:S1'"
        ).fetchone()) == ("retired", 0)
    assert member_path.is_dir()
    assert _git(member_path, "status", "--porcelain") == ""


def _seed_dev3_member_heads(path, card_id, accepted_sha):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, iteration, "
            "result_kind, contract_id, contract_version, canonical_payload, "
            "accepted_task_refs, accepted_checkpoint_refs, actor_evidence, idempotency_key, "
            "accepted, created_at) VALUES "
            "('checkpoint:DEV3.9:S1:cycle-1', ?, 'initiative-1', 'DEV3', 'S1', 1, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.dev3', '1', ?, '[]', "
            "'[]', ?, 'dev3-checkpoint-key', 1, 2)",
            (
                card_id,
                json.dumps(
                    {
                        "step": "DEV3.9",
                        "next_route": "DEV4",
                        "accepted_member_heads": [
                            {"repository_identity": "repo-1", "accepted_sha": accepted_sha}
                        ],
                    }
                ),
                json.dumps({"actor_profile": "default", "source_transition_id": 1}),
            ),
        )
        conn.commit()


def _update_boundary(commands_module, path, provider, registry):
    return commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={"kanban_update_initiative": commands_module._handle_update_initiative},
        workspace_registry=registry,
    )


def test_public_dev4_3_and_4_run_approved_effects_before_checkpoint_commit(
    commands_module, tmp_path, monkeypatch
):
    path, provider, workspace, registry, repository, member_path, _ = _workspace_case(
        commands_module, tmp_path, monkeypatch
    )
    card_id = _seed_dev4_tasks_for_existing_initiative(path)
    _accept_dev4_2_chain(path, provider, commands_module)
    accepted_sha = _commit(member_path, "feature.txt", "accepted\n")
    delivery_sha = _commit(member_path, "dev4-evidence.txt", "closure evidence\n")
    _seed_dev3_member_heads(path, card_id, accepted_sha)
    operation_id = "workspace-merge-initiative-1:S1-repo-1-" + delivery_sha
    merge_payload = _dev4_3(accepted_sha, delivery_sha, operation_id)
    _approve(path, merge_payload, version=4)
    boundary = _update_boundary(commands_module, path, provider, registry)

    merged = _submit(boundary, merge_payload, version=4)

    assert merged["result"] == "ACCEPTED", merged
    merge_sha = _git(repository, "rev-parse", "origin/main")
    assert subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", delivery_sha, merge_sha]
    ).returncode == 0

    retire_payload = _dev4_4()
    _approve(path, retire_payload, version=5)
    retired = _submit(boundary, retire_payload, version=5)

    assert retired["result"] == "ACCEPTED", retired
    assert member_path.is_dir()
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT lifecycle_state, active FROM segment_workspaces "
            "WHERE workspace_id='initiative-1:S1'"
        ).fetchone() == ("retired", 0)
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id='approval:DEV4.4:S1'"
        ).fetchone()[0] == "consumed"


def _seed_dev4_tasks_for_existing_initiative(path):
    with sqlite3.connect(path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE initiative_id='initiative-1'"
        ).fetchone()[0]
    _seed_dev4_tasks(path, card_id, workspace_id="initiative-1:S1")
    return card_id


def test_unapproved_dev4_merge_request_cannot_touch_git_or_journal(
    commands_module, tmp_path, monkeypatch
):
    path, provider, _, registry, repository, member_path, base_sha = _workspace_case(
        commands_module, tmp_path, monkeypatch
    )
    card_id = _seed_dev4_tasks_for_existing_initiative(path)
    _accept_dev4_2_chain(path, provider, commands_module)
    accepted_sha = _commit(member_path, "feature.txt", "accepted\n")
    _seed_dev3_member_heads(path, card_id, accepted_sha)
    operation_id = "workspace-merge-initiative-1:S1-repo-1-" + accepted_sha
    payload = _dev4_3(accepted_sha, accepted_sha, operation_id)

    response = _submit(
        _update_boundary(commands_module, path, provider, registry),
        payload,
        version=4,
    )

    assert response["result"] == "REJECTED", response
    assert _git(repository, "rev-parse", "origin/main") == base_sha
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM external_operation_journal WHERE operation_kind='workspace_merge'"
        ).fetchone()[0] == 0


def test_dev4_5_transition_materializes_next_segment_from_updated_main(
    commands_module, tmp_path, monkeypatch
):
    """S1 merge/retirement and DEV4.5 make S1 the exact base of DEV2/S2."""
    path, provider, _, registry, repository, member_path, _ = _workspace_case(
        commands_module, tmp_path, monkeypatch
    )
    card_id = _seed_dev4_tasks_for_existing_initiative(path)
    definitions = [
        {"segment_id": "S1", "ordinal": 1, "dependency_ids": []},
        {"segment_id": "S2", "ordinal": 2, "dependency_ids": ["S1"]},
    ]
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE initiative_segment_projections "
            "SET parsed_segment_definitions=?, readiness_refs=? "
            "WHERE projection_id='projection-1'",
            (
                json.dumps(definitions),
                json.dumps({"S1": "ready:S1", "S2": "ready:S2"}),
            ),
        )
        conn.execute(
            "INSERT INTO segment_workspaces "
            "(workspace_id, initiative_card_id, initiative_id, segment_id, projection_id, "
            "lifecycle_state, controller_binding_ref, active, created_at, updated_at) "
            "VALUES ('initiative-1:S2', ?, 'initiative-1', 'S2', 'projection-1', "
            "'planned', 'adrian-kanban:workspace-controller:v1', 1, 2, 2)",
            (card_id,),
        )
        conn.execute(
            "INSERT INTO segment_workspace_members "
            "(workspace_id, repository_identity, relative_path, branch, required_base_sha, "
            "observed_head, member_state, observed_at) VALUES "
            "('initiative-1:S2', 'repo-1', 'initiative-1/S2/repo-1', "
            "'initiative-1/S2', NULL, NULL, 'planned', 2)"
        )
        conn.commit()

    _accept_dev4_2_chain(path, provider, commands_module)
    accepted_sha = _commit(member_path, "feature.txt", "S1 accepted feature\n")
    delivery_sha = _commit(member_path, "dev4-evidence.txt", "S1 closure evidence\n")
    _seed_dev3_member_heads(path, card_id, accepted_sha)
    operation_id = "workspace-merge-initiative-1:S1-repo-1-" + delivery_sha
    update_boundary = _update_boundary(commands_module, path, provider, registry)

    for version, payload in (
        (4, _dev4_3(accepted_sha, delivery_sha, operation_id)),
        (5, _dev4_4()),
        (6, _dev4_5()),
    ):
        _approve(path, payload, version=version)
        response = _submit(update_boundary, payload, version=version)
        assert response["result"] == "ACCEPTED", response

    merged_main = _git(repository, "rev-parse", "origin/main")
    assert subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", delivery_sha, merged_main]
    ).returncode == 0
    _seed_reconciliation(
        path,
        result_id="reconcile-dev4-s1",
        from_phase="DEV4",
        from_segment_id="S1",
        to_phase="DEV2",
        to_segment_id="S2",
    )
    transition_payload = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV2",
        "to_segment_id": "S2",
        "reconciliation_ref": "reconcile-dev4-s1",
        "phase_close_ref": "checkpoint:DEV4.5:S1",
        "approval_id": "approval-transition-s2",
        "board": "orchestrator",
    }
    _approve_transition(
        path,
        approval_id="approval-transition-s2",
        attempt_id="attempt-transition-s2",
        operation="kanban_transition_initiative",
        target="initiative-1",
        expected_version=7,
        payload=transition_payload,
    )
    transition_boundary = commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={
            "kanban_transition_initiative": commands_module._handle_transition_initiative
        },
        workspace_registry=registry,
    )

    transitioned = _submit_transition(
        transition_boundary,
        "kanban_transition_initiative",
        attempt_id="attempt-transition-s2",
        key="transition-s2-key",
        target="initiative-1",
        version=7,
        payload=transition_payload,
    )

    assert transitioned["result"] == "ACCEPTED", transitioned
    s2_member = tmp_path / "segment-workspaces" / "initiative-1" / "S2" / "repo-1"
    assert s2_member.is_dir()
    assert _git(s2_member, "rev-parse", "HEAD") == merged_main
    assert _git(s2_member, "branch", "--show-current") == "initiative-1/S2"
    assert (s2_member / "feature.txt").read_text(encoding="utf-8") == "S1 accepted feature\n"
    assert (s2_member / "dev4-evidence.txt").read_text(encoding="utf-8") == "S1 closure evidence\n"
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT from_phase, from_segment_id, to_phase, to_segment_id "
            "FROM initiative_transitions ORDER BY transition_id DESC LIMIT 1"
        ).fetchone() == ("DEV4", "S1", "DEV2", "S2")
        assert conn.execute(
            "SELECT required_base_sha, observed_head, member_state "
            "FROM segment_workspace_members WHERE workspace_id='initiative-1:S2'"
        ).fetchone() == (merged_main, merged_main, "materialized")
