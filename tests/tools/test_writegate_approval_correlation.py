"""Approval-correlation tests for the Write-Gate approval primitive.

Covers brief §9 suite 7 — the correlation contract, exercised through the real
gateway approval queue (the actual correlation mechanism, not a stub):

* **Wrong / absent request id fails closed.** ``resolve_gateway_approval``
  matches on the exact host-generated ``request_id``; a wrong id resolves
  nothing, so the typed request stays pending and the primitive denies.
* **Generic session/permanent approvals and resolve-all cannot authorize a
  Write-Gate request.** Only a ``once`` on the exact ``write_gate:<id>``
  pattern authorizes it; a generic session grant on a different pattern is
  isolated.
* **Reconnect replay retains the typed pending request** and only a matching
  ``once`` decision on the same request id authorizes it.
* **Gateway without a notify callback fails closed** — no invisible CLI fallback.

``request_write_gate_approval`` blocks the calling thread until the human
answer arrives (or the approval timeout elapses), so each request runs on a
worker thread. The test captures the host-generated ``ApprovalEntry`` request
id from the notify callback, proves a wrong/generic resolution does not
authorize, then resolves the exact pending request with ``deny`` or ``once``
so the thread joins. The durable ``approval_reference`` is the host-generated
entry id, never the tool-level ``wg-*`` label.
"""

from __future__ import annotations

import threading

import tools.approval as approval_mod


def _gate_all(monkeypatch, *, gateway=True):
    """Force the gateway approval surface and disable the CLI/selected paths."""
    monkeypatch.setattr(
        approval_mod, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setattr(approval_mod, "_is_cron_approval_context", lambda: False)
    monkeypatch.setattr(
        approval_mod, "_is_unattended_platform_approval_context", lambda: False)
    monkeypatch.setattr(approval_mod, "_is_interactive_cli", lambda: False)
    monkeypatch.setattr(
        approval_mod, "_is_gateway_approval_context", lambda: gateway)
    monkeypatch.setattr(
        approval_mod, "_present_with_selected_transport",
        lambda **kw: {"selected": False})


def _register_session(monkeypatch, session_key="sess-1"):
    """Register a gateway notify callback that captures the host request id.

    The gateway generates its own ``request_id`` on the pending
    ``ApprovalEntry`` (via ``setdefault(uuid4().hex)``). The notify callback
    receives that id in the approval data it is handed, so we capture it and
    resolve the request using the *host-generated* id, never the caller
    label.  Returns ``(captured_dict, notify_ready_event)``.
    """
    import contextlib
    captured = {}
    ready = threading.Event()

    def _notify_cb(approval_data):
        captured["request_id"] = approval_data.get("request_id")
        ready.set()

    @contextlib.contextmanager
    def _cm():
        with monkeypatch.context() as m:
            m.setitem(approval_mod._gateway_notify_cbs, session_key, _notify_cb)
            yield captured, ready
    return _cm()


def _run_request(session_key, request_id, command="write_gate_exception",
                 description="Write-Gate exception"):
    """Run a blocking approval request on a worker thread.

    Returns ``(thread, done_event, result_holder)``. ``result_holder`` is
    populated with the structured reference once the worker finishes.
    """
    result_holder = {}
    done = threading.Event()

    def _worker():
        result_holder["r"] = approval_mod.request_write_gate_approval(
            request_id=request_id, command=command,
            description=description, session_key=session_key,
        )
        done.set()

    t = threading.Thread(target=_worker)
    t.start()
    return t, done, result_holder


def _resolve(session_key, request_id, choice="once", resolve_all=False):
    """Resolve the pending gateway approval for a request id."""
    return approval_mod.resolve_gateway_approval(
        session_key, choice, resolve_all=resolve_all, request_id=request_id)


def test_wrong_request_id_denies(monkeypatch):
    """Resolving a *different* request id must not authorize the pending
    write_gate request — it stays pending and the primitive denies.

    We wait for the host to enqueue the typed request (notify fired), prove the
    wrong id resolves nothing (``== 0``), then resolve the real host id with
    ``deny`` so the worker joins with ``approved is False``.
    """
    _gate_all(monkeypatch)
    with _register_session(monkeypatch) as (captured, ready):
        t, done, holder = _run_request("sess-1", "wg-1")
        # Wait for the host to enqueue the typed request.
        assert ready.wait(timeout=5)
        # A wrong id resolves nothing — the typed request stays pending.
        assert _resolve("sess-1", "wg-WRONG") == 0
        # Resolve the real host id with deny -> the request is denied.
        _resolve("sess-1", captured["request_id"], "deny")
        assert done.wait(timeout=5)
        assert holder["r"]["approved"] is False


def test_absent_request_id_denies(monkeypatch):
    """resolve-all cannot satisfy a typed write_gate request."""
    _gate_all(monkeypatch)
    with _register_session(monkeypatch) as (captured, ready):
        t, done, holder = _run_request("sess-1", "wg-1")
        assert ready.wait(timeout=5)
        # /approve all resolves everything — but must NOT authorize the typed
        # write_gate request.
        _resolve("sess-1", "once", resolve_all=True)
        # The typed request is still pending; resolve it with deny to join.
        _resolve("sess-1", captured["request_id"], "deny")
        assert done.wait(timeout=5)
        assert holder["r"]["approved"] is False


def test_generic_session_approval_cannot_authorize(monkeypatch):
    """approve_session on a different pattern (a generic grant) must not
    authorize the typed write_gate request."""
    _gate_all(monkeypatch)
    with _register_session(monkeypatch) as (captured, ready):
        t, done, holder = _run_request("sess-1", "wg-1")
        assert ready.wait(timeout=5)
        # A generic session approval on an unrelated pattern.
        approval_mod.approve_session("sess-1", "some_other_command")
        # The generic grant did not authorize the typed request; resolve with
        # deny to join and confirm the denial.
        _resolve("sess-1", captured["request_id"], "deny")
        assert done.wait(timeout=5)
        assert holder["r"]["approved"] is False


def test_reconnect_replay_retains_typed_request(monkeypatch):
    """The typed pending request is retained in the queue and only a matching
    once decision on the same request id authorizes it."""
    _gate_all(monkeypatch)
    with _register_session(monkeypatch) as (captured, ready):
        t, done, holder = _run_request("sess-1", "wg-replay")
        assert ready.wait(timeout=5)
        # The request is replayable / still pending.
        pending = approval_mod.get_pending_gateway_approval("sess-1")
        assert pending is not None
        assert pending["pattern_key"] == "write_gate:wg-replay"
        # Resolve it with the correct host id + once.
        _resolve("sess-1", captured["request_id"], "once")
        assert done.wait(timeout=5)
        result = holder["r"]
    assert result["approved"] is True
    # The durable reference is the host-generated entry id, not the label.
    assert result["approval_reference"] == captured["request_id"]
    assert result["approval_reference"] != "wg-replay"


def test_gateway_without_notify_denies(monkeypatch):
    """A gateway process with no registered notify callback must fail closed —
    it must not fall through to an invisible CLI prompt."""
    # Gateway context (so the queue/notify path is taken) but no session has a
    # registered notify callback.
    _gate_all(monkeypatch, gateway=True)
    # Do NOT register a notify callback (no session registered).

    def _cli_reached(*a, **k):
        _cli_reached.reached = True
        return "once"
    _cli_reached.reached = False
    monkeypatch.setattr(approval_mod, "prompt_dangerous_approval", _cli_reached)

    # No session registered → no notify callback → the gateway path must fail
    # closed (deny) and must not reach the invisible CLI prompt.
    result = approval_mod.request_write_gate_approval(
        request_id="wg-1", command="write_gate_exception",
        description="Write-Gate exception", session_key="sess-nobody",
    )
    assert result["approved"] is False
    assert result["approval_reference"] == "wg-1"
    assert _cli_reached.reached is False


def test_approved_request_returns_host_reference(monkeypatch):
    """An approved request returns the structured host reference with a
    non-empty decision_at, suitable for lease creation."""
    _gate_all(monkeypatch)
    with _register_session(monkeypatch) as (captured, ready):
        t, done, holder = _run_request("sess-1", "wg-1")
        assert ready.wait(timeout=5)
        _resolve("sess-1", captured["request_id"], "once")
        assert done.wait(timeout=5)
        result = holder["r"]
    assert result["approved"] is True
    assert result["decision"] == "once"
    # Durable reference is the host-generated ApprovalEntry id, not wg-1.
    assert result["approval_reference"] == captured["request_id"]
    assert result["approval_reference"] != "wg-1"
    assert result["decision_at"]  # non-empty host decision timestamp
