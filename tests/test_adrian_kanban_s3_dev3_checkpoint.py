"""DEV3.9 classification checkpoint and same-segment DEV4 transition tests."""

from __future__ import annotations

import importlib
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


_STEPS = (
    ("DEV3.1", "builder-tester"),
    ("DEV3.2", "builder-tester"),
    ("DEV3.3", "test-authority-reviewer"),
    ("DEV3.4", "test-authority-reviewer"),
    ("DEV3.5", "test-authority-reviewer"),
    ("DEV3.6", "independent-reviewer"),
    ("DEV3.7", "independent-reviewer"),
    ("DEV3.8", "independent-reviewer"),
)


def _metadata(step: str) -> dict:
    values = {
        "DEV3.1": {
            "build_ref": "refs/heads/initiative-1/S1@" + "a" * 40,
            "changed_files": ["module.py"],
            "implementation_evidence": ["tests/output.txt@" + "a" * 40],
        },
        "DEV3.2": {
            "tests_ref": "tests/S1@" + "b" * 40,
            "suites": ["happy", "unhappy", "fringe"],
            "fixtures": ["valid", "invalid"],
        },
        "DEV3.3": {"findings": [], "conclusion": "PASSED"},
        "DEV3.4": {
            "execution_ref": "run:S1:1",
            "suite_results": [
                {"suite": name, "passed": 2, "failed": 0}
                for name in ("happy", "unhappy", "fringe")
            ],
            "failures": [],
        },
        "DEV3.5": {"findings": [], "conclusion": "PASSED"},
        "DEV3.6": {"findings": [], "conclusion": "PASSED"},
        "DEV3.7": {"findings": [], "conclusion": "PASSED"},
        "DEV3.8": {"dispositions": [], "unresolved_count": 0},
    }
    return values[step]


def _seed_dev3_cycle(path, card_id, *, segment_id="S1", workspace_id="ws:S1"):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE initiative_transitions SET to_segment_id=? "
            "WHERE initiative_id='initiative-1'",
            (segment_id,),
        )
        prior_ref = "checkpoint:DEV2.3:S1"
        for ordinal, (step, profile) in enumerate(_STEPS, start=1):
            task_id = f"task:{step}"
            candidate_id = f"candidate:{step}"
            task_card_id = conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, body, created_at, "
                "board_slug, record_version) VALUES "
                "('task', 'initiative-1', ?, ?, '', ?, 'orchestrator', 0)",
                (task_id, step, ordinal + 1),
            ).lastrowid
            contract_payload = {"predecessor_ref": prior_ref}
            conn.execute(
                "INSERT INTO task_lifecycle_contracts "
                "(contract_id, contract_version, step, task_card_id, task_id, "
                "initiative_card_id, initiative_id, segment_id, workspace_id, "
                "execution_profile, canonical_contract_payload, registry_hash, "
                "skill_id, skill_version, skill_hash, created_at) VALUES "
                "('adrian-kanban.lifecycle.dev3', '1', ?, ?, ?, ?, "
                "'initiative-1', ?, ?, ?, ?, ?, 'dev3-orchestration', "
                "'0.1.0', ?, ?)",
                (
                    step,
                    task_card_id,
                    task_id,
                    card_id,
                    segment_id,
                    workspace_id,
                    profile,
                    json.dumps(contract_payload),
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
                    json.dumps(_metadata(step)),
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


def _payload(**result_changes):
    result = {
        "step": "DEV3.9",
        "cycle": 1,
        "accepted_build_ref": "refs/heads/initiative-1/S1@" + "a" * 40,
        "integrated_review_candidate_ref": "candidate:DEV3.8",
        "classification_record_ref": "2-design/S1/dev3-classification.md@" + "d" * 40,
        "classifications": [],
        "unresolved_remediable_count": 0,
        "temporary_material_refs": [],
        "next_route": "DEV4",
    }
    result.update(result_changes)
    return {
        "initiative_id": "initiative-1",
        "update_kind": "orchestration_checkpoint",
        "update": {
            "result_id": "checkpoint:DEV3.9:S1:cycle-1",
            "phase": "DEV3",
            "segment_id": "S1",
            "iteration": 1,
            "contract_id": "adrian-kanban.lifecycle.dev3",
            "contract_version": "1",
            "result": result,
            "accepted_task_refs": [f"candidate:{step}" for step, _ in _STEPS],
            "accepted_checkpoint_refs": [],
        },
        "approval_id": "approval:DEV3.9:S1:cycle-1",
        "board": "orchestrator",
    }


def test_dev3_9_public_checkpoint_accepts_complete_clean_cycle(
    commands_module, tmp_path, monkeypatch
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV3")
    _seed_dev3_cycle(path, card_id)
    payload = _payload()
    _approve(path, payload)

    response = _submit(_boundary(commands_module, path, provider), payload)

    assert response["result"] == "ACCEPTED", response
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT phase, segment_id, result_kind, canonical_payload, "
            "accepted_task_refs, actor_evidence FROM initiative_phase_results "
            "WHERE result_id='checkpoint:DEV3.9:S1:cycle-1'"
        ).fetchone()
        assert tuple(row[:3]) == ("DEV3", "S1", "orchestration_checkpoint")
        assert json.loads(row["canonical_payload"])["next_route"] == "DEV4"
        assert json.loads(row["accepted_task_refs"]) == [
            f"candidate:{step}" for step, _ in _STEPS
        ]
        assert json.loads(row["actor_evidence"])["source_transition_id"] == 1


@pytest.mark.parametrize(
    "gap",
    [
        "wrong_segment",
        "wrong_workspace",
        "wrong_profile",
        "wrong_predecessor",
        "missing_step",
        "build_mismatch",
        "design_to_tests_findings",
        "failed_suite",
        "broad_review_findings",
        "test_result_findings",
        "independent_review_findings",
        "unresolved_synthesis",
        "unaccounted_classification",
        "unresolved_remediable",
        "wrong_route",
        "extra_result_field",
    ],
)
def test_dev3_9_rejects_incomplete_or_non_exit_cycle(
    commands_module, tmp_path, monkeypatch, gap
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV3")
    _seed_dev3_cycle(path, card_id)
    payload = _payload()
    with sqlite3.connect(path) as conn:
        if gap == "wrong_segment":
            payload["update"]["segment_id"] = "S2"
        elif gap == "wrong_workspace":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET workspace_id='ws:other' "
                "WHERE step='DEV3.8'"
            )
        elif gap == "wrong_profile":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET execution_profile='default' "
                "WHERE step='DEV3.7'"
            )
        elif gap == "wrong_predecessor":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET canonical_contract_payload='{}' "
                "WHERE step='DEV3.6'"
            )
        elif gap == "missing_step":
            payload["update"]["accepted_task_refs"].pop()
        elif gap == "build_mismatch":
            payload["update"]["result"]["accepted_build_ref"] = "other@" + "a" * 40
        elif gap == "design_to_tests_findings":
            _replace_metadata(conn, "DEV3.3", {"findings": [{"issue": "gap"}], "conclusion": "PASSED"})
        elif gap == "failed_suite":
            value = _metadata("DEV3.4")
            value["suite_results"][0]["failed"] = 1
            value["failures"] = ["test failed"]
            _replace_metadata(conn, "DEV3.4", value)
        elif gap == "broad_review_findings":
            _replace_metadata(conn, "DEV3.5", {"findings": [{"issue": "gap"}], "conclusion": "PASSED"})
        elif gap == "test_result_findings":
            _replace_metadata(conn, "DEV3.6", {"findings": [{"issue": "gap"}], "conclusion": "PASSED"})
        elif gap == "independent_review_findings":
            _replace_metadata(conn, "DEV3.7", {"findings": [{"issue": "gap"}], "conclusion": "PASSED"})
        elif gap == "unresolved_synthesis":
            _replace_metadata(
                conn,
                "DEV3.8",
                {
                    "dispositions": [
                        {"finding_ref": "finding-1", "disposition": "remediate", "route": "DEV3.10"}
                    ],
                    "unresolved_count": 1,
                },
            )
        elif gap == "unaccounted_classification":
            payload["update"]["result"]["classifications"] = [
                {
                    "finding_ref": "finding-1",
                    "classification": "implementation_detail",
                    "evidence_refs": ["review@" + "e" * 40],
                    "route": "RECORD_ONLY",
                }
            ]
        elif gap == "unresolved_remediable":
            payload["update"]["result"]["unresolved_remediable_count"] = 1
        elif gap == "wrong_route":
            payload["update"]["result"]["next_route"] = "DEV3.10"
        elif gap == "extra_result_field":
            payload["update"]["result"]["extra"] = "invented"
        conn.commit()
    _approve(path, payload)

    response = _submit(_boundary(commands_module, path, provider), payload)

    assert response["result"] == "REJECTED", response
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "approved"


def _replace_metadata(conn, step: str, metadata: dict) -> None:
    conn.execute(
        "UPDATE task_candidate_handoffs SET metadata_json=? WHERE candidate_id=?",
        (json.dumps(metadata), f"candidate:{step}"),
    )


def test_dev3_to_dev4_transition_evidence_is_same_segment_and_current_visit(
    commands_module, tmp_path, monkeypatch
):
    path, _ = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV3")
    _seed_dev3_cycle(path, card_id)
    payload = _payload()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
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
                json.dumps(payload["update"]["result"]),
                json.dumps(payload["update"]["accepted_task_refs"]),
                json.dumps({"actor_profile": "default", "source_transition_id": 1}),
            ),
        )
        module = importlib.import_module(f"{commands_module.__package__}.transition_evidence")
        arguments = dict(
            initiative_card_id=card_id,
            initiative_id="initiative-1",
            from_phase="DEV3",
            from_segment_id="S1",
            to_phase="DEV4",
            to_segment_id="S1",
            previous_transition_id=1,
            phase_close_ref="checkpoint:DEV3.9:S1:cycle-1",
        )
        assert module.validate_transition_evidence(conn, **arguments)["step"] == "DEV3.9"
        for change in (
            {"to_segment_id": "S2"},
            {"to_phase": "DEV2"},
            {"previous_transition_id": 2},
        ):
            with pytest.raises(ValueError):
                module.validate_transition_evidence(conn, **(arguments | change))


def test_dev3_9_routes_every_remediable_finding_to_full_revision_cycle(
    commands_module, tmp_path, monkeypatch
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV3")
    _seed_dev3_cycle(path, card_id)
    finding_ref = "candidate:DEV3.5#findings[0]"
    with sqlite3.connect(path) as conn:
        _replace_metadata(
            conn,
            "DEV3.5",
            {
                "findings": [
                    {
                        "file": "module.py",
                        "line": 4,
                        "severity": "material",
                        "issue": "gate omitted",
                        "expected": "fail closed",
                    }
                ],
                "conclusion": "CHANGES_REQUIRED",
            },
        )
        _replace_metadata(
            conn,
            "DEV3.8",
            {
                "dispositions": [
                    {
                        "finding_ref": finding_ref,
                        "disposition": "remediate",
                        "route": "DEV3.10",
                    }
                ],
                "unresolved_count": 1,
            },
        )
        conn.commit()
    payload = _payload(
        classifications=[
            {
                "finding_ref": finding_ref,
                "classification": "build_bug",
                "evidence_refs": ["module.py@" + "e" * 40 + "#L4"],
                "route": "DEV3.10",
            }
        ],
        unresolved_remediable_count=1,
        next_route="DEV3.10",
    )
    _approve(path, payload)

    response = _submit(_boundary(commands_module, path, provider), payload)

    assert response["result"] == "ACCEPTED", response


@pytest.mark.parametrize(
    ("classification", "route"),
    [
        ("build_bug", "DEV4"),
        ("test_defect", "D1"),
        ("design_gap", "DEV3.10"),
        ("design_relevant_decision", "DEV4"),
        ("design_ambiguity", "DEV4"),
        ("implementation_detail", "DEV3.10"),
        ("invented", "DEV3.10"),
    ],
)
def test_dev3_9_rejects_classification_route_mismatch(
    commands_module, tmp_path, monkeypatch, classification, route
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV3")
    _seed_dev3_cycle(path, card_id)
    finding_ref = "candidate:DEV3.5#findings[0]"
    with sqlite3.connect(path) as conn:
        _replace_metadata(
            conn,
            "DEV3.5",
            {"findings": [{"issue": "gap"}], "conclusion": "CHANGES_REQUIRED"},
        )
        _replace_metadata(
            conn,
            "DEV3.8",
            {
                "dispositions": [
                    {
                        "finding_ref": finding_ref,
                        "disposition": "remediate",
                        "route": "DEV3.10",
                    }
                ],
                "unresolved_count": 1,
            },
        )
        conn.commit()
    payload = _payload(
        classifications=[
            {
                "finding_ref": finding_ref,
                "classification": classification,
                "evidence_refs": ["review@" + "e" * 40],
                "route": route,
            }
        ],
        unresolved_remediable_count=1,
        next_route="DEV3.10",
    )
    _approve(path, payload)

    response = _submit(_boundary(commands_module, path, provider), payload)

    assert response["result"] == "REJECTED", response


def _seed_dev3_revision_pair(path, card_id, *, segment_id="S1", workspace_id="ws:S1"):
    values = (
        (
            "DEV3.10",
            "candidate:DEV3.10",
            "checkpoint:DEV3.9:S1:cycle-1",
            {
                "revision_brief_ref": "2-design/S1/revision-1.md@" + "f" * 40,
                "finding_changes": [
                    {
                        "finding_ref": "candidate:DEV3.5#findings[0]",
                        "required_change": "restore the omitted gate",
                    }
                ],
            },
        ),
        (
            "DEV3.11",
            "candidate:DEV3.11",
            "candidate:DEV3.10",
            {
                "build_ref": "refs/heads/initiative-1/S1@" + "1" * 40,
                "changed_files": ["module.py"],
                "revision_evidence": ["tests/revision-output.txt@" + "1" * 40],
            },
        ),
    )
    with sqlite3.connect(path) as conn:
        for ordinal, (step, candidate_id, predecessor_ref, metadata) in enumerate(
            values, start=30
        ):
            task_id = f"task:{step}"
            task_card_id = conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, body, created_at, "
                "board_slug, record_version) VALUES "
                "('task', 'initiative-1', ?, ?, '', ?, 'orchestrator', 0)",
                (task_id, step, ordinal),
            ).lastrowid
            conn.execute(
                "INSERT INTO task_lifecycle_contracts "
                "(contract_id, contract_version, step, task_card_id, task_id, "
                "initiative_card_id, initiative_id, segment_id, workspace_id, "
                "execution_profile, canonical_contract_payload, registry_hash, "
                "skill_id, skill_version, skill_hash, created_at) VALUES "
                "('adrian-kanban.lifecycle.dev3', '1', ?, ?, ?, ?, "
                "'initiative-1', ?, ?, 'builder-tester', ?, ?, "
                "'dev3-orchestration', '0.1.0', ?, ?)",
                (
                    step,
                    task_card_id,
                    task_id,
                    card_id,
                    segment_id,
                    workspace_id,
                    json.dumps({"predecessor_ref": predecessor_ref}),
                    "b" * 64,
                    "c" * 64,
                    ordinal,
                ),
            )
            conn.execute(
                "INSERT INTO task_candidate_handoffs "
                "(candidate_id, task_card_id, task_id, execution_run_id, reviewer, "
                "summary, metadata_json, submitted_by, created_at) VALUES "
                "(?, ?, ?, ?, 'test-authority-reviewer', '', ?, 'builder-tester', ?)",
                (candidate_id, task_card_id, task_id, ordinal, json.dumps(metadata), ordinal),
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
                    ordinal + 100,
                    ordinal,
                ),
            )
        conn.commit()


def _seed_remediation_checkpoint(path, card_id):
    result = _payload(
        classifications=[
            {
                "finding_ref": "candidate:DEV3.5#findings[0]",
                "classification": "build_bug",
                "evidence_refs": ["module.py@" + "e" * 40 + "#L4"],
                "route": "DEV3.10",
            }
        ],
        unresolved_remediable_count=1,
        next_route="DEV3.10",
    )["update"]["result"]
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, canonical_payload, "
            "accepted_task_refs, accepted_checkpoint_refs, actor_evidence, "
            "idempotency_key, accepted, created_at) VALUES "
            "('checkpoint:DEV3.9:S1:cycle-1', ?, 'initiative-1', 'DEV3', 'S1', 1, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.dev3', '1', "
            "?, ?, '[]', ?, 'checkpoint-remediate-key', 1, 25)",
            (
                card_id,
                json.dumps(result),
                json.dumps([f"candidate:{step}" for step, _ in _STEPS]),
                json.dumps({"actor_profile": "default", "source_transition_id": 1}),
            ),
        )
        conn.commit()


def _revision_payload(**result_changes):
    result = {
        "step": "DEV3.12",
        "cycle": 2,
        "prior_classification_checkpoint_ref": "checkpoint:DEV3.9:S1:cycle-1",
        "revision_brief_candidate_ref": "candidate:DEV3.10",
        "revision_build_candidate_ref": "candidate:DEV3.11",
        "revised_build_ref": "refs/heads/initiative-1/S1@" + "1" * 40,
        "full_cycle_steps": [f"DEV3.{index}" for index in range(1, 10)],
        "next_route": "DEV3.1",
    }
    result.update(result_changes)
    return {
        "initiative_id": "initiative-1",
        "update_kind": "orchestration_checkpoint",
        "update": {
            "result_id": "checkpoint:DEV3.12:S1:cycle-2",
            "phase": "DEV3",
            "segment_id": "S1",
            "iteration": 2,
            "contract_id": "adrian-kanban.lifecycle.dev3",
            "contract_version": "1",
            "result": result,
            "accepted_task_refs": ["candidate:DEV3.10", "candidate:DEV3.11"],
            "accepted_checkpoint_refs": ["checkpoint:DEV3.9:S1:cycle-1"],
        },
        "approval_id": "approval:DEV3.12:S1:cycle-2",
        "board": "orchestrator",
    }


def test_dev3_12_public_checkpoint_forces_full_cycle_recheck(
    commands_module, tmp_path, monkeypatch
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV3")
    _seed_dev3_cycle(path, card_id)
    _seed_remediation_checkpoint(path, card_id)
    _seed_dev3_revision_pair(path, card_id)
    payload = _revision_payload()
    _approve(path, payload)

    response = _submit(_boundary(commands_module, path, provider), payload)

    assert response["result"] == "ACCEPTED", response
    with sqlite3.connect(path) as conn:
        stored = json.loads(
            conn.execute(
                "SELECT canonical_payload FROM initiative_phase_results "
                "WHERE result_id='checkpoint:DEV3.12:S1:cycle-2'"
            ).fetchone()[0]
        )
        assert stored["next_route"] == "DEV3.1"


@pytest.mark.parametrize(
    "gap",
    [
        "wrong_cycle",
        "wrong_segment",
        "wrong_workspace",
        "wrong_profile",
        "wrong_task_predecessor",
        "wrong_checkpoint",
        "checkpoint_not_remediable",
        "brief_mismatch",
        "build_mismatch",
        "partial_cycle",
        "wrong_route",
        "extra_result_field",
    ],
)
def test_dev3_12_rejects_partial_or_unrelated_revision_recheck(
    commands_module, tmp_path, monkeypatch, gap
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(path, "DEV3")
    _seed_dev3_cycle(path, card_id)
    _seed_remediation_checkpoint(path, card_id)
    _seed_dev3_revision_pair(path, card_id)
    payload = _revision_payload()
    with sqlite3.connect(path) as conn:
        if gap == "wrong_cycle":
            payload["update"]["result"]["cycle"] = 1
        elif gap == "wrong_segment":
            payload["update"]["segment_id"] = "S2"
        elif gap == "wrong_workspace":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET workspace_id='ws:other' "
                "WHERE step='DEV3.11'"
            )
        elif gap == "wrong_profile":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET execution_profile='default' "
                "WHERE step='DEV3.10'"
            )
        elif gap == "wrong_task_predecessor":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET canonical_contract_payload='{}' "
                "WHERE step='DEV3.11'"
            )
        elif gap == "wrong_checkpoint":
            payload["update"]["accepted_checkpoint_refs"] = ["missing"]
        elif gap == "checkpoint_not_remediable":
            row = conn.execute(
                "SELECT canonical_payload FROM initiative_phase_results "
                "WHERE result_id='checkpoint:DEV3.9:S1:cycle-1'"
            ).fetchone()
            value = json.loads(row[0])
            value["next_route"] = "DEV4"
            conn.execute(
                "UPDATE initiative_phase_results SET canonical_payload=? "
                "WHERE result_id='checkpoint:DEV3.9:S1:cycle-1'",
                (json.dumps(value),),
            )
        elif gap == "brief_mismatch":
            payload["update"]["result"]["revision_brief_candidate_ref"] = "other"
        elif gap == "build_mismatch":
            payload["update"]["result"]["revised_build_ref"] = "other"
        elif gap == "partial_cycle":
            payload["update"]["result"]["full_cycle_steps"].pop()
        elif gap == "wrong_route":
            payload["update"]["result"]["next_route"] = "DEV3.9"
        elif gap == "extra_result_field":
            payload["update"]["result"]["extra"] = True
        conn.commit()
    _approve(path, payload)

    response = _submit(_boundary(commands_module, path, provider), payload)

    assert response["result"] == "REJECTED", response
