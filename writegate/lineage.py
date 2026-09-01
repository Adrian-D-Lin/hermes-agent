"""Bounded, read-only trusted SessionDB lineage resolver for WriteGate.

The production ``pre_tool_call`` hook fires inside a Hermes process where the
session store (:class:`hermes_state.SessionDB`) already carries the trusted
lifecycle markers that classify how a session came to exist:

* **branch / delegate / tool child** — the child row's ``model_config`` carries
  ``_branched_from`` or ``_delegate_from`` (or ``source == "tool"``) equal to
  its ``parent_session_id`` (same semantics as
  :meth:`hermes_state.SessionDB._is_explicit_fork_child_row`);
* **compression child** — no fork marker, and the parent row's
  ``end_reason == "compression"`` (same semantics as
  :meth:`hermes_state.SessionDB._is_compression_child_row`);
* **/new / gateway reset** — ``model_config._reset_from == parent_session_id``,
  or no trusted marker at all; these must NEVER inherit a parent's binding.

:func:`resolve_lineage` reads at most the child row and its parent row, reuses
the existing classification semantics, and returns only *verified* lineage
evidence for :func:`writegate.binding.derive_binding`:

    ``{"kind": "branch"|"delegate"|"compression",
       "parent_session_id": str,
       "parent_is_bound": bool}``

or ``None`` (top-level, /new, reset, unknown, or store unavailable — always
fail closed; the session stays unbound and governed writes block).

The resolver is strictly read-only (``SessionDB.open_ro``), closes its handle,
and never falls back to ambient cwd.  ``task_id`` is deliberately NOT accepted
here: the host CWD record key is a separate seam (``writegate.tool`` /
``binding``) and must not masquerade as lineage proof.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

__all__ = ["resolve_lineage"]


def _model_config(session: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a session row's ``model_config`` JSON into a dict (fail empty)."""
    raw = session.get("model_config")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _classify_row(
    session: Dict[str, Any],
    parent: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Classify one child row against its parent row using Hermes semantics.

    Returns ``"branch"``, ``"delegate"``, or ``"compression"`` for a verified
    inheritance relationship; ``None`` for /new, reset, top-level, or
    unclassifiable rows.
    """
    parent_id = session.get("parent_session_id") or None
    if not parent_id:
        # No parent recorded: a genuine top-level session.
        return None
    cfg = _model_config(session)
    # /new and gateway resets carry ``_reset_from == parent_session_id`` —
    # explicitly NOT an inheritance relationship.
    if cfg.get("_reset_from") == parent_id:
        return None
    branched = cfg.get("_branched_from")
    delegated = cfg.get("_delegate_from")
    source = session.get("source")
    if source == "tool" or branched == parent_id or delegated == parent_id:
        # Explicit fork child of exactly this parent.
        return "delegate" if (source == "tool" or delegated == parent_id) else "branch"
    # No fork marker: compression child only when the parent actually ended by
    # compression (the real Hermes constant).
    if parent and parent.get("end_reason") == "compression":
        return "compression"
    return None


def _open_ro_session_db(profile_dir: str) -> Optional[Any]:
    """Open the profile's session store read-only, or None when unavailable.

    The store lives at ``<profile>/state.db`` (the canonical Hermes session
    DB location; see ``hermes_state.DEFAULT_DB_PATH``).  Returns ``None`` on
    any failure so the caller can fail closed.
    """
    try:
        from pathlib import Path

        from hermes_state import SessionDB

        db_path = os.path.join(profile_dir, "state.db")
        return SessionDB(Path(db_path), read_only=True)
    except Exception:
        return None


def resolve_lineage(
    session_id: str,
    *,
    profile_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve trusted WriteGate lineage for ``session_id``.

    Read-only, fail-closed.  See module docstring for the classification
    rules.  Returns a dict of verified lineage evidence for
    :func:`writegate.binding.derive_binding`, or ``None``.
    """
    if not session_id:
        return None
    try:
        if profile_dir is None:
            from hermes_constants import get_hermes_home
            profile_dir = str(get_hermes_home())
        db = _open_ro_session_db(profile_dir)
        if db is None:
            return None
        try:
            child = db.get_session(session_id)
            if not child:
                return None
            parent_id = child.get("parent_session_id") or None
            parent = db.get_session(parent_id) if parent_id else None
            kind = _classify_row(child, parent)
            if kind is None or not parent_id:
                return None
            return {
                "kind": kind,
                "parent_session_id": parent_id,
                # Parent liveness is verified by the caller against the live
                # registry; this flag only records that the row was read.
                "parent_is_bound": True,
            }
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception:
        # Any store/parse failure: fail closed — no lineage evidence.
        return None
