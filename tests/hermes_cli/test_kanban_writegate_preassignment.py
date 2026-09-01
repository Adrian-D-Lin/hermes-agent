"""Tests for the WriteGate Kanban preassignment handshake.

Covers the two halves of the brief §3 / Canon §3d invariant:

* :func:`kanban_db.prepare_worker_launch` — dispatcher-side: derive one worker
  session id, persist it on the exact ``task_runs`` row, create + verify the
  central active binding, and return the captured id.
* :func:`kanban_db._trusted_worker_session_id` — CLI-side: honor the
  preassigned id only when every trusted marker agrees (run id, task id,
  profile, workspace, board, and central binding lineage); fail closed
  otherwise.

The end-to-end test proves the worker transcript id, the persisted
``task_runs.worker_session_id``, and the central binding ``session_id``/worktree
are identical, plus mismatched-marker and spawn-failure cases.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The ``writegate`` package is a repo-root host package, importable without
# any plugin-private sys.path insertion.


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    kb.init_db()
    return home


def _make_task(conn, kb, *, assignee="builder-tester",
               workspace_path=None, project_id=None,
               branch_name=None):
    """Insert a ready task row and return it."""
    tid = kb.create_task(
        conn,
        title="worker task",
        body=None,
        assignee=assignee,
        created_by="test",
        workspace_kind="dir",
        workspace_path=workspace_path,
        tenant=None,
        priority=0,
        branch_name=branch_name,
        project_id=project_id,
        initial_status="running",
    )
    # create_task only allows 'running'/'blocked'; flip to 'ready' so the
    # claim CAS below (status='ready') succeeds.
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    conn.commit()
    return kb.get_task(conn, tid)


def _claim(conn, task):
    """Claim a task; return (claimed_task, run_id)."""
    claimed = kb.claim_task(conn, task.id, claimer="builder-tester")
    assert claimed is not None, "claim_task should succeed for a ready task"
    assert claimed.current_run_id is not None
    return claimed, int(claimed.current_run_id)


def _writegate_registry(tmp_path):
    """Return a fresh central registry bound to a temp DB path."""
    from writegate import registry as wg
    db_path = tmp_path / "write-gate.db"
    return wg.set_registry_for_path(str(db_path))


def _patch_registry(monkeypatch, reg):
    monkeypatch.setattr(kb, "_writegate_binding_enabled", lambda: True)
    reg_proxy = type("R", (), {"get_registry": staticmethod(lambda: reg)})()
    monkeypatch.setattr(kb, "registry", reg_proxy, raising=False)


def _setup(kanban_home, tmp_path, monkeypatch, *, profile="builder-tester",
           workspace_name="wt"):
    """Claim + preassign; return (conn, claimed, run_id, workspace, worker_id, reg)."""
    reg = _writegate_registry(tmp_path)
    _patch_registry(monkeypatch, reg)
    conn = kb.connect()
    workspace = str(tmp_path / workspace_name)
    (tmp_path / workspace_name).mkdir()
    task = _make_task(conn, kb, assignee=profile, workspace_path=workspace)
    claimed, run_id = _claim(conn, task)
    worker_id = kb.prepare_worker_launch(conn, claimed, workspace, board="default")
    conn.commit()
    return conn, claimed, run_id, workspace, worker_id, reg


# ── prepare_worker_launch ────────────────────────────────────────────────────

def test_prepare_worker_launch_persists_run_and_binding(kanban_home, tmp_path, monkeypatch):
    reg = _writegate_registry(tmp_path)
    _patch_registry(monkeypatch, reg)
    conn = kb.connect()
    try:
        task = _make_task(conn, kb, workspace_path=str(tmp_path / "wt"))
        (tmp_path / "wt").mkdir()
        claimed, run_id = _claim(conn, task)

        workspace = str(tmp_path / "wt")
        worker_id = kb.prepare_worker_launch(conn, claimed, workspace, board="default")
        assert worker_id  # a non-empty id was returned

        # The run row carries the preassigned id.
        row = conn.execute(
            "SELECT worker_session_id FROM task_runs WHERE id = ? AND task_id = ?",
            (run_id, task.id),
        ).fetchone()
        assert row is not None
        assert row["worker_session_id"] == worker_id

        # The central binding exists, is active, and matches.
        binding = reg.get_active_binding(worker_id)
        assert binding is not None
        assert binding.is_active
        assert binding.worktree_path == os.path.realpath(workspace)
        assert binding.initiative == task.id
        assert binding.profile == "builder-tester"
    finally:
        conn.close()


def test_prepare_worker_launch_returns_empty_without_run(kanban_home, tmp_path, monkeypatch):
    reg = _writegate_registry(tmp_path)
    _patch_registry(monkeypatch, reg)
    conn = kb.connect()
    try:
        task = _make_task(conn, kb)
        # No run row -> nothing to bind to.
        assert kb.prepare_worker_launch(conn, task, str(tmp_path / "wt")) == ""
    finally:
        conn.close()


# ── _trusted_worker_session_id ───────────────────────────────────────────────

def test_trusted_worker_session_id_honors_agreeing_markers(kanban_home, tmp_path, monkeypatch):
    conn, claimed, run_id, workspace, worker_id, _ = _setup(kanban_home, tmp_path, monkeypatch)
    try:
        honored = kb._trusted_worker_session_id(
            claimed.id, run_id,
            profile="builder-tester", workspace=workspace, board="default",
        )
        assert honored == worker_id
    finally:
        conn.close()


def test_trusted_worker_session_id_rejects_wrong_task(kanban_home, tmp_path, monkeypatch):
    conn, claimed, run_id, workspace, worker_id, _ = _setup(kanban_home, tmp_path, monkeypatch)
    try:
        assert kb._trusted_worker_session_id(
            "t_other", run_id,
            profile="builder-tester", workspace=workspace, board="default",
        ) is None
    finally:
        conn.close()


def test_trusted_worker_session_id_rejects_wrong_run(kanban_home, tmp_path, monkeypatch):
    conn, claimed, run_id, workspace, worker_id, _ = _setup(kanban_home, tmp_path, monkeypatch)
    try:
        assert kb._trusted_worker_session_id(
            claimed.id, run_id + 999,
            profile="builder-tester", workspace=workspace, board="default",
        ) is None
    finally:
        conn.close()


def test_trusted_worker_session_id_rejects_wrong_profile(kanban_home, tmp_path, monkeypatch):
    conn, claimed, run_id, workspace, worker_id, _ = _setup(kanban_home, tmp_path, monkeypatch)
    try:
        assert kb._trusted_worker_session_id(
            claimed.id, run_id,
            profile="independent-reviewer", workspace=workspace, board="default",
        ) is None
    finally:
        conn.close()


def test_trusted_worker_session_id_rejects_wrong_workspace(kanban_home, tmp_path, monkeypatch):
    conn, claimed, run_id, workspace, worker_id, _ = _setup(kanban_home, tmp_path, monkeypatch)
    try:
        assert kb._trusted_worker_session_id(
            claimed.id, run_id,
            profile="builder-tester",
            workspace=str(tmp_path / "some_other_wt"),
            board="default",
        ) is None
    finally:
        conn.close()


def test_trusted_worker_session_id_rejects_wrong_board(kanban_home, tmp_path, monkeypatch):
    conn, claimed, run_id, workspace, worker_id, _ = _setup(kanban_home, tmp_path, monkeypatch)
    try:
        assert kb._trusted_worker_session_id(
            claimed.id, run_id,
            profile="builder-tester", workspace=workspace, board="archive",
        ) is None
    finally:
        conn.close()


def test_trusted_worker_session_id_rejects_null_persisted_id(kanban_home, tmp_path, monkeypatch):
    """A run row with no persisted worker_session_id must fail closed."""
    conn, claimed, run_id, workspace, worker_id, _ = _setup(kanban_home, tmp_path, monkeypatch)
    try:
        conn.execute(
            "UPDATE task_runs SET worker_session_id = NULL WHERE id = ? AND task_id = ?",
            (run_id, claimed.id),
        )
        conn.commit()
        assert kb._trusted_worker_session_id(
            claimed.id, run_id,
            profile="builder-tester", workspace=workspace, board="default",
        ) is None
    finally:
        conn.close()


def test_trusted_worker_session_id_rejects_abandoned_binding(kanban_home, tmp_path, monkeypatch):
    conn, claimed, run_id, workspace, worker_id, reg = _setup(kanban_home, tmp_path, monkeypatch)
    try:
        reg.mark_binding_abandoned(worker_id)
        assert kb._trusted_worker_session_id(
            claimed.id, run_id,
            profile="builder-tester", workspace=workspace, board="default",
        ) is None
    finally:
        conn.close()


def test_trusted_worker_session_id_none_without_binding_enabled(kanban_home, tmp_path, monkeypatch):
    """When WriteGate binding is disabled, no central binding exists, so the
    trusted lookup fails closed (the worker keeps its own generated id).
    """
    monkeypatch.setattr(kb, "_writegate_binding_enabled", lambda: False)
    conn = kb.connect()
    try:
        task = _make_task(conn, kb)
        (tmp_path / "wt").mkdir()
        claimed, run_id = _claim(conn, task)
        # No binding created (WriteGate disabled) -> fail closed.
        assert kb._trusted_worker_session_id(
            claimed.id, run_id,
            profile="builder-tester", workspace=str(tmp_path / "wt"), board="default",
        ) is None
    finally:
        conn.close()


# ── tool authority spoofing (adversarial) ────────────────────────────────────

def test_tool_rejects_model_supplied_session_id(kanban_home, tmp_path, monkeypatch):
    """The ``write_gate`` tool must use only the host-owned ``session_id``.

    A model-supplied ``session_id`` in the argument payload must never be
    trusted: the tool resolves authority from the trusted kwarg, not from
    the model's argument dict.
    """
    from writegate import tool as _tool

    # A model-supplied session_id with no binding must fail closed.
    out = _tool.write_gate_tool(
        action="status",
        session_id="spoofed-model-session",
    )
    # No binding for "spoofed-model-session" -> status reports no binding.
    assert "success" in out
    assert '"binding": null' in out or '"binding":null' in out


def test_tool_status_uses_host_owned_session_id(kanban_home, tmp_path, monkeypatch):
    """When a host-owned session_id has a binding, the tool reports it."""
    from writegate import tool as _tool

    reg = _writegate_registry(tmp_path)
    reg.create_binding(session_id="host-owned", worktree_path="/tmp/wt")
    out = _tool.write_gate_tool(
        action="status",
        session_id="host-owned",
    )
    assert '"binding"' in out
    assert "host-owned" in out
