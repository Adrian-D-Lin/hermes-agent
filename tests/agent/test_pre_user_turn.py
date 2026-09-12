"""Behavioural tests for the generic pre-user-turn admission seam."""

import sys
from logging.handlers import RotatingFileHandler
from types import ModuleType, SimpleNamespace

import pytest

from agent.turn_admission import resolve_pre_user_turn


def _agent(**overrides):
    values = {
        "session_id": "session-1",
        "model": "model-1",
        "provider": "provider-1",
        "base_url": "http://model.invalid",
        "platform": "desktop",
        "_parent_session_id": "parent-1",
        "_user_id": "user-1",
        "_pending_cli_user_message": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _resolve(agent, history=None, *, persist=None):
    return resolve_pre_user_turn(
        agent,
        "opening prompt",
        history,
        "task-1",
        persist,
        agent.model,
        agent.platform,
    )


def test_no_hook_and_allow_only_results_continue(monkeypatch):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: False)
    assert _resolve(_agent()) is None

    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: [None, {"action": "allow"}],
    )
    assert _resolve(_agent()) is None


def test_respond_intercepts_without_mutating_live_history(monkeypatch):
    history = [{"role": "assistant", "content": {"nested": ["original"]}}]

    def invoke(_name, **payload):
        payload["conversation_history"][0]["content"]["nested"].append("hook")
        return [{"action": "respond", "response": "Choose a project."}]

    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke)
    result = _resolve(_agent(), history)

    assert result["action"] == "respond"
    assert result["result"]["final_response"] == "Choose a project."
    assert result["result"]["api_calls"] == 0
    assert result["result"]["completed"] is True
    assert result["result"]["failed"] is False
    assert history == [{"role": "assistant", "content": {"nested": ["original"]}}]


def test_rewrite_preserves_separate_model_and_persisted_messages(monkeypatch):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: [
            {
                "action": "rewrite",
                "model_message": "anchor\n\nopening prompt",
                "persist_message": "opening prompt",
            }
        ],
    )

    assert _resolve(_agent()) == {
        "action": "rewrite",
        "model_message": "anchor\n\nopening prompt",
        "persist_message": "opening prompt",
    }


@pytest.mark.parametrize(
    "directives",
    [
        ["not a directive"],
        [{"action": "respond", "response": "  "}],
        [{"action": "rewrite", "model_message": "expanded"}],
        [{"action": "unknown"}],
        [
            {"action": "respond", "response": "one"},
            {"action": "rewrite", "model_message": "two", "persist_message": "two"},
        ],
    ],
)
def test_malformed_or_conflicting_results_fail_closed(monkeypatch, directives):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: directives
    )

    result = _resolve(_agent())
    assert result["action"] == "fail"
    assert result["result"]["api_calls"] == 0
    assert result["result"]["completed"] is False
    assert result["result"]["failed"] is True
    assert result["result"]["final_response"].strip()


def test_reserved_fail_closed_directive_preserves_actionable_message(monkeypatch):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: [
            {"action": "fail_closed", "response": "Startup policy timed out."}
        ],
    )

    result = _resolve(_agent())
    assert result["action"] == "fail"
    assert result["result"]["final_response"] == "Startup policy timed out."
    assert result["result"]["failed"] is True


@pytest.mark.parametrize("failure_site", ["inspection", "invocation"])
def test_lifecycle_failures_are_fail_closed(monkeypatch, failure_site):
    def raises(*_args, **_kwargs):
        raise RuntimeError("boom")

    if failure_site == "inspection":
        monkeypatch.setattr("hermes_cli.lifecycle.has_hook", raises)
    else:
        monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", raises)

    result = _resolve(_agent())
    assert result["action"] == "fail"
    assert result["result"]["api_calls"] == 0
    assert result["result"]["failed"] is True


def test_interception_clears_only_matching_staged_cli_input(monkeypatch):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: [{"action": "respond", "response": "Select."}],
    )
    matching = _agent(_pending_cli_user_message={"content": "clean prompt"})
    _resolve(matching, persist="clean prompt")
    assert matching._pending_cli_user_message is None

    different = _agent(_pending_cli_user_message={"content": "next prompt"})
    _resolve(different, persist="clean prompt")
    assert different._pending_cli_user_message == {"content": "next prompt"}


def test_conversation_loop_responds_before_turn_context(monkeypatch):
    concurrent_logging = ModuleType("concurrent_log_handler")
    concurrent_logging.ConcurrentRotatingFileHandler = RotatingFileHandler
    monkeypatch.setitem(sys.modules, "concurrent_log_handler", concurrent_logging)
    from agent import conversation_loop

    agent = _agent(_pending_cli_user_message={"content": "opening prompt"})
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: [
            {"action": "respond", "response": "1. Project Alpha"}
        ],
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("turn setup must not run for an intercepted response")

    monkeypatch.setattr(conversation_loop, "build_turn_context", forbidden)
    result = conversation_loop.run_conversation(agent, "opening prompt")

    assert result["final_response"] == "1. Project Alpha"
    assert result["api_calls"] == 0
    assert agent._pending_cli_user_message is None
