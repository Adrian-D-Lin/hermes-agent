"""The existing public task-completion tool hosts the two-step task override."""

from __future__ import annotations

import sqlite3

import pytest

from tests.test_adrian_kanban_s3_initiatives import commands_module  # noqa: F401
from tests.test_adrian_kanban_s3_override_approval import evidence
from tests.test_adrian_kanban_s3_task_override_derivation import task_override_case
from writegate.kanban_approvals import KanbanInitiativeApprovalHost


def _normalizer(commands_module, path, provider):
    boundary = commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={"kanban_complete": commands_module._handle_complete},
        state_resolver=lambda conn, operation, target, payload: conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE task_id=? AND board_slug=?",
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
        "task_id": "task-parent",
        "gate_override": {
            "to_status": "done",
            "override_reason": "Adrian explicitly directed this task movement.",
        },
        "idempotency_key": "public-task-override-1",
        "attempt_id": "public-task-attempt-1",
    }
    args.update(changes)
    return args


def _runtime(**changes):
    runtime = {
        "session_id": "session-default",
        "turn_id": "task-override-turn-1",
        "api_request_id": "api-request-task-1",
        "user_task": (
            "Prepare an exact override moving task-parent to done and wait for "
            "my approval before moving it."
        ),
        "profile": "default",
    }
    runtime.update(changes)
    return runtime


@pytest.fixture
def public_task_case(task_override_case, commands_module, monkeypatch):
    conn, _module, _initiative_id, _run_id = task_override_case
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    provider_module = __import__(
        f"{commands_module.__package__}.provider", fromlist=["provider"]
    )
    provider = provider_module.AdrianKanbanAuthorityProvider(path)
    provider_module.register_provider(provider)
    monkeypatch.setattr(
        commands_module,
        "mint_current_tailscale_authorizer",
        lambda *, request_id, issued_at, ttl_seconds: evidence(request_id, issued_at),
    )
    return path, _normalizer(commands_module, path, provider), commands_module, monkeypatch


def test_public_surface_remains_18_operations_with_one_task_override_variant(
    commands_module,
):
    assert len(commands_module.TOOL_SCHEMAS) == 18
    schema = commands_module.TOOL_SCHEMAS["kanban_complete"]["parameters"]
    assert schema["properties"]["gate_override"]["additionalProperties"] is False
    assert schema["properties"]["gate_override"]["required"] == [
        "to_status",
        "override_reason",
    ]
    assert "oneOf" in schema


def test_authenticated_task_prepare_approve_rederive_execute_is_one_public_call(
    public_task_case,
):
    path, normalizer, commands_module, monkeypatch = public_task_case
    presentations = []

    def approve(database_path, *, initial_authorizer, prepared, session_key, now):
        assert database_path == str(path)
        assert session_key == "session-default"
        presentations.append(prepared["canonical_digest"])
        with sqlite3.connect(path) as approval_conn:
            approval_conn.row_factory = sqlite3.Row
            approval_conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                approval_conn,
                prepared["approval_id"],
                evidence("task-approval-click-1", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact task gate override.",
                now=now,
            )
            approval_conn.commit()
        return {"approved": True}

    monkeypatch.setattr(commands_module, "present_and_record_override_approval", approve)

    result = normalizer.submit("kanban_complete", _args(), _runtime())

    assert result["result"] == "ACCEPTED", result
    assert result["value"]["result"] == "accepted_with_active_decision"
    assert result["value"]["from_status"] == "running"
    assert result["value"]["to_status"] == "done"
    assert len(presentations) == 1
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT status FROM tasks WHERE id='task-parent'"
        ).fetchone()[0] == "done"
        assert check.execute(
            "SELECT COUNT(*) FROM task_gate_override_records"
        ).fetchone()[0] == 1
        assert check.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "consumed"


def test_task_override_denial_cancels_without_moving_task(public_task_case):
    path, normalizer, commands_module, monkeypatch = public_task_case

    def deny(database_path, *, initial_authorizer, prepared, session_key, now):
        with sqlite3.connect(path) as approval_conn:
            approval_conn.row_factory = sqlite3.Row
            approval_conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).cancel(
                approval_conn, prepared["approval_id"], "user denied", now=now
            )
            approval_conn.commit()
        return {"approved": False}

    monkeypatch.setattr(commands_module, "present_and_record_override_approval", deny)

    result = normalizer.submit("kanban_complete", _args(), _runtime())

    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT status FROM tasks WHERE id='task-parent'"
        ).fetchone()[0] == "running"
        assert check.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "cancelled"
        assert check.execute(
            "SELECT COUNT(*) FROM task_gate_override_records"
        ).fetchone()[0] == 0


def test_exact_task_replay_returns_saved_execution_without_presenting_again(
    public_task_case,
):
    path, normalizer, commands_module, monkeypatch = public_task_case
    calls = []

    def approve(database_path, *, initial_authorizer, prepared, session_key, now):
        calls.append(prepared["request_id"])
        with sqlite3.connect(path) as approval_conn:
            approval_conn.row_factory = sqlite3.Row
            approval_conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                approval_conn,
                prepared["approval_id"],
                evidence("task-approval-click-1", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact task gate override.",
                now=now,
            )
            approval_conn.commit()
        return {"approved": True}

    monkeypatch.setattr(commands_module, "present_and_record_override_approval", approve)
    first = normalizer.submit("kanban_complete", _args(), _runtime())
    second = normalizer.submit("kanban_complete", _args(), _runtime())

    assert second == first
    assert calls == ["kanban-task-gate-override:task-override-turn-1"]
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT COUNT(*) FROM task_gate_override_records"
        ).fetchone()[0] == 1
        assert check.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "consumed"


def test_approved_task_interruption_resumes_without_second_prompt(public_task_case):
    path, normalizer, commands_module, monkeypatch = public_task_case
    calls = []

    def approve_then_interrupt(
        database_path, *, initial_authorizer, prepared, session_key, now
    ):
        calls.append(prepared["request_id"])
        with sqlite3.connect(path) as approval_conn:
            approval_conn.row_factory = sqlite3.Row
            approval_conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                approval_conn,
                prepared["approval_id"],
                evidence("task-approval-click-1", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact task gate override.",
                now=now,
            )
            approval_conn.commit()
        raise RuntimeError("simulated interruption after durable task approval")

    monkeypatch.setattr(
        commands_module,
        "present_and_record_override_approval",
        approve_then_interrupt,
    )
    interrupted = normalizer.submit("kanban_complete", _args(), _runtime())
    assert interrupted["result"] == "REJECTED"
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "approved"
        assert check.execute(
            "SELECT COUNT(*) FROM task_gate_override_records"
        ).fetchone()[0] == 0

    monkeypatch.setattr(
        commands_module,
        "present_and_record_override_approval",
        lambda *args, **kwargs: pytest.fail("approved replay must not prompt again"),
    )
    resumed = normalizer.submit("kanban_complete", _args(), _runtime())

    assert resumed["result"] == "ACCEPTED", resumed
    assert calls == ["kanban-task-gate-override:task-override-turn-1"]
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "consumed"
        assert check.execute(
            "SELECT COUNT(*) FROM task_gate_override_records"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "args,runtime",
    [
        (_args(summary="ordinary completion must not mix"), _runtime()),
        (_args(), _runtime(turn_id="")),
        (_args(), _runtime(user_task="")),
        (_args(), _runtime(profile="builder-tester")),
    ],
)
def test_untrusted_or_mixed_task_override_shape_creates_no_proposal(
    public_task_case, args, runtime
):
    path, normalizer, _commands_module, _monkeypatch = public_task_case

    result = normalizer.submit("kanban_complete", args, runtime)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT COUNT(*) FROM gate_override_proposals"
        ).fetchone()[0] == 0
        assert check.execute(
            "SELECT COUNT(*) FROM write_gate_kanban_approvals"
        ).fetchone()[0] == 0
