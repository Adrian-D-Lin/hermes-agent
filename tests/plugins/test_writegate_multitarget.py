"""Multi-target preflight + recovery tests for ``writegate/enforcement.py``.

Covers brief §9 suite 9 and the multi-target governed-route rules:

* **All-or-nothing preflight** — a multi-file ``patch`` (or V4A move) requires
  authority/recovery for *every* target before any execution; one failing target
  blocks the whole tool call (brief §5 / A7/A8).
* **Every governed target gets recovery evidence** on the leased path.
* **Move/rename governs both source and destination** (brief §5).
* **Exact canonical lease worktree equality** — a broader lease must not accept a
  narrower target (A12).

These tests drive :func:`enforcement.decide` end to end against a real registry
with a recovery writer factory, so the recovery path is exercised for real.
"""

import importlib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = "writegate"


def _utcnow_iso():
    """Lazy import of the host package's timestamp helper."""
    from writegate.registry import utcnow_iso
    return utcnow_iso()


@pytest.fixture
def mods(monkeypatch):
    """Import the ``writegate.*`` modules fresh from the repo-root host
    package (no plugin-private ``sys.path`` insertion)."""
    import importlib
    import sys
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]
    enforcement = importlib.import_module(_PACKAGE + ".enforcement")
    registry_mod = importlib.import_module(_PACKAGE + ".registry")
    recovery_mod = importlib.import_module(_PACKAGE + ".recovery")
    yield type("Mods", (), {
        "enforcement": enforcement,
        "registry": registry_mod,
        "recovery": recovery_mod,
    })
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]


@pytest.fixture
def reg(tmp_path, mods):
    return mods.registry.set_registry_for_path(str(tmp_path / "write-gate.db"))


@pytest.fixture
def project(tmp_path):
    """A real project root with Canon / 4-artifacts / 5-archive dirs."""
    root = tmp_path / "proj"
    (root / "Canon").mkdir(parents=True)
    (root / "4-artifacts").mkdir(parents=True)
    (root / "5-archive").mkdir(parents=True)
    (root / "work").mkdir(parents=True)
    return str(root)


def test_in_worktree_write_allowed_without_lease(reg, mods, project):
    """A7: an ordinary write inside the bound worktree is allowed without a lease."""
    reg.create_binding(session_id="sess", worktree_path=str(Path(project) / "work"))
    d = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": str(Path(project) / "work" / "new.txt")},
        session_id="sess",
        reg=reg,
        project_root=project,
        base_dir=project,
    )
    assert d.allowed
    assert not d.leased


def test_multi_target_one_bad_blocks_whole_call(reg, mods, project):
    """A7/A8: a multi-file patch where one target is protected (Canon) and one
    is inside the worktree must fail the whole call — no partial execution."""
    reg.create_binding(session_id="sess", worktree_path=str(Path(project) / "work"))
    d = mods.enforcement.decide(
        tool_name="patch",
        args={
            "patch": (
                "*** Update File: Canon/policy.md\n"
                "*** Update File: work/allowed.txt\n"
            ),
        },
        session_id="sess",
        reg=reg,
        project_root=project,
        base_dir=project,
    )
    # Canon target is protected and has no lease -> whole call blocked.
    assert not d.allowed
    # No recovery evidence was written for the good target either — the whole
    # preflight fails before any execution.
    archive = Path(project) / "5-archive" / "write-gate" / "sess"
    assert not archive.exists()


def test_multi_target_all_good_allowed_with_recovery(reg, mods, project):
    """A multi-file patch whose targets are inside the worktree is allowed
    without a lease; in-worktree writes need no recovery evidence.

    A second, out-of-worktree target requires a matching lease and produces
    recovery evidence before execution.
    """
    reg.create_binding(session_id="sess", worktree_path=str(Path(project) / "work"))
    (Path(project) / "work" / "a.txt").write_text("old a")
    # A lease covering Canon so the protected target is governed.
    reg.create_lease(
        lease_id="lease-canon",
        approval_reference="ref",
        session_id="sess",
        confirmed_worktree=str(Path(project) / "work"),
        approved_folder=str(Path(project) / "Canon"),
        approved_at=_utcnow_iso(),
    )
    d = mods.enforcement.decide(
        tool_name="patch",
        args={
            "patch": (
                "*** Update File: work/a.txt\n"
                "*** Update File: Canon/note.md\n"
            ),
        },
        session_id="sess",
        reg=reg,
        project_root=project,
        base_dir=project,
        recovery_writer_factory=lambda worktree, session_id, lease_id: mods.recovery.RecoveryWriter(
            reg, project, session_id, lease_id
        ),
    )
    assert d.allowed
    # Recovery evidence exists for the out-of-worktree (leased) target.
    evidence = list((Path(project) / "5-archive" / "write-gate" / "sess").glob("*"))
    assert len(evidence) == 1


def test_move_governs_source_and_destination(reg, mods, project):
    """A move governs BOTH endpoints: the source is inside the worktree but the
    destination is protected (Canon) -> blocked."""
    reg.create_binding(session_id="sess", worktree_path=str(Path(project) / "work"))
    (Path(project) / "work" / "src.txt").write_text("data")
    d = mods.enforcement.decide(
        tool_name="patch",
        args={
            "patch": (
                "*** Move File: work/src.txt -> Canon/moved.txt\n"
            ),
        },
        session_id="sess",
        reg=reg,
        project_root=project,
        base_dir=project,
    )
    assert not d.allowed


def test_lease_exact_worktree_equality(reg, mods, project):
    """A12: a lease whose confirmed_worktree is *broader* than the bound
    worktree must NOT authorize a write to a target outside the bound worktree.
    Matching lease worktree must be exact canonical equality.

    The target is outside the bound worktree (``proj/Canon``), so ordinary
    in-worktree authority does not apply; only a matching lease would authorize
    it.  The broader lease (``confirmed_worktree=proj``) does not exactly match
    the bound ``proj/work``, so it must be rejected.
    """
    reg.create_binding(session_id="sess", worktree_path=str(Path(project) / "work"))
    reg.create_lease(
        lease_id="lease-broad",
        approval_reference="ref",
        session_id="sess",
        confirmed_worktree=project,
        approved_folder=project,
        approved_at=_utcnow_iso(),
    )
    d = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": str(Path(project) / "Canon" / "x.txt")},
        session_id="sess",
        reg=reg,
        project_root=project,
        base_dir=project,
    )
    assert not d.allowed


def test_lease_matching_scope_allows(reg, mods, project):
    """A9/A12: a lease whose folder contains the target authorizes the write."""
    reg.create_binding(session_id="sess", worktree_path=str(Path(project) / "work"))
    reg.create_lease(
        lease_id="lease-canon",
        approval_reference="ref",
        session_id="sess",
        confirmed_worktree=str(Path(project) / "work"),
        approved_folder=str(Path(project) / "Canon"),
        approved_at=_utcnow_iso(),
    )
    d = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": str(Path(project) / "Canon" / "note.md")},
        session_id="sess",
        reg=reg,
        project_root=project,
        base_dir=project,
        recovery_writer_factory=lambda worktree, session_id, lease_id: mods.recovery.RecoveryWriter(
            reg, project, session_id, lease_id
        ),
    )
    assert d.allowed
    assert d.leased
    archive = Path(project) / "5-archive" / "write-gate" / "sess"
    assert archive.exists()


def test_scope_mismatch_lease_denies(reg, mods, project):
    """A12: a lease scoped to one Canon subfolder must not authorize a write to
    a different Canon subfolder."""
    reg.create_binding(session_id="sess", worktree_path=str(Path(project) / "work"))
    reg.create_lease(
        lease_id="lease-canon-a",
        approval_reference="ref",
        session_id="sess",
        confirmed_worktree=str(Path(project) / "work"),
        approved_folder=str(Path(project) / "Canon" / "a"),
        approved_at=_utcnow_iso(),
    )
    (Path(project) / "Canon" / "a").mkdir(parents=True)
    d = mods.enforcement.decide(
        tool_name="write_file",
        args={"path": str(Path(project) / "Canon" / "b" / "note.md")},
        session_id="sess",
        reg=reg,
        project_root=project,
        base_dir=project,
    )
    assert not d.allowed
