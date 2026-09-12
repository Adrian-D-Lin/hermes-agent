"""Lineage-derivation tests for ``writegate/binding.py``.

Covers the ``§3c`` lineage rules from the WriteGate policy (Canon A21-A28):

  * **Resume / in-place compression** — the same ``session_id`` keeps its own
    active binding (A23, A24).
  * **``/new``** — a fresh session id starts unbound, no derivation (A27).
  * **Branch / compression child / delegate with a new session id** — derive
    only from a trusted recorded ``parent_session_id`` whose parent is
    actively bound; same-worktree delegates inherit the parent worktree, a
    distinct trusted ``assigned_workspace`` is bound to that exact workspace
    with lineage (A21, A22, A25, A26).
  * **Legacy / null session** — no trusted parent -> read-only, no binding
    (A28).

These are pure-derivation tests: they exercise :func:`derive_binding` against a
fresh registry with a controlled parent binding, mirroring how the runtime
feeds the profile session row + parent binding into the derivation.
"""

import importlib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = "writegate"


@pytest.fixture
def mods(monkeypatch):
    import sys
    parent_path = str(_REPO_ROOT / "plugins" / "write-gate")
    if parent_path not in sys.path:
        sys.path.insert(0, parent_path)
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]
    binding = importlib.import_module(_PACKAGE + ".binding")
    registry_mod = importlib.import_module(_PACKAGE + ".registry")
    yield type("Mods", (), {"binding": binding, "registry": registry_mod})
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]
    if parent_path in sys.path:
        sys.path.remove(parent_path)


@pytest.fixture
def reg(tmp_path, mods):
    return mods.registry.set_registry_for_path(str(tmp_path / "write-gate.db"))


def _bind(mods, reg, tmp_path, session_id, worktree, *, parent_session_id=None,
          project="proj", board="orchestrator", profile="default"):
    """Create a parent binding at a real temp worktree."""
    wt = str(tmp_path / worktree)
    Path(wt).mkdir(parents=True, exist_ok=True)
    return reg.create_binding(
        session_id=session_id,
        worktree_path=wt,
        project=project,
        board=board,
        profile=profile,
        parent_session_id=parent_session_id,
    )


def test_resume_same_session_keeps_binding(reg, mods, tmp_path):
    """A23/A24: resuming or re-entering with the same id returns its own
    active binding — nothing is derived, nothing is lost."""
    own = _bind(mods, reg, tmp_path, "sess-resume", "wt-resume")
    derived = mods.binding.derive_binding(
        reg, session_id="sess-resume", is_new_session=False,
    )
    assert derived is not None
    assert derived.id == own.id
    assert derived.worktree_path == own.worktree_path


def test_new_session_starts_unbound(reg, mods, tmp_path):
    """A27: a brand-new session id has no binding and derives nothing."""
    assert mods.binding.derive_binding(
        reg, session_id="sess-new", is_new_session=True,
    ) is None
    assert reg.get_active_binding("sess-new") is None


def test_branch_child_derives_from_trusted_parent(reg, mods, tmp_path):
    """A26: a branch/compression child with a new session id derives a binding
    from a trusted recorded parent whose parent is actively bound."""
    parent = _bind(mods, reg, tmp_path, "sess-parent", "wt-parent")
    derived = mods.binding.derive_binding(
        reg,
        session_id="sess-branch",
        parent_session_id="sess-parent",
        parent_is_bound=True,
    )
    assert derived is not None
    assert derived.session_id == "sess-branch"
    # Same worktree inherited from the parent.
    assert derived.worktree_path == parent.worktree_path
    # Lineage recorded back to the trusted parent.
    assert derived.parent_session_id == "sess-parent"
    assert derived.parent_binding_id == parent.id
    assert derived.producer == mods.registry.PRODUCER_LINEAGE


def test_delegate_same_worktree_inherits_parent(reg, mods, tmp_path):
    """A21: an in-process delegate with the same worktree inherits the exact
    parent worktree via lineage."""
    parent = _bind(mods, reg, tmp_path, "sess-parent", "wt-parent")
    derived = mods.binding.derive_binding(
        reg,
        session_id="sess-delegate",
        parent_session_id="sess-parent",
        parent_is_bound=True,
        assigned_workspace=None,  # None -> inherit parent worktree
    )
    assert derived is not None
    assert derived.worktree_path == parent.worktree_path
    assert derived.parent_session_id == "sess-parent"


def test_delegate_isolated_workspace_bound_exact(reg, mods, tmp_path):
    """A22: a delegate with a distinct trusted workspace is bound to that exact
    workspace with lineage — not the parent's worktree."""
    parent = _bind(mods, reg, tmp_path, "sess-parent", "wt-parent")
    isolated = str(tmp_path / "wt-isolated")
    Path(isolated).mkdir(parents=True, exist_ok=True)
    derived = mods.binding.derive_binding(
        reg,
        session_id="sess-delegate2",
        parent_session_id="sess-parent",
        parent_is_bound=True,
        assigned_workspace=isolated,
    )
    assert derived is not None
    assert derived.worktree_path == isolated
    assert derived.parent_session_id == "sess-parent"


def test_logical_candidate_presents_and_persists_all_canonical_roots(
    reg, mods, tmp_path
):
    member_a = tmp_path / "coord" / "repo-a"
    member_b = tmp_path / "coord" / "repo-b"
    member_a.mkdir(parents=True)
    member_b.mkdir(parents=True)
    producer = mods.binding.TrustedBindingProducer(reg)
    candidate = producer.derive_candidate(
        session_id="sess-logical",
        worktree_path=str(member_a),
        logical_workspace_id="coord-init-1",
        member_roots=(str(member_a), str(member_b)),
        binding_version=2,
    )
    presentation = candidate.to_presentation()
    assert presentation["member_roots"] == [
        str(member_a.resolve()),
        str(member_b.resolve()),
    ]
    record = producer.confirm(candidate, confirmed=True)
    assert record.logical_workspace_id == "coord-init-1"
    assert record.member_roots == (
        str(member_a.resolve()),
        str(member_b.resolve()),
    )
    assert record.binding_version == 2


def test_branch_child_inherits_complete_logical_binding(reg, mods, tmp_path):
    member_a = tmp_path / "coord" / "repo-a"
    member_b = tmp_path / "coord" / "repo-b"
    member_a.mkdir(parents=True)
    member_b.mkdir(parents=True)
    parent = reg.create_binding(
        session_id="sess-logical-parent",
        worktree_path=str(member_a.resolve()),
        logical_workspace_id="coord-init-1",
        member_roots=(str(member_a.resolve()), str(member_b.resolve())),
        binding_version=4,
    )
    child = mods.binding.derive_binding(
        reg,
        session_id="sess-logical-child",
        parent_session_id=parent.session_id,
        parent_is_bound=True,
    )
    assert child.logical_workspace_id == parent.logical_workspace_id
    assert child.member_roots == parent.member_roots
    assert child.binding_version == parent.binding_version


def test_distinct_delegate_from_logical_parent_is_narrow_single_root(
    reg, mods, tmp_path
):
    member_a = tmp_path / "coord" / "repo-a"
    member_b = tmp_path / "coord" / "repo-b"
    isolated = tmp_path / "segment" / "repo-a"
    member_a.mkdir(parents=True)
    member_b.mkdir(parents=True)
    isolated.mkdir(parents=True)
    parent = reg.create_binding(
        session_id="sess-logical-parent",
        worktree_path=str(member_a.resolve()),
        logical_workspace_id="coord-init-1",
        member_roots=(str(member_a.resolve()), str(member_b.resolve())),
        binding_version=4,
    )
    child = mods.binding.derive_binding(
        reg,
        session_id="sess-isolated-child",
        parent_session_id=parent.session_id,
        parent_is_bound=True,
        assigned_workspace=str(isolated),
    )
    assert child.logical_workspace_id is None
    assert child.member_roots == (str(isolated.resolve()),)
    assert child.binding_version is None


def test_parent_not_bound_yields_no_binding(reg, mods, tmp_path):
    """A parent that is not actively bound must not authorize a child binding."""
    _bind(mods, reg, tmp_path, "sess-parent", "wt-parent")
    # Mark the parent superseded (no longer active) — a stale parent cannot
    # authorize a new child.
    reg.create_binding(session_id="sess-parent", worktree_path="/wt/parent-old")
    assert mods.binding.derive_binding(
        reg,
        session_id="sess-child",
        parent_session_id="sess-parent",
        parent_is_bound=True,
    ) is None


def test_legacy_null_session_reads_only(reg, mods, tmp_path):
    """A28: a session with no trusted parent and no own binding derives nothing
    (read-only); it is never guessed or backfilled."""
    assert reg.get_active_binding("sess-legacy") is None
    assert mods.binding.derive_binding(
        reg,
        session_id="sess-legacy",
        parent_session_id=None,
        parent_is_bound=False,
    ) is None


def test_ambient_cwd_never_used_as_source(reg, mods, tmp_path):
    """Derivation must never infer a binding from ambient cwd alone."""
    # No parent, no own binding, cwd is irrelevant.
    assert mods.binding.derive_binding(
        reg,
        session_id="sess-cwd",
        ambient_cwd=str(tmp_path),
        parent_session_id=None,
        parent_is_bound=False,
    ) is None


def test_confirmed_binding_supersedes_and_preserves_history(reg, mods, tmp_path):
    """Re-anchor: a second confirmation supersedes the prior active binding and
    preserves a coherent supersession history (A21 lineage + A2)."""
    first = _bind(mods, reg, tmp_path, "sess-anchor", "wt-1")
    # Re-anchor to a new worktree.
    producer = mods.binding.TrustedBindingProducer(reg)
    Path(tmp_path / "wt-2").mkdir(parents=True, exist_ok=True)
    candidate = producer.derive_candidate(
        session_id="sess-anchor", worktree_path=str(tmp_path / "wt-2"),
    )
    second = producer.confirm(candidate, confirmed=True)
    assert second is not None
    active = reg.get_active_binding("sess-anchor")
    assert active is not None
    assert active.id == second.id
    # Exactly one active, one superseded.
    statuses = [r.status for r in reg.list_bindings("sess-anchor")]
    assert statuses.count("active") == 1
    assert statuses.count("superseded") == 1
    # The superseded row points forward to the new row.
    prior = [r for r in reg.list_bindings("sess-anchor") if r.status == "superseded"][0]
    assert prior.superseded_by_id == second.id
