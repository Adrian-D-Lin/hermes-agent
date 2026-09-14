"""Policy-boundary tests for the generic pre_user_turn plugin hook."""

import threading
import time

import hermes_cli.plugins as plugins_module
from hermes_cli.plugins import (
    SHELL_UNSUPPORTED_HOOKS,
    VALID_HOOKS,
    PluginManager,
)


def test_pre_user_turn_is_registered_python_only_policy_hook():
    assert "pre_user_turn" in VALID_HOOKS
    assert "pre_user_turn" in SHELL_UNSUPPORTED_HOOKS


def test_pre_user_turn_timeout_and_suppressed_refire_fail_closed(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins._resolve_hook_callback_timeout",
        lambda _hook_name=None: 0.05,
    )
    hold = threading.Event()
    starts = []

    def hung_policy(**_kwargs):
        starts.append(1)
        hold.wait(timeout=10.0)
        return None

    manager = PluginManager()
    manager._hooks["pre_user_turn"] = [hung_policy]

    started = time.monotonic()
    first = manager.invoke_hook("pre_user_turn", session_id="session-1")
    second = manager.invoke_hook("pre_user_turn", session_id="session-1")
    elapsed = time.monotonic() - started
    hold.set()

    assert len(starts) == 1
    assert elapsed < 1.0
    for result in (first, second):
        assert result == [
            {
                "action": "fail_closed",
                "response": "pre_user_turn plugin callback timed out or is still running",
            }
        ]


def test_pre_user_turn_exception_fails_closed_and_later_callback_runs(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins._resolve_hook_callback_timeout",
        lambda _hook_name=None: 1.0,
    )

    def boom(**_kwargs):
        raise RuntimeError("policy failure")

    manager = PluginManager()
    manager._hooks["pre_user_turn"] = [boom, lambda **_kwargs: {"action": "allow"}]

    assert manager.invoke_hook("pre_user_turn", session_id="session-1") == [
        {
            "action": "fail_closed",
            "response": "pre_user_turn plugin callback timed out or is still running",
        },
        {"action": "allow"},
    ]


def test_pre_tool_call_timeout_shape_is_unchanged(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins._resolve_hook_callback_timeout",
        lambda _hook_name=None: 0.05,
    )
    hold = threading.Event()

    def hung_policy(**_kwargs):
        hold.wait(timeout=10.0)
        return None

    manager = PluginManager()
    manager._hooks["pre_tool_call"] = [hung_policy]
    result = manager.invoke_hook("pre_tool_call", tool_name="terminal", args={})
    hold.set()

    assert result == [
        {
            "action": "block",
            "message": "pre_tool_call plugin callback timed out or is still running",
        }
    ]


def test_pre_user_timeout_exceeds_bounded_host_approval(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"plugins": {"hook_callback_timeout": 12}},
    )
    monkeypatch.setattr("tools.approval._get_approval_timeout", lambda: 90)

    assert plugins_module._resolve_hook_callback_timeout("post_tool_call") == 12
    assert plugins_module._resolve_hook_callback_timeout("pre_user_turn") == 120


def test_explicit_zero_disables_pre_user_timeout_wrapper(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"plugins": {"hook_callback_timeout": 0}},
    )
    monkeypatch.setattr("tools.approval._get_approval_timeout", lambda: 300)

    assert plugins_module._resolve_hook_callback_timeout("pre_user_turn") == 0


def test_pre_user_timeout_is_clamped_to_existing_maximum(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"plugins": {"hook_callback_timeout": 20}},
    )
    monkeypatch.setattr("tools.approval._get_approval_timeout", lambda: 700)

    assert plugins_module._resolve_hook_callback_timeout("pre_user_turn") == 600


def test_pre_user_callback_can_outlive_ordinary_hook_deadline(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins._resolve_hook_callback_timeout",
        lambda hook_name=None: 0.2 if hook_name == "pre_user_turn" else 0.05,
    )

    def bounded_human_wait(**_kwargs):
        time.sleep(0.08)
        return {"action": "allow"}

    manager = PluginManager()
    manager._hooks["pre_user_turn"] = [bounded_human_wait]

    assert manager.invoke_hook("pre_user_turn", session_id="session-1") == [
        {"action": "allow"}
    ]
