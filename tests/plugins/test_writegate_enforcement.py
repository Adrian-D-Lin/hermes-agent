"""Tests for the write-gate enforcement ``decide`` path and the tool entrypoint.

Covers ``plugins/write-gate/writegate/enforcement.py`` and
``plugins/write-gate/writegate/tool.py``:

  * ``is_always_allowed`` / ``is_recognized_kanban`` — the read/kanban
    exemption set.
  * ``decide`` — fail closed when unbound; allow reads; require a bound
    session for a governed mutation; refuse traversal / unresolvable targets.
  * ``write_gate_tool`` — the service-gated action discriminator.
"""

import importlib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = "writegate"


@pytest.fixture
def mods(monkeypatch):
    """Import the ``writegate`` package + key submodules from the repo path."""
    import sys
    parent_path = str(_REPO_ROOT / "plugins" / "write-gate")
    if parent_path not in sys.path:
        sys.path.insert(0, parent_path)
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]
    pkg = importlib.import_module(_PACKAGE)
    enforcement = importlib.import_module(_PACKAGE + ".enforcement")
    tool = importlib.import_module(_PACKAGE + ".tool")
    registry_mod = importlib.import_module(_PACKAGE + ".registry")
    yield type("Mods", (), {
        "enforcement": enforcement,
        "tool": tool,
        "registry": registry_mod,
    })
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]
    if parent_path in sys.path:
        sys.path.remove(parent_path)


@pytest.fixture
def reg(tmp_path, mods):
    db_path = tmp_path / "write-gate.db"
    return mods.registry.set_registry_for_path(str(db_path))


# -- decision set -------------------------------------------------------------

def test_is_always_allowed_reads(mods):
    assert mods.enforcement.is_always_allowed("read_file")
    assert mods.enforcement.is_always_allowed("search_files")


def test_is_recognized_kanban_exact_only(mods):
    assert "kanban_complete" in mods.enforcement.RECOGNIZED_KANBAN_TOOLS
    assert not mods.enforcement.is_recognized_kanban("kanban_complete_x")


# -- decide: fail closed when unbound -----------------------------------------

def test_decide_fails_closed_when_unbound(reg, mods):
    d = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": "/some/path.txt"},
        session_id="unbound-session",
        reg=reg,
    )
    assert not d.allowed
    assert d.reason  # non-empty reason


# -- decide: reads allowed ----------------------------------------------------

def test_decide_read_allowed(reg, mods):
    d = mods.enforcement.decide(
        tool_name="read_file",
        args={"path": "/some/path.txt"},
        session_id="any-session",
        reg=reg,
    )
    assert d.allowed


# -- decide: governed mutation requires a bound session -----------------------

def test_decide_governed_mutation_requires_binding(reg, mods):
    # Unbound -> blocked.
    d = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": "/some/path.txt"},
        session_id="unbound",
        reg=reg,
    )
    assert not d.allowed

    # Bound -> the governed mutation is allowed (or escalated) rather than
    # failing closed on the binding gate. Use a path inside the worktree.
    reg.create_binding(session_id="bound", worktree_path="/wt")
    d2 = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": "/wt/some/path.txt"},
        session_id="bound",
        reg=reg,
    )
    # Either allowed or escalated — never the "unbound" fail-closed reason.
    assert d2.allowed or getattr(d2, "escalated", False)


# -- decide: traversal / unresolvable target refused --------------------------

def test_decide_traversal_refirmed_as_governed(reg, mods):
    reg.create_binding(session_id="bound", worktree_path="/wt")
    # A path that escapes the worktree is a governed target that must not be
    # allowed through the binding gate.
    d = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": "/wt/../../etc/passwd"},
        session_id="bound",
        reg=reg,
    )
    # Either blocked or escalated — not silently allowed.
    assert not (d.allowed and not getattr(d, "escalated", False))


# -- tool entrypoint ----------------------------------------------------------

def test_write_gate_tool_confirm_requires_binding(reg, mods):
    # No binding -> the confirm action must fail closed (blocked), not raise.
    out = mods.tool.write_gate_tool(action="confirm_binding")
    assert out  # structured JSON response
    assert "blocked" in out.lower() or "denied" in out.lower() or "error" in out.lower()


def test_write_gate_tool_unknown_action(reg, mods):
    out = mods.tool.write_gate_tool(action="no_such_action")
    assert out  # structured error response
