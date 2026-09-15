"""Published-artifact validation against the configured integration branch.

A published artifact is valid only when its pinned commit is contained by the
remote-authoritative integration head of the trusted repository registration
that owns the active session worktree.  The integration head is resolved
through the single :class:`RepositoryBindingResolver` (online evidence only);
``origin/main`` is never assumed, and cached/offline evidence is never accepted
at this publication boundary.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Optional

from .repository_binding import RepositoryBindingError, RepositoryBindingResolver

_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "--no-replace-objects", "-C", root, *args],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError(
            "Git operation failed; verify the repository is accessible and retry."
        ) from None


def _git_common_dir(root: str) -> Optional[str]:
    """Return the canonical absolute Git common directory for ``root``.

    Uses ``rev-parse --path-format=absolute --git-common-dir`` so that a
    worktree and its canonical checkout compare on identity, not on
    path-string prefix.  Returns ``None`` when the path is not inside a Git
    worktree or the directory cannot be resolved.
    """
    result = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode != 0:
        return None
    try:
        out = result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not out:
        return None
    return out


def resolve_repository_registration(
    worktree_path: str, trusted_registry: Any
) -> Any:
    """Resolve a Git worktree to exactly one trusted repository registration.

    A worktree and its registered canonical checkout must share the same
    resolved ``git rev-parse --path-format=absolute --git-common-dir``.  Zero
    or multiple matches fail closed with an actionable ``ValueError``.
    """
    if not isinstance(worktree_path, str) or not worktree_path.strip():
        raise ValueError(
            "worktree path must be a nonblank string; verify the active "
            "binding worktree and retry"
        )
    try:
        registrations = tuple(trusted_registry._registrations)
    except Exception:
        raise ValueError(
            "trusted repository registry is unavailable; restore the registry "
            "connection and retry"
        ) from None

    worktree_common = _git_common_dir(worktree_path)
    if worktree_common is None:
        raise ValueError(
            "the active worktree does not resolve to a Git worktree; verify "
            "the binding worktree is a valid Git worktree and retry"
        )

    matches = []
    for registration in registrations:
        registration_common = _git_common_dir(registration.repository_root)
        if registration_common is None:
            continue
        if registration_common == worktree_common:
            matches.append(registration)

    if len(matches) == 0:
        raise ValueError(
            "no trusted repository registration matches the active worktree; "
            "verify the trusted repository registry and retry"
        )
    if len(matches) > 1:
        raise ValueError(
            "multiple trusted repository registrations match the active "
            "worktree; verify the trusted repository registry and retry"
        )
    return matches[0]


def read_published_blob(
    worktree_path: str,
    trusted_registry: Any,
    commit: str,
    path: str,
) -> bytes:
    """Read and validate a published artifact blob.

    The pinned commit must be a 40- or 64-character lowercase hex SHA, and
    must be an ancestor of the remote-authoritative integration head of the
    trusted repository registration that owns ``worktree_path``.  The blob is
    read from the pinned commit (never the dirty filesystem), and the exact
    pinned-commit ancestry and blob-hash semantics are preserved.
    """
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ValueError(
            "Invalid commit reference; expected a 40- or 64-character lowercase hex SHA."
        )

    registration = resolve_repository_registration(worktree_path, trusted_registry)
    integration_branch = registration.integration_branch

    try:
        head = RepositoryBindingResolver(registration).resolve_integration_head(
            allow_offline=False
        )
    except RepositoryBindingError as exc:
        message = str(exc)
        if message.startswith("Fetch failed:"):
            raise ValueError(
                "Fetch failed while resolving the remote integration head "
                f"for {registration.remote_label}; verify the remote is "
                "reachable and retry."
            ) from None
        raise ValueError(
            f"Unable to resolve the remote integration head for "
            f"{registration.remote_label}; verify the remote is reachable and "
            "retry."
        ) from None
    except (subprocess.SubprocessError, OSError):
        raise ValueError(
            f"Unable to resolve the remote integration head for "
            f"{registration.remote_label}; verify the remote is reachable and "
            "retry."
        ) from None

    remote_tip = head.head_sha
    for label, sha in (
        ("pinned commit", commit),
        (f"remote {registration.remote_label} tip", remote_tip),
    ):
        tip = _git(worktree_path, "cat-file", "-t", sha)
        if tip.returncode != 0 or tip.stdout.strip() != b"commit":
            raise ValueError(
                f"{label} is not a commit object; fetch {registration.remote_label} "
                "through the authorized workflow then retry."
            )

    merge = _git(worktree_path, "merge-base", "--is-ancestor", commit, remote_tip)
    if merge.returncode == 1:
        raise ValueError(
            f"The pinned commit is not an ancestor of {registration.remote_label}; "
            f"publish or merge the manifest to the configured integration branch "
            f"({integration_branch}), then retry."
        )
    if merge.returncode != 0:
        raise ValueError(
            "Ancestry check failed; fetch "
            f"{registration.remote_label} through the authorized workflow then retry."
        )

    blob = _git(worktree_path, "cat-file", "blob", f"{commit}:{path}")
    if blob.returncode != 0:
        raise ValueError(
            "The pinned manifest could not be read from the commit; verify the exact "
            "pinned commit and path, then retry."
        )
    return blob.stdout
