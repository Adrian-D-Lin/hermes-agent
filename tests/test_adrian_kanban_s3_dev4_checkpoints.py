"""DEV4.2b-e initiative checkpoint chain for one active segment."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_orchestration_checkpoints import (
    _approve,
    _boundary,
    _database,
    _seed_initiative,
    _submit,
    commands_module,  # noqa: F401
)


_TASKS = (
    ("DEV4.1a", "independent-reviewer"),
    ("DEV4.1b", "test-authority-reviewer"),
    ("DEV4.1c", "builder-tester"),
    ("DEV4.2a", "independent-reviewer"),
)


def _task_metadata(step: str, *, observations=None) -> dict:
    return {
        "DEV4.1a": {"candidates": []},
        "DEV4.1b": {
            "overreach_findings": [],
            "underreach_findings": [],
            "conclusion": "VERIFIED",
        },
        "DEV4.1c": {"executed_dispositions": []},
        "DEV4.2a": {"observations": [] if observations is None else observations},
    }[step]


def _seed_dev4_tasks(path, card_id, *, observations=None, segment_id="S1", workspace_id="ws:S1"):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE initiative_transitions SET to_segment_id=? "
            "WHERE initiative_id='initiative-1'",
            (segment_id,),
        )
        prior_ref = "checkpoint:DEV3.9:S1:cycle-1"
        for ordinal, (step, profile) in enumerate(_TASKS, start=1):
            task_id = f"task:{step}"
            candidate_id = f"candidate:{step}"
            task_card_id = conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, body, created_at, "
                "board_slug, record_version) VALUES "
                "('task', 'initiative-1', ?, ?, '', ?, 'orchestrator', 0)",
                (task_id, step, ordinal + 1),
            ).lastrowid
            conn.execute(
                "INSERT INTO task_lifecycle_contracts "
                "(contract_id, contract_version, step, task_card_id, task_id, "
                "initiative_card_id, initiative_id, segment_id, workspace_id, "
                "execution_profile, canonical_contract_payload, registry_hash, "
                "skill_id, skill_version, skill_hash, created_at) VALUES "
                "('adrian-kanban.lifecycle.dev4', '1', ?, ?, ?, ?, "
                "'initiative-1', ?, ?, ?, ?, ?, 'dev4-closure', '0.1.0', ?, ?)",
                (
                    step,
                    task_card_id,
                    task_id,
                    card_id,
                    segment_id,
                    workspace_id,
                    profile,
                    json.dumps({"predecessor_ref": prior_ref}),
                    "b" * 64,
                    "c" * 64,
                    ordinal + 1,
                ),
            )
            conn.execute(
                "INSERT INTO task_candidate_handoffs "
                "(candidate_id, task_card_id, task_id, execution_run_id, reviewer, "
                "summary, metadata_json, submitted_by, created_at) VALUES "
                "(?, ?, ?, ?, 'test-authority-reviewer', '', ?, ?, ?)",
                (
                    candidate_id,
                    task_card_id,
                    task_id,
                    ordinal,
                    json.dumps(_task_metadata(step, observations=observations)),
                    profile,
                    ordinal + 1,
                ),
            )
            conn.execute(
                "INSERT INTO task_reviewer_verdicts "
                "(verdict_id, task_card_id, task_id, candidate_id, review_run_id, "
                "reviewer, verdict, summary, created_at) VALUES "
                "(?, ?, ?, ?, ?, 'test-authority-reviewer', 'accepted', '', ?)",
                (
                    f"verdict:{step}",
                    task_card_id,
                    task_id,
                    candidate_id,
                    100 + ordinal,
                    ordinal + 1,
                ),
            )
            prior_ref = candidate_id
        conn.commit()


def _checkpoint(step: str, *, approval_id: str, result: dict, task_refs=None, checkpoint_refs=None):
    iteration = {
        "DEV4.2b": 1,
        "DEV4.2c": 2,
        "DEV4.2d": 3,
        "DEV4.2e": 4,
        "DEV4.3": 5,
        "DEV4.4": 6,
        "DEV4.5": 7,
    }[step]
    return {
        "initiative_id": "initiative-1",
        "update_kind": "orchestration_checkpoint",
        "update": {
            "result_id": f"checkpoint:{step}:S1",
            "phase": "DEV4",
            "segment_id": "S1",
            "iteration": iteration,
            "contract_id": "adrian-kanban.lifecycle.dev4",
            "contract_version": "1",
            "result": {"step": step, **result},
            "accepted_task_refs": [] if task_refs is None else task_refs,
            "accepted_checkpoint_refs": [] if checkpoint_refs is None else checkpoint_refs,
        },
        "approval_id": approval_id,
        "board": "orchestrator",
    }


def _dev4_2b(*, observations=None):
    observation_refs = [
        f"candidate:DEV4.2a#observations[{index}]"
        for index, _ in enumerate([] if observations is None else observations)
    ]
    return _checkpoint(
        "DEV4.2b",
        approval_id="approval:DEV4.2b:S1",
        task_refs=["candidate:DEV4.2a"],
        result={
            "ratification_package_candidate_ref": "candidate:DEV4.2a",
            "observation_refs": observation_refs,
            "integration_review_ref": "2-design/S1/integration-review.md@" + "a" * 40,
            "ratification_presentation_ref": "2-design/S1/ratification.md@" + "b" * 40,
            "next_route": "DEV4.2c",
        },
    )


def _dev4_2c(*, decisions=None):
    return _checkpoint(
        "DEV4.2c",
        approval_id="approval:DEV4.2c:S1",
        checkpoint_refs=["checkpoint:DEV4.2b:S1"],
        result={
            "integration_review_checkpoint_ref": "checkpoint:DEV4.2b:S1",
            "human_approval_ref": "approval:DEV4.2c:S1",
            "decisions": [] if decisions is None else decisions,
            "next_route": "DEV4.2d",
        },
    )


def _dev4_2d(*, integration_records=None, dev3_return_records=None):
    return _checkpoint(
        "DEV4.2d",
        approval_id="approval:DEV4.2d:S1",
        checkpoint_refs=["checkpoint:DEV4.2c:S1"],
        result={
            "human_ratification_checkpoint_ref": "checkpoint:DEV4.2c:S1",
            "integration_records": [] if integration_records is None else integration_records,
            "dev3_return_records": [] if dev3_return_records is None else dev3_return_records,
            "next_route": "DEV4.2e",
        },
    )


def _dev4_2e():
    return _checkpoint(
        "DEV4.2e",
        approval_id="approval:DEV4.2e:S1",
        checkpoint_refs=["checkpoint:DEV4.2d:S1"],
        result={
            "disposition_checkpoint_ref": "checkpoint:DEV4.2d:S1",
            "implementation_context_ref": "2-design/S1/implementation-context.md@" + "c" * 40,
            "segment_document_ref": "2-design/S1/segment-doc.md@" + "d" * 40,
            "profile_updates": [],
            "all_observations_accounted": True,
            "next_route": "DEV4.3",
        },
    )


def _accept(path, provider, commands_module, payload, version):
    _approve(path, payload, version=version)
    response = _submit(_boundary(commands_module, path, provider), payload, version=version)
    assert response["result"] == "ACCEPTED", response


def test_dev4_empty_observation_checkpoint_chain_reaches_merge_ready(
    commands_module, tmp_path, monkeypatch
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV4")
    _seed_dev4_tasks(path, card_id)

    for version, payload in enumerate((_dev4_2b(), _dev4_2c(), _dev4_2d(), _dev4_2e())):
        _accept(path, provider, commands_module, payload, version)

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT result_id, segment_id, canonical_payload FROM initiative_phase_results "
            "ORDER BY created_at, result_id"
        ).fetchall()
        assert [row[0] for row in rows] == [
            "checkpoint:DEV4.2b:S1",
            "checkpoint:DEV4.2c:S1",
            "checkpoint:DEV4.2d:S1",
            "checkpoint:DEV4.2e:S1",
        ]
        assert all(row[1] == "S1" for row in rows)
        assert json.loads(rows[-1][2])["next_route"] == "DEV4.3"


@pytest.mark.parametrize(
    "gap",
    [
        "hidden_observation",
        "wrong_task",
        "wrong_segment",
        "wrong_workspace",
        "wrong_profile",
        "wrong_predecessor",
        "failed_archive_review",
        "extra_field",
    ],
)
def test_dev4_2b_rejects_incomplete_or_cross_segment_review(
    commands_module, tmp_path, monkeypatch, gap
):
    observations = [
        {
            "observation": "design rule should be explicit",
            "evidence": "module.py@" + "e" * 40 + "#L4",
            "recommendation": "reflect",
        }
    ] if gap == "hidden_observation" else []
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV4")
    _seed_dev4_tasks(path, card_id, observations=observations)
    payload = _dev4_2b(observations=[])
    with sqlite3.connect(path) as conn:
        if gap == "wrong_task":
            payload["update"]["accepted_task_refs"] = ["candidate:DEV4.1c"]
        elif gap == "wrong_segment":
            payload["update"]["segment_id"] = "S2"
        elif gap == "wrong_workspace":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET workspace_id='ws:other' "
                "WHERE step='DEV4.2a'"
            )
        elif gap == "wrong_profile":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET execution_profile='default' "
                "WHERE step='DEV4.2a'"
            )
        elif gap == "wrong_predecessor":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET canonical_contract_payload='{}' "
                "WHERE step='DEV4.2a'"
            )
        elif gap == "failed_archive_review":
            conn.execute(
                "UPDATE task_candidate_handoffs SET metadata_json=? "
                "WHERE candidate_id='candidate:DEV4.1b'",
                (json.dumps({"overreach_findings": ["gap"], "underreach_findings": [], "conclusion": "VERIFIED"}),),
            )
        elif gap == "extra_field":
            payload["update"]["result"]["extra"] = True
        conn.commit()
    _approve(path, payload)

    response = _submit(_boundary(commands_module, path, provider), payload)

    assert response["result"] == "REJECTED", response


def test_dev4_human_ratification_records_every_observation_and_exact_approval(
    commands_module, tmp_path, monkeypatch
):
    observation = {
        "observation": "design rule should be explicit",
        "evidence": "module.py@" + "e" * 40 + "#L4",
        "recommendation": "reflect",
    }
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV4")
    _seed_dev4_tasks(path, card_id, observations=[observation])
    _accept(path, provider, commands_module, _dev4_2b(observations=[observation]), 0)
    decision = {
        "observation_ref": "candidate:DEV4.2a#observations[0]",
        "outcome": "Reflect",
        "rationale": "implemented behavior is accepted design intent",
        "target_ref": "D1:reflection-package:S1:1",
    }
    _accept(path, provider, commands_module, _dev4_2c(decisions=[decision]), 1)
    integration = {
        "observation_ref": decision["observation_ref"],
        "outcome": "Reflect",
        "d1_d4_result_ref": "D4:integrated:S1:1",
    }
    _accept(
        path,
        provider,
        commands_module,
        _dev4_2d(integration_records=[integration]),
        2,
    )
    _accept(path, provider, commands_module, _dev4_2e(), 3)


def _accept_dev4_2_chain(path, provider, commands_module):
    for version, payload in enumerate((_dev4_2b(), _dev4_2c(), _dev4_2d(), _dev4_2e())):
        _accept(path, provider, commands_module, payload, version)


def _isolate_checkpoint_admission(monkeypatch, commands_module):
    """Keep synthetic evidence tests below the real Git effect boundary."""
    monkeypatch.setattr(
        commands_module,
        "_preflight_dev4_workspace_checkpoint_effect",
        lambda _context: None,
    )


def _seed_merge_state(path, card_id, *, include_second_segment=True):
    base_sha = "1" * 40
    accepted_sha = "2" * 40
    delivery_sha = "3" * 40
    definitions = [{"segment_id": "S1", "ordinal": 1, "dependency_ids": []}]
    if include_second_segment:
        definitions.append({"segment_id": "S2", "ordinal": 2, "dependency_ids": ["S1"]})
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
                json.dumps(definitions),
                json.dumps({item["segment_id"]: f"ready:{item['segment_id']}" for item in definitions}),
            ),
        )
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
        conn.execute(
            "INSERT INTO segment_workspaces "
            "(workspace_id, initiative_card_id, initiative_id, segment_id, projection_id, "
            "lifecycle_state, controller_binding_ref, active, created_at, updated_at) VALUES "
            "('initiative-1:S1', ?, 'initiative-1', 'S1', 'projection-1', 'active', "
            "'adrian-kanban:workspace-controller:v1', 1, 2, 2)",
            (card_id,),
        )
        conn.execute(
            "INSERT INTO segment_workspace_members "
            "(workspace_id, repository_identity, relative_path, branch, required_base_sha, "
            "observed_head, member_state, observed_at) VALUES "
            "('initiative-1:S1', 'repo-1', 'initiative-1/S1/repo-1', "
            "'initiative-1/S1', ?, ?, 'merged', 3)",
            (base_sha, delivery_sha),
        )
        operation_id = "workspace-merge-initiative-1:S1-repo-1-" + delivery_sha
        intended_git = f"base={base_sha};source={delivery_sha};branch=initiative-1/S1"
        intended_fs = "target=/controlled/initiative-1/S1/repo-1;retain=true"
        conn.execute(
            "INSERT INTO external_operation_journal "
            "(operation_id, idempotency_id, member_target, ordinal, operation_kind, "
            "workspace_id, repository_identity, state, intended_git_evidence, "
            "intended_filesystem_evidence, observed_git_evidence, "
            "observed_filesystem_evidence, error_disposition, recovery_disposition, "
            "actor_evidence, created_at) VALUES (?, ?, 'repo-1', 1, 'workspace_merge', "
            "'initiative-1:S1', 'repo-1', 'prepared', ?, ?, NULL, NULL, NULL, NULL, "
            "'system:workspace-controller', 2)",
            (operation_id, operation_id, intended_git, intended_fs),
        )
        conn.execute(
            "INSERT INTO external_operation_journal "
            "(operation_id, idempotency_id, member_target, ordinal, operation_kind, "
            "workspace_id, repository_identity, state, intended_git_evidence, "
            "intended_filesystem_evidence, observed_git_evidence, "
            "observed_filesystem_evidence, error_disposition, recovery_disposition, "
            "actor_evidence, created_at) VALUES (?, ?, 'repo-1', 2, 'workspace_merge', "
            "'initiative-1:S1', 'repo-1', 'verified', ?, ?, ?, ?, NULL, NULL, "
            "'system:workspace-controller', 3)",
            (
                operation_id,
                operation_id,
                intended_git,
                intended_fs,
                f"base={base_sha};source={delivery_sha};merge={'4' * 40};remote={'4' * 40};merge_commit=true;source_contained=true;remote_contained=true",
                "retained=true;target=/controlled/initiative-1/S1/repo-1",
            ),
        )
        if include_second_segment:
            conn.execute(
                "INSERT INTO segment_workspaces "
                "(workspace_id, initiative_card_id, initiative_id, segment_id, projection_id, "
                "lifecycle_state, controller_binding_ref, active, created_at, updated_at) VALUES "
                "('initiative-1:S2', ?, 'initiative-1', 'S2', 'projection-1', 'planned', "
                "'adrian-kanban:workspace-controller:v1', 1, 2, 2)",
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
    return base_sha, accepted_sha, delivery_sha, operation_id


def _dev4_3(accepted_sha, delivery_sha, operation_id):
    return _checkpoint(
        "DEV4.3",
        approval_id="approval:DEV4.3:S1",
        checkpoint_refs=["checkpoint:DEV4.2e:S1"],
        result={
            "implementation_context_checkpoint_ref": "checkpoint:DEV4.2e:S1",
            "dev3_checkpoint_ref": "checkpoint:DEV3.9:S1:cycle-1",
            "workspace_id": "initiative-1:S1",
            "member_merges": [
                {
                    "repository_identity": "repo-1",
                    "dev3_accepted_sha": accepted_sha,
                    "segment_delivery_sha": delivery_sha,
                    "merge_operation_id": operation_id,
                }
            ],
            "merge_evidence_ref": "2-design/S1/merge-evidence.md@" + "4" * 40,
            "test_evidence_ref": "tests:S1@" + "5" * 40,
            "segment_boundary_evidence_ref": "boundary:S1@" + "6" * 40,
            "next_route": "DEV4.4",
        },
    )


def _dev4_4():
    return _checkpoint(
        "DEV4.4",
        approval_id="approval:DEV4.4:S1",
        checkpoint_refs=["checkpoint:DEV4.3:S1"],
        result={
            "merge_checkpoint_ref": "checkpoint:DEV4.3:S1",
            "workspace_id": "initiative-1:S1",
            "retirement_record_ref": "2-design/segment-register.md@" + "7" * 40,
            "retired_member_ids": ["repo-1"],
            "next_route": "DEV4.5",
        },
    )


def _dev4_5(*, final=False):
    return _checkpoint(
        "DEV4.5",
        approval_id="approval:DEV4.5:S1",
        checkpoint_refs=["checkpoint:DEV4.4:S1"],
        result={
            "workspace_retirement_checkpoint_ref": "checkpoint:DEV4.4:S1",
            "completed_segment_id": "S1",
            "action": "close_initiative" if final else "admit_next_segment",
            "next_segment_id": None if final else "S2",
            "closure_evidence_ref": "2-design/S1/closure.md@" + "8" * 40,
            "next_route": "CLOSED" if final else "DEV2",
        },
    )


def test_dev4_3_consumes_exact_verified_merge_journal_for_every_member(
    commands_module, tmp_path, monkeypatch
):
    _isolate_checkpoint_admission(monkeypatch, commands_module)
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV4")
    _seed_dev4_tasks(path, card_id)
    _accept_dev4_2_chain(path, provider, commands_module)
    _, accepted_sha, delivery_sha, operation_id = _seed_merge_state(path, card_id)

    _accept(
        path,
        provider,
        commands_module,
        _dev4_3(accepted_sha, delivery_sha, operation_id),
        4,
    )


def test_dev4_4_requires_and_records_retained_workspace_retirement(
    commands_module, tmp_path, monkeypatch
):
    _isolate_checkpoint_admission(monkeypatch, commands_module)
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV4")
    _seed_dev4_tasks(path, card_id)
    _accept_dev4_2_chain(path, provider, commands_module)
    _, accepted_sha, delivery_sha, operation_id = _seed_merge_state(path, card_id)
    _accept(path, provider, commands_module, _dev4_3(accepted_sha, delivery_sha, operation_id), 4)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE segment_workspaces SET lifecycle_state='retired', active=0 WHERE workspace_id='initiative-1:S1'"
        )
        conn.execute(
            "UPDATE segment_workspace_members SET member_state='retired' WHERE workspace_id='initiative-1:S1'"
        )
        conn.commit()

    _accept(path, provider, commands_module, _dev4_4(), 5)


@pytest.mark.parametrize("final", [False, True])
def test_dev4_5_derives_only_next_registered_segment_or_final_closure(
    commands_module, tmp_path, monkeypatch, final
):
    _isolate_checkpoint_admission(monkeypatch, commands_module)
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV4")
    _seed_dev4_tasks(path, card_id)
    _accept_dev4_2_chain(path, provider, commands_module)
    _, accepted_sha, delivery_sha, operation_id = _seed_merge_state(
        path, card_id, include_second_segment=not final
    )
    _accept(path, provider, commands_module, _dev4_3(accepted_sha, delivery_sha, operation_id), 4)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE segment_workspaces SET lifecycle_state='retired', active=0 WHERE workspace_id='initiative-1:S1'"
        )
        conn.execute(
            "UPDATE segment_workspace_members SET member_state='retired' WHERE workspace_id='initiative-1:S1'"
        )
        conn.commit()
    _accept(path, provider, commands_module, _dev4_4(), 5)

    _accept(path, provider, commands_module, _dev4_5(final=final), 6)
