"""Canonical-path containment primitives for WriteGate.

This module is deliberately **pure** (no database, no network, no time): it
resolves a requested target and a candidate root to canonical absolute paths
and decides whether the target is *contained* in the root.  Every enforcement
path (binding containment, lease-folder containment, recovery-root containment)
shares these helpers so the policy is evaluated identically everywhere.

Design rules (from ``Canon/write-gate-policy.md`` §3, §4):

* Containment is resolved with **real-path / parent resolution**, never
  string-prefix matching.  A string-prefix test such as
  ``target.startswith(worktree)`` lets ``/home/proj-evil`` pass as contained
  in ``/home/proj`` — a classic escape.
* **Traversal** (``..`` components) and **symlink escapes** (a path that
  resolves outside the root through a symlink) are rejected.
* Targets that do **not yet exist** (new files, new directories) cannot be
  ``realpath``-resolved to a physical inode, so containment is computed against
  the resolved *parent* plus the final component — the parent must be inside
  the root, and the final component must not be a traversal.

The functions return ``None`` on success (contained) or a short
``reason`` string explaining the rejection, so callers can surface a precise
failure to the model.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def canonicalize_target(path: str, *, must_exist: bool = False,
                        base_dir: Optional[str] = None) -> Optional[str]:
    """Resolve ``path`` to a canonical absolute path string.

    When ``must_exist`` is false (the common case — most governed writes
    target a not-yet-existing file), the *parent* directory is resolved to a
    real path and the final component is appended verbatim (after a traversal
    check).  When ``must_exist`` is true, :func:`os.path.realpath` is used so a
    symlinked path is fully unwrapped.

    ``base_dir`` is the trusted per-session/task directory that relative
    targets resolve against.  It defaults to ``os.getcwd()`` only when the
    caller passes nothing, so enforcement never silently keys off the process
    cwd.  A relative target resolved against a different ``base_dir`` yields a
    different canonical path than the same name resolved against the process
    cwd, which is exactly what keeps the cache honest across a cwd change.

    Returns the canonical absolute path, or ``None`` when the path cannot be
    resolved (e.g. an empty string, or a parent that does not exist when
    ``must_exist`` is set).
    """
    if not path or not isinstance(path, str):
        return None
    # Reject a literal traversal component up front so the append logic below
    # cannot smuggle one through on a not-yet-existing target.
    if has_traversal_component(path):
        return None

    p = Path(path)
    if p.is_absolute():
        abs_path = p
    else:
        # Relative targets resolve against the trusted per-session/task base
        # directory the file tool uses, never the enforcement process cwd.
        base = Path(base_dir) if base_dir else Path(os.getcwd())
        abs_path = base / p

    if must_exist:
        if not abs_path.exists():
            return None
        try:
            return os.path.realpath(str(abs_path))
        except (OSError, ValueError):
            return None

    # Not-yet-existing: resolve the parent to a real path, then append the
    # final component.  ``parent()`` on a bare filename yields cwd, which is
    # what we want.
    parent = abs_path.parent
    try:
        real_parent = os.path.realpath(str(parent))
    except (OSError, ValueError):
        return None
    return os.path.join(real_parent, abs_path.name)


def has_traversal_component(path_str: str) -> bool:
    """Return true when ``path_str`` contains a ``..`` path component.

    Checks each split component rather than the raw string so that a substring
    like ``a..b`` (a legal filename) is not mistaken for traversal, while
    ``../x``, ``a/../b`` and ``..`` are all caught.
    """
    if not path_str:
        return False
    parts = path_str.replace("\\", "/").split("/")
    return ".." in parts


def is_within(target_canonical: str, root_canonical: str) -> bool:
    """Return true when ``target_canonical`` is contained in ``root_canonical``.

    Both arguments must already be canonical (see :func:`canonicalize_target`).
    Containment is component-wise: ``target == root`` counts as contained (the
    root itself), and ``target`` must share every parent component of ``root``.
    A trailing-slash or mixed-separator mismatch is normalised first.
    """
    if not target_canonical or not root_canonical:
        return False
    t = _normalize(target_canonical)
    r = _normalize(root_canonical)
    if t == r:
        return True
    # Component-aware containment: split both sides on the normalised "/"
    # separator so the check is correct regardless of the native OS separator
    # (Windows paths use "\\" while canonical forms are "/").  The root's
    # components must be an exact leading run of the target's components, which
    # also avoids the string-prefix footgun where ``/home/proj-evil`` looks
    # inside ``/home/proj``.
    root_components = [c for c in r.split("/") if c]
    target_components = [c for c in t.split("/") if c]
    if len(target_components) <= len(root_components):
        return False
    if target_components[: len(root_components)] != root_components:
        return False
    # The remainder must not itself be a traversal.  (canonicalize_target
    # already rejected raw traversal, but guard defensively for callers that
    # pass a pre-built path.)
    remainder = "/".join(target_components[len(root_components):])
    return not has_traversal_component(remainder)


def containment_result(
    requested_path: str,
    root_path: str,
    *,
    root_must_exist: bool = True,
    requested_must_exist: bool = False,
) -> Optional[str]:
    """Resolve both sides and return ``None`` when contained, else a reason.

    This is the single entry point enforcement code calls.  ``root_path`` is
    always resolved as an existing directory (the confirmed worktree / approved
    folder must exist).  ``requested_path`` is resolved as a not-yet-existing-
    aware target by default.
    """
    root_canonical = canonicalize_target(root_path, must_exist=root_must_exist)
    if root_canonical is None:
        return f"WriteGate: cannot resolve worktree/root path {root_path!r}"
    target_canonical = canonicalize_target(requested_path, must_exist=requested_must_exist)
    if target_canonical is None:
        if has_traversal_component(requested_path):
            return f"WriteGate: path traversal rejected for {requested_path!r}"
        return f"WriteGate: cannot resolve target path {requested_path!r}"
    if not is_within(target_canonical, root_canonical):
        return (
            f"WriteGate: target {target_canonical!r} is outside the bound "
            f"worktree/root {root_canonical!r}"
        )
    return None


def _normalize(path: str) -> str:
    """Normalise separators and strip a trailing separator (no filesystem I/O)."""
    return path.replace("\\", "/").rstrip("/") or "/"


def resolve_symlink_escape(candidate: str, root: str) -> Optional[str]:
    """Return a reason string if ``candidate`` escapes ``root`` via a symlink.

    Best-effort: returns ``None`` when no symlink escape is detected or when
    the filesystem cannot be probed.  This is a *defense-in-depth* layer on top
    of :func:`containment_result` (which already unwraps symlinks through
    ``realpath``), not a replacement.
    """
    try:
        real_root = os.path.realpath(root)
        real_candidate = os.path.realpath(candidate)
    except (OSError, ValueError):
        return None
    if not is_within(real_candidate, real_root):
        return f"symlink escape: {candidate!r} resolves outside {root!r}"
    return None
