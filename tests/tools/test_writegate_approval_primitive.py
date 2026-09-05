"""Tests for the Write-Gate approval primitive in ``tools.approval``.

Covers ``request_write_gate_approval`` — the host-owned, ``once``-only
approval primitive the ``writegate`` plugin uses to authorize a bounded
out-of-worktree / protected-location write.

A ``once`` decision mints one bounded 5-minute lease; the originally blocked
operation may retry under the same exact session/worktree/scope lease until
expiry.  The primitive routes through the **currently active** approval
surface:

  * Headless / single-query / cron / unattended → deny immediately.
  * Selected non-builtin transport → ``_present_with_selected_transport``.
  * Gateway / TUI / desktop → notify callback + ``_await_gateway_decision``.
  * Interactive CLI → ``prompt_dangerous_approval``.

It never consults yolo / session / permanent caches and never persists.  It
returns a structured reference ``{"approved", "decision",
"approval_reference", "decision_at"}`` — ``approved`` is ``True`` only on an
explicit ``once`` decision.
"""

from __future__ import annotations

import pytest

import tools.approval as approval_mod


# ── Headless / no-human fast path ────────────────────────────────────────────

def test_headless_single_query_denies_immediately(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: True
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: False
    )
    result = approval_mod.request_write_gate_approval(
        request_id="wg-1", command="write_gate_exception",
        description="Write-Gate exception",
    )
    assert result["approved"] is False
    assert result["decision"] == "deny"
    assert result["approval_reference"] == "wg-1"
    assert result["decision_at"] == ""


def test_cron_context_denies(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_cron_approval_context", lambda: True
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: False
    )
    result = approval_mod.request_write_gate_approval(
        request_id="wg-1", command="write_gate_exception",
        description="Write-Gate exception",
    )
    assert result["approved"] is False


def test_unattended_platform_denies(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: True
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: False
    )
    result = approval_mod.request_write_gate_approval(
        request_id="wg-1", command="write_gate_exception",
        description="Write-Gate exception",
    )
    assert result["approved"] is False


# ── Interactive CLI path ─────────────────────────────────────────────────────

def test_cli_once_approved(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_cron_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: True
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_present_with_selected_transport",
        lambda **kw: {"selected": False},
    )

    def _stub(command, description, **kwargs):
        assert kwargs["allow_permanent"] is False
        assert kwargs["allow_session"] is False
        return "once"

    monkeypatch.setattr(
        approval_mod, "prompt_dangerous_approval", _stub
    )
    result = approval_mod.request_write_gate_approval(
        request_id="wg-1", command="write_gate_exception",
        description="Write-Gate exception",
    )
    assert result["approved"] is True
    assert result["decision"] == "once"
    assert result["approval_reference"] == "wg-1"
    assert result["decision_at"]  # non-empty host decision timestamp


def test_cli_denied(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_cron_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: True
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_present_with_selected_transport",
        lambda **kw: {"selected": False},
    )
    monkeypatch.setattr(
        approval_mod, "prompt_dangerous_approval",
        lambda *a, **k: "deny"
    )
    result = approval_mod.request_write_gate_approval(
        request_id="wg-1", command="write_gate_exception",
        description="Write-Gate exception",
    )
    assert result["approved"] is False
    assert result["decision"] == "deny"


def test_cli_timeout_fails_closed(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_cron_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: True
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_present_with_selected_transport",
        lambda **kw: {"selected": False},
    )
    monkeypatch.setattr(
        approval_mod, "prompt_dangerous_approval",
        lambda *a, **k: "timeout"
    )
    result = approval_mod.request_write_gate_approval(
        request_id="wg-1", command="write_gate_exception",
        description="Write-Gate exception",
    )
    assert result["approved"] is False


def test_cli_transport_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_cron_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: True
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_present_with_selected_transport",
        lambda **kw: {"selected": False},
    )

    def _boom(*a, **k):
        raise RuntimeError("surface unavailable")

    monkeypatch.setattr(approval_mod, "prompt_dangerous_approval", _boom)
    result = approval_mod.request_write_gate_approval(
        request_id="wg-1", command="write_gate_exception",
        description="Write-Gate exception",
    )
    assert result["approved"] is False


# ── Gateway path ─────────────────────────────────────────────────────────────

def test_gateway_once_approved(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_cron_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: True
    )
    monkeypatch.setattr(
        approval_mod, "_present_with_selected_transport",
        lambda **kw: {"selected": False},
    )

    captured = {}

    def _notify_cb(data):
        captured["data"] = data

    def _await(session_key, notify_cb, approval_data, surface="gateway"):
        captured["awaited"] = approval_data
        return {"resolved": True, "choice": "once"}

    monkeypatch.setattr(
        approval_mod, "_await_gateway_decision", _await
    )

    with monkeypatch.context() as m:
        import threading
        m.setitem(approval_mod._gateway_notify_cbs, "sess-1", _notify_cb)
        result = approval_mod.request_write_gate_approval(
            request_id="wg-1", command="write_gate_exception",
            description="Write-Gate exception", session_key="sess-1",
        )

    assert result["approved"] is True
    assert result["decision"] == "once"
    # The approval data must not offer session/permanent grants.
    assert captured["awaited"]["allow_permanent"] is False
    assert captured["awaited"]["allow_session"] is False


def test_gateway_denied(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_cron_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: True
    )
    monkeypatch.setattr(
        approval_mod, "_present_with_selected_transport",
        lambda **kw: {"selected": False},
    )

    def _await(session_key, notify_cb, approval_data, surface="gateway"):
        return {"resolved": True, "choice": "deny"}

    monkeypatch.setattr(
        approval_mod, "_await_gateway_decision", _await
    )

    with monkeypatch.context() as m:
        m.setitem(approval_mod._gateway_notify_cbs, "sess-1", lambda d: None)
        result = approval_mod.request_write_gate_approval(
            request_id="wg-1", command="write_gate_exception",
            description="Write-Gate exception", session_key="sess-1",
        )
    assert result["approved"] is False


def test_gateway_notify_failed_denies(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_cron_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: True
    )
    monkeypatch.setattr(
        approval_mod, "_present_with_selected_transport",
        lambda **kw: {"selected": False},
    )

    def _await(session_key, notify_cb, approval_data, surface="gateway"):
        return {"resolved": False, "choice": None, "notify_failed": True}

    monkeypatch.setattr(
        approval_mod, "_await_gateway_decision", _await
    )

    with monkeypatch.context() as m:
        m.setitem(approval_mod._gateway_notify_cbs, "sess-1", lambda d: None)
        result = approval_mod.request_write_gate_approval(
            request_id="wg-1", command="write_gate_exception",
            description="Write-Gate exception", session_key="sess-1",
        )
    assert result["approved"] is False


# ── Selected transport path ──────────────────────────────────────────────────

def test_selected_transport_once_approved(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_cron_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: False
    )

    def _present(**kw):
        assert kw["allow_session"] is False
        assert kw["allow_permanent"] is False
        return {"selected": True, "choice": "once", "failure": None}

    monkeypatch.setattr(approval_mod, "_present_with_selected_transport", _present)
    result = approval_mod.request_write_gate_approval(
        request_id="wg-1", command="write_gate_exception",
        description="Write-Gate exception",
    )
    assert result["approved"] is True
    assert result["decision"] == "once"


def test_selected_transport_failure_denies(monkeypatch):
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_cron_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_interactive_cli", lambda: False
    )
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: False
    )

    def _present(**kw):
        return {"selected": True, "choice": None, "failure": "error"}

    monkeypatch.setattr(approval_mod, "_present_with_selected_transport", _present)
    result = approval_mod.request_write_gate_approval(
        request_id="wg-1", command="write_gate_exception",
        description="Write-Gate exception",
    )
    assert result["approved"] is False
