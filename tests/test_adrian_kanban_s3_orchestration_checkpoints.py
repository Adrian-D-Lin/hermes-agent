"""Orchestration-checkpoint tests derived from Kanban design v0.28 section 8.3."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from writegate.kanban_approvals import create_kanban_approval_schema


@pytest.fixture(scope="module")
def commands_module():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_checkpoints"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    module = importlib.import_module(f"{package_name}.commands")
    yield module
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


def _database(tmp_path, monkeypatch, commands_module):
    provider_module = importlib.import_module(
        f"{commands_module.__package__}.provider"
    )
    schema_module = importlib.import_module(f"{commands_module.__package__}.schema")
    database_path = (tmp_path / "kanban.sqlite3").resolve()
    with kb.connect_closing(database_path) as conn:
        schema_module.create_schema(conn)
        create_kanban_approval_schema(conn)
        conn.commit()

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        f"  database_path: {database_path.as_posix()}\n",
        encoding="utf-8",
    )
    provider = provider_module.AdrianKanbanAuthorityProvider(str(database_path))
    provider_module.register_provider(provider)
    return database_path, provider


def _approval_digest(payload: dict) -> str:
    approved = {key: value for key, value in payload.items() if key != "approval_id"}
    return hashlib.sha256(
        json.dumps(approved, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _approve(database_path: Path, payload: dict, *, version: int = 0) -> None:
    now = int(time.time())
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO write_gate_kanban_approvals ("
            "approval_id, approval_type, state, request_id, operation, "
            "initiative_id, proposed_creation_id, expected_version, "
            "canonical_digest, canonicalization_version, authorizer_evidence, "
            "session_id, prepared_at, approved_at, expires_at, approval_evidence"
            ") VALUES (?, 'kanban_initiative_mutation', 'approved', ?, "
            "'kanban_update_initiative', 'initiative-1', NULL, ?, ?, 1, ?, "
            "'session-checkpoint', ?, ?, ?, ?)",
            (
                payload["approval_id"],
                f"attempt:{payload['approval_id']}",
                version,
                _approval_digest(payload),
                '{"peer_identity":"adrian@tailnet"}',
                now - 2,
                now - 1,
                now + 600,
                "approved-on-second-action",
            ),
        )
        conn.commit()


def _seed_initiative(database_path: Path, phase: str) -> int:
    with sqlite3.connect(database_path) as conn:
        conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')")
        card_id = conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, "
            "board_slug, record_version) VALUES "
            "('initiative', 'initiative-1', NULL, 'Initiative', 'body', 1, "
            "'orchestrator', 0)"
        ).lastrowid
        conn.execute(
            "INSERT INTO initiative_transitions "
            "(initiative_card_id, initiative_id, previous_transition_id, "
            "transition_id, from_phase, from_segment_id, to_phase, "
            "to_segment_id, trigger, actor_evidence, canonical_payload, "
            "created_at) VALUES (?, 'initiative-1', NULL, 1, NULL, NULL, ?, "
            "NULL, 'initialization', '{}', '{}', 1)",
            (card_id, phase),
        )
        conn.commit()
        return card_id


def _seed_accepted_handoff(
    database_path: Path,
    card_id: int,
    *,
    step: str,
    candidate_id: str,
    sequence: int,
) -> None:
    task_id = f"task:{step}"
    task_card_id: int
    with sqlite3.connect(database_path) as conn:
        task_card_id = conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, "
            "board_slug, record_version) VALUES "
            "('task', 'initiative-1', ?, ?, '', 1, 'orchestrator', 0)",
            (task_id, step),
        ).lastrowid
        conn.execute(
            "INSERT INTO task_lifecycle_contracts "
            "(contract_id, contract_version, step, task_card_id, task_id, "
            "initiative_card_id, initiative_id, segment_id, workspace_id, "
            "execution_profile, canonical_contract_payload, registry_hash, "
            "skill_id, skill_version, skill_hash, created_at) VALUES "
            "(?, '1', ?, ?, ?, ?, 'initiative-1', NULL, NULL, "
            "'independent-reviewer', '{}', ?, ?, '1', ?, 1)",
            (
                f"adrian-kanban.lifecycle.{step.split('.')[0].lower()}",
                step,
                task_card_id,
                task_id,
                card_id,
                "r" * 64,
                f"skill:{step}",
                "s" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO task_candidate_handoffs "
            "(candidate_id, task_card_id, task_id, execution_run_id, reviewer, "
            "summary, metadata_json, submitted_by, created_at) VALUES "
            "(?, ?, ?, ?, 'test-authority-reviewer', '', '{}', "
            "'independent-reviewer', 1)",
            (candidate_id, task_card_id, task_id, sequence),
        )
        conn.execute(
            "INSERT INTO task_reviewer_verdicts "
            "(verdict_id, task_card_id, task_id, candidate_id, review_run_id, "
            "reviewer, verdict, summary, created_at) VALUES "
            "(?, ?, ?, ?, ?, 'test-authority-reviewer', 'accepted', '', 1)",
            (
                f"verdict:{candidate_id}",
                task_card_id,
                task_id,
                candidate_id,
                1000 + sequence,
            ),
        )
        conn.commit()


def _boundary(commands_module, database_path, provider):
    return commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_update_initiative": commands_module._handle_update_initiative
        },
    )


def _submit(boundary, payload: dict, *, actor_profile: str = "default", version: int = 0):
    return boundary.submit(
        "kanban_update_initiative",
        attempt_id=f"attempt:{payload['approval_id']}",
        idempotency_key=f"key:{payload['approval_id']}",
        target="initiative-1",
        expected_version=version,
        session_id="session-checkpoint",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile=actor_profile,
        payload=payload,
    )


def _checkpoint_payload(step: str, accepted_task_refs: list[str]) -> dict:
    phase = "D4" if step.startswith("D4.") else "DEV1"
    contract = f"adrian-kanban.lifecycle.{phase.lower()}"
    result: dict = {"step": step}
    if step == "D4.3":
        result.update(
            {
                "write_gate_approval_ref": "write-gate:change-set:1",
                "approved_change_set_digest": "a" * 64,
                "current_document_refs": ["Canon/design.md@" + "b" * 40],
            }
        )
    elif step == "D4.4":
        result.update(
            {
                "write_gate_approval_ref": "write-gate:change-set:1",
                "approval_lease_ref": "write-gate:lease:1",
                "approved_change_set_digest": "a" * 64,
                "execution_result": "applied",
                "post_write_documents": [
                    {"path": "Canon/design.md", "sha": "c" * 40}
                ],
                "item_determinations": [
                    {"item_id": "change-1", "determination": "applied"}
                ],
            }
        )
    elif step == "DEV1.2":
        result.update(
            {
                "cumulative_record_ref": "2-design/dev1.2.md@" + "d" * 40,
                "source_angles": [
                    {
                        "step": f"DEV1.1{letter}",
                        "candidate_ref": f"candidate:DEV1.1{letter}",
                        "item_count": index,
                    }
                    for index, letter in enumerate("abcde", start=1)
                ],
                "total_item_count": 15,
            }
        )
    return {
        "initiative_id": "initiative-1",
        "update_kind": "orchestration_checkpoint",
        "update": {
            "result_id": f"checkpoint:{step}",
            "phase": phase,
            "segment_id": None,
            "iteration": 1,
            "contract_id": contract,
            "contract_version": "1",
            "result": result,
            "accepted_task_refs": accepted_task_refs,
            "accepted_checkpoint_refs": [],
        },
        "approval_id": f"approval:{step}",
        "board": "orchestrator",
    }


def test_d4_3_checkpoint_pins_exact_accepted_predecessors_and_replays(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    _seed_accepted_handoff(
        database_path, card_id, step="D4.1", candidate_id="candidate:D4.1", sequence=1
    )
    _seed_accepted_handoff(
        database_path, card_id, step="D4.2", candidate_id="candidate:D4.2", sequence=2
    )
    payload = _checkpoint_payload("D4.3", ["candidate:D4.1", "candidate:D4.2"])
    _approve(database_path, payload)
    boundary = _boundary(commands_module, database_path, provider)

    first = _submit(boundary, payload)
    replay = _submit(boundary, payload)

    assert first["result"] == "ACCEPTED"
    assert replay == first
    assert first["value"] == {
        "initiative_id": "initiative-1",
        "update_kind": "orchestration_checkpoint",
        "record_version": 1,
        "result_id": "checkpoint:D4.3",
        "step": "D4.3",
    }
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM initiative_phase_results WHERE result_id = ?",
            ("checkpoint:D4.3",),
        ).fetchone()
        assert row["result_kind"] == "orchestration_checkpoint"
        assert json.loads(row["canonical_payload"])["step"] == "D4.3"
        assert json.loads(row["accepted_task_refs"]) == [
            "candidate:D4.1",
            "candidate:D4.2",
        ]
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
            (payload["approval_id"],),
        ).fetchone()[0] == "consumed"


@pytest.mark.parametrize(
    ("phase", "refs", "actor"),
    [
        ("D1", ["candidate:D4.1", "candidate:D4.2"], "default"),
        ("D4", ["candidate:D4.1"], "default"),
        ("D4", ["candidate:D4.1", "candidate:D4.2"], "builder-tester"),
    ],
)
def test_d4_3_rejects_wrong_phase_incomplete_predecessors_or_wrong_actor_atomically(
    commands_module, tmp_path, monkeypatch, phase, refs, actor
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, phase)
    _seed_accepted_handoff(
        database_path, card_id, step="D4.1", candidate_id="candidate:D4.1", sequence=1
    )
    _seed_accepted_handoff(
        database_path, card_id, step="D4.2", candidate_id="candidate:D4.2", sequence=2
    )
    payload = _checkpoint_payload("D4.3", refs)
    _approve(database_path, payload)

    result = _submit(
        _boundary(commands_module, database_path, provider),
        payload,
        actor_profile=actor,
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0] == 0
        assert conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
            (payload["approval_id"],),
        ).fetchone()[0] == "approved"


def test_d4_4_requires_matching_d4_3_checkpoint_and_change_set_digest(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "('checkpoint:D4.3', ?, 'initiative-1', 'D4', NULL, 1, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.d4', '1', "
            "?, '[]', '[]', '{}', 'seed:d4.3', 1, 1)",
            (
                card_id,
                json.dumps(
                    {
                        "step": "D4.3",
                        "write_gate_approval_ref": "write-gate:change-set:1",
                        "approved_change_set_digest": "a" * 64,
                        "current_document_refs": ["Canon/design.md@" + "b" * 40],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        conn.commit()
    payload = _checkpoint_payload("D4.4", [])
    payload["update"]["iteration"] = 2
    payload["update"]["accepted_checkpoint_refs"] = ["checkpoint:D4.3"]
    _approve(database_path, payload)

    accepted = _submit(_boundary(commands_module, database_path, provider), payload)
    assert accepted["result"] == "ACCEPTED"

    second_payload = _checkpoint_payload("D4.4", [])
    second_payload["approval_id"] = "approval:D4.4:mismatch"
    second_payload["update"]["result_id"] = "checkpoint:D4.4:mismatch"
    second_payload["update"]["iteration"] = 3
    second_payload["update"]["accepted_checkpoint_refs"] = ["checkpoint:D4.3"]
    second_payload["update"]["result"]["approved_change_set_digest"] = "e" * 64
    _approve(database_path, second_payload, version=1)
    rejected = _submit(
        _boundary(commands_module, database_path, provider),
        second_payload,
        version=1,
    )
    assert rejected["result"] == "REJECTED"


def test_dev1_2_checkpoint_requires_all_five_angle_handoffs(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "DEV1")
    refs = []
    for sequence, letter in enumerate("abcde", start=1):
        candidate = f"candidate:DEV1.1{letter}"
        refs.append(candidate)
        _seed_accepted_handoff(
            database_path,
            card_id,
            step=f"DEV1.1{letter}",
            candidate_id=candidate,
            sequence=sequence,
        )
    payload = _checkpoint_payload("DEV1.2", refs)
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT result_kind, canonical_payload FROM initiative_phase_results"
        ).fetchone()
        assert row[0] == "orchestration_checkpoint"
        assert json.loads(row[1])["total_item_count"] == 15


def test_unknown_checkpoint_step_rejects_without_spending_approval(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path, "D4")
    payload = _checkpoint_payload("D4.3", [])
    payload["update"]["result_id"] = "checkpoint:D4.9"
    payload["update"]["result"]["step"] = "D4.9"
    payload["approval_id"] = "approval:D4.9"
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
            (payload["approval_id"],),
        ).fetchone()[0] == "approved"


def test_d4_3_rejects_unexpected_checkpoint_predecessor(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    _seed_accepted_handoff(
        database_path, card_id, step="D4.1", candidate_id="candidate:D4.1", sequence=1
    )
    _seed_accepted_handoff(
        database_path, card_id, step="D4.2", candidate_id="candidate:D4.2", sequence=2
    )
    payload = _checkpoint_payload("D4.3", ["candidate:D4.1", "candidate:D4.2"])
    payload["update"]["accepted_checkpoint_refs"] = ["unexpected:checkpoint"]
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
            (payload["approval_id"],),
        ).fetchone()[0] == "approved"


def test_checkpoint_rejects_candidate_whose_unified_card_identity_is_corrupt(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    _seed_accepted_handoff(
        database_path, card_id, step="D4.1", candidate_id="candidate:D4.1", sequence=1
    )
    _seed_accepted_handoff(
        database_path, card_id, step="D4.2", candidate_id="candidate:D4.2", sequence=2
    )
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE adrian_kanban_cards SET task_id = 'corrupt-task-id' "
            "WHERE task_id = 'task:D4.2'"
        )
        conn.commit()
    payload = _checkpoint_payload("D4.3", ["candidate:D4.1", "candidate:D4.2"])
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0] == 0


def _seed_checkpoint(
    database_path: Path,
    card_id: int,
    *,
    result_id: str,
    step: str,
    iteration: int,
    created_at: int,
) -> None:
    canonical_payload = {"step": step}
    if step == "DEV1.2":
        canonical_payload["cumulative_record_ref"] = (
            "2-design/dev1.2.md@" + "a" * 40
        )
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "(?, ?, 'initiative-1', 'DEV1', NULL, ?, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.dev1', '1', "
            "?, '[]', '[]', '{}', ?, 1, ?)",
            (
                result_id,
                card_id,
                iteration,
                json.dumps(
                    canonical_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                f"seed:{result_id}",
                created_at,
            ),
        )
        conn.commit()


def _dev1_3_payload(*, dry: bool = True) -> dict:
    payload = _checkpoint_payload("DEV1.2", [])
    payload["approval_id"] = "approval:DEV1.3"
    payload["update"]["result_id"] = "checkpoint:DEV1.3"
    payload["update"]["iteration"] = 2
    payload["update"]["accepted_checkpoint_refs"] = ["checkpoint:DEV1.2"]
    payload["update"]["result"] = {
        "step": "DEV1.3",
        "pass_counter": 1,
        "current_cumulative_record_ref": "2-design/dev1.2.md@" + "a" * 40,
        "updated_cumulative_record_ref": "2-design/dev1.3.md@" + "b" * 40,
        "follow_up_task_refs": [],
        "no_new_material_declarations": [
            {"step": f"DEV1.1{letter}", "no_new_material": True}
            for letter in "abcde"
        ],
        "dry": dry,
    }
    return payload


def test_dev1_3_accepts_latest_cumulative_checkpoint_and_five_angle_dry_result(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "DEV1")
    _seed_checkpoint(
        database_path,
        card_id,
        result_id="checkpoint:DEV1.2",
        step="DEV1.2",
        iteration=1,
        created_at=1,
    )
    payload = _dev1_3_payload()
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "ACCEPTED"
    assert result["value"]["step"] == "DEV1.3"
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT canonical_payload, accepted_checkpoint_refs "
            "FROM initiative_phase_results WHERE result_id = 'checkpoint:DEV1.3'"
        ).fetchone()
        assert json.loads(row[0])["dry"] is True
        assert json.loads(row[1]) == ["checkpoint:DEV1.2"]


def test_dev1_3_dry_rejects_any_angle_with_new_material(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "DEV1")
    _seed_checkpoint(
        database_path,
        card_id,
        result_id="checkpoint:DEV1.2",
        step="DEV1.2",
        iteration=1,
        created_at=1,
    )
    payload = _dev1_3_payload()
    payload["update"]["result"]["no_new_material_declarations"][2][
        "no_new_material"
    ] = False
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_phase_results "
            "WHERE result_id = 'checkpoint:DEV1.3'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
            (payload["approval_id"],),
        ).fetchone()[0] == "approved"


def test_d4_4_rejects_corrupt_d4_3_payload_even_when_missing_values_match(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "('checkpoint:D4.3', ?, 'initiative-1', 'D4', NULL, 1, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.d4', '1', "
            "?, '[]', '[]', '{}', 'seed:d4.3', 1, 1)",
            (card_id, json.dumps({"step": "D4.3"})),
        )
        conn.commit()
    payload = _checkpoint_payload("D4.4", [])
    payload["update"]["iteration"] = 2
    payload["update"]["accepted_checkpoint_refs"] = ["checkpoint:D4.3"]
    payload["update"]["result"].pop("write_gate_approval_ref")
    payload["update"]["result"].pop("approved_change_set_digest")
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
            (payload["approval_id"],),
        ).fetchone()[0] == "approved"


def test_dev1_2_rejects_boolean_total_even_when_equal_to_numeric_sum(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "DEV1")
    refs = []
    for sequence, letter in enumerate("abcde", start=1):
        candidate = f"candidate:DEV1.1{letter}"
        refs.append(candidate)
        _seed_accepted_handoff(
            database_path,
            card_id,
            step=f"DEV1.1{letter}",
            candidate_id=candidate,
            sequence=sequence,
        )
    payload = _checkpoint_payload("DEV1.2", refs)
    for index, angle in enumerate(payload["update"]["result"]["source_angles"]):
        angle["item_count"] = 1 if index == 0 else 0
    payload["update"]["result"]["total_item_count"] = True
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"


def test_dev1_3_rejects_nonboolean_dry_and_stale_cumulative_reference(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "DEV1")
    _seed_checkpoint(
        database_path,
        card_id,
        result_id="checkpoint:DEV1.2",
        step="DEV1.2",
        iteration=1,
        created_at=1,
    )
    boundary = _boundary(commands_module, database_path, provider)

    nonboolean = _dev1_3_payload()
    nonboolean["update"]["result"]["dry"] = "yes"
    _approve(database_path, nonboolean)
    assert _submit(boundary, nonboolean)["result"] == "REJECTED"

    stale = _dev1_3_payload()
    stale["approval_id"] = "approval:DEV1.3:stale"
    stale["update"]["result_id"] = "checkpoint:DEV1.3:stale"
    stale["update"]["result"]["current_cumulative_record_ref"] = (
        "2-design/stale.md@" + "c" * 40
    )
    _approve(database_path, stale)
    assert _submit(boundary, stale)["result"] == "REJECTED"
