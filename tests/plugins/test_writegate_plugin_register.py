"""Tests for the write-gate plugin entrypoint registration seam.

Covers ``plugins/write-gate/__init__.py``:

  * ``register(ctx)`` is inert when ``security.write_gate.enabled`` is false.
  * ``register(ctx)`` registers the ``write_gate`` tool (in the ``write_gate``
    toolset) and the ``pre_tool_call`` enforcement hook when enabled.
"""

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "write-gate"


class _Ctx:
    """Minimal stand-in for the plugin loader's ctx object."""

    def __init__(self):
        self.tools = {}
        self.hooks = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"toolset": toolset, "handler": handler}

    def register_hook(self, event, handler):
        self.hooks[event] = handler


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """Import the plugin's ``__init__.py`` as a standalone module, with the
    ``writegate`` package importable from the repo-root host package."""
    import sys
    # The writegate package is a repo-root host package, importable without
    # any plugin-private sys.path insertion.
    import writegate  # noqa: F401
    spec = importlib.util.spec_from_file_location(
        "writegate_plugin_under_test",
        _PLUGIN_DIR / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_register_inert_when_disabled(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_write_gate_enabled", lambda: False)
    ctx = _Ctx()
    plugin.register(ctx)
    assert ctx.tools == {}
    assert ctx.hooks == {}


def test_register_enabled_registers_tool_and_hook(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_write_gate_enabled", lambda: True)
    ctx = _Ctx()
    plugin.register(ctx)
    assert "write_gate" in ctx.tools
    assert ctx.tools["write_gate"]["toolset"] == "write_gate"
    assert "pre_tool_call" in ctx.hooks


def test_handler_forwards_exception_payload_and_preserves_host_authority(
    plugin, monkeypatch
):
    import writegate.tool

    captured = {}

    def fake_write_gate_tool(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(writegate.tool, "write_gate_tool", fake_write_gate_tool)
    monkeypatch.setattr(plugin, "_write_gate_enabled", lambda: True)

    ctx = _Ctx()
    plugin.register(ctx)

    handler = ctx.tools["write_gate"]["handler"]
    args = {
        "action": "request_exception",
        "stated_outcome": "fix protected file",
        "affected_files": ["/protected/a.py"],
        "session_id": "model-controlled-session",
        "task_id": "model-controlled-task",
    }
    handler(args, session_id="host-session", task_id="host-task")

    assert captured == {
        "action": "request_exception",
        "stated_outcome": "fix protected file",
        "affected_files": ["/protected/a.py"],
        "session_id": "host-session",
        "task_id": "host-task",
    }
