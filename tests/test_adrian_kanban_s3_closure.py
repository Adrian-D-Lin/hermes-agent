"""Initiative-closure tests derived from Kanban design v0.28 section 7.9."""

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
    package_name = "s3_adrian_kanban_closure"
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


def _digest(payload):
    return hashlib.sha256(
        json.dumps(
            {key: value for key, value in payload.items() if key != "approval_id"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _approve(database_path, payload):
    now = int(time.time())
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO write_gate_kanban_approvals ("
            "approval_id, approval_type, state, request_id, operation, "
            "initiative_id, proposed_creation_id, expected_version, "
            "canonical_digest, canonicalization_version, authorizer_evidence, "
            "session_id, prepared_at, approved_at, expires_at, approval_evidence"
            ") VALUES ('approval-close', 'kanban_initiative_mutation', "
            "'approved', 'attempt-close', 'kanban_close_initiative', "
            "'initiative-1', NULL, 0, ?, 1, ?, 'session-initiative', ?, ?, ?, ?)",
            (
                _digest(payload),
                '{"peer_identity":"adrian@tailnet"}',
                now - 2,
                now - 1,
                now + 600,
                "approved-on-second-action",
            ),
        )


def _phase_result(conn, card_id, phase, segment_id, result_id, result_kind, result):
    conn.execute(
        "INSERT INTO initiative_phase_results ("
        "result_id, initiative_card_id, initiative_id, phase, segment_id, "
        "iteration, result_kind, contract_id, contract_version, canonical_payload, "
        "accepted_task_refs, accepted_checkpoint_refs, actor_evidence, "
        "idempotency_key, accepted, created_at) VALUES "
        "(?, ?, 'initiative-1', ?, ?, 1, ?, ?, '1', ?, '[]', '[]', ?, ?, 1, 10)",
        (
            result_id,
            card_id,
            phase,
            segment_id,
            result_kind,
            f"adrian-kanban.lifecycle.{phase.lower()}",
            json.dumps(result, sort_keys=True, separators=(",", ":")),
            json.dumps(
                {"session_id": "session", "actor_profile": "default"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            f"key-{result_id}",
        ),
    )


def _seed_closable(database_path):
    with sqlite3.connect(database_path) as conn:
        conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')")
        card_id = conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, board_slug, "
            "record_version) VALUES ('initiative', 'initiative-1', NULL, "
            "'Initiative', '# [[INITIATIVE_LEDGER]]', 1, 'orchestrator', 0)"
        ).lastrowid
        conn.execute(
            "INSERT INTO initiative_transitions "
            "(initiative_card_id, initiative_id, previous_transition_id, "
            "transition_id, from_phase, from_segment_id, to_phase, to_segment_id, "
            "trigger, actor_evidence, canonical_payload, created_at) VALUES "
            "(?, 'initiative-1', NULL, 1, NULL, NULL, 'D1', NULL, "
            "'initialization', 'session', '{}', 1)",
            (card_id,),
        )
        conn.execute(
            "INSERT INTO initiative_transitions "
            "(initiative_card_id, initiative_id, previous_transition_id, "
            "transition_id, from_phase, from_segment_id, to_phase, to_segment_id, "
            "trigger, actor_evidence, canonical_payload, created_at) VALUES "
            "(?, 'initiative-1', 1, 2, 'DEV3', 'S1', 'DEV4', 'S1', "
            "'exit-gate', 'session', '{}', 2)",
            (card_id,),
        )
        conn.execute(
            "INSERT INTO initiative_segment_projections "
            "(projection_id, projection_version, initiative_card_id, initiative_id, "
            "manifest_path, manifest_sha, content_digest, parsed_segment_definitions, "
            "readiness_refs, validation_result, projected_at) VALUES "
            "('projection-1', 1, ?, 'initiative-1', 'segments.json', ?, ?, ?, ?, "
            "'accepted', 3)",
            (
                card_id,
                "a" * 40,
                "b" * 64,
                json.dumps([{"segment_id": "S1", "ordinal": 1}]),
                json.dumps({"S1": "ready:S1"}),
            ),
        )
        conn.execute(
            "INSERT INTO segment_workspaces "
            "(workspace_id, initiative_card_id, initiative_id, segment_id, "
            "projection_id, lifecycle_state, controller_binding_ref, active, "
            "created_at, updated_at) VALUES ('workspace-S1', ?, 'initiative-1', "
            "'S1', 'projection-1', 'retired', 'binding-S1', 0, 3, 4)",
            (card_id,),
        )
        conn.execute(
            "INSERT INTO segment_workspace_members "
            "(workspace_id, repository_identity, relative_path, branch, "
            "required_base_sha, observed_head, member_state, observed_at) VALUES "
            "('workspace-S1', 'repo', 'initiative-1/S1', 'segment/S1', ?, ?, "
            "'retired', 4)",
            ("c" * 40, "d" * 40),
        )
        resolved = []
        for phase, segment in (
            ("D1", None),
            ("D2", None),
            ("D3", None),
            ("D4", None),
            ("DEV1", None),
            ("DEV2", "S1"),
            ("DEV3", "S1"),
            ("DEV4", "S1"),
        ):
            result_id = f"result-{phase}-{segment or 'initiative'}"
            resolved.append(result_id)
            _phase_result(conn, card_id, phase, segment, result_id, "phase_result", {})
        reconciliation = {
            "initiative_id": "initiative-1",
            "board": "orchestrator",
            "previous_transition_id": 2,
            "from_phase": "DEV4",
            "from_segment_id": "S1",
            "to_phase": "closed",
            "to_segment_id": None,
            "canon_route": "Canon/design-lifecycle.md#initiative-closure",
            "exit_gate_ref": "Canon/design-lifecycle.md#7.9",
            "verification_result": "accepted",
        }
        _phase_result(
            conn,
            card_id,
            "DEV4",
            "S1",
            "reconciliation-close",
            "repository_reconciliation",
            reconciliation,
        )
        closure = {
            "final_summary_ref": "final-summary.md@" + "e" * 40,
            "user_approval_ref": "adrian:closure-decision-1",
            "repository_reconciliation_ref": "reconciliation-close",
            "resolved_phase_result_refs": resolved,
            "cancelled_task_refs": [],
            "closure_conclusion": "approved",
        }
        _phase_result(
            conn,
            card_id,
            "DEV4",
            "S1",
            "closure-result",
            "initiative_closure",
            closure,
        )
    return resolved


def _payload():
    return {
        "initiative_id": "initiative-1",
        "closure_result_ref": "closure-result",
        "approval_id": "approval-close",
        "board": "orchestrator",
    }


def _submit(commands_module, database_path, provider, key="key-close", payload=None):
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_close_initiative": commands_module._handle_close_initiative},
    )
    return boundary.submit(
        "kanban_close_initiative",
        attempt_id="attempt-close",
        idempotency_key=key,
        target="initiative-1",
        expected_version=0,
        session_id="session-initiative",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile="default",
        payload=_payload() if payload is None else payload,
    )


def _public_closure_payload():
    return {
        "initiative_id": "initiative-1",
        "closure_result_ref": "closure-result",
        "dev4_5_checkpoint_ref": "checkpoint-DEV4.5",
        "final_summary_ref": "final-summary.md@" + "e" * 40,
        "repository_reconciliation_ref": "reconciliation-close",
        "resolved_phase_result_refs": _seed_result_refs(),
        "cancelled_task_refs": [],
        "approval_id": "approval-close",
        "board": "orchestrator",
    }


def _replace_seeded_closure_with_final_checkpoint(database_path):
    with sqlite3.connect(database_path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards "
            "WHERE initiative_id='initiative-1' AND card_type='initiative'"
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM initiative_phase_results WHERE result_id='closure-result'"
        )
        result = {
            "step": "DEV4.5",
            "workspace_retirement_checkpoint_ref": "checkpoint-DEV4.4",
            "completed_segment_id": "S1",
            "action": "close_initiative",
            "next_segment_id": None,
            "closure_evidence_ref": "final-summary.md@" + "e" * 40,
            "next_route": "CLOSED",
        }
        conn.execute(
            "INSERT INTO initiative_phase_results ("
            "result_id,initiative_card_id,initiative_id,phase,segment_id,iteration,"
            "result_kind,contract_id,contract_version,canonical_payload,"
            "accepted_task_refs,accepted_checkpoint_refs,actor_evidence,"
            "idempotency_key,accepted,created_at) VALUES ("
            "'checkpoint-DEV4.5',?,'initiative-1','DEV4','S1',2,"
            "'orchestration_checkpoint','adrian-kanban.lifecycle.dev4','1',?,"
            "'[]','[\"checkpoint-DEV4.4\"]',?,'key-checkpoint-DEV4.5',1,11)",
            (
                card_id,
                json.dumps(result, sort_keys=True, separators=(",", ":")),
                json.dumps(
                    {
                        "session_id": "session",
                        "actor_profile": "default",
                        "source_transition_id": 2,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )


def test_public_close_composes_closure_record_from_final_checkpoint_atomically(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_closable(database_path)
    _replace_seeded_closure_with_final_checkpoint(database_path)
    payload = _public_closure_payload()
    _approve(database_path, payload)

    result = _submit(
        commands_module,
        database_path,
        provider,
        "key-public-close",
        payload=payload,
    )

    assert result["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT result_kind,canonical_payload,accepted_checkpoint_refs,"
            "idempotency_key,accepted FROM initiative_phase_results "
            "WHERE result_id='closure-result'"
        ).fetchone()
        assert row is not None
        assert row[0] == "initiative_closure"
        assert json.loads(row[1]) == {
            "cancelled_task_refs": [],
            "closure_conclusion": "approved",
            "final_summary_ref": payload["final_summary_ref"],
            "repository_reconciliation_ref": "reconciliation-close",
            "resolved_phase_result_refs": _seed_result_refs(),
            "user_approval_ref": "approval-close",
        }
        assert json.loads(row[2]) == ["checkpoint-DEV4.5"]
        assert row[3] == "key-public-close"
        assert row[4] == 1
        assert conn.execute(
            "SELECT closed_at FROM adrian_kanban_cards "
            "WHERE initiative_id='initiative-1'"
        ).fetchone()[0] is not None
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id='approval-close'"
        ).fetchone()[0] == "consumed"


@pytest.mark.parametrize("defect", ("missing", "wrong_action", "wrong_summary"))
def test_public_close_rejects_invalid_final_checkpoint_without_spending_approval(
    commands_module, tmp_path, monkeypatch, defect
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_closable(database_path)
    _replace_seeded_closure_with_final_checkpoint(database_path)
    payload = _public_closure_payload()
    with sqlite3.connect(database_path) as conn:
        if defect == "missing":
            conn.execute(
                "DELETE FROM initiative_phase_results "
                "WHERE result_id='checkpoint-DEV4.5'"
            )
        else:
            checkpoint = json.loads(
                conn.execute(
                    "SELECT canonical_payload FROM initiative_phase_results "
                    "WHERE result_id='checkpoint-DEV4.5'"
                ).fetchone()[0]
            )
            if defect == "wrong_action":
                checkpoint["action"] = "admit_next_segment"
            else:
                checkpoint["closure_evidence_ref"] = "other-summary"
            conn.execute(
                "UPDATE initiative_phase_results SET canonical_payload=? "
                "WHERE result_id='checkpoint-DEV4.5'",
                (json.dumps(checkpoint, sort_keys=True, separators=(",", ":")),),
            )
    _approve(database_path, payload)

    result = _submit(
        commands_module,
        database_path,
        provider,
        f"key-public-{defect}",
        payload=payload,
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_phase_results "
            "WHERE result_id='closure-result'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT closed_at FROM adrian_kanban_cards "
            "WHERE initiative_id='initiative-1'"
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id='approval-close'"
        ).fetchone()[0] == "approved"


def test_closure_is_atomic_idempotent_and_removes_initiative_from_active_state(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_closable(database_path)
    _approve(database_path, _payload())
    first = _submit(commands_module, database_path, provider)
    replay = _submit(commands_module, database_path, provider)
    assert first["result"] == "ACCEPTED"
    assert replay == first
    assert first["value"]["record_version"] == 1
    assert isinstance(first["value"]["closed_at"], int)
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT closed_at, record_version, body FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-1'"
        ).fetchone() == (
            first["value"]["closed_at"],
            1,
            "# [[INITIATIVE_LEDGER]]",
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_transitions "
            "WHERE initiative_id = 'initiative-1'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_phase_results "
            "WHERE initiative_id = 'initiative-1'"
        ).fetchone()[0] == 10
        closure_payload = json.loads(
            conn.execute(
                "SELECT canonical_payload FROM initiative_phase_results "
                "WHERE result_id = 'closure-result'"
            ).fetchone()[0]
        )
        assert closure_payload["repository_reconciliation_ref"] == (
            "reconciliation-close"
        )
        assert closure_payload["resolved_phase_result_refs"] == _seed_result_refs()
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id = 'approval-close'"
        ).fetchone()[0] == "consumed"


def _seed_result_refs():
    return [
        f"result-{phase}-{segment or 'initiative'}"
        for phase, segment in (
            ("D1", None),
            ("D2", None),
            ("D3", None),
            ("D4", None),
            ("DEV1", None),
            ("DEV2", "S1"),
            ("DEV3", "S1"),
            ("DEV4", "S1"),
        )
    ]


@pytest.mark.parametrize(
    "defect",
    (
        "missing_phase",
        "active_workspace",
        "open_task",
        "archived_task_without_cancellation_ref",
        "done_lifecycle_task_without_verdict",
    ),
)
def test_closure_rejects_unresolved_work_without_spending_approval(
    commands_module, tmp_path, monkeypatch, defect
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_closable(database_path)
    with sqlite3.connect(database_path) as conn:
        if defect == "missing_phase":
            conn.execute(
                "DELETE FROM initiative_phase_results "
                "WHERE result_id = 'result-D3-initiative'"
            )
        elif defect == "active_workspace":
            conn.execute(
                "UPDATE segment_workspaces SET active = 1, lifecycle_state = 'active' "
                "WHERE workspace_id = 'workspace-S1'"
            )
        elif defect == "open_task":
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) "
                "VALUES ('open-task', 'Open', 'ready', 1)"
            )
            conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, created_at, board_slug, "
                "record_version) VALUES ('task', 'initiative-1', 'open-task', "
                "'Open', 1, 'orchestrator', 0)"
            )
        elif defect == "archived_task_without_cancellation_ref":
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) "
                "VALUES ('archived-task', 'Archived', 'archived', 1)"
            )
            conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, created_at, board_slug, "
                "record_version) VALUES ('task', 'initiative-1', 'archived-task', "
                "'Archived', 1, 'orchestrator', 0)"
            )
        else:
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) "
                "VALUES ('done-task', 'Done lifecycle task', 'done', 1)"
            )
            task_card_id = conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, created_at, board_slug, "
                "record_version) VALUES ('task', 'initiative-1', 'done-task', "
                "'Done lifecycle task', 1, 'orchestrator', 0)"
            ).lastrowid
            initiative_card_id = conn.execute(
                "SELECT id FROM adrian_kanban_cards "
                "WHERE initiative_id = 'initiative-1' AND card_type = 'initiative'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO task_lifecycle_contracts ("
                "contract_id, contract_version, step, task_card_id, task_id, "
                "initiative_card_id, initiative_id, segment_id, workspace_id, "
                "execution_profile, canonical_contract_payload, registry_hash, "
                "skill_id, skill_version, skill_hash, created_at) VALUES ("
                "'adrian-kanban.lifecycle.d2', '1', 'D2', ?, 'done-task', ?, "
                "'initiative-1', NULL, NULL, 'builder-tester', '{}', ?, "
                "'d2-iterative-review', '1', ?, 1)",
                (task_card_id, initiative_card_id, "a" * 64, "b" * 64),
            )
    _approve(database_path, _payload())
    result = _submit(commands_module, database_path, provider, f"key-{defect}")
    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT closed_at FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-1'"
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id = 'approval-close'"
        ).fetchone()[0] == "approved"


def test_closure_rejects_cancellation_ref_that_is_not_an_archived_task(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_closable(database_path)
    with sqlite3.connect(database_path) as conn:
        closure = json.loads(
            conn.execute(
                "SELECT canonical_payload FROM initiative_phase_results "
                "WHERE result_id = 'closure-result'"
            ).fetchone()[0]
        )
        closure["cancelled_task_refs"] = ["not-an-archived-task"]
        conn.execute(
            "UPDATE initiative_phase_results SET canonical_payload = ? "
            "WHERE result_id = 'closure-result'",
            (json.dumps(closure, sort_keys=True, separators=(",", ":")),),
        )
    _approve(database_path, _payload())
    result = _submit(commands_module, database_path, provider, "key-bad-cancel-ref")
    assert result["result"] == "REJECTED"


def test_closure_requires_default_profile_and_exact_reconciliation(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_closable(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE initiative_phase_results SET actor_evidence = ? "
            "WHERE result_id = 'closure-result'",
            (json.dumps({"actor_profile": "independent-reviewer"}),),
        )
    _approve(database_path, _payload())
    result = _submit(commands_module, database_path, provider, "key-wrong-actor")
    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id = 'approval-close'"
        ).fetchone()[0] == "approved"


def test_closure_rejects_cross_initiative_phase_result_references(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_closable(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-2')")
        foreign_card_id = conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, board_slug, "
            "record_version) VALUES ('initiative', 'initiative-2', NULL, "
            "'Foreign', '# [[INITIATIVE_LEDGER]]', 1, 'orchestrator', 0)"
        ).lastrowid
        conn.execute(
            "INSERT INTO initiative_phase_results ("
            "result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, canonical_payload, "
            "accepted_task_refs, accepted_checkpoint_refs, actor_evidence, "
            "idempotency_key, accepted, created_at) VALUES ("
            "'foreign-result-D3', ?, 'initiative-2', 'D3', NULL, 1, "
            "'phase_result', 'adrian-kanban.lifecycle.d3', '1', '{}', '[]', '[]', "
            "?, 'key-foreign-result-D3', 1, 10)",
            (
                foreign_card_id,
                json.dumps({"session_id": "foreign", "actor_profile": "default"}),
            ),
        )
        closure = json.loads(
            conn.execute(
                "SELECT canonical_payload FROM initiative_phase_results "
                "WHERE result_id = 'closure-result'"
            ).fetchone()[0]
        )
        closure["resolved_phase_result_refs"].remove("result-D3-initiative")
        closure["resolved_phase_result_refs"].append("foreign-result-D3")
        conn.execute(
            "UPDATE initiative_phase_results SET canonical_payload = ? "
            "WHERE result_id = 'closure-result'",
            (json.dumps(closure, sort_keys=True, separators=(",", ":")),),
        )
    _approve(database_path, _payload())
    result = _submit(commands_module, database_path, provider, "key-foreign-result")
    assert result["result"] == "REJECTED"


def test_closure_rejects_dev4_before_the_final_registered_segment(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_closable(database_path)
    with sqlite3.connect(database_path) as conn:
        initiative_card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-1' AND card_type = 'initiative'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE initiative_segment_projections "
            "SET parsed_segment_definitions = ? WHERE projection_id = 'projection-1'",
            (json.dumps([{"segment_id": "S1", "ordinal": 1}, {"segment_id": "S2", "ordinal": 2}]),),
        )
        conn.execute(
            "INSERT INTO segment_workspaces ("
            "workspace_id, initiative_card_id, initiative_id, segment_id, "
            "projection_id, lifecycle_state, controller_binding_ref, active, "
            "created_at, updated_at) VALUES ('workspace-S2', ?, 'initiative-1', "
            "'S2', 'projection-1', 'retired', 'binding-S2', 0, 3, 4)",
            (initiative_card_id,),
        )
        conn.execute(
            "INSERT INTO segment_workspace_members ("
            "workspace_id, repository_identity, relative_path, branch, "
            "required_base_sha, observed_head, member_state, observed_at) VALUES ("
            "'workspace-S2', 'repo', 'initiative-1/S2', 'segment/S2', ?, ?, "
            "'retired', 4)",
            ("e" * 40, "f" * 40),
        )
        closure = json.loads(
            conn.execute(
                "SELECT canonical_payload FROM initiative_phase_results "
                "WHERE result_id = 'closure-result'"
            ).fetchone()[0]
        )
        for phase in ("DEV2", "DEV3", "DEV4"):
            result_id = f"result-{phase}-S2"
            _phase_result(
                conn,
                initiative_card_id,
                phase,
                "S2",
                result_id,
                "phase_result",
                {},
            )
            closure["resolved_phase_result_refs"].append(result_id)
        conn.execute(
            "UPDATE initiative_phase_results SET canonical_payload = ? "
            "WHERE result_id = 'closure-result'",
            (json.dumps(closure, sort_keys=True, separators=(",", ":")),),
        )
    _approve(database_path, _payload())
    result = _submit(commands_module, database_path, provider, "key-not-final")
    assert result["result"] == "REJECTED"


def _move_closure_to_pc1(database_path, segment_id):
    with sqlite3.connect(database_path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-1' AND card_type = 'initiative'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO initiative_transitions ("
            "initiative_card_id, initiative_id, previous_transition_id, "
            "transition_id, from_phase, from_segment_id, to_phase, to_segment_id, "
            "trigger, actor_evidence, canonical_payload, created_at) VALUES ("
            "?, 'initiative-1', 2, 3, 'DEV4', 'S1', 'PC1', ?, "
            "'exit-gate', 'session', '{}', 3)",
            (card_id, segment_id),
        )
        reconciliation = {
            "initiative_id": "initiative-1",
            "board": "orchestrator",
            "previous_transition_id": 3,
            "from_phase": "PC1",
            "from_segment_id": segment_id,
            "to_phase": "closed",
            "to_segment_id": None,
            "canon_route": "Canon/design-lifecycle.md#initiative-closure",
            "exit_gate_ref": "Canon/design-lifecycle.md#7.9",
            "verification_result": "accepted",
        }
        conn.execute(
            "UPDATE initiative_phase_results SET phase = 'PC1', segment_id = ?, "
            "contract_id = 'adrian-kanban.lifecycle.pc1', canonical_payload = ? "
            "WHERE result_id = 'reconciliation-close'",
            (
                segment_id,
                json.dumps(reconciliation, sort_keys=True, separators=(",", ":")),
            ),
        )
        conn.execute(
            "UPDATE initiative_phase_results SET phase = 'PC1', segment_id = ?, "
            "contract_id = 'adrian-kanban.lifecycle.pc1' "
            "WHERE result_id = 'closure-result'",
            (segment_id,),
        )


@pytest.mark.parametrize(
    ("segment_id", "expected"),
    ((None, "ACCEPTED"), ("S1", "REJECTED")),
)
def test_pc1_closure_accepts_only_milestone_null_segment_scope(
    commands_module, tmp_path, monkeypatch, segment_id, expected
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_closable(database_path)
    _move_closure_to_pc1(database_path, segment_id)
    _approve(database_path, _payload())
    result = _submit(
        commands_module,
        database_path,
        provider,
        f"key-pc1-{segment_id or 'null'}",
    )
    assert result["result"] == expected
