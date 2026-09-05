"""Tests for the write-gate enforcement ``decide`` path and the tool entrypoint.

Covers ``writegate/enforcement.py`` and
``writegate/tool.py``:

  * ``is_always_allowed`` / ``is_recognized_kanban`` — the read/kanban
    exemption set.
  * ``decide`` — fail closed when unbound; allow reads; require a bound
    session for a governed mutation; refuse traversal / unresolvable targets.
  * ``write_gate_tool`` — the service-gated action discriminator.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = "writegate"
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "write-gate"


@pytest.fixture
def plugin():
    """Import the plugin's ``__init__.py`` as a standalone module, with the
    ``writegate`` package importable from the repo-root host package."""
    import sys
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    import writegate  # noqa: F401
    spec = importlib.util.spec_from_file_location(
        "writegate_plugin_under_test",
        _PLUGIN_DIR / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mods(monkeypatch):
    """Import the ``writegate`` package + key submodules from the repo-root
    host package (no plugin-private ``sys.path`` insertion)."""
    import importlib
    import sys
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


# -- pre_tool_call: fail closed without host session id -----------------------

def test_pre_tool_call_blocks_without_session_id(plugin):
    """Gate 2: a governed write with no host-owned session id must block, not
    fall through to allow."""
    import json
    block = plugin._on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/some/path.txt"},
        # No session_id kwarg: the model cannot supply one.
    )
    assert block is not None
    assert block.get("action") == "block"
    assert "session" in block.get("message", "").lower()


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("read_file", {"path": "/some/path.txt"}),
        ("search_files", {"query": "needle"}),
        ("write_gate", {"action": "status"}),
        ("kanban_show", {"task_id": "task-1"}),
    ],
)
def test_pre_tool_call_exempt_routes_do_not_require_session_id(
    plugin, tool_name, args
):
    """Reads and canonical control-plane operations remain usable before
    Session Startup has supplied a host-owned session id.

    The Write-Gate governs mutations; it must not become a general read gate
    or deadlock the startup/control tools that establish its authority.
    """
    assert plugin._on_pre_tool_call(tool_name=tool_name, args=args) is None


def test_pre_tool_call_unknown_kanban_name_still_requires_session_id(plugin):
    """The exemption is an exact allow-list, never a ``kanban_*`` wildcard."""
    block = plugin._on_pre_tool_call(
        tool_name="kanban_unrecognized_operation",
        args={"path": "/some/path.txt"},
    )
    assert block is not None
    assert block.get("action") == "block"


def test_present_approval_preserves_host_reference_and_timestamp(
    monkeypatch, mods
):
    """The tool must not collapse the host approval to a bare string: the
    lease audit needs the exact host reference and decision clock."""
    expected = {
        "approved": True,
        "decision": "once",
        "approval_reference": "host-approval-42",
        "decision_at": "2026-09-01T10:20:30+00:00",
    }
    monkeypatch.setattr(
        "tools.approval.request_write_gate_approval",
        lambda **kwargs: dict(expected),
    )
    result = mods.tool._present_and_get_decision(
        {
            "request_id": "wg-tool-label",
            "session_id": "sess-1",
            "stated_outcome": "update one file",
        },
        kind="WRITE_GATE_EXCEPTION",
    )
    assert result == expected


def test_present_approval_surfaces_exception_request_in_order(
    monkeypatch, mods
):
    captured = {}

    def approve_once(**kwargs):
        captured.update(kwargs)
        return {
            "approved": True,
            "decision": "once",
            "approval_reference": "host-approval-exception",
            "decision_at": "2026-09-01T10:20:30+00:00",
        }

    monkeypatch.setattr(
        "tools.approval.request_write_gate_approval",
        approve_once,
    )
    mods.tool._present_and_get_decision(
        {
            "request_id": "wg-exception-request",
            "session_id": "sess-1",
            "stated_outcome": "update policy",
            "affected_files": ["/worktree/Canon/a.md", "/worktree/Canon/b.md"],
            "approved_folder": "/worktree/Canon",
            "recovery_location": "/worktree/5-archive/write-gate/sess-1/lease-1",
            "lease_validity": "Lease lasts 5 minutes from approval.",
            "confirmed_worktree": "/worktree",
            "warning": "Generic approval cannot satisfy this request.",
        },
        kind="WRITE_GATE_EXCEPTION",
    )

    assert captured["command"].splitlines() == [
        "WRITE-GATE EXCEPTION REQUEST",
        "Outcome: update policy",
        "Affected files:",
        "  - /worktree/Canon/a.md",
        "  - /worktree/Canon/b.md",
        "Approved folder: /worktree/Canon",
        "Recovery location: /worktree/5-archive/write-gate/sess-1/lease-1",
        "Lease validity: Lease lasts 5 minutes from approval.",
        "Confirmed worktree: /worktree",
        "Session: sess-1",
        "Warning: Generic approval cannot satisfy this request.",
    ]
    assert captured["description"] == (
        "WRITE_GATE_EXCEPTION: update policy for session sess-1"
    )


def test_present_approval_surfaces_worktree_binding_in_order(
    monkeypatch, mods
):
    captured = {}

    def approve_once(**kwargs):
        captured.update(kwargs)
        return {
            "approved": True,
            "decision": "once",
            "approval_reference": "host-approval-binding",
            "decision_at": "2026-09-01T10:20:30+00:00",
        }

    monkeypatch.setattr(
        "tools.approval.request_write_gate_approval",
        approve_once,
    )
    mods.tool._present_and_get_decision(
        {
            "request_id": "wg-binding-request",
            "session_id": "sess-2",
            "project": "GRC App",
            "initiative": "Kanban lifecycle",
            "board": "development",
            "worktree_path": "/worktree",
            "git_branch": "feature/kanban",
            "profile": "session-agent",
            "confirmation_basis": "derived-from-card",
            "note": "Confirming persists the worktree binding.",
        },
        kind="CONFIRMED_WORKTREE_BINDING",
    )

    assert captured["command"].splitlines() == [
        "WRITE-GATE WORKTREE BINDING",
        "Project: GRC App",
        "Initiative: Kanban lifecycle",
        "Board: development",
        "Worktree: /worktree",
        "Branch: feature/kanban",
        "Profile: session-agent",
        "Confirmation basis: derived-from-card",
        "Session: sess-2",
        "Note: Confirming persists the worktree binding.",
    ]
    assert captured["description"] == (
        "CONFIRMED_WORKTREE_BINDING: /worktree for session sess-2"
    )


def test_exception_lease_receives_host_approval_metadata(
    monkeypatch, reg, mods, tmp_path
):
    """The structured host decision is the lease authority record."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    reg.create_binding(session_id="sess-1", worktree_path=str(worktree))
    expected_at = "2026-09-01T10:20:30+00:00"
    monkeypatch.setattr(
        mods.tool,
        "_present_and_get_decision",
        lambda *args, **kwargs: {
            "approved": True,
            "decision": "once",
            "approval_reference": "host-approval-42",
            "decision_at": expected_at,
        },
    )

    result = json.loads(mods.tool._handle_request_exception(
        "sess-1",
        {
            "affected_files": [str(worktree / "Canon" / "policy.md")],
            "stated_outcome": "update policy",
        },
    ))
    assert result["status"] == "lease_created"
    lease = reg.get_lease(result["lease"]["lease_id"])
    assert lease is not None
    assert lease.approval_reference == "host-approval-42"
    assert lease.approved_at == expected_at


# -- Decision.to_block: remediation + universal prohibition --------------------

def test_decision_to_block_exposes_remediation_and_prohibition(mods):
    d = mods.enforcement.Decision(
        allowed=False,
        reason="Write-Gate: some reason",
        remediation="Do the specific remediation.",
    )
    block = d.to_block()
    assert block is not None
    assert block["action"] == "block"
    msg = block["message"]
    assert "Write-Gate: some reason" in msg
    assert "Required next action:" in msg
    assert "Do the specific remediation." in msg
    assert mods.enforcement.ALTERNATE_ROUTE_PROHIBITION in msg


# -- decide: protected/outside no-lease message names the exception flow ------

def test_decide_outside_worktree_no_lease_names_exception_flow(reg, mods, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    reg.create_binding(session_id="bound", worktree_path=str(worktree))
    outside = tmp_path / "outside" / "file.txt"
    d = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": str(outside)},
        session_id="bound",
        reg=reg,
        project_root=str(worktree),
    )
    assert not d.allowed
    msg = d.to_block()["message"]
    assert "request_exception" in msg
    assert "affected_files" in msg
    assert "stated_outcome" in msg
    assert "wait for human approval" in msg
    assert "write_file or patch" in msg


# -- pre_tool_call: missing host session id includes stop/operator guidance ---

def test_pre_tool_call_missing_session_id_includes_stop_operator_guidance(plugin, mods):
    block = plugin._on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/some/path.txt"},
        # No session_id kwarg: the model cannot supply one.
    )
    assert block is not None
    assert block.get("action") == "block"
    msg = block.get("message", "")
    assert "stop" in msg.lower() or "ask" in msg.lower()
    assert "operator" in msg.lower()
    assert mods.enforcement.ALTERNATE_ROUTE_PROHIBITION in msg


# -- decide: traversal block includes remediation and prohibition -------------

def test_decide_traversal_block_includes_remediation_and_prohibition(reg, mods):
    reg.create_binding(session_id="bound", worktree_path="/wt")
    d = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": "/wt/../../etc/passwd"},
        session_id="bound",
        reg=reg,
    )
    assert not d.allowed
    msg = d.to_block()["message"]
    assert "traversal" in msg.lower()
    assert "normalized path" in msg.lower()
    assert mods.enforcement.ALTERNATE_ROUTE_PROHIBITION in msg


# -- pre_tool_call: enforcement exception includes stop/report guidance -------

def test_pre_tool_call_enforcement_exception_includes_stop_report_guidance(
    plugin, mods, monkeypatch
):
    import writegate.registry as registry_mod

    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(registry_mod, "get_registry", _boom)
    block = plugin._on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/some/path.txt"},
        session_id="sess-1",
    )
    assert block is not None
    assert block.get("action") == "block"
    msg = block.get("message", "")
    assert "stop" in msg.lower()
    assert "report" in msg.lower()
    assert "operator" in msg.lower()
    assert mods.enforcement.ALTERNATE_ROUTE_PROHIBITION in msg
