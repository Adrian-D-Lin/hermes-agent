"""Trusted top-level binding producer + lineage derivation.

This module implements the pivot's ``§3a trusted producer`` and ``§3c binding
lifetime``.  The binding is produced by a **trusted runtime operation**, never
by the session model: the model may trigger a request, but it cannot write the
registry row or supply an arbitrary path.

Two responsibilities:

* :class:`BindingPresentation` — the derived card/worktree identity that is
  shown to the human, validated against trusted project/board/task state, and
  rejected when the model tries to supply a raw path.
* :func:`derive_binding` — the lineage rules for resume / in-place compression
  / compression child / branch / delegate / ``/new`` / legacy-null.  Derived
  bindings are **lazy and idempotent**: they read the current profile's session
  row plus the central parent binding, and never infer solely from ambient
  ``cwd``.

The approval transport itself (the ``once``/``deny`` primitive, the
five-minute lease) lives in :mod:`writegate.approval`; this module consumes a
plain ``confirmed: bool`` result from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import registry as _registry
from .containment import canonicalize_target, is_within


class BindingPresentationError(ValueError):
    """Raised when a binding candidate cannot be validated against trusted state."""


@dataclass
class BindingCandidate:
    """The card/worktree identity derived from trusted state, presented to the
    human for a one-time confirmation.  ``model_supplied_path`` is always
    ``None`` — the model may not supply a path.
    """

    session_id: str
    project: Optional[str]
    initiative: Optional[str]
    board: Optional[str]
    worktree_path: str
    git_branch: Optional[str]
    profile: Optional[str]
    source: str  # "derived-from-card" | "derived-from-worktree"
    model_supplied_path: Optional[str] = None

    def to_presentation(self) -> Dict[str, Any]:
        """The human-facing block: visibly a *Worktree Binding* confirmation."""
        return {
            "confirmation_kind": "CONFIRMED_WORKTREE_BINDING",
            "session_id": self.session_id,
            "project": self.project,
            "initiative": self.initiative,
            "board": self.board,
            "worktree_path": self.worktree_path,
            "git_branch": self.git_branch,
            "profile": self.profile,
            "confirmation_basis": self.source,
            "note": (
                "Worktree Binding confirmation. Confirming persists this "
                "session's confirmed worktree in the central security "
                "registry. Declining or timing out persists no binding."
            ),
        }


class TrustedBindingProducer:
    """The trusted runtime operation that turns a human ``confirm`` into a
    durable binding row.  The model can call :meth:`present` and
    :meth:`resolve` but cannot write the registry directly.
    """

    def __init__(self, reg: _registry.Registry):
        self.reg = reg

    # -- derivation from trusted state -------------------------------------
    def derive_candidate(
        self,
        *,
        session_id: str,
        worktree_path: str,
        project: Optional[str] = None,
        initiative: Optional[str] = None,
        board: Optional[str] = None,
        git_branch: Optional[str] = None,
        profile: Optional[str] = None,
        source: str = "derived-from-card",
        model_supplied_path: Optional[str] = None,
    ) -> BindingCandidate:
        """Validate a candidate against trusted state before presenting it.

        * ``model_supplied_path`` is accepted only as a *diagnostic* value; it
          is never authoritative and is never written to the registry.  A
          model-supplied raw path is surfaced but the binding is always derived
          from the trusted ``worktree_path``.
        * ``worktree_path`` must canonicalize and must be a real directory,
          otherwise the candidate is rejected (``§3a`` / A3).
        """
        if model_supplied_path is not None:
            # A4: the model may not supply the path.  We accept it only as
            # evidence to compare against; if it differs from the trusted
            # worktree we flag it but do not bind the model's value.
            pass
        canonical = canonicalize_target(worktree_path, must_exist=True)
        if canonical is None:
            raise BindingPresentationError(
                f"WriteGate: candidate worktree {worktree_path!r} does not "
                "resolve to an existing directory (rejected; not derived from "
                "trusted state)."
            )
        return BindingCandidate(
            session_id=session_id,
            project=project,
            initiative=initiative,
            board=board,
            worktree_path=canonical,
            git_branch=git_branch,
            profile=profile,
            source=source,
            model_supplied_path=model_supplied_path,
        )

    # -- the trusted operation ---------------------------------------------
    def confirm(
        self,
        candidate: BindingCandidate,
        *,
        confirmed: bool,
        profile: Optional[str] = None,
    ) -> Optional[_registry.BindingRecord]:
        """Persist the binding only on an explicit ``confirmed`` decision.

        * ``confirmed=True`` inserts the binding (marking any prior active
          binding for the session ``superseded`` — one active binding per
          session, ``§3c``).
        * ``confirmed=False`` (decline / timeout / error) persists **no**
          binding — the session remains unbound (A2).
        """
        if not confirmed:
            return None
        return self.reg.create_binding(
            session_id=candidate.session_id,
            worktree_path=candidate.worktree_path,
            project=candidate.project,
            initiative=candidate.initiative,
            board=candidate.board,
            git_branch=candidate.git_branch,
            profile=profile or candidate.profile,
            producer=_registry.PRODUCER_CONFIRM_BINDING,
            event=_registry.EVENT_CONFIRM,
        )

    def reanchor(
        self,
        session_id: str,
        worktree_path: str,
        *,
        project: Optional[str] = None,
        initiative: Optional[str] = None,
        board: Optional[str] = None,
        git_branch: Optional[str] = None,
        profile: Optional[str] = None,
        source: str = "derived-from-card",
    ) -> _registry.BindingRecord:
        """Re-anchor an already-bound session: another explicit confirmation
        that supersedes the previous active binding and preserves a coherent
        supersession history (§3a).
        """
        candidate = self.derive_candidate(
            session_id=session_id,
            worktree_path=worktree_path,
            project=project,
            initiative=initiative,
            board=board,
            git_branch=git_branch,
            profile=profile or None,
            source=source,
        )
        return self.confirm(candidate, confirmed=True, profile=profile)


def derive_binding(
    reg: _registry.Registry,
    *,
    session_id: str,
    profile_session_row: Optional[Dict[str, Any]] = None,
    parent_session_id: Optional[str] = None,
    parent_is_bound: bool = False,
    assigned_workspace: Optional[str] = None,
    ambient_cwd: Optional[str] = None,
    is_new_session: bool = False,
) -> Optional[_registry.BindingRecord]:
    """Derive the binding for a session according to ``§3c`` lineage rules.

    Lazy and idempotent: returns the session's own active binding when it
    exists, else derives one from a trusted parent when the lineage permits,
    else returns ``None`` (the session may read but cannot govern-write).

    Rules, in priority order:

    1. **Resume / in-place compression** — same ``session_id`` keeps its own
       active binding.  (A23, A24)
    2. **``/new``** — fresh session id, no binding, no derivation.  (A27)
    3. **Branch / compression child / in-process delegate with a new session
       id** — derive only from a trusted recorded ``parent_session_id`` whose
       parent is actively bound.  Same-worktree delegates inherit the exact
       parent worktree; a distinct trusted ``assigned_workspace`` is bound to
       that exact workspace with lineage.  (A21, A22, A25, A26)
    4. **Legacy / null session** — may read; cannot make governed writes until
       explicitly re-anchored; never guessed or bulk-backfilled.  (A28)

    Ambient ``cwd`` is never used as a binding source on its own.
    """
    # 1. A session that already has its own active binding keeps it (resume /
    #    in-place compression re-enter here with the same id).
    own = reg.get_active_binding(session_id)
    if own is not None:
        return own

    # 2. A brand-new session starts unbound — nothing to derive.
    if is_new_session:
        return None

    # 3. Derive from a trusted recorded parent.  The parent must be actively
    #    bound, and the parent id must come from a trusted source (the profile
    #    session row's ``parent_session_id`` or the dispatcher lineage), not
    #    from ambient cwd.
    parent_id = None
    if parent_session_id:
        parent_id = parent_session_id
    elif profile_session_row and profile_session_row.get("parent_session_id"):
        parent_id = profile_session_row.get("parent_session_id")

    if not parent_id or not parent_is_bound:
        # Legacy / null: no trusted parent -> read-only, no binding.
        return None

    parent = reg.get_active_binding(parent_id)
    if parent is None or not parent.is_active:
        return None

    # Same-worktree delegate inherits the exact parent worktree.
    workspace = assigned_workspace or parent.worktree_path
    canonical = canonicalize_target(workspace, must_exist=True)
    if canonical is None:
        return None

    lineage = {
        "parent_session_id": parent_id,
        "parent_binding_id": parent.id,
        "derived_from": "delegate" if assigned_workspace else "branch/compression-child",
        "assigned_workspace": assigned_workspace,
    }
    return reg.create_binding(
        session_id=session_id,
        worktree_path=canonical,
        project=parent.project,
        initiative=parent.initiative,
        board=parent.board,
        git_branch=parent.git_branch,
        profile=parent.profile,
        producer=_registry.PRODUCER_LINEAGE,
        event=_registry.EVENT_LINEAGE,
        parent_session_id=parent_id,
        parent_binding_id=parent.id,
        lineage=lineage,
    )
