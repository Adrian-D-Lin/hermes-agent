"""Top-level session startup binding acceptance tests for ``writegate``.

Covers the runtime-integration brief §1–§5: the ordinary top-level session can
now bind.  The previously-defective ``_build_binding_candidate`` only derived a
candidate from an existing central binding or a model-supplied path; a fresh
top-level session had neither and always returned ``None``.  It now derives the
candidate from the **host-owned** session cwd record
(:func:`tools.terminal_tool.get_session_cwd`) resolved to its real Git worktree
root, rejecting remote/backend records and any model-supplied path.

Covered paths:

* **Top-level confirm** — happy (bind to the real Git root), deny, timeout,
  missing/deleted cwd, non-Git cwd, subdirectory-to-worktree-root, model-path
  spoof, remote/backend cwd rejection, empty-task-id spoof.
* **Explicit re-anchor** — confirm supersedes + preserves history; deny leaves
  the old binding active; re-anchor on an unbound session errors.
* **Lineage** — resume/compression retain identity; branch / compression child /
  delegate inherit via real tmp worktrees; invalid/unbound parent denies;
  ``/new`` stays unbound.
* **First governed write** — reads work before binding; the production
  ``pre_tool_call`` hook lazily derives trusted lineage; /new + unbound block.

These drive the registered ``writegate.tool`` surface, the production
``pre_tool_call`` hook (imported via importlib from the plugin dir), and
:func:`writegate.binding.derive_binding` — not pure helpers in isolation.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import writegate.registry  # noqa: F401  (host package: shared PRODUCER constants)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "write-gate"
_PACKAGE = "writegate"


# ── fixtures ------------------------------------------------------------------

@pytest.fixture
def mods(monkeypatch):
    """Import the ``writegate`` package + submodules from the repo-root host
    package (no plugin-private ``sys.path`` insertion)."""
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]
    binding = importlib.import_module(_PACKAGE + ".binding")
    registry_mod = importlib.import_module(_PACKAGE + ".registry")
    enforcement = importlib.import_module(_PACKAGE + ".enforcement")
    tool = importlib.import_module(_PACKAGE + ".tool")
    yield type("Mods", (), {
        "binding": binding,
        "registry": registry_mod,
        "enforcement": enforcement,
        "tool": tool,
    })
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]


@pytest.fixture
def reg(tmp_path, mods):
    return mods.registry.set_registry_for_path(str(tmp_path / "write-gate.db"))


@pytest.fixture
def git_repo(tmp_path):
    """A real Git repo with a ``sub`` subdirectory.  Returns (root, subdir)."""
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=str(root), check=True,
        capture_output=True, text=True,
    )
    return root, root / "sub"


@pytest.fixture
def cwd_recorder(git_repo):
    """Install a real ``tools.terminal_tool.get_session_cwd`` backed by the
    in-process record store, seeded from ``git_repo``.

    Returns a helper ``record(session_id, subdir)`` that records a cwd for a
    session id and returns the exact absolute path recorded.  ``subdir`` is
    created (and nested) so callers can test subdirectory→worktree-root
    resolution.
    """
    from tools.terminal_tool import record_session_cwd, get_session_cwd

    # Ensure the real terminal_tool is importable and used by binding.py.
    if "tools.terminal_tool" not in sys.modules:
        import tools.terminal_tool  # noqa: F401

    # Host-owned CWD records are keyed by the *task id* (the top-level session
    # key), not the agent session id.  The confirm/seam path passes
    # task_id=session_id explicitly, so the real store needs no patching here.

    def _record(session_id, subdir=None):
        if subdir:
            target = git_repo[0] / subdir
            target.mkdir(parents=True, exist_ok=True)
        else:
            target = git_repo[0]
        record_session_cwd(session_id, str(target))
        return str(target)

    _record("seed")  # prime the import path
    return _record


@pytest.fixture
def approve(monkeypatch, mods):
    """Install a stub approval transport on the tool module.  Returns a
    factory: ``approve(decision)`` patches ``_present_and_get_decision`` so the
    tool seam sees an explicit host approval."""
    def _factory(decision):
        monkeypatch.setattr(
            mods.tool,
            "_present_and_get_decision",
            lambda pres, kind: {
                "approved": decision == "once",
                "decision": decision,
                "approval_reference": "host-ref",
                "decision_at": "2026-09-01T10:00:00+00:00",
            },
        )
    return _factory


@pytest.fixture
def plugin(mods, monkeypatch):
    """Import the plugin's ``__init__.py`` as a standalone module via
    importlib (the repository plugin-loader seam), with the ``writegate``
    package importable from the repo-root host package."""
    spec = importlib.util.spec_from_file_location(
        "writegate_plugin_under_test",
        _PLUGIN_DIR / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_session_db(tmp_path, rows):
    """Build a real read-only :class:`hermes_state.SessionDB` at
    ``tmp_path/hermes.db`` containing exactly the given sessions-table rows.

    ``rows`` is a list of dicts keyed by the sessions-table columns used in
    the lineage tests (id, source, model_config, parent_session_id,
    end_reason).  Returns ``(db, profile_dir)`` — the profile dir is the
    directory ``resolve_lineage(profile_dir=...)`` must open.  The real
    SessionDB class is used so the production resolver exercises genuine
    ``get_session`` semantics, not a fake.
    """
    import sqlite3

    from pathlib import Path

    from hermes_state import SessionDB

    profile_dir = Path(tmp_path) / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # The resolver opens the canonical profile store name, state.db, and
    # get_session LEFT-JOINs system_prompts — so build the full read shape
    # here (a subset is enough: get_session only reads the sessions columns
    # plus the optional joined prompt).
    db_path = profile_dir / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE system_prompts ("
            "hash TEXT PRIMARY KEY, "
            "prompt TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, "
            "source TEXT NOT NULL, "
            "user_id TEXT, "
            "session_key TEXT, "
            "chat_id TEXT, "
            "chat_type TEXT, "
            "thread_id TEXT, "
            "display_name TEXT, "
            "origin_json TEXT, "
            "expiry_finalized INTEGER DEFAULT 0, "
            "model TEXT, "
            "model_config TEXT, "
            "system_prompt TEXT, "
            "system_prompt_hash TEXT, "
            "parent_session_id TEXT, "
            "started_at REAL NOT NULL DEFAULT 0, "
            "ended_at REAL, "
            "end_reason TEXT, "
            "message_count INTEGER DEFAULT 0, "
            "tool_call_count INTEGER DEFAULT 0, "
            "input_tokens INTEGER DEFAULT 0, "
            "output_tokens INTEGER DEFAULT 0, "
            "cache_read_tokens INTEGER DEFAULT 0, "
            "cache_write_tokens INTEGER DEFAULT 0, "
            "reasoning_tokens INTEGER DEFAULT 0, "
            "cwd TEXT, "
            "git_branch TEXT, "
            "git_repo_root TEXT, "
            "git_metadata_generation INTEGER NOT NULL DEFAULT 0, "
            "billing_provider TEXT)"
        )
        for row in rows:
            conn.execute(
                "INSERT INTO sessions (id, source, model_config, "
                "parent_session_id, end_reason) VALUES (?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row.get("source") or "cli",
                    row.get("model_config"),
                    row.get("parent_session_id"),
                    row.get("end_reason"),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    db = SessionDB(db_path, read_only=True)
    return db, str(profile_dir)


# ── §1: top-level confirm binding --------------------------------------------

def test_confirm_binding_binds_real_git_root(cwd_recorder, approve, reg, mods, git_repo):
    """A fresh top-level session binds to the real Git worktree root derived
    from the host-owned session cwd record, not a model path."""
    cwd_recorder("sess-top", "sub")
    approve("once")
    # A model-supplied spoof hint must be ignored as authority.
    out = mods.tool.write_gate_tool(
        action="confirm_binding",
        session_id="sess-top",
        confirmed_worktree="/tmp/evil-spoof",
    )
    data = json.loads(out)
    assert data["success"] is True
    assert data["status"] == "bound"
    binding = reg.get_active_binding("sess-top")
    assert binding is not None
    # Bound to the real repo root, not the spoof.
    assert binding.worktree_path == str(git_repo[0])
    assert binding.producer == mods.registry.PRODUCER_CONFIRM_BINDING


def test_confirm_binding_subdirectory_resolves_to_worktree_root(
    cwd_recorder, approve, reg, mods, git_repo
):
    """A cwd recorded in a nested subdirectory resolves to the worktree root."""
    cwd_recorder("sess-nested", "sub/deeper")
    approve("once")
    out = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-nested")
    data = json.loads(out)
    assert data["status"] == "bound"
    binding = reg.get_active_binding("sess-nested")
    assert binding.worktree_path == str(git_repo[0])


def test_confirm_binding_deny_persists_nothing(cwd_recorder, approve, reg, mods):
    """A decline persists no binding; the session stays unbound."""
    cwd_recorder("sess-deny", "sub")
    approve("deny")
    out = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-deny")
    data = json.loads(out)
    assert data["status"] == "declined"
    assert reg.get_active_binding("sess-deny") is None


def test_confirm_binding_timeout_fails_closed(cwd_recorder, approve, reg, mods):
    """A timeout (non-``once`` decision) fails closed: nothing persists."""
    cwd_recorder("sess-timeout", "sub")
    approve("timeout")
    out = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-timeout")
    data = json.loads(out)
    assert data["status"] == "declined"
    assert reg.get_active_binding("sess-timeout") is None


def test_confirm_binding_missing_cwd_cannot_derive(cwd_recorder, approve, reg, mods):
    """A session with no recorded cwd cannot derive a candidate."""
    out = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-no-cwd")
    data = json.loads(out)
    assert data["success"] is False
    assert "cannot present" in data["error"]
    assert reg.get_active_binding("sess-no-cwd") is None


def test_confirm_binding_deleted_cwd_cannot_derive(cwd_recorder, approve, reg, mods, tmp_path):
    """A cwd record pointing at a now-deleted directory cannot derive."""
    from tools.terminal_tool import record_session_cwd
    dead = tmp_path / "dead"
    dead.mkdir()
    record_session_cwd("sess-dead", str(dead))
    import shutil
    shutil.rmtree(dead)
    out = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-dead")
    data = json.loads(out)
    assert data["success"] is False
    assert "cannot present" in data["error"]


def test_confirm_binding_non_git_cwd_rejected(cwd_recorder, approve, reg, mods, tmp_path):
    """A cwd that is a real directory but not inside a Git project is rejected."""
    from tools.terminal_tool import record_session_cwd
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    record_session_cwd("sess-plain", str(plain))
    out = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-plain")
    data = json.loads(out)
    assert data["success"] is False
    assert "cannot present" in data["error"]


def test_confirm_binding_model_path_spoof_rejected(cwd_recorder, approve, reg, mods, git_repo):
    """The model may not supply the binding path: a spoof hint is ignored even
    on ``once`` approval, and the real Git root is bound instead."""
    cwd_recorder("sess-spoof", "sub")
    approve("once")
    out = mods.tool.write_gate_tool(
        action="confirm_binding",
        session_id="sess-spoof",
        confirmed_worktree="/tmp/definitely-not-a-real-repo/evil",
    )
    data = json.loads(out)
    assert data["status"] == "bound"
    binding = reg.get_active_binding("sess-spoof")
    assert binding.worktree_path == str(git_repo[0])
    assert "/evil" not in binding.worktree_path


def test_confirm_binding_remote_backend_rejected(cwd_recorder, approve, monkeypatch, reg, mods):
    """A non-local backend (docker) cwd must not become local write authority."""
    cwd_recorder("sess-remote", "sub")
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    out = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-remote")
    data = json.loads(out)
    assert data["success"] is False
    assert "cannot present" in data["error"]
    assert reg.get_active_binding("sess-remote") is None


def test_confirm_binding_sibling_outside_process_cwd_resolves(
    cwd_recorder, approve, reg, mods, tmp_path, monkeypatch
):
    """A valid session worktree commonly sits outside the process cwd; it must
    still resolve (the rejected cwd-ancestry heuristic regression)."""
    from tools.terminal_tool import record_session_cwd
    sibling = tmp_path / "sibling-proj"
    (sibling / "sub").mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=str(sibling), check=True,
        capture_output=True, text=True,
    )
    record_session_cwd("sess-sibling", str(sibling / "sub"))
    # Force the process cwd to something unrelated so the recorded cwd is a
    # sibling/outside the process cwd.
    monkeypatch.chdir(tmp_path / "proj")
    approve("once")
    out = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-sibling")
    data = json.loads(out)
    assert data["status"] == "bound", data
    binding = reg.get_active_binding("sess-sibling")
    assert binding.worktree_path == str(sibling)


def test_confirm_binding_empty_task_id_falls_back_to_session_id(
    cwd_recorder, approve, reg, mods, git_repo
):
    """Fail-closed against the shared-default spoof: an empty task id must not
    read the shared ``default`` record.  With no trusted task id, the handler
    falls back to session_id; an unkeyed/default record must not authorize."""
    cwd_recorder("sess-fallback", "sub")
    approve("once")
    # No task_id provided -> falls back to get_session_cwd(session_id).
    out = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-fallback")
    data = json.loads(out)
    assert data["status"] == "bound", data
    binding = reg.get_active_binding("sess-fallback")
    assert binding.worktree_path == str(git_repo[0])


def test_confirm_binding_default_record_does_not_leak(
    cwd_recorder, approve, reg, mods, git_repo
):
    """A spoof: record a cwd under the shared ``default`` key, then attempt to
    bind a different session with an empty task id.  The shared record must
    NOT authorize the other session."""
    from tools.terminal_tool import record_session_cwd
    # Seed the shared default record.
    record_session_cwd("default", str(git_repo[0]))
    # A different session with empty task id must not read the default record.
    out = mods.tool.write_gate_tool(
        action="confirm_binding",
        session_id="sess-other",
        task_id="",
    )
    data = json.loads(out)
    # No cwd recorded for sess-other under its own key -> cannot present.
    assert data["success"] is False
    assert "cannot present" in data["error"]


# ── §2: explicit re-anchor ----------------------------------------------------

def test_reanchor_confirm_supersedes_and_preserves_history(
    cwd_recorder, approve, reg, mods, tmp_path
):
    """Re-anchor of a changed worktree supersedes the prior active binding and
    preserves a coherent supersession history."""
    cwd_recorder("sess-anchor", "sub")
    approve("once")
    out = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-anchor")
    assert json.loads(out)["status"] == "bound"

    # Change the recorded cwd to a new git repo and re-anchor.
    new_repo = tmp_path / "proj2"
    (new_repo / "sub").mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=str(new_repo), check=True,
        capture_output=True, text=True,
    )
    from tools.terminal_tool import record_session_cwd
    record_session_cwd("sess-anchor", str(new_repo / "sub"))
    approve("once")
    out = mods.tool.write_gate_tool(action="reanchor", session_id="sess-anchor")
    data = json.loads(out)
    assert data["status"] == "reanchored", data

    active = reg.get_active_binding("sess-anchor")
    assert active.worktree_path == str(new_repo)
    statuses = [r.status for r in reg.list_bindings("sess-anchor")]
    assert statuses.count("active") == 1
    assert statuses.count("superseded") == 1
    prior = [r for r in reg.list_bindings("sess-anchor") if r.status == "superseded"][0]
    assert prior.superseded_by_id == active.id


def test_reanchor_deny_leaves_old_binding_active(cwd_recorder, approve, reg, mods, git_repo):
    """A re-anchor deny/timeout leaves the prior active binding active."""
    cwd_recorder("sess-anchor2", "sub")
    approve("once")
    mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-anchor2")

    approve("deny")
    out = mods.tool.write_gate_tool(action="reanchor", session_id="sess-anchor2")
    data = json.loads(out)
    assert data["status"] == "declined", data
    binding = reg.get_active_binding("sess-anchor2")
    assert binding is not None
    assert binding.worktree_path == str(git_repo[0])


def test_reanchor_on_unbound_session_errors(cwd_recorder, approve, reg, mods):
    """Re-anchor is privileged to an already-bound session; an unbound session
    errors and is directed to ``confirm_binding``."""
    approve("once")
    out = mods.tool.write_gate_tool(action="reanchor", session_id="sess-unbound")
    data = json.loads(out)
    assert data["success"] is False
    assert "reanchor requires" in data["error"]
    assert reg.get_active_binding("sess-unbound") is None


# ── §3: lineage ---------------------------------------------------------------

def test_resume_retains_binding(reg, mods, tmp_path):
    """A23/A24: resume / in-place compression with the same id keeps its own
    active binding."""
    parent_wt = str(tmp_path / "resume-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    reg.create_binding(session_id="sess-resume", worktree_path=parent_wt)
    derived = mods.binding.derive_binding(
        reg, session_id="sess-resume", is_new_session=False,
    )
    assert derived is not None
    assert derived.id == reg.get_active_binding("sess-resume").id


def test_new_session_stays_unbound(reg, mods):
    """A27: ``/new`` — a fresh session id starts unbound, no derivation."""
    assert mods.binding.derive_binding(
        reg, session_id="sess-new", is_new_session=True,
    ) is None
    assert reg.get_active_binding("sess-new") is None


def test_branch_child_derives_from_trusted_parent(reg, mods, tmp_path):
    """A26: a branch/compression child with a new session id derives from a
    trusted recorded parent whose parent is actively bound (real worktree)."""
    parent_wt = str(tmp_path / "parent-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    parent = reg.create_binding(
        session_id="sess-parent", worktree_path=parent_wt,
        project="proj", board="orchestrator",
    )
    derived = mods.binding.derive_binding(
        reg, session_id="sess-branch",
        parent_session_id="sess-parent", parent_is_bound=True,
    )
    assert derived is not None
    assert derived.worktree_path == parent_wt
    assert derived.parent_session_id == "sess-parent"
    assert derived.producer == writegate.registry.PRODUCER_LINEAGE


def test_delegate_inherits_parent_worktree(reg, mods, tmp_path):
    """A21: an in-process delegate with the same worktree inherits the exact
    parent worktree via lineage."""
    parent_wt = str(tmp_path / "parent-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    parent = reg.create_binding(
        session_id="sess-parent", worktree_path=parent_wt,
        project="proj", board="orchestrator",
    )
    derived = mods.binding.derive_binding(
        reg, session_id="sess-delegate",
        parent_session_id="sess-parent", parent_is_bound=True,
    )
    assert derived is not None
    assert derived.worktree_path == parent_wt


def test_delegate_distinct_workspace_bound_exact(reg, mods, tmp_path):
    """A22: a delegate with a distinct trusted workspace is bound to that exact
    workspace with lineage."""
    parent_wt = str(tmp_path / "parent-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    reg.create_binding(
        session_id="sess-parent", worktree_path=parent_wt,
        project="proj", board="orchestrator",
    )
    isolated = str(tmp_path / "isolated")
    Path(isolated).mkdir(parents=True, exist_ok=True)
    derived = mods.binding.derive_binding(
        reg, session_id="sess-delegate2",
        parent_session_id="sess-parent", parent_is_bound=True,
        assigned_workspace=isolated,
    )
    assert derived is not None
    assert derived.worktree_path == isolated


def test_invalid_abandoned_parent_denies(reg, mods, tmp_path):
    """A parent that is no longer actively bound (abandoned) must not authorize
    a child binding."""
    parent_wt = str(tmp_path / "parent-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    reg.create_binding(
        session_id="sess-parent", worktree_path=parent_wt,
        project="proj", board="orchestrator",
    )
    # Abandon the parent (as the dispatcher does after a failed spawn).
    reg.mark_binding_abandoned("sess-parent")
    assert reg.get_active_binding("sess-parent") is None
    assert mods.binding.derive_binding(
        reg, session_id="sess-child",
        parent_session_id="sess-parent", parent_is_bound=False,
    ) is None


def test_legacy_null_session_reads_only(reg, mods):
    """A28: a session with no trusted parent and no own binding derives nothing
    (read-only); it is never guessed or backfilled."""
    assert reg.get_active_binding("sess-legacy") is None
    assert mods.binding.derive_binding(
        reg, session_id="sess-legacy",
        parent_session_id=None, parent_is_bound=False,
    ) is None


# ── §5: first governed write lazily derives -----------------------------------

def test_reads_work_before_binding(reg, mods):
    """Reads still work before binding (enforcement exempt)."""
    decision = mods.enforcement.decide(
        tool_name="read_file",
        args={"path": "/some/path.txt"},
        session_id="unbound",
        reg=reg,
    )
    assert decision.allowed


def test_governed_write_blocks_when_unbound(reg, mods):
    """A governed write blocks when unbound (fail closed)."""
    decision = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": "/some/path.txt"},
        session_id="unbound",
        reg=reg,
    )
    assert not decision.allowed
    assert "no confirmed worktree binding" in decision.reason


def test_first_governed_write_lazy_derive_via_hook(
    approve, reg, mods, plugin, tmp_path, monkeypatch
):
    """The production ``pre_tool_call`` hook lazily derives a binding from a
    trusted *SessionDB* lineage row, then allows the write.  The parent is
    bound first; the child derives via the verified branch marker — NOT from
    ambient cwd and NOT from task_id alone."""
    parent_wt = str(tmp_path / "parent-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    reg.create_binding(
        session_id="sess-parent", worktree_path=parent_wt,
        project="proj", board="orchestrator",
    )

    # Trusted SessionDB rows: a branch child pointing at its bound parent.
    db, profile_dir = _make_session_db(
        tmp_path,
        [
            {"id": "sess-parent", "end_reason": None},
            {
                "id": "sess-child",
                "parent_session_id": "sess-parent",
                "model_config": json.dumps({"_branched_from": "sess-parent"}),
            },
        ],
    )
    monkeypatch.chdir(tmp_path / "parent-wt")
    try:
        block = plugin._on_pre_tool_call(
            tool_name="write_file",
            args={"path": parent_wt + "/notes.md"},
            session_id="sess-child",
            task_id="sess-child",
            _writegate_profile_dir=profile_dir,
        )
        # The child derived the parent's worktree via verified lineage, so the
        # write is allowed (not blocked on the binding gate).
        assert block is None, block
        derived = reg.get_active_binding("sess-child")
        assert derived is not None
        assert derived.worktree_path == parent_wt
        assert derived.producer == writegate.registry.PRODUCER_LINEAGE
    finally:
        try:
            db.close()
        except Exception:
            pass


def test_hook_compression_child_derives_via_sessiondb(
    approve, reg, plugin, tmp_path, monkeypatch
):
    """A compression child (no fork marker; parent ended with
    ``end_reason='compression'``) derives through the production hook."""
    parent_wt = str(tmp_path / "parent-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    reg.create_binding(
        session_id="sess-parent", worktree_path=parent_wt,
        project="proj", board="orchestrator",
    )
    db, profile_dir = _make_session_db(
        tmp_path,
        [
            {"id": "sess-parent", "end_reason": "compression"},
            {
                "id": "sess-child",
                "parent_session_id": "sess-parent",
            },
        ],
    )
    monkeypatch.chdir(tmp_path / "parent-wt")
    try:
        block = plugin._on_pre_tool_call(
            tool_name="write_file",
            args={"path": parent_wt + "/notes.md"},
            session_id="sess-child",
            task_id="sess-child",
            _writegate_profile_dir=profile_dir,
        )
        assert block is None, block
        derived = reg.get_active_binding("sess-child")
        assert derived is not None
        assert derived.worktree_path == parent_wt
        assert derived.producer == writegate.registry.PRODUCER_LINEAGE
    finally:
        try:
            db.close()
        except Exception:
            pass


def test_hook_delegate_tool_source_derives_via_sessiondb(
    approve, reg, plugin, tmp_path, monkeypatch
):
    """A delegate session with ``source='tool'`` and a trusted host cwd record
    under its task id is bound to that distinct workspace (A22) through the
    production hook."""
    parent_wt = str(tmp_path / "parent-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    reg.create_binding(
        session_id="sess-parent", worktree_path=parent_wt,
        project="proj", board="orchestrator",
    )
    isolated = tmp_path / "isolated"
    (isolated / "sub").mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=str(isolated), check=True,
        capture_output=True, text=True,
    )
    from tools.terminal_tool import record_session_cwd
    record_session_cwd("task-delegate", str(isolated / "sub"))

    db, profile_dir = _make_session_db(
        tmp_path,
        [
            {"id": "sess-parent", "end_reason": None},
            {
                "id": "sess-delegate",
                "source": "tool",
                "parent_session_id": "sess-parent",
            },
        ],
    )
    try:
        block = plugin._on_pre_tool_call(
            tool_name="write_file",
            args={"path": str(isolated / "notes.md")},
            session_id="sess-delegate",
            task_id="task-delegate",
            _writegate_profile_dir=profile_dir,
        )
        assert block is None, block
        derived = reg.get_active_binding("sess-delegate")
        assert derived is not None
        # Distinct trusted workspace is authoritative (A22).
        assert derived.worktree_path == str(isolated)
        assert derived.producer == writegate.registry.PRODUCER_LINEAGE
    finally:
        try:
            db.close()
        except Exception:
            pass


def test_hook_new_with_active_continuity_parent_stays_unbound(
    approve, reg, plugin, tmp_path, monkeypatch
):
    """``/new`` with a continuity parent (``_reset_from`` marker) must stay
    unbound through the production hook — the marker is not lineage proof."""
    parent_wt = str(tmp_path / "parent-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    reg.create_binding(
        session_id="sess-parent", worktree_path=parent_wt,
        project="proj", board="orchestrator",
    )
    db, profile_dir = _make_session_db(
        tmp_path,
        [
            {"id": "sess-parent", "end_reason": None},
            {
                "id": "sess-new",
                "parent_session_id": "sess-parent",
                "model_config": json.dumps({"_reset_from": "sess-parent"}),
            },
        ],
    )
    try:
        block = plugin._on_pre_tool_call(
            tool_name="write_file",
            args={"path": parent_wt + "/notes.md"},
            session_id="sess-new",
            task_id="sess-new",
            _writegate_profile_dir=profile_dir,
        )
        assert block is not None
        assert block["action"] == "block"
        assert reg.get_active_binding("sess-new") is None
    finally:
        try:
            db.close()
        except Exception:
            pass


def test_hook_abandoned_parent_denies_child(
    approve, reg, plugin, tmp_path, monkeypatch
):
    """A branch child whose trusted parent row exists but whose parent binding
    was abandoned must not inherit anything through the production hook."""
    parent_wt = str(tmp_path / "parent-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    reg.create_binding(
        session_id="sess-parent", worktree_path=parent_wt,
        project="proj", board="orchestrator",
    )
    reg.mark_binding_abandoned("sess-parent")
    db, profile_dir = _make_session_db(
        tmp_path,
        [
            {"id": "sess-parent", "end_reason": None},
            {
                "id": "sess-child",
                "parent_session_id": "sess-parent",
                "model_config": json.dumps({"_branched_from": "sess-parent"}),
            },
        ],
    )
    try:
        block = plugin._on_pre_tool_call(
            tool_name="write_file",
            args={"path": parent_wt + "/notes.md"},
            session_id="sess-child",
            task_id="sess-child",
            _writegate_profile_dir=profile_dir,
        )
        assert block is not None
        assert block["action"] == "block"
        assert reg.get_active_binding("sess-child") is None
    finally:
        try:
            db.close()
        except Exception:
            pass


def test_hook_missing_sessiondb_row_derives_nothing(
    approve, reg, plugin, tmp_path
):
    """No SessionDB row at all: the production hook derives nothing and the
    governed write blocks (fail closed)."""
    block = plugin._on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/some/path.txt"},
        session_id="sess-orphan",
        task_id="sess-orphan",
    )
    assert block is not None
    assert block["action"] == "block"
    assert reg.get_active_binding("sess-orphan") is None


def test_new_session_governed_write_blocks_via_hook(
    cwd_recorder, approve, reg, mods, plugin, tmp_path
):
    """A ``/new`` session (brand-new id, no lineage) blocks governed writes via
    the production hook - it derives nothing."""
    block = plugin._on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/some/path.txt"},
        session_id="sess-new",
        task_id="sess-new",
    )
    assert block is not None
    assert block["action"] == "block"
    assert reg.get_active_binding("sess-new") is None


def test_ordinary_unbound_top_level_blocks_via_hook(
    cwd_recorder, approve, reg, mods, plugin, tmp_path
):
    """An ordinary unbound top-level session (no lineage, no confirm) blocks
    governed writes via the production hook."""
    block = plugin._on_pre_tool_call(
        tool_name="write_file",
        args={"path": "/some/path.txt"},
        session_id="sess-unbound",
        task_id="sess-unbound",
    )
    assert block is not None
    assert block["action"] == "block"
    assert reg.get_active_binding("sess-unbound") is None


# ── §5b: request_exception requires a binding -------------------------------

def test_request_exception_unbound_denied_registries_no_row(
    cwd_recorder, approve, reg, mods
):
    """An unbound session cannot mint external/protected authority via
    request_exception: it returns a structured bind-first denial and creates no
    request or lease row."""
    cwd_recorder("sess-req", "sub")
    out = mods.tool.write_gate_tool(
        action="request_exception",
        session_id="sess-req",
        affected_files=["Canon/foo.md"],
        stated_outcome="need to write outside worktree",
    )
    data = json.loads(out)
    assert data["success"] is False
    assert "bind-first" in data["error"] or "binding" in data["error"]


def test_request_exception_bound_session_mints_request(
    cwd_recorder, approve, reg, mods
):
    """A bound session's request_exception mints a request scoped to the
    confirmed worktree (not the ambient cwd)."""
    cwd_recorder("sess-req2", "sub")
    approve("once")
    mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-req2")

    out = mods.tool.write_gate_tool(
        action="request_exception",
        session_id="sess-req2",
        affected_files=["Canon/foo.md"],
        stated_outcome="need to write outside worktree",
    )
    data = json.loads(out)
    assert data["success"] is True, data
    request_id = data["request_id"]
    request = reg.get_request(request_id)
    assert request is not None
    assert request.session_id == "sess-req2"


# ── §5c: pre-deployment gaps ---------------------------------------------------


def _enabled_write_gate_config(home: Path) -> None:
    """Write ``security.write_gate.enabled: true`` into a profile's config.yaml
    so the plugin's ``register()`` gate passes under ``load_config_readonly``."""
    from hermes_constants import get_hermes_home
    import yaml

    assert str(get_hermes_home()) == str(home)
    cfg_path = home / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump({"security": {"write_gate": {"enabled": True}}})
    )
    # Invalidate the in-process config cache so the next read sees the file.
    try:
        import hermes_cli.config as _cfg

        if hasattr(_cfg, "_config_cache"):
            _cfg._config_cache.clear()
    except Exception:
        pass


def test_plugin_register_inert_when_disabled(plugin, mods, reg, tmp_path, monkeypatch):
    """Without ``security.write_gate.enabled: true`` the plugin registers no
    tool and no hook (inert) — the pre_tool_call path never engages."""
    _enabled_write_gate_config = None  # config stays absent -> disabled

    class _Ctx:
        def __init__(self):
            self.tools = []
            self.hooks = {}

        def register_tool(self, name, **kw):
            self.tools.append(name)

        def register_hook(self, event, fn):
            self.hooks.setdefault(event, []).append(fn)

    ctx = _Ctx()
    plugin.register(ctx)
    assert ctx.tools == []
    assert ctx.hooks == {}


def test_plugin_register_enabled_registers_tool_and_hook(
    plugin, mods, reg, tmp_path, monkeypatch
):
    """With ``security.write_gate.enabled: true`` the plugin registers the
    single ``write_gate`` tool and the ``pre_tool_call`` enforcement hook."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    _enabled_write_gate_config(home)

    class _Ctx:
        def __init__(self):
            self.tools = []
            self.hooks = {}

        def register_tool(self, name, **kw):
            self.tools.append(name)

        def register_hook(self, event, fn):
            self.hooks.setdefault(event, []).append(fn)

    ctx = _Ctx()
    plugin.register(ctx)
    assert ctx.tools == ["write_gate"]
    assert "pre_tool_call" in ctx.hooks and len(ctx.hooks["pre_tool_call"]) == 1


def test_registry_rollback_preserves_rows_on_failed_migration(
    reg, mods, tmp_path
):
    """Reopening the registry with a corrupted/legacy artifact must not lose
    the existing rows: migration is re-applied idempotently and prior binding
    rows survive the reopen (rollback-safe persistence)."""
    wt = str(tmp_path / "wt-rollback")
    Path(wt).mkdir(parents=True, exist_ok=True)
    reg.create_binding(
        session_id="sess-rb", worktree_path=wt,
        project="proj", board="orchestrator",
    )
    original = reg.get_active_binding("sess-rb")
    assert original is not None

    # Simulate a second process reopening the same file mid-migration: the
    # Registry constructor must migrate idempotently and keep the row.
    reopened = mods.registry.set_registry_for_path(str(tmp_path / "write-gate.db"))
    kept = reopened.get_active_binding("sess-rb")
    assert kept is not None
    assert kept.worktree_path == original.worktree_path
    assert kept.id == original.id


def test_spawn_failure_abandonment_leaves_no_binding(
    reg, mods, plugin, tmp_path
):
    """A spawn/startup failure must not leave a half-created binding: the
    lineage resolver + hook never fabricate a binding when no SessionDB row
    exists for the session (spawn failure -> no row -> unbound -> blocked)."""
    block = plugin._on_pre_tool_call(
        tool_name="write_file",
        args={"path": str(tmp_path / "notes.md")},
        session_id="sess-never-spawned",
        task_id="sess-never-spawned",
    )
    assert block is not None
    assert block["action"] == "block"
    assert reg.get_active_binding("sess-never-spawned") is None


def test_confirm_repeat_supersedes_without_duplicate_active(
    cwd_recorder, approve, reg, mods, git_repo
):
    """``confirm_binding`` reports ``already_bound`` for a session that already
    has an active binding — re-anchoring (``reanchor``) is the explicit
    second confirmation that supersedes the prior row. A re-anchor on the same
    worktree leaves exactly one active binding and preserves supersession
    history (no duplicate active rows)."""
    cwd_recorder("sess-repeat", "sub")
    approve("once")
    first = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-repeat")
    # A repeat confirm is a no-op report, not a re-bind.
    repeat = mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-repeat")
    # An explicit re-anchor supersedes the prior binding.
    approve("once")
    second = mods.tool.write_gate_tool(action="reanchor", session_id="sess-repeat")
    d1 = json.loads(first)
    dr = json.loads(repeat)
    d2 = json.loads(second)
    assert d1["success"] is True
    assert d1["status"] == "bound"
    assert dr["success"] is True
    assert dr["status"] == "already_bound"
    assert dr["binding"]["id"] == d1["binding"]["id"]
    assert d2["success"] is True
    active = reg.get_active_binding("sess-repeat")
    assert active is not None
    assert active.id == d2["binding"]["id"]
    assert d1["binding"]["id"] != active.id
    # Exactly one active binding for the session.
    active_count = reg._tx().execute(
        "SELECT COUNT(*) AS n FROM write_gate_bindings "
        "WHERE session_id = ? AND status = 'active'",
        ("sess-repeat",),
    ).fetchone()[0]
    assert active_count == 1
    # Supersession history preserved: the first row points forward.
    first_row = reg.get_binding_by_id(d1["binding"]["id"])
    assert first_row.status == "superseded"
    assert first_row.superseded_by_id == active.id
    # Both binding rows exist; exactly one is active.
    all_rows = reg._tx().execute(
        "SELECT status FROM write_gate_bindings WHERE session_id = ?",
        ("sess-repeat",),
    ).fetchall()
    assert len(all_rows) == 2
    assert sum(1 for (status,) in all_rows if status == "active") == 1


def test_new_with_active_continuity_parent_stays_unbound_tool_side(
    approve, reg, mods, plugin, tmp_path
):
    """/new continuity (a parent row with an active binding, but the child row
    carries the ``_reset_from`` marker) must stay unbound — covered through
    the tool seam too: request_exception on such a session is bind-first."""
    parent_wt = str(tmp_path / "parent-wt")
    Path(parent_wt).mkdir(parents=True, exist_ok=True)
    reg.create_binding(
        session_id="sess-continuity-parent", worktree_path=parent_wt,
        project="proj", board="orchestrator",
    )
    out = mods.tool.write_gate_tool(
        action="request_exception",
        session_id="sess-new-continuity",
        affected_files=["Canon/foo.md"],
        stated_outcome="should not mint without a binding",
    )
    data = json.loads(out)
    assert data["success"] is False
    assert reg.get_active_binding("sess-new-continuity") is None


def test_request_exception_session_id_spoof_ignored(
    cwd_recorder, approve, reg, mods, git_repo
):
    """A model-supplied session_id must not mint authority: the tool only
    accepts the host-owned session id. Calling with no host id (empty) yields
    a bind-first/identity denial and no request row is created for the
    claimed session."""
    cwd_recorder("sess-host", "sub")
    approve("once")
    mods.tool.write_gate_tool(action="confirm_binding", session_id="sess-host")
    out = mods.tool.write_gate_tool(
        action="request_exception",
        session_id="",  # host id absent; model spoof cannot fill it
        affected_files=["Canon/foo.md"],
        stated_outcome="spoof attempt",
    )
    data = json.loads(out)
    assert data["success"] is False
    # No request minted under the spoofed identity.
    rows = reg._tx().execute(
        "SELECT COUNT(*) AS n FROM write_gate_requests WHERE session_id = ?",
        ("sess-host",),
    ).fetchone()
    assert rows[0] == 0


def test_exception_lease_allows_then_blocks_after_expiry(
    monkeypatch, reg, mods, tmp_path
):
    """The five-minute lease window (A30): an out-of-worktree target authorized
    by an active lease is allowed with host approval metadata; the same target
    blocks again once the lease has expired. The lease row carries the
    host-owned approval reference, not a model value."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # The lease's approved folder is the narrowest common parent of the
    # affected files; a real, existing external parent is required for the
    # target to canonicalize (the enforcement contract: a lease authorizes
    # creation under an existing canonical parent, never under a bare name).
    outside = tmp_path / "outside"
    (outside / "shared").mkdir(parents=True)
    reg.create_binding(session_id="sess-lease", worktree_path=str(worktree))
    expected_at = "2026-09-01T10:00:00+00:00"
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
    target = str(outside / "shared" / "policy.md")
    result = json.loads(mods.tool._handle_request_exception(
        "sess-lease",
        {
            "affected_files": [target],
            "stated_outcome": "update policy",
        },
    ))
    assert result["status"] == "lease_created", result
    lease = reg.get_lease(result["lease"]["lease_id"])
    assert lease is not None
    assert lease.approval_reference == "host-approval-42"
    # The approved folder is the narrowest common parent of the affected
    # files — a raw path string, canonicalized at check time (not required to
    # pre-exist).  The enforcement contract for a leased target: it must
    # canonicalize, i.e. its parent directory must exist.
    assert lease.approved_folder == str(outside / "shared")
    (outside / "shared" / "policy.md").write_text("initial\n", encoding="utf-8")
    # Active lease: the out-of-worktree target is allowed, lease-authorized,
    # with the host approval metadata.  The approval timestamp is pinned to a
    # fixed instant; pin the clock on *every* loaded copy of the registry
    # module so the five-minute window is deterministic no matter which copy
    # the enforcement layer consults at call time.
    for mod in sys.modules.values():
        if getattr(mod, "__name__", "") == "writegate.registry":
            monkeypatch.setattr(mod, "utcnow_iso", lambda: expected_at, raising=False)

    # Sanity: the lease must be visible as active under the pinned clock.
    active = reg.list_active_leases(session_id="sess-lease")
    assert len(active) == 1, active
    assert active[0].lease_id == lease.lease_id

    d1 = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": target},
        session_id="sess-lease",
        reg=reg,
        project_root=str(worktree),
    )
    assert d1.allowed, d1.reason
    assert d1.leased

    # A sibling file inside the same approved folder is also covered.
    (outside / "shared" / "other.md").write_text("initial\n", encoding="utf-8")
    d1b = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": str(outside / "shared" / "other.md")},
        session_id="sess-lease",
        reg=reg,
        project_root=str(worktree),
    )
    assert d1b.allowed
    assert d1b.leased

    # A target OUTSIDE the approved folder is not covered by the lease:
    # it must be blocked even though the lease is active.
    (outside / "policy.md").write_text("initial\n", encoding="utf-8")
    d_outside = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": str(outside / "policy.md")},
        session_id="sess-lease",
        reg=reg,
        project_root=str(worktree),
    )
    assert not d_outside.allowed
    assert "no matching active lease" in d_outside.reason

    # Expired lease: force the expiry into the past; the same target blocks
    # again.  Restore the real clock first.
    monkeypatch.undo()
    conn = reg._tx()
    conn.execute(
        "UPDATE write_gate_leases SET expires_at = ? WHERE lease_id = ?",
        ("2026-09-01T09:59:00+00:00", lease.lease_id),
    )
    conn.commit()
    d2 = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": target},
        session_id="sess-lease",
        reg=reg,
        project_root=str(worktree),
    )
    assert not d2.allowed
    assert "no matching active lease" in d2.reason
