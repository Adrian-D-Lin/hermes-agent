"""Tests for the write-gate recovery snapshot writer and the tool entrypoint.

Covers ``writegate/recovery.py`` and
``writegate/tool.py``:

  * ``derive_project_root`` walks up to the ``.git`` directory and falls back
    to the worktree itself for a non-Git path.
  * ``RecoveryWriter.snapshot_existing_file`` copies a file and verifies the
    byte-identical digest.
  * ``RecoveryWriter.snapshot_absent`` records an absence marker.
  * ``write_gate_tool`` returns a JSON error for an unknown action.
"""

import importlib
import json
import os
from pathlib import Path

import pytest

_PACKAGE = "writegate"


@pytest.fixture
def recovery_mods():
    import sys
    repo_root = Path(__file__).resolve().parents[2]
    path = str(repo_root / "plugins" / "write-gate")
    if path not in sys.path:
        sys.path.insert(0, path)
    import writegate  # noqa: F401
    recovery = importlib.import_module(_PACKAGE + ".recovery")
    tool = importlib.import_module(_PACKAGE + ".tool")
    yield type("Mods", (), {"recovery": recovery, "tool": tool})


@pytest.fixture
def reg(tmp_path):
    import sys
    repo_root = Path(__file__).resolve().parents[2]
    path = str(repo_root / "plugins" / "write-gate")
    if path not in sys.path:
        sys.path.insert(0, path)
    import writegate  # noqa: F401
    reg = writegate.registry.set_registry_for_path(str(tmp_path / "write-gate.db"))
    return reg


# -- derive_project_root ------------------------------------------------------

def test_derive_project_root_finds_git(tmp_path, recovery_mods):
    worktree = tmp_path / "proj" / "sub" / "src"
    worktree.mkdir(parents=True)
    (tmp_path / "proj" / ".git").mkdir()
    root = recovery_mods.recovery.derive_project_root(str(worktree))
    assert root == str(tmp_path / "proj")


def test_derive_project_root_falls_back_for_non_git(
    tmp_path, recovery_mods, monkeypatch
):
    worktree = tmp_path / "no-git"
    worktree.mkdir()
    # The constrained test environment may place pytest temp directories
    # beneath a real Git worktree.  Mask only parent ``.git`` discovery so
    # this case remains the intended non-Git binding without changing normal
    # directory existence checks.
    real_isdir = recovery_mods.recovery.os.path.isdir
    monkeypatch.setattr(
        recovery_mods.recovery.os.path,
        "isdir",
        lambda path: False
        if recovery_mods.recovery.os.path.basename(path) == ".git"
        else real_isdir(path),
    )
    root = recovery_mods.recovery.derive_project_root(str(worktree))
    assert root == str(worktree)


def test_derive_project_root_none_for_missing(tmp_path, recovery_mods):
    root = recovery_mods.recovery.derive_project_root(str(tmp_path / "missing"))
    assert root is None


# -- RecoveryWriter -----------------------------------------------------------

def test_snapshot_existing_file_copies_and_verifies(tmp_path, recovery_mods, reg):
    src = tmp_path / "existing.txt"
    src.write_text("hello write-gate\n")
    writer = recovery_mods.recovery.RecoveryWriter(
        reg, str(tmp_path), "sess-X", "lease-1",
    )
    outcome = writer.snapshot_existing_file(str(src))
    assert outcome.ok
    assert len(outcome.evidence_paths) == 1
    dest = outcome.evidence_paths[0]
    assert Path(dest).exists()
    assert Path(dest).read_text() == "hello write-gate\n"


def test_snapshot_existing_file_missing_target(tmp_path, recovery_mods, reg):
    writer = recovery_mods.recovery.RecoveryWriter(
        reg, str(tmp_path), "sess-X", "lease-1",
    )
    outcome = writer.snapshot_existing_file(str(tmp_path / "nope.txt"))
    assert not outcome.ok
    assert "not an existing file" in outcome.error


def test_snapshot_absent_records_marker(tmp_path, recovery_mods, reg):
    writer = recovery_mods.recovery.RecoveryWriter(
        reg, str(tmp_path), "sess-X", "lease-1",
    )
    absent = tmp_path / "not-yet.txt"
    outcome = writer.snapshot_absent(str(absent))
    assert outcome.ok
    assert len(outcome.evidence_paths) == 1


# -- write_gate_tool ----------------------------------------------------------

def test_tool_unknown_action_returns_error(tmp_path, recovery_mods):
    out = recovery_mods.tool.write_gate_tool(action="no_such_action")
    data = json.loads(out)
    assert data["success"] is False
    assert "unknown action" in data["error"]
