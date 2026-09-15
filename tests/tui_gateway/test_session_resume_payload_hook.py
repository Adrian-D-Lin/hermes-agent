"""Contract tests for generic reconnect notices supplied by Python plugins."""

import threading
import time
from unittest.mock import MagicMock, patch

import hermes_cli.plugins as plugins_module
import pytest
from hermes_cli.plugins import SHELL_UNSUPPORTED_HOOKS, VALID_HOOKS, PluginManager
from hermes_cli.plugins_dispatch import _HOOK_TIMEOUT_BOUNDED_HOOKS


@pytest.fixture()
def server():
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import importlib

        mod = importlib.import_module("tui_gateway.server")
    methods = dict(mod._methods)
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)


def test_session_resume_payload_is_registered_python_only_bounded_hook():
    assert "session_resume_payload" in VALID_HOOKS
    assert "session_resume_payload" in SHELL_UNSUPPORTED_HOOKS
    assert "session_resume_payload" in _HOOK_TIMEOUT_BOUNDED_HOOKS


def test_session_resume_payload_timeout_fails_open_without_refire(monkeypatch):
    monkeypatch.setattr(plugins_module, "_resolve_hook_callback_timeout", lambda _name=None: 0.02)
    hold = threading.Event()
    starts = []

    def hung_observer(**_kwargs):
        starts.append(1)
        hold.wait(timeout=10)
        return {"id": "late", "text": "too late"}

    manager = PluginManager()
    manager._hooks["session_resume_payload"] = [hung_observer]

    started = time.monotonic()
    first = manager.invoke_hook("session_resume_payload", session_id="stored")
    second = manager.invoke_hook("session_resume_payload", session_id="stored")
    elapsed = time.monotonic() - started
    hold.set()

    assert first == []
    assert second == []
    assert starts == [1]
    assert elapsed < 1


def _install_fake_rpc(server, monkeypatch, method, result, events):
    contract = object()
    monkeypatch.setitem(server._contracts.METHODS, method, contract)
    monkeypatch.setitem(server._methods, method, lambda rid, params: server._ok(rid, dict(result)))
    monkeypatch.setattr(server._contracts, "validate_params", lambda active, params: (params, None))
    monkeypatch.setattr(server._contracts, "check_params_accepted", lambda active, params: None)
    monkeypatch.setattr(server._contracts, "check_result", lambda active, value: events.append("validated"))


def test_resume_enriches_after_validation_with_strict_ordered_notices(server, monkeypatch):
    events = []
    _install_fake_rpc(
        server,
        monkeypatch,
        "session.resume",
        {"session_key": "  canonical-session  ", "resumed": "fallback"},
        events,
    )

    def invoke(name, **kwargs):
        events.append((name, kwargs["session_id"], kwargs["method"], kwargs["result"]["session_key"]))
        return [
            {"id": " first ", "text": " Alpha "},
            {"id": "first", "text": "duplicate"},
            {"id": 2, "text": "wrong id type"},
            {"id": "wrong-text", "text": 3},
            {"id": "blank", "text": "  "},
            "not a dict",
            {"id": "second", "text": "Beta"},
        ]

    monkeypatch.setattr(plugins_module, "invoke_hook", invoke)
    response = server.handle_request(
        {"id": "rpc", "method": "session.resume", "params": {"session_id": "request-fallback"}}
    )

    assert events == [
        "validated",
        ("session_resume_payload", "canonical-session", "session.resume", "  canonical-session  "),
    ]
    assert response["result"]["resume_notices"] == [
        {"id": "first", "text": "Alpha"},
        {"id": "second", "text": "Beta"},
    ]


def test_activate_uses_resumed_then_request_identity(server, monkeypatch):
    seen = []

    def invoke(_name, **kwargs):
        seen.append(kwargs["session_id"])
        return [{"id": "notice", "text": "Resume setup"}]

    monkeypatch.setattr(plugins_module, "invoke_hook", invoke)

    events = []
    _install_fake_rpc(server, monkeypatch, "session.activate", {"resumed": " stored-tip "}, events)
    first = server.handle_request(
        {"id": 1, "method": "session.activate", "params": {"session_id": "requested"}}
    )
    assert first["result"]["resume_notices"] == [{"id": "notice", "text": "Resume setup"}]

    _install_fake_rpc(server, monkeypatch, "session.activate", {"unchanged": True}, events)
    second = server.handle_request(
        {"id": 2, "method": "session.activate", "params": {"session_id": " requested "}}
    )
    assert second["result"]["resume_notices"] == [{"id": "notice", "text": "Resume setup"}]
    assert seen == ["stored-tip", "requested"]


def test_hook_failure_or_malformed_values_preserve_original_result(server, monkeypatch):
    original = {"session_key": "stored", "other": [1, 2]}
    events = []
    _install_fake_rpc(server, monkeypatch, "session.resume", original, events)

    monkeypatch.setattr(plugins_module, "invoke_hook", lambda *_args, **_kwargs: [{"id": "x"}])
    malformed = server.handle_request(
        {"id": 1, "method": "session.resume", "params": {"session_id": "requested"}}
    )
    assert malformed["result"] == original

    def boom(*_args, **_kwargs):
        raise RuntimeError("plugin unavailable")

    monkeypatch.setattr(plugins_module, "invoke_hook", boom)
    failed = server.handle_request(
        {"id": 2, "method": "session.resume", "params": {"session_id": "requested"}}
    )
    assert failed["result"] == original


def test_unrelated_rpc_is_structurally_unchanged(server, monkeypatch):
    events = []
    original = {"session_key": "stored", "other": True}
    _install_fake_rpc(server, monkeypatch, "unrelated.method", original, events)
    calls = []
    monkeypatch.setattr(plugins_module, "invoke_hook", lambda *_args, **_kwargs: calls.append(1))

    response = server.handle_request({"id": 1, "method": "unrelated.method", "params": {}})

    assert response == {"jsonrpc": "2.0", "id": 1, "result": original}
    assert calls == []
