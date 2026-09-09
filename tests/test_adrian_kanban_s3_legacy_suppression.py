"""Legacy native automation is inert under Adrian Kanban authority."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose, kanban_specify, kanban_swarm, kanban_transfer
from tools import kanban_tools


_UNSUPPORTED = "not a recognized Kanban mutation"


def _select_plugin(monkeypatch) -> None:
    monkeypatch.setattr(
        kb, "resolve_selected_authority", lambda: kb.AUTHORITY_ADRIAN_KANBAN
    )


def test_native_mutation_authority_guard_allows_native_and_fails_closed_on_resolution(
    monkeypatch,
):
    monkeypatch.setattr(
        kb, "resolve_selected_authority", lambda: kb.AUTHORITY_NATIVE
    )
    assert kb.require_native_mutation_authority("legacy_mutation") is None

    monkeypatch.setattr(
        kb,
        "resolve_selected_authority",
        lambda: (_ for _ in ()).throw(RuntimeError("configuration unavailable")),
    )
    with pytest.raises(kb.AuthorityAdmissionRejected, match=_UNSUPPORTED):
        kb.require_native_mutation_authority("legacy_mutation")


@pytest.mark.parametrize(
    ("call", "outcome_type"),
    [
        (lambda: kanban_specify.specify_task("legacy-task"), kanban_specify.SpecifyOutcome),
        (
            lambda: kanban_decompose.decompose_task("legacy-task"),
            kanban_decompose.DecomposeOutcome,
        ),
    ],
)
def test_legacy_triage_automation_rejects_before_native_reads(
    monkeypatch, call, outcome_type
):
    _select_plugin(monkeypatch)
    monkeypatch.setattr(kb, "can_exit_triage", lambda: True)
    monkeypatch.setattr(
        kb,
        "connect_closing",
        lambda *args, **kwargs: pytest.fail("legacy automation opened native DB"),
    )

    outcome = call()

    assert type(outcome) is outcome_type
    assert outcome.ok is False
    assert _UNSUPPORTED in outcome.reason


def test_legacy_swarm_rejects_before_native_transaction(monkeypatch):
    _select_plugin(monkeypatch)
    monkeypatch.setattr(kb, "can_exit_triage", lambda: True)
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(kb.AuthorityAdmissionRejected, match=_UNSUPPORTED):
            kanban_swarm.create_swarm(
                conn,
                goal="legacy graph",
                workers=[],
                verifier_assignee="reviewer",
                synthesizer_assignee="default",
            )
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_board_import_rejects_before_reading_archive_but_export_remains_read_only(
    monkeypatch, tmp_path
):
    _select_plugin(monkeypatch)

    with pytest.raises(kb.AuthorityAdmissionRejected, match=_UNSUPPORTED):
        kanban_transfer.import_board(str(tmp_path / "missing.tar.gz"))

    monkeypatch.setattr(kb, "_normalize_board_slug", lambda value: "missing")
    monkeypatch.setattr(kb, "board_exists", lambda slug: False)
    with pytest.raises(ValueError, match="does not exist"):
        kanban_transfer.export_board("missing", str(tmp_path / "out.tar.gz"))


@pytest.mark.parametrize(
    ("handler", "operation"),
    [
        (kanban_tools._handle_specify, "kanban_specify"),
        (kanban_tools._handle_decompose, "kanban_decompose"),
    ],
)
def test_legacy_model_tools_reject_before_orchestrator_or_board_resolution(
    monkeypatch, handler, operation
):
    _select_plugin(monkeypatch)
    monkeypatch.setattr(
        kanban_tools,
        "_require_dispatcher_orchestrator",
        lambda *_args, **_kwargs: pytest.fail("legacy role guard was reached"),
    )
    monkeypatch.setattr(
        kanban_tools,
        "_connect",
        lambda *_args, **_kwargs: pytest.fail("legacy native board was opened"),
    )

    result = json.loads(handler({"task_id": "legacy-task"}))

    assert set(result) == {"error"}
    assert operation in result["error"]
    assert _UNSUPPORTED in result["error"]


@pytest.mark.parametrize(
    "method_name",
    ["_kanban_dispatcher_watcher", "_kanban_notifier_watcher"],
)
def test_gateway_native_watchers_exit_before_lock_poll_or_delivery(
    monkeypatch, method_name
):
    _select_plugin(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    monkeypatch.setattr(
        kanban_watchers,
        "_acquire_singleton_lock",
        lambda *_args, **_kwargs: pytest.fail("native dispatcher lock was attempted"),
    )

    async def forbidden_sleep(*_args, **_kwargs):
        pytest.fail("native watcher reached its polling loop")

    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", forbidden_sleep)
    runner = SimpleNamespace(
        _running=True,
        adapters={},
        _kanban_sub_fail_counts={},
        _kanban_dispatcher_lock_handle=None,
    )

    asyncio.run(getattr(kanban_watchers.GatewayKanbanWatchersMixin, method_name)(runner))
