"""Initiative mutation tests derived from Adrian Kanban design v0.28."""

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
    package_name = "s3_adrian_kanban_initiatives"
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


_BODY = """# [[INITIATIVE_LEDGER]]

## Initiative

### Objective
Deliver the replacement Kanban.

### Board and workspace context
- Board: `orchestrator`
- Working reference: `E:/AI/s3-hermes-agent`
- Initiative ID: `initiative-1`

### Authoritative artifacts
- `2-design/kanban.md` — governing design

### Cleared outcomes
- None

### Open items
- Complete delivery

### Related task cards
- None

### Constraints
- Initiative card is not dispatchable and cannot be a dependency endpoint.

### Cold-session continuation
Continue the active implementation.
"""


def _approval_digest(payload: dict) -> str:
    approved_payload = {key: value for key, value in payload.items() if key != "approval_id"}
    return hashlib.sha256(
        json.dumps(
            approved_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _approve(
    database_path: Path,
    *,
    approval_id: str,
    attempt_id: str,
    operation: str,
    target: str,
    expected_version: int,
    payload: dict,
    session_id: str = "session-initiative",
):
    now = int(time.time())
    creation = operation == "kanban_create_initiative"
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO write_gate_kanban_approvals ("
            "approval_id, approval_type, state, request_id, operation, "
            "initiative_id, proposed_creation_id, expected_version, "
            "canonical_digest, canonicalization_version, authorizer_evidence, "
            "session_id, prepared_at, approved_at, expires_at, approval_evidence"
            ") VALUES (?, 'kanban_initiative_mutation', 'approved', ?, ?, ?, ?, "
            "?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (
                approval_id,
                attempt_id,
                operation,
                None if creation else target,
                target if creation else None,
                expected_version,
                _approval_digest(payload),
                '{"peer_identity":"adrian@tailnet"}',
                session_id,
                now - 2,
                now - 1,
                now + 600,
                "approved-on-second-action",
            ),
        )
        conn.commit()


def _submit(
    boundary,
    operation,
    *,
    attempt_id,
    key,
    target,
    version,
    payload,
    actor_profile="default",
):
    return boundary.submit(
        operation,
        attempt_id=attempt_id,
        idempotency_key=key,
        target=target,
        expected_version=version,
        session_id="session-initiative",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile=actor_profile,
        payload=payload,
    )


def test_create_initiative_consumes_exact_approval_and_initializes_d1(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    payload = {
        "initiative_id": "initiative-1",
        "title": "Kanban replacement",
        "body": _BODY,
        "approval_id": "approval-create",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-create",
        attempt_id="attempt-create",
        operation="kanban_create_initiative",
        target="initiative-1",
        expected_version=0,
        payload=payload,
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_create_initiative": commands_module._handle_create_initiative
        },
    )

    first = _submit(
        boundary,
        "kanban_create_initiative",
        attempt_id="attempt-create",
        key="key-create",
        target="initiative-1",
        version=0,
        payload=payload,
    )
    replay = _submit(
        boundary,
        "kanban_create_initiative",
        attempt_id="attempt-create",
        key="key-create",
        target="initiative-1",
        version=0,
        payload=payload,
    )

    assert first["result"] == "ACCEPTED"
    assert replay == first
    assert first["value"] == {
        "initiative_id": "initiative-1",
        "phase": "D1",
        "record_version": 0,
    }
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        card = conn.execute(
            "SELECT * FROM adrian_kanban_cards WHERE initiative_id = ?",
            ("initiative-1",),
        ).fetchone()
        assert card is not None
        assert card["card_type"] == "initiative"
        assert card["task_id"] is None
        assert card["body"] == _BODY
        assert card["closed_at"] is None
        transition = conn.execute(
            "SELECT * FROM initiative_transitions WHERE initiative_id = ?",
            ("initiative-1",),
        ).fetchone()
        assert transition["previous_transition_id"] is None
        assert transition["to_phase"] == "D1"
        assert transition["to_segment_id"] is None
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
            ("approval-create",),
        ).fetchone()[0] == "consumed"
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_cards"
        ).fetchone()[0] == 1


def test_wrong_approval_digest_rejects_without_spending_or_creating(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    payload = {
        "initiative_id": "initiative-1",
        "title": "Kanban replacement",
        "body": _BODY,
        "approval_id": "approval-create",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-create",
        attempt_id="attempt-create",
        operation="kanban_create_initiative",
        target="initiative-1",
        expected_version=0,
        payload={**payload, "title": "different approved title"},
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_create_initiative": commands_module._handle_create_initiative
        },
    )

    result = _submit(
        boundary,
        "kanban_create_initiative",
        attempt_id="attempt-create",
        key="key-create",
        target="initiative-1",
        version=0,
        payload=payload,
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_cards"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
            ("approval-create",),
        ).fetchone()[0] == "approved"


def _seed_initiative(database_path: Path):
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')"
        )
        card_id = conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, "
            "board_slug, record_version) VALUES "
            "('initiative', 'initiative-1', NULL, 'Initiative', ?, 1, "
            "'orchestrator', 0)",
            (_BODY,),
        ).lastrowid
        conn.execute(
            "INSERT INTO initiative_transitions "
            "(initiative_card_id, initiative_id, previous_transition_id, "
            "transition_id, from_phase, from_segment_id, to_phase, "
            "to_segment_id, trigger, actor_evidence, canonical_payload, "
            "created_at) VALUES (?, 'initiative-1', NULL, 1, NULL, NULL, "
            "'D1', NULL, 'initialization', 'session-initiative', '{}', 1)",
            (card_id,),
        )
        conn.commit()


def _seed_reconciliation(
    database_path: Path,
    *,
    result_id: str,
    from_phase: str,
    to_phase: str,
    from_segment_id: str | None = None,
    to_segment_id: str | None = None,
):
    payload = {
        "initiative_id": "initiative-1",
        "board": "orchestrator",
        "previous_transition_id": 1,
        "from_phase": from_phase,
        "from_segment_id": from_segment_id,
        "to_phase": to_phase,
        "to_segment_id": to_segment_id,
        "canon_route": f"Canon/design-lifecycle.md#{to_phase}",
        "exit_gate_ref": f"exit-gate:{from_phase}:{to_phase}",
        "verification_result": "accepted",
    }
    with sqlite3.connect(database_path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE initiative_id = ?",
            ("initiative-1",),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "(?, ?, 'initiative-1', ?, ?, 1, 'repository_reconciliation', "
            "?, '1', ?, '[]', '[]', ?, ?, 1, 2)",
            (
                result_id,
                card_id,
                from_phase,
                from_segment_id,
                f"adrian-kanban.lifecycle.{from_phase.lower()}",
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                json.dumps(
                    {
                        "session_id": "reconciliation-session",
                        "actor_profile": "default",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                f"key-{result_id}",
            ),
        )
        conn.commit()


def test_transition_supports_approved_non_linear_route_with_reconciliation(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path)
    _seed_reconciliation(
        database_path,
        result_id="reconciliation-1",
        from_phase="D1",
        to_phase="DEV1",
    )
    payload = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV1",
        "reconciliation_ref": "reconciliation-1",
        "approval_id": "approval-transition",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-transition",
        attempt_id="attempt-transition",
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

    result = _submit(
        boundary,
        "kanban_transition_initiative",
        attempt_id="attempt-transition",
        key="key-transition",
        target="initiative-1",
        version=0,
        payload=payload,
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {
        "initiative_id": "initiative-1",
        "from_phase": "D1",
        "to_phase": "DEV1",
        "transition_id": 2,
        "record_version": 1,
    }
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM initiative_transitions WHERE initiative_id = ? "
            "ORDER BY transition_id",
            ("initiative-1",),
        ).fetchall()
        assert [row["transition_id"] for row in rows] == [1, 2]
        assert rows[1]["previous_transition_id"] == 1
        assert rows[1]["repository_reconciliation_ref"] == payload[
            "reconciliation_ref"
        ]
        assert conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-1'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id = 'approval-transition'"
        ).fetchone()[0] == "consumed"


def test_transition_reports_independent_shape_failures_without_spending_approval(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path)
    payload = {
        "initiative_id": "initiative-1", "board": "orchestrator",
        "to_phase": "unrecognized-private-value", "to_segment_id": 123,
        "approval_id": "approval-transition", "reconciliation_ref": "",
        "sensitive-unknown-key": "sensitive-value",
    }
    _approve(
        database_path, approval_id="approval-transition", attempt_id="attempt-shape",
        operation="kanban_transition_initiative", target="initiative-1",
        expected_version=0, payload=payload,
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path), provider=provider,
        handlers={"kanban_transition_initiative": commands_module._handle_transition_initiative},
    )
    result = _submit(
        boundary, "kanban_transition_initiative", attempt_id="attempt-shape",
        key="key-shape", target="initiative-1", version=0, payload=payload,
    )
    assert result["result"] == "REJECTED"
    findings = {check["code"]: check for check in result["failed_checks"]}
    assert set(findings) == {
        "TRANSITION_UNKNOWN_FIELDS", "TRANSITION_PHASE_UNKNOWN",
        "TRANSITION_FIELD_INVALID:reconciliation_ref",
        "TRANSITION_FIELD_INVALID:to_segment_id",
    }
    assert result["not_evaluated_checks"] == [{
        "code": "TRANSITION_SEGMENT_PHASE_COMPATIBILITY",
        "requires": ["TRANSITION_PHASE_UNKNOWN", "TRANSITION_FIELD_INVALID:to_segment_id"],
    }]
    for check in findings.values():
        assert check["accepted_format"]
        assert check["remediation"]
        assert check["responsible_actor"] == "orchestrator"
    serialized = json.dumps(result)
    for secret in ("sensitive-unknown-key", "sensitive-value", "unrecognized-private-value"):
        assert secret not in serialized
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = 'approval-transition'"
        ).fetchone()[0] == "approved"
        assert conn.execute("SELECT COUNT(*) FROM adrian_kanban_command_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("phase,segment,accepted", [
    ("DEV2", "S1", True), ("DEV3", "S1", True), ("DEV4", "S1", True),
    ("D1", None, True), ("D2", None, True), ("D3", None, True),
    ("D4", None, True), ("DEV1", None, True), ("PC1", None, True),
    ("DEV2", None, False), ("DEV3", None, False), ("DEV4", None, False),
    ("D1", "S1", False), ("PC1", "S1", False),
])
def test_transition_shape_keeps_explicit_phase_segment_contract(
    commands_module, phase, segment, accepted
):
    mutations = importlib.import_module(f"{commands_module.__package__}.initiative_mutations")
    diagnostics = importlib.import_module(f"{commands_module.__package__}.diagnostics")
    payload = {
        "initiative_id": " initiative-1 ", "to_phase": phase,
        "to_segment_id": segment, "reconciliation_ref": " reconciliation-1 ",
        "approval_id": "approval-1", "board": "orchestrator",
    }
    original = payload.copy()
    if accepted:
        normalized = mutations._validate_transition_payload(payload)
        assert normalized["initiative_id"] == "initiative-1"
        assert normalized["reconciliation_ref"] == "reconciliation-1"
        assert normalized["to_phase"] == phase
        assert normalized["to_segment_id"] == segment
    else:
        with pytest.raises(diagnostics.CommandRejected) as caught:
            mutations._validate_transition_payload(payload)
        assert [check.code for check in caught.value.failed_checks] == [
            "TRANSITION_SEGMENT_PHASE_COMPATIBILITY"
        ]
    assert payload == original


def test_transition_missing_fields_are_reported_together(commands_module):
    mutations = importlib.import_module(f"{commands_module.__package__}.initiative_mutations")
    diagnostics = importlib.import_module(f"{commands_module.__package__}.diagnostics")
    with pytest.raises(diagnostics.CommandRejected) as caught:
        mutations._validate_transition_payload({})
    assert {check.target for check in caught.value.failed_checks} == {
        "initiative_id", "to_phase", "reconciliation_ref", "approval_id", "board"
    }
    assert caught.value.not_evaluated_checks[0].requires == ("TRANSITION_FIELD_INVALID:to_phase",)


@pytest.mark.parametrize("phase", [[], {}, False, 1, "", "  "])
def test_transition_bad_phase_is_diagnostic_not_internal_exception(commands_module, phase):
    mutations = importlib.import_module(f"{commands_module.__package__}.initiative_mutations")
    diagnostics = importlib.import_module(f"{commands_module.__package__}.diagnostics")
    with pytest.raises(diagnostics.CommandRejected) as caught:
        mutations._validate_transition_payload({
            "initiative_id": "initiative-1", "to_phase": phase,
            "reconciliation_ref": "ref-1", "approval_id": "approval-1", "board": "orchestrator",
        })
    assert [check.code for check in caught.value.failed_checks] == ["TRANSITION_FIELD_INVALID:to_phase"]
    assert caught.value.not_evaluated_checks[0].requires == ("TRANSITION_FIELD_INVALID:to_phase",)


def test_transition_shape_normalizes_phase_before_enum_check(commands_module):
    mutations = importlib.import_module(f"{commands_module.__package__}.initiative_mutations")
    normalized = mutations._validate_transition_payload({
        "initiative_id": "initiative-1", "to_phase": " DEV2 ", "to_segment_id": " S1 ",
        "reconciliation_ref": "ref-1", "approval_id": "approval-1", "board": "orchestrator",
    })
    assert normalized["to_phase"] == "DEV2"
    assert normalized["to_segment_id"] == "S1"


def test_transition_rejects_missing_reconciliation_without_spending_approval(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path)
    payload = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV1",
        "reconciliation_ref": "missing-reconciliation",
        "approval_id": "approval-transition",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-transition",
        attempt_id="attempt-transition",
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

    result = _submit(
        boundary,
        "kanban_transition_initiative",
        attempt_id="attempt-transition",
        key="key-transition",
        target="initiative-1",
        version=0,
        payload=payload,
    )

    assert result["result"] == "REJECTED"
    assert result["failed_checks"][0]["code"] == "RECONCILIATION_NOT_FOUND"
    assert result["not_evaluated_checks"] == [{
        "code": "RECONCILIATION_CONTENT", "requires": ["RECONCILIATION_NOT_FOUND"]
    }]
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_transitions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id = 'approval-transition'"
        ).fetchone()[0] == "approved"


@pytest.mark.parametrize("malformed", [False, True])
def test_reconciliation_reports_independent_stored_evidence_failures(
    commands_module, tmp_path, monkeypatch, malformed
):
    database_path, _provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path)
    _seed_reconciliation(database_path, result_id="bad-ref", from_phase="D1", to_phase="DEV1")
    mutations = importlib.import_module(f"{commands_module.__package__}.initiative_mutations")
    diagnostics = importlib.import_module(f"{commands_module.__package__}.diagnostics")
    with sqlite3.connect(database_path) as conn:
        # Disposable historical/corrupt-state fixture: no production record is edited.
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM initiative_phase_results WHERE result_id='bad-ref'").fetchone()
        route = json.loads(row["canonical_payload"])
        route["to_phase"] = "secret-wrong-phase"
        route["previous_transition_id"] = 88
        route.pop("from_segment_id")
        route["canon_route"] = ""
        route["exit_gate_ref"] = None
        conn.execute(
            "UPDATE initiative_phase_results SET accepted=0, phase='D2', "
            "actor_evidence=?, canonical_payload=? WHERE result_id='bad-ref'",
            ("[secret malformed" if malformed else '{"actor_profile":"private-profile"}',
             "[secret malformed" if malformed else json.dumps(route)),
        )
        conn.commit()
        with pytest.raises(diagnostics.CommandRejected) as caught:
            mutations._validate_reconciliation(
                conn, row["initiative_card_id"], "initiative-1", "orchestrator", "bad-ref",
                1, "D1", None, "DEV1", None,
            )
        checks = {check.code: check for check in caught.value.failed_checks}
        expected = {"RECONCILIATION_ROW_MISMATCH:accepted", "RECONCILIATION_ROW_MISMATCH:phase"}
        if malformed:
            expected |= {"RECONCILIATION_ACTOR_INVALID", "RECONCILIATION_PAYLOAD_INVALID"}
            assert {c.code for c in caught.value.not_evaluated_checks} == {
                "RECONCILIATION_ACTOR_PROFILE", "RECONCILIATION_ROUTE_FIELDS"
            }
        else:
            expected |= {
                "RECONCILIATION_ACTOR_PROFILE", "RECONCILIATION_FIELD_MISMATCH:to_phase",
                "RECONCILIATION_FIELD_MISMATCH:previous_transition_id",
                "RECONCILIATION_FIELD_MISMATCH:from_segment_id",
                "RECONCILIATION_FIELD_INVALID:canon_route", "RECONCILIATION_FIELD_INVALID:exit_gate_ref",
            }
            assert caught.value.not_evaluated_checks == ()
        assert set(checks) == expected
        serialized = json.dumps([c.as_dict() for c in checks.values()])
        for secret in ("secret-wrong-phase", "private-profile", "secret malformed"):
            assert secret not in serialized
        assert not conn.in_transaction


def test_repository_reconciliation_result_requires_default_orchestrator(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path)
    reconciliation = {
        "initiative_id": "initiative-1",
        "board": "orchestrator",
        "previous_transition_id": 1,
        "from_phase": "D1",
        "from_segment_id": None,
        "to_phase": "DEV1",
        "to_segment_id": None,
        "canon_route": "Canon/design-lifecycle.md#DEV1",
        "exit_gate_ref": "exit-gate:D1:DEV1",
        "verification_result": "accepted",
    }
    payload = {
        "initiative_id": "initiative-1",
        "update_kind": "phase_result",
        "update": {
            "result_id": "reconciliation-1",
            "phase": "D1",
            "segment_id": None,
            "iteration": 1,
            "result_kind": "repository_reconciliation",
            "contract_id": "adrian-kanban.lifecycle.d1",
            "contract_version": "1",
            "result": reconciliation,
            "accepted_task_refs": [],
            "accepted_checkpoint_refs": [],
        },
        "approval_id": "approval-reconciliation",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-reconciliation",
        attempt_id="attempt-reconciliation",
        operation="kanban_update_initiative",
        target="initiative-1",
        expected_version=0,
        payload=payload,
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_update_initiative": commands_module._handle_update_initiative
        },
    )

    rejected = _submit(
        boundary,
        "kanban_update_initiative",
        attempt_id="attempt-reconciliation",
        key="key-reconciliation",
        target="initiative-1",
        version=0,
        payload=payload,
        actor_profile="builder-tester",
    )

    assert rejected["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_phase_results"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id = 'approval-reconciliation'"
        ).fetchone()[0] == "approved"

    accepted = _submit(
        boundary,
        "kanban_update_initiative",
        attempt_id="attempt-reconciliation",
        key="key-reconciliation",
        target="initiative-1",
        version=0,
        payload=payload,
    )
    assert accepted["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT result_kind, actor_evidence, canonical_payload "
            "FROM initiative_phase_results WHERE result_id = 'reconciliation-1'"
        ).fetchone()
        assert row[0] == "repository_reconciliation"
        assert json.loads(row[1])["actor_profile"] == "default"
        assert json.loads(row[2]) == reconciliation


def test_segment_transition_requires_manifest_defined_segment(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path)
    _seed_reconciliation(
        database_path,
        result_id="reconciliation-dev2",
        from_phase="D1",
        to_phase="DEV2",
        to_segment_id="S2",
    )
    with sqlite3.connect(database_path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE initiative_id = ?",
            ("initiative-1",),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO initiative_segment_projections "
            "(projection_id, projection_version, initiative_card_id, initiative_id, "
            "manifest_path, manifest_sha, content_digest, parsed_segment_definitions, "
            "readiness_refs, validation_result, projected_at) VALUES "
            "('projection-1', 1, ?, 'initiative-1', '2-design/segments.json', "
            "?, ?, ?, ?, 'accepted', 3)",
            (
                card_id,
                "a" * 40,
                "b" * 64,
                json.dumps([{"segment_id": "S1", "ordinal": 1}]),
                json.dumps({"S1": "ready:S1"}),
            ),
        )
        conn.commit()
    payload = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV2",
        "to_segment_id": "S2",
        "reconciliation_ref": "reconciliation-dev2",
        "approval_id": "approval-transition",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-transition",
        attempt_id="attempt-transition",
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

    result = _submit(
        boundary,
        "kanban_transition_initiative",
        attempt_id="attempt-transition",
        key="key-transition",
        target="initiative-1",
        version=0,
        payload=payload,
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_transitions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id = 'approval-transition'"
        ).fetchone()[0] == "approved"


def test_body_update_preserves_structural_contract_and_phase_result_is_append_only(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path)
    changed_body = _BODY.replace("Complete delivery", "Complete and verify delivery")
    body_payload = {
        "initiative_id": "initiative-1",
        "update_kind": "body_update",
        "update": {"body": changed_body},
        "approval_id": "approval-body",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-body",
        attempt_id="attempt-body",
        operation="kanban_update_initiative",
        target="initiative-1",
        expected_version=0,
        payload=body_payload,
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_update_initiative": commands_module._handle_update_initiative
        },
    )
    body_result = _submit(
        boundary,
        "kanban_update_initiative",
        attempt_id="attempt-body",
        key="key-body",
        target="initiative-1",
        version=0,
        payload=body_payload,
    )
    assert body_result["result"] == "ACCEPTED"

    phase_payload = {
        "initiative_id": "initiative-1",
        "update_kind": "phase_result",
        "update": {
            "result_id": "result-d1-1",
            "phase": "D1",
            "segment_id": None,
            "iteration": 1,
            "result_kind": "phase_close",
            "contract_id": "adrian-kanban.lifecycle.d1",
            "contract_version": "1",
            "result": {
                "baseline_refs": ["git:abc"],
                "artifact_refs": ["2-design/kanban.md@abc"],
                "conclusion": "ready",
                "dispositions": [],
                "next_route": "D2",
            },
            "accepted_task_refs": [],
            "accepted_checkpoint_refs": [],
        },
        "approval_id": "approval-phase-result",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-phase-result",
        attempt_id="attempt-phase-result",
        operation="kanban_update_initiative",
        target="initiative-1",
        expected_version=1,
        payload=phase_payload,
    )
    phase_result = _submit(
        boundary,
        "kanban_update_initiative",
        attempt_id="attempt-phase-result",
        key="key-phase-result",
        target="initiative-1",
        version=1,
        payload=phase_payload,
    )
    assert phase_result["result"] == "ACCEPTED"

    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        card = conn.execute(
            "SELECT body, record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-1'"
        ).fetchone()
        assert card["body"] == changed_body
        assert card["record_version"] == 2
        result = conn.execute(
            "SELECT * FROM initiative_phase_results WHERE result_id = ?",
            ("result-d1-1",),
        ).fetchone()
        assert result["accepted"] == 1
        assert json.loads(result["canonical_payload"])["next_route"] == "D2"
        assert json.loads(result["actor_evidence"])["session_id"] == (
            "session-initiative"
        )


def test_malformed_body_update_is_atomic_and_leaves_approval_reusable(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path)
    payload = {
        "initiative_id": "initiative-1",
        "update_kind": "body_update",
        "update": {"body": "# not the initiative contract"},
        "approval_id": "approval-body",
        "board": "orchestrator",
    }
    _approve(
        database_path,
        approval_id="approval-body",
        attempt_id="attempt-body",
        operation="kanban_update_initiative",
        target="initiative-1",
        expected_version=0,
        payload=payload,
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_update_initiative": commands_module._handle_update_initiative
        },
    )
    result = _submit(
        boundary,
        "kanban_update_initiative",
        attempt_id="attempt-body",
        key="key-body",
        target="initiative-1",
        version=0,
        payload=payload,
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT body FROM adrian_kanban_cards WHERE initiative_id = 'initiative-1'"
        ).fetchone()[0] == _BODY
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals "
            "WHERE approval_id = 'approval-body'"
        ).fetchone()[0] == "approved"
