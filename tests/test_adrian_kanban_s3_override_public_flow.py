"""The existing public initiative transition tool hosts the two-step override."""

from __future__ import annotations

import sqlite3

import pytest

from tests.test_adrian_kanban_s3_initiatives import (
    _database,
    _seed_initiative,
    _seed_reconciliation,
    commands_module,  # noqa: F401
)
from tests.test_adrian_kanban_s3_override_approval import evidence
from writegate.kanban_approvals import KanbanInitiativeApprovalHost


def _normalizer(commands_module, path, provider):
    boundary = commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={
            "kanban_transition_initiative": commands_module._handle_transition_initiative
        },
        state_resolver=lambda conn, operation, target, payload: conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id=? AND board_slug=?",
            (target, payload["board"]),
        ).fetchone()[0],
        known_profiles={"default", "builder-tester"},
    )
    return commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda operation, args, runtime: (
            "orchestrator",
            None,
            runtime.get("profile", "default"),
        ),
    )


def _args(**changes):
    args = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV1",
        "reconciliation_ref": "reconciliation-1",
        "gate_override": {
            "override_reason": "Adrian explicitly directed this nonstandard route."
        },
        "idempotency_key": "public-override-1",
        "attempt_id": "public-attempt-1",
    }
    args.update(changes)
    return args


def _runtime(**changes):
    runtime = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "api_request_id": "api-request-1",
        "user_task": (
            "Prepare an exact override moving initiative-1 to DEV1 and wait for "
            "my approval before moving it."
        ),
        "profile": "default",
    }
    runtime.update(changes)
    return runtime


@pytest.fixture
def public_case(commands_module, tmp_path, monkeypatch):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    _seed_reconciliation(
        path,
        result_id="reconciliation-1",
        from_phase="D1",
        to_phase="DEV1",
    )
    monkeypatch.setattr(
        commands_module,
        "mint_current_tailscale_authorizer",
        lambda *, request_id, issued_at, ttl_seconds: evidence(request_id, issued_at),
    )
    return path, _normalizer(commands_module, path, provider), commands_module, monkeypatch


def test_public_surface_remains_18_operations_with_one_transition_variant(commands_module):
    assert len(commands_module.TOOL_SCHEMAS) == 18
    schema = commands_module.TOOL_SCHEMAS["kanban_transition_initiative"]["parameters"]
    assert "gate_override" in schema["properties"]
    assert schema["properties"]["gate_override"]["additionalProperties"] is False
    assert schema["properties"]["gate_override"]["required"] == ["override_reason"]
    assert "oneOf" in schema


def test_authenticated_prepare_approve_rederive_execute_is_one_public_call(public_case):
    path, normalizer, commands_module, monkeypatch = public_case
    presentations = []

    def approve(database_path, *, initial_authorizer, prepared, session_key, now):
        assert database_path == str(path)
        assert session_key == "session-1"
        presentations.append(prepared["canonical_digest"])
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                conn,
                prepared["approval_id"],
                evidence("approval-click-1", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact gate override.",
                now=now,
            )
            conn.commit()
        return {
            "approved": True,
            "approval_reference": "approval-click-1",
            "request_id": prepared["request_id"],
            "canonical_digest": prepared["canonical_digest"],
        }

    monkeypatch.setattr(commands_module, "present_and_record_override_approval", approve)

    result = normalizer.submit(
        "kanban_transition_initiative", _args(), _runtime()
    )

    assert result["result"] == "ACCEPTED", result
    assert result["value"]["result"] == "accepted_with_active_decision"
    assert result["value"]["from_phase"] == "D1"
    assert result["value"]["to_phase"] == "DEV1"
    assert len(presentations) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id='initiative-1'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 1
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "consumed"


def test_denial_cancels_preparation_without_moving_initiative(public_case):
    path, normalizer, commands_module, monkeypatch = public_case

    def deny(database_path, *, initial_authorizer, prepared, session_key, now):
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).cancel(
                conn, prepared["approval_id"], "user denied", now=now
            )
            conn.commit()
        return {
            "approved": False,
            "request_id": prepared["request_id"],
            "canonical_digest": prepared["canonical_digest"],
        }

    monkeypatch.setattr(commands_module, "present_and_record_override_approval", deny)

    result = normalizer.submit(
        "kanban_transition_initiative", _args(), _runtime()
    )

    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id='initiative-1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "cancelled"
        assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 0


def test_exact_replay_returns_saved_execution_without_presenting_again(public_case):
    path, normalizer, commands_module, monkeypatch = public_case
    calls = []

    def approve(database_path, *, initial_authorizer, prepared, session_key, now):
        calls.append(prepared["request_id"])
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                conn,
                prepared["approval_id"],
                evidence("approval-click-1", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact gate override.",
                now=now,
            )
            conn.commit()
        return {"approved": True}

    monkeypatch.setattr(commands_module, "present_and_record_override_approval", approve)
    first = normalizer.submit("kanban_transition_initiative", _args(), _runtime())
    second = normalizer.submit("kanban_transition_initiative", _args(), _runtime())

    assert second == first
    assert calls == ["kanban-gate-override:turn-1"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0] == 2


def test_approved_interruption_resumes_execution_without_second_prompt(public_case):
    path, normalizer, commands_module, monkeypatch = public_case
    calls = []

    def approve_then_interrupt(
        database_path, *, initial_authorizer, prepared, session_key, now
    ):
        calls.append(prepared["request_id"])
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                conn,
                prepared["approval_id"],
                evidence("approval-click-1", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact gate override.",
                now=now,
            )
            conn.commit()
        raise RuntimeError("simulated interruption after durable approval")

    monkeypatch.setattr(
        commands_module,
        "present_and_record_override_approval",
        approve_then_interrupt,
    )
    interrupted = normalizer.submit(
        "kanban_transition_initiative", _args(), _runtime()
    )
    assert interrupted["result"] == "REJECTED"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "approved"
        assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 0

    monkeypatch.setattr(
        commands_module,
        "present_and_record_override_approval",
        lambda *args, **kwargs: pytest.fail("approved replay must not prompt again"),
    )
    resumed = normalizer.submit("kanban_transition_initiative", _args(), _runtime())

    assert resumed["result"] == "ACCEPTED", resumed
    assert calls == ["kanban-gate-override:turn-1"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "consumed"
        assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 1


@pytest.mark.parametrize(
    "args,runtime",
    [
        (_args(phase_close_ref="close-1"), _runtime()),
        (_args(approval_id="approval-1"), _runtime()),
        (_args(), _runtime(turn_id="")),
        (_args(), _runtime(user_task="")),
        (_args(), _runtime(profile="builder-tester")),
    ],
)
def test_untrusted_or_mixed_override_shape_creates_no_proposal(
    public_case, args, runtime
):
    path, normalizer, _, _ = public_case

    result = normalizer.submit("kanban_transition_initiative", args, runtime)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM write_gate_kanban_approvals"
        ).fetchone()[0] == 0
