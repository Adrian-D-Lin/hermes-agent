"""Real D4 close-to-transition boundary, approval atomicity and stale evidence."""

import json
import importlib
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_adrian_kanban_s3_d4_close import close_case, commands_module, _proof  # noqa: F401
from tests.test_adrian_kanban_s3_orchestration_checkpoints import (
    _approve as approve_close,
    _boundary,
    _submit as submit_close,
)
from tests.test_adrian_kanban_s3_initiatives import (
    _approve,
    _database,
    _seed_initiative,
    _submit,
    _seed_reconciliation,
)
from tests.test_adrian_kanban_s3_dev3_checkpoint import (
    _payload as _dev3_payload,
    _seed_dev3_cycle,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _repository_with_origin_main(root: Path) -> tuple[Path, str]:
    repository = root / "repository"
    remote = root / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    _git(repository, "config", "user.name", "Transition Test")
    _git(repository, "config", "user.email", "transition@example.invalid")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "main")
    return repository.resolve(), _git(repository, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "gap",
    [
        None,
        "missing_ref",
        "unaccepted_close",
        "stale_visit",
        "unaccepted_review",
        "missing_checkpoint",
    ],
)
def test_transition_requires_successful_current_d4_close(
    commands_module, close_case, gap
):
    c = close_case
    close_payload = {
        "initiative_id": "initiative-1",
        "update_kind": "phase_result",
        "update": c.update,
        "approval_id": "approval-close",
        "board": "orchestrator",
    }
    approve_close(c.path, close_payload, version=2)
    close_boundary = _boundary(
        commands_module, c.path, c.provider, phase_result_preparer=lambda *_: _proof(c)
    )
    closed = submit_close(close_boundary, close_payload, version=2)
    assert closed["result"] == "ACCEPTED", closed
    with sqlite3.connect(c.path) as conn:
        actor = json.loads(
            conn.execute(
                "SELECT actor_evidence FROM initiative_phase_results WHERE result_id='d4-close'"
            ).fetchone()[0]
        )
        predecessor = conn.execute(
            "SELECT MAX(transition_id) FROM initiative_transitions"
        ).fetchone()[0]
        assert actor["source_transition_id"] == predecessor
        assert actor["actor_profile"] == "default"
    _seed_reconciliation(
        c.path, result_id="reconcile-close", from_phase="D4", to_phase="DEV1"
    )
    transition = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV1",
        "reconciliation_ref": "reconcile-close",
        "phase_close_ref": "d4-close",
        "approval_id": "approval-transition",
        "board": "orchestrator",
    }
    if gap == "missing_ref":
        del transition["phase_close_ref"]
    with sqlite3.connect(c.path) as conn:
        if gap == "unaccepted_close":
            conn.execute(
                "UPDATE initiative_phase_results SET accepted=0 WHERE result_id='d4-close'"
            )
        elif gap == "stale_visit":
            conn.execute(
                "UPDATE initiative_phase_results SET actor_evidence=? WHERE result_id='d4-close'",
                (
                    json.dumps({
                        "source_transition_id": predecessor + 1,
                        "actor_profile": "default",
                    }),
                ),
            )
        elif gap == "unaccepted_review":
            conn.execute(
                "DELETE FROM task_reviewer_verdicts WHERE candidate_id='post-review'"
            )
        elif gap == "missing_checkpoint":
            conn.execute(
                "UPDATE initiative_phase_results SET accepted=0 WHERE result_id='checkpoint:D4.4'"
            )
    _approve(
        c.path,
        approval_id="approval-transition",
        attempt_id="attempt-transition",
        operation="kanban_transition_initiative",
        target="initiative-1",
        expected_version=3,
        payload=transition,
    )
    if gap is None:
        mutations = importlib.import_module(
            f"{commands_module.__package__}.initiative_mutations"
        )
        with sqlite3.connect(c.path) as preflight_conn:
            preflight_conn.row_factory = sqlite3.Row
            preflight = mutations._preflight_transition_initiative(
                SimpleNamespace(
                    payload=transition,
                    connection=preflight_conn,
                    attempt_id="attempt-transition",
                    idempotency_key="transition-key",
                    binding=SimpleNamespace(
                        expected_version=3,
                        session_id="session-initiative",
                        actor_profile="default",
                    ),
                )
            )
            assert preflight["initiative_id"] == "initiative-1"
            assert preflight["to_phase"] == "DEV1"
            assert preflight["to_segment_id"] is None
            assert preflight_conn.in_transaction is False
            assert preflight_conn.execute(
                "SELECT state FROM write_gate_kanban_approvals "
                "WHERE approval_id='approval-transition'"
            ).fetchone()[0] == "approved"
    boundary = commands_module._CommandBoundary(
        database_path=str(c.path),
        provider=c.provider,
        handlers={
            "kanban_transition_initiative": commands_module._handle_transition_initiative
        },
    )

    def submit():
        return _submit(
            boundary,
            "kanban_transition_initiative",
            attempt_id="attempt-transition",
            key="transition-key",
            target="initiative-1",
            version=3,
            payload=transition,
        )

    response = submit()
    assert response["result"] == ("ACCEPTED" if gap is None else "REJECTED"), response
    with sqlite3.connect(c.path) as conn:
        state = conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id='approval-transition'"
        ).fetchone()[0]
        phase, trigger, saved = conn.execute(
            "SELECT to_phase, trigger, canonical_payload FROM initiative_transitions ORDER BY transition_id DESC LIMIT 1"
        ).fetchone()
        version = conn.execute(
            "SELECT record_version FROM adrian_kanban_cards WHERE card_type='initiative'"
        ).fetchone()[0]
        assert (state, phase, version) == (
            ("consumed", "DEV1", 4) if gap is None else ("approved", "D4", 3)
        )
        if gap is None:
            assert trigger == "model_assessment"
            assert json.loads(saved)["phase_close_ref"] == "d4-close"
        else:
            assert "phase_close_ref" in str(response), response
    if gap is None:
        assert submit() == response


def test_transition_tool_exposes_route_specific_close_reference(commands_module):
    parameters = commands_module.TOOL_SCHEMAS["kanban_transition_initiative"][
        "parameters"
    ]
    assert "phase_close_ref" not in parameters["required"]
    assert parameters["properties"]["phase_close_ref"]["type"] == "string"
    ordinary_route, override_route = parameters["oneOf"]
    assert set(ordinary_route["required"]) == {"phase_close_ref", "approval_id"}
    assert ordinary_route["not"] == {"required": ["gate_override"]}
    assert override_route["required"] == ["gate_override"]
    assert override_route["not"] == {"required": ["phase_close_ref", "approval_id"]}


def test_dev2_transition_preflights_then_materializes_before_mutation_transaction(
    commands_module, close_case, monkeypatch
):
    """Git/filesystem work is admitted before, never inside, capability SQL."""
    c = close_case
    workspace = importlib.import_module(f"{commands_module.__package__}.workspace")
    registry = workspace._TrustedRepositoryRegistry(())
    events = []

    def preflight(context):
        assert context.connection.in_transaction is False
        events.append("preflight")
        return {
            "initiative_id": "initiative-1",
            "to_phase": "DEV2",
            "to_segment_id": "S1",
        }

    def materialize(conn, supplied_registry, **fields):
        assert conn.in_transaction is False
        assert supplied_registry is registry
        assert fields["initiative_id"] == "initiative-1"
        assert fields["segment_id"] == "S1"
        assert type(fields["materialized_at"]) is int
        events.append("materialize")
        return "/controlled/initiative-1/S1"

    def handler(context):
        assert context.connection.in_transaction is True
        events.append("handler")
        return {"transition": "recorded"}

    monkeypatch.setattr(
        commands_module, "_preflight_transition_initiative", preflight
    )
    monkeypatch.setattr(
        commands_module, "_materialize_segment_workspace", materialize
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(c.path),
        provider=c.provider,
        handlers={"kanban_transition_initiative": handler},
        workspace_registry=registry,
    )
    payload = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV2",
        "to_segment_id": "S1",
        "reconciliation_ref": "reconcile-dev2",
        "phase_close_ref": "close-dev2",
        "approval_id": "approval-dev2",
        "board": "orchestrator",
    }

    result = _submit(
        boundary,
        "kanban_transition_initiative",
        attempt_id="attempt-dev2",
        key="transition-dev2-key",
        target="initiative-1",
        version=2,
        payload=payload,
    )

    assert result["result"] == "ACCEPTED", result
    assert events == ["preflight", "materialize", "handler"]


def test_failed_transition_preflight_has_no_workspace_or_mutation_side_effect(
    commands_module, close_case, monkeypatch
):
    c = close_case
    workspace = importlib.import_module(f"{commands_module.__package__}.workspace")
    registry = workspace._TrustedRepositoryRegistry(())
    effects = []

    def reject_preflight(_context):
        raise ValueError("exact transition approval is invalid")

    monkeypatch.setattr(
        commands_module, "_preflight_transition_initiative", reject_preflight
    )
    monkeypatch.setattr(
        commands_module,
        "_materialize_segment_workspace",
        lambda *args, **kwargs: effects.append("materialized"),
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(c.path),
        provider=c.provider,
        handlers={
            "kanban_transition_initiative": lambda _context: effects.append(
                "handled"
            )
        },
        workspace_registry=registry,
    )

    result = _submit(
        boundary,
        "kanban_transition_initiative",
        attempt_id="attempt-invalid-dev2",
        key="transition-invalid-dev2-key",
        target="initiative-1",
        version=2,
        payload={
            "initiative_id": "initiative-1",
            "to_phase": "DEV2",
            "to_segment_id": "S1",
            "reconciliation_ref": "reconcile-dev2",
            "phase_close_ref": "close-dev2",
            "approval_id": "approval-dev2",
            "board": "orchestrator",
        },
    )

    assert result["result"] == "REJECTED"
    assert effects == []


def test_real_dev1_to_dev2_transition_materializes_then_commits_exact_segment(
    commands_module, tmp_path, monkeypatch
):
    """The public boundary performs the real DEV1.7 -> DEV2/S1 sequence."""
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path)
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
    definitions = [
        {"segment_id": "S1", "ordinal": 1, "dependency_ids": []},
        {"segment_id": "S2", "ordinal": 2, "dependency_ids": ["S1"]},
    ]
    readiness = {"S1": "ready:S1", "S2": "ready:S2"}
    with sqlite3.connect(database_path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE initiative_id='initiative-1'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE initiative_transitions SET to_phase='DEV1' "
            "WHERE initiative_id='initiative-1'"
        )
        conn.execute(
            "INSERT INTO initiative_segment_projections "
            "(projection_id, projection_version, initiative_card_id, initiative_id, "
            "manifest_path, manifest_sha, content_digest, parsed_segment_definitions, "
            "readiness_refs, validation_result, projected_at) VALUES "
            "('projection-1', 1, ?, 'initiative-1', 'segments.json', ?, ?, ?, ?, "
            "'accepted', 2)",
            (card_id, "a" * 40, "b" * 64, json.dumps(definitions), json.dumps(readiness)),
        )
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, canonical_payload, "
            "accepted_task_refs, accepted_checkpoint_refs, actor_evidence, "
            "idempotency_key, accepted, created_at) VALUES "
            "('dev1-close', ?, 'initiative-1', 'DEV1', NULL, 1, "
            "'segment_manifest_projection', 'adrian-kanban.lifecycle.dev1', '1', "
            "?, '[]', '[]', ?, 'dev1-close-key', 1, 2)",
            (
                card_id,
                json.dumps(
                    {
                        "projection_id": "projection-1",
                        "projection_version": 1,
                        "readiness_refs": readiness,
                    }
                ),
                json.dumps({"actor_profile": "default", "source_transition_id": 1}),
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
    _seed_reconciliation(
        database_path,
        result_id="reconcile-dev1",
        from_phase="DEV1",
        to_phase="DEV2",
        to_segment_id="S1",
    )
    payload = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV2",
        "to_segment_id": "S1",
        "reconciliation_ref": "reconcile-dev1",
        "phase_close_ref": "dev1-close",
        "approval_id": "approval-dev1",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-dev1",
        attempt_id="attempt-dev1",
        operation="kanban_transition_initiative",
        target="initiative-1",
        expected_version=0,
        payload=payload,
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_transition_initiative": commands_module._handle_transition_initiative
        },
        workspace_registry=registry,
    )

    response = _submit(
        boundary,
        "kanban_transition_initiative",
        attempt_id="attempt-dev1",
        key="transition-dev1-key",
        target="initiative-1",
        version=0,
        payload=payload,
    )

    assert response["result"] == "ACCEPTED", response
    target = controlled_root / "initiative-1" / "S1" / "repo-1"
    assert target.is_dir()
    assert _git(target, "rev-parse", "HEAD") == base_sha
    assert _git(target, "branch", "--show-current") == "initiative-1/S1"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT to_phase, to_segment_id FROM initiative_transitions "
            "ORDER BY transition_id DESC LIMIT 1"
        ).fetchone() == ("DEV2", "S1")
        assert conn.execute(
            "SELECT lifecycle_state FROM segment_workspaces "
            "WHERE workspace_id='initiative-1:S1'"
        ).fetchone()[0] == "active"
        assert conn.execute(
            "SELECT required_base_sha, observed_head, member_state "
            "FROM segment_workspace_members WHERE workspace_id='initiative-1:S1'"
        ).fetchone() == (base_sha, base_sha, "materialized")
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id='approval-dev1'"
        ).fetchone()[0] == "consumed"


def test_real_dev3_to_dev4_transition_preserves_exact_segment(
    commands_module, tmp_path, monkeypatch
):
    """The public boundary consumes the admitted DEV3.9 clean-cycle result."""
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path)
    with sqlite3.connect(database_path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE initiative_id='initiative-1'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE initiative_transitions SET to_phase='DEV3', to_segment_id='S1' "
            "WHERE initiative_id='initiative-1'"
        )
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
        conn.commit()
    _seed_dev3_cycle(database_path, card_id)
    checkpoint = _dev3_payload()
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, canonical_payload, "
            "accepted_task_refs, accepted_checkpoint_refs, actor_evidence, "
            "idempotency_key, accepted, created_at) VALUES "
            "('checkpoint:DEV3.9:S1:cycle-1', ?, 'initiative-1', 'DEV3', 'S1', 1, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.dev3', '1', "
            "?, ?, '[]', ?, 'checkpoint-key', 1, 20)",
            (
                card_id,
                json.dumps(checkpoint["update"]["result"]),
                json.dumps(checkpoint["update"]["accepted_task_refs"]),
                json.dumps({"actor_profile": "default", "source_transition_id": 1}),
            ),
        )
        conn.commit()
    _seed_reconciliation(
        database_path,
        result_id="reconcile-dev3",
        from_phase="DEV3",
        from_segment_id="S1",
        to_phase="DEV4",
        to_segment_id="S1",
    )
    payload = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV4",
        "to_segment_id": "S1",
        "reconciliation_ref": "reconcile-dev3",
        "phase_close_ref": "checkpoint:DEV3.9:S1:cycle-1",
        "approval_id": "approval-dev3",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-dev3",
        attempt_id="attempt-dev3",
        operation="kanban_transition_initiative",
        target="initiative-1",
        expected_version=0,
        payload=payload,
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_transition_initiative": commands_module._handle_transition_initiative
        },
    )

    response = _submit(
        boundary,
        "kanban_transition_initiative",
        attempt_id="attempt-dev3",
        key="transition-dev3-key",
        target="initiative-1",
        version=0,
        payload=payload,
    )

    assert response["result"] == "ACCEPTED", response
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT from_phase, from_segment_id, to_phase, to_segment_id "
            "FROM initiative_transitions ORDER BY transition_id DESC LIMIT 1"
        ).fetchone() == ("DEV3", "S1", "DEV4", "S1")
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id='approval-dev3'"
        ).fetchone()[0] == "consumed"
