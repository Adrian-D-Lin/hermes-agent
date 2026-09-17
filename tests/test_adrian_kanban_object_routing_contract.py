"""Cross-directional Initiative Tracker object-routing contract tests."""

from __future__ import annotations

import importlib
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_initiatives import (
    _BODY,
    _database,
    commands_module,  # noqa: F401
)
from tests.test_adrian_kanban_s3_override_approval import evidence


def _seed_both_object_types(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO adrian_kanban_initiatives (initiative_id) VALUES (?)",
            ("initiative-1",),
        )
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, "
            "board_slug, record_version) VALUES "
            "('initiative', 'initiative-1', NULL, 'Initiative', ?, 1, "
            "'orchestrator', 0)",
            (_BODY,),
        )
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, assignee, status, created_at, workspace_kind) VALUES "
            "('task-1', 'Task', 'builder-tester', 'ready', 1, 'scratch')"
        )
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, "
            "board_slug, record_version) VALUES "
            "('task', 'initiative-1', 'task-1', 'Task', '', 1, "
            "'orchestrator', 0)"
        )
        conn.commit()


def _counts(path) -> tuple[int, int, int]:
    with sqlite3.connect(path) as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM adrian_kanban_cards").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            conn.execute(
                "SELECT COUNT(*) FROM write_gate_kanban_approvals"
            ).fetchone()[0],
        )


@pytest.mark.parametrize(
    "operation",
    (
        "kanban_create",
        "kanban_complete",
        "kanban_block",
        "kanban_unblock",
        "kanban_comment",
        "kanban_heartbeat",
        "kanban_attach",
        "kanban_attach_url",
        "kanban_request_changes",
        "kanban_request_review",
    ),
)
def test_every_task_mutation_rejects_an_initiative_target_without_state_change(
    commands_module, tmp_path, monkeypatch, operation
):
    path, _provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_both_object_types(path)
    before = _counts(path)

    with sqlite3.connect(path) as conn, pytest.raises(
        commands_module.CommandRejected
    ) as rejected:
        conn.row_factory = sqlite3.Row
        commands_module._preflight_object_type_operation_boundaries(
            conn,
            operation,
            "orchestrator",
            {"task_id": "initiative-1"},
        )

    check = rejected.value.failed_checks[0]
    assert check.code == "OBJECT_TYPE_OPERATION_MISMATCH"
    assert check.target == "task_id"
    assert check.retry == "return_route"
    if operation == "kanban_create":
        assert check.accepted_format == "kanban_create_initiative"
    elif operation == "kanban_complete":
        assert check.accepted_format == "kanban_close_initiative"
    else:
        assert "kanban_update_initiative" in check.remediation
        assert "kanban_transition_initiative" in check.remediation
        assert "kanban_close_initiative" in check.remediation
    assert _counts(path) == before


@pytest.mark.parametrize(
    "operation",
    (
        "kanban_create_initiative",
        "kanban_update_initiative",
        "kanban_transition_initiative",
        "kanban_close_initiative",
    ),
)
def test_every_initiative_mutation_rejects_a_task_target_without_approval(
    commands_module, tmp_path, monkeypatch, operation
):
    path, _provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_both_object_types(path)
    before = _counts(path)

    with sqlite3.connect(path) as conn, pytest.raises(
        commands_module.CommandRejected
    ) as rejected:
        conn.row_factory = sqlite3.Row
        commands_module._preflight_object_type_operation_boundaries(
            conn,
            operation,
            "orchestrator",
            {"initiative_id": "task-1"},
        )

    check = rejected.value.failed_checks[0]
    assert check.code == "OBJECT_TYPE_OPERATION_MISMATCH"
    assert check.target == "initiative_id"
    assert check.retry == "return_route"
    if operation == "kanban_create_initiative":
        assert check.accepted_format == "kanban_create"
    elif operation == "kanban_close_initiative":
        assert check.accepted_format == "kanban_complete"
    else:
        assert "kanban_complete" in check.remediation
        assert "kanban_comment" in check.remediation
    assert _counts(path) == before


@pytest.mark.parametrize("bad_endpoint", ("parent_id", "child_id"))
def test_task_link_rejects_an_initiative_at_either_endpoint_atomically(
    commands_module, tmp_path, monkeypatch, bad_endpoint
):
    path, _provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_both_object_types(path)
    payload = {"parent_id": "task-1", "child_id": "task-1"}
    payload[bad_endpoint] = "initiative-1"
    before = _counts(path)

    with sqlite3.connect(path) as conn, pytest.raises(
        commands_module.CommandRejected
    ) as rejected:
        conn.row_factory = sqlite3.Row
        commands_module._preflight_object_type_operation_boundaries(
            conn, "kanban_link", "orchestrator", payload
        )

    assert [check.target for check in rejected.value.failed_checks] == [bad_endpoint]
    assert _counts(path) == before


def test_correct_family_unknown_identity_and_reads_are_not_reclassified(
    commands_module, tmp_path, monkeypatch
):
    path, _provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_both_object_types(path)

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        for operation in commands_module.ORDINARY_TASK_OPERATIONS:
            payload = (
                {"parent_id": "task-1", "child_id": "unknown-task"}
                if operation == "kanban_link"
                else {"task_id": "task-1"}
            )
            commands_module._preflight_object_type_operation_boundaries(
                conn, operation, "orchestrator", payload
            )
        for operation in commands_module.INITIATIVE_OPERATIONS:
            commands_module._preflight_object_type_operation_boundaries(
                conn,
                operation,
                "orchestrator",
                {"initiative_id": "initiative-1"},
            )
        for operation in commands_module.READ_ONLY_OPERATIONS:
            commands_module._preflight_object_type_operation_boundaries(
                conn,
                operation,
                "orchestrator",
                {"task_id": "initiative-1"},
            )


def test_public_task_create_requires_and_exposes_lifecycle_contract(
    commands_module, tmp_path, monkeypatch
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_both_object_types(path)
    schema = commands_module.TOOL_SCHEMAS["kanban_create"]["parameters"]
    assert "lifecycle_contract_v1" in schema["required"]
    assert set(schema["properties"]["lifecycle_contract_v1"]["required"]) == {
        "version",
        "step",
        "baseline_refs",
        "governing_source_refs",
        "prior_record_refs",
    }
    boundary = commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={"kanban_create": commands_module._handle_create},
    )
    normalizer = commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda operation, args, runtime: (
            "orchestrator",
            None,
            "default",
        ),
    )
    before = _counts(path)

    result = normalizer.submit(
        "kanban_create",
        {
            "task_id": "task-new",
            "initiative_id": "initiative-1",
            "title": "New lifecycle task",
            "assignee": "builder-tester",
            "idempotency_key": "create-missing-contract",
        },
        {"session_id": "session-1", "turn_id": "turn-1"},
    )

    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    assert result["failed_checks"][0]["code"] == "LIFECYCLE_CONTRACT_REQUIRED"
    assert "kanban_create_initiative" in result["failed_checks"][0]["remediation"]
    assert _counts(path) == before


@pytest.mark.parametrize(
    ("operation", "args", "expected_route"),
    (
        (
            "kanban_complete",
            {
                "task_id": "initiative-1",
                "idempotency_key": "wrong-task-family",
            },
            "kanban_close_initiative",
        ),
        (
            "kanban_create_initiative",
            {
                "initiative_id": "task-1",
                "title": "Wrong family",
                "body": _BODY,
                "idempotency_key": "wrong-initiative-family",
            },
            "kanban_create",
        ),
    ),
)
def test_public_normalizer_returns_actionable_route_before_approval_or_mutation(
    commands_module,
    tmp_path,
    monkeypatch,
    operation,
    args,
    expected_route,
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_both_object_types(path)
    boundary = commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={},
    )
    normalizer = commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda operation, args, runtime: (
            "orchestrator",
            None,
            "default",
        ),
    )
    before = _counts(path)

    result = normalizer.submit(
        operation,
        args,
        {"session_id": "session-route", "turn_id": "turn-route"},
    )

    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    assert result["failed_checks"][0]["code"] == (
        "OBJECT_TYPE_OPERATION_MISMATCH"
    )
    assert expected_route in result["failed_checks"][0]["remediation"]
    assert result["failed_checks"][0]["retry"] == "return_route"
    assert _counts(path) == before


def _public_create_normalizer(commands_module, path, provider):
    boundary = commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={
            "kanban_create_initiative": commands_module._handle_create_initiative,
        },
        state_resolver=lambda conn, operation, target, payload: 0,
        known_profiles={"default"},
    )
    return commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda operation, args, runtime: (
            "orchestrator",
            None,
            "default",
        ),
    )


def _create_args():
    return {
        "initiative_id": "initiative-public-create",
        "title": "Public initiative creation",
        "body": _BODY.replace("initiative-1", "initiative-public-create"),
        "idempotency_key": "public-create-1",
    }


def _runtime():
    return {
        "session_id": "session-public-create",
        "turn_id": "turn-public-create",
        "api_request_id": "api-public-create",
        "user_task": "Create this standalone initiative after approval.",
    }


@pytest.fixture
def public_create(commands_module, tmp_path, monkeypatch):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    approval_module = importlib.import_module(
        f"{commands_module.__package__}.initiative_mutation_approval"
    )
    monkeypatch.setattr(
        approval_module,
        "mint_current_tailscale_authorizer",
        lambda *, request_id, issued_at, ttl_seconds: evidence(request_id, issued_at),
    )
    return path, _public_create_normalizer(commands_module, path, provider), approval_module


def test_public_initiative_create_prepares_approves_consumes_and_replays(
    public_create, monkeypatch
):
    path, normalizer, approval_module = public_create
    presentations = []

    def approve(**request):
        presentations.append(request)
        return {
            "approved": True,
            "decision": "once",
            "decision_at": "2026-09-17T05:00:00Z",
            "approval_reference": "desktop-create-approval",
        }

    monkeypatch.setattr(approval_module, "request_write_gate_approval", approve)
    first = normalizer.submit("kanban_create_initiative", _create_args(), _runtime())
    replay = normalizer.submit("kanban_create_initiative", _create_args(), _runtime())

    assert first["result"] == "ACCEPTED", first
    assert replay == first
    assert len(presentations) == 1
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        card = conn.execute(
            "SELECT card_type, task_id, record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = 'initiative-public-create'"
        ).fetchone()
        approval = conn.execute(
            "SELECT operation, initiative_id, proposed_creation_id, "
            "expected_version, state FROM write_gate_kanban_approvals"
        ).fetchone()
    assert tuple(card) == ("initiative", None, 0)
    assert approval["operation"] == "kanban_create_initiative"
    assert approval["initiative_id"] is None
    assert approval["proposed_creation_id"] == "initiative-public-create"
    assert approval["expected_version"] == 0
    assert approval["state"] == "consumed"


def test_denied_public_initiative_create_leaves_no_card(public_create, monkeypatch):
    path, normalizer, approval_module = public_create
    monkeypatch.setattr(
        approval_module,
        "request_write_gate_approval",
        lambda **request: {
            "approved": False,
            "decision": "deny",
            "decision_at": "2026-09-17T05:00:00Z",
        },
    )

    result = normalizer.submit("kanban_create_initiative", _create_args(), _runtime())

    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_cards"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "cancelled"
