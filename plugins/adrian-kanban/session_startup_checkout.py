"""Canonical project-checkout establishment (S2C2b).

This module is the S2C2b write primitive of the project-repository-binding
work. It is the direct successor to the S2C2a provisioning seam
(:mod:`session_startup_provisioning`), which verified the exact GitHub
repository, SSH reachability, integration branch, and remote branch SHA.

Given the four verified inputs, :func:`establish_canonical_checkout`
establishes (or revalidates) the canonical checkout on disk:

* If the target path is absent, it clones through the injected Git runner
  using the canonical SSH URL.
* If the target path already exists, it is validated as an existing Git
  checkout that exactly matches the requested repository, on the configured
  integration branch, and clean.
* The configured integration branch is fetched and its remote ref is
  verified against the previously verified base SHA. A moved branch is a
  recoverable mismatch; it is never silently accepted.

Containment uses canonical path semantics: the checkout must be a direct
system-derived child of the approved checkout root and must not be the root
itself. Traversal, symlink escape, and paths derived from untrusted display
text are rejected. Repository identity is enforced against the settled
personal owner; generic repository normalization and SHA validation are
reused from :mod:`repository_binding` rather than duplicated.

No Git, client, config writer, Project DB, or board mutation is performed
here; only the checkout primitive. Credentials and ``GIT_SSH_COMMAND``
internals are never returned.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .repository_binding import (
    normalize_github_repository,
    validate_github_repository_name,
    validate_integration_branch,
)
from .session_startup_provisioning import PROVISIONING_OWNER, ssh_git_command

# Default wall-clock bound for any single git subprocess. A clone or fetch
# that exceeds this is treated as a runner failure, not a hang.
DEFAULT_GIT_TIMEOUT_SECONDS = 300

# A "clean" checkout has no changes in the working tree or index relative to
# the checked-out commit. This is the state a fresh clone produces and the
# state an existing checkout must be in to be reused.
REMOTE_TRACKING_REF = "origin/{branch}"


class CheckoutError(Exception):
    """One actionable recoverable error type for the checkout primitive.

    The message names the failed condition and a practical remediation.
    Subprocess timeout/runner failures are converted into this type so the
    caller sees a single, uniform, actionable failure.
    """


@dataclass(frozen=True)
class CheckoutEvidence:
    """Structured evidence for a later journal checkpoint.

    Contains everything a checkpoint needs to record what was established,
    without leaking credentials or SSH command internals.
    """

    canonical_checkout: str
    normalized_origin: str
    integration_branch: str
    remote_ref: str
    base_sha: str
    created_by_operation: bool

    def as_dict(self) -> Dict[str, Any]:
        """Return a plain mapping suitable for a journal checkpoint.

        ``created_by_operation`` is a JSON boolean, not the strings
        ``"true"``/``"false"``.
        """

        return {
            "canonical_checkout": self.canonical_checkout,
            "normalized_origin": self.normalized_origin,
            "integration_branch": self.integration_branch,
            "remote_ref": self.remote_ref,
            "base_sha": self.base_sha,
            "created_by_operation": bool(self.created_by_operation),
        }


def _require_nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CheckoutError(f"{name} must be a nonblank string")
    return value.strip()


def _validate_checkout_root(root: str) -> str:
    """Return the approved checkout root as a normalized absolute path.

    The root must be a nonblank absolute path. It is the controlled approved
    checkout root from S2C2a, not derived from untrusted display text.
    """

    root = _require_nonblank(root, "checkout root")
    root = os.path.normpath(os.path.expanduser(root))
    if not os.path.isabs(root):
        raise CheckoutError(
            "checkout root must be an absolute path; verify the approved "
            "checkout root and retry"
        )
    return root


def _validate_checkout_path(
    checkout: str, root: str
) -> str:
    """Return the canonical checkout path with containment enforced.

    Containment uses canonical path semantics, not string prefixes: the
    checkout must be a *direct* system-derived child of ``root`` and must not
    be the root itself. A checkout that escapes ``root`` via ``..`` traversal
    or a symlink is rejected. The full target is resolved so that
    ``<approved-root>/<name>`` itself being a symlink outside the root is
    caught, while an absent path still validates (it is created by clone).
    """

    checkout = _require_nonblank(checkout, "canonical checkout")
    checkout = os.path.normpath(os.path.expanduser(checkout))
    if not os.path.isabs(checkout):
        raise CheckoutError(
            "canonical checkout must be an absolute path; verify the "
            "approved checkout path and retry"
        )

    real_root = os.path.realpath(root)
    # Resolve the full checkout target so a checkout that *is* a symlink
    # escaping the root is contained against the real root. An absent path
    # has no target yet; fall back to the un-resolved path so the later
    # clone step can create it, but still reject ``..`` traversal, which
    # ``normpath`` collapses before resolution.
    try:
        real_checkout = os.path.realpath(checkout)
    except OSError:
        real_checkout = checkout

    # Direct-child containment via canonical full-path semantics: the checkout
    # must be exactly one path component beneath the (real) root, and must not
    # *be* the root. Using commonpath (with parent equality) rejects traversal
    # escapes, a checkout equal to the root, and a checkout nested deeper than
    # one level, while a symlink target inside the root still passes because
    # realpath collapses it to ``real_root/<name>``.
    if real_checkout == real_root:
        raise CheckoutError(
            "canonical checkout is not a direct child of the approved "
            "checkout root; verify the checkout path and retry"
        )
    common = os.path.commonpath([real_root, real_checkout])
    if common != real_root:
        raise CheckoutError(
            "canonical checkout is not a direct child of the approved "
            "checkout root; verify the checkout path and retry"
        )
    remainder = os.path.relpath(real_checkout, real_root)
    if os.sep in remainder or remainder in ("", "."):
        raise CheckoutError(
            "canonical checkout is not a direct child of the approved "
            "checkout root; verify the checkout path and retry"
        )

    return checkout


def _validate_repository(repository: str) -> str:
    """Validate the exact ``owner/repository`` verified by S2C2a.

    The owner is fixed to the settled personal owner
    (:data:`PROVISIONING_OWNER`) and is enforced case-insensitively. A bare
    repository name is rejected: this primitive receives the full verified
    identity, not a name to be re-parented. The result is returned lowercased
    for stable identity comparison.
    """

    repository = _require_nonblank(repository, "repository")
    stripped = repository.strip()
    if "/" not in stripped:
        raise CheckoutError(
            "repository must be an owner/repo; this primitive receives the "
            "exact verified identity, not a bare name; verify the verified "
            "repository and retry"
        )
    parts = stripped.split("/")
    if len(parts) != 2 or not all(p.strip() for p in parts):
        raise CheckoutError(
            "repository must be an owner/repo with exactly two nonblank "
            "components"
        )
    owner, repo = parts[0].strip(), parts[1].strip()
    if owner.lower() != PROVISIONING_OWNER.lower():
        raise CheckoutError(
            f"repository owner must be {PROVISIONING_OWNER}, got "
            f"'{owner}'; verify the verified repository and retry"
        )
    try:
        validate_github_repository_name(repo)
    except ValueError as exc:
        raise CheckoutError(str(exc)) from exc
    return f"{owner.lower()}/{repo.lower()}"


def _ssh_url_for(repository: str) -> str:
    """Return the canonical SSH URL for the repository.

    The URL is derived from the verified ``owner/repository``; it is not
    derived from untrusted display text. The SSH identity formatter is reused
    from :mod:`session_startup_provisioning` rather than duplicated.
    """

    owner, repo = repository.split("/", 1)
    return ssh_git_command(owner, repo)


class _OriginState:
    """Three-state result of reading a checkout's origin."""

    NOT_GIT = "not_git"          # path is not a Git checkout
    NON_GITHUB = "non_github"    # a Git checkout whose origin is not GitHub
    MISMATCH = "mismatch"        # a GitHub origin that is not the target
    MATCH = "match"              # the GitHub origin equals the target

    def __init__(self, state: str, normalized: Optional[str] = None):
        self.state = state
        self.normalized = normalized


def _run_git(
    runner: Callable, argv: list, *, timeout: int
) -> "subprocess.CompletedProcess":
    """Run one git argv through ``runner`` and return its result.

    ``runner`` follows a single invocation contract: it is called with an
    argv list and the keyword arguments ``capture_output``, ``text``,
    ``shell``, ``check``, and ``timeout``. Any runner failure (timeout,
    OSError, or other) is converted into the module's single recoverable
    error type so every call site stays uniform.
    """

    try:
        return runner(
            argv,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=timeout,
        )
    except CheckoutError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise CheckoutError(
            f"git command timed out: {' '.join(argv)}; verify the Git runner "
            "is available and retry"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckoutError(
            f"git command could not run: {' '.join(argv)}: {exc}; verify the "
            "Git runner is available and retry"
        ) from exc


def _resolve_origin(root: str, target_repo: str, runner: Callable) -> _OriginState:
    """Classify a checkout's origin against the target repository.

    ``None``-style outcomes are expressed as explicit states rather than
    sentinels: ``NOT_GIT`` when the path is not a Git checkout, ``NON_GITHUB``
    when the origin is a GitHub URL that is not the target, and ``MISMATCH``
    when the normalized origin differs from ``target_repo``. ``MATCH`` is
    returned when the origin equals ``target_repo``.
    """

    result = _run_git(
        runner,
        ["git", "-C", root, "config", "--get", "remote.origin.url"],
        timeout=DEFAULT_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return _OriginState(_OriginState.NOT_GIT)
    origin = result.stdout.strip()
    if not origin:
        return _OriginState(_OriginState.NOT_GIT)
    try:
        normalized = normalize_github_repository(origin)
    except ValueError:
        return _OriginState(_OriginState.NON_GITHUB)
    if normalized != target_repo:
        return _OriginState(_OriginState.MISMATCH, normalized)
    return _OriginState(_OriginState.MATCH, normalized)


def _git_ref_sha(
    checkout: str, ref: str, runner: Callable
) -> Optional[str]:
    """Return the SHA a ref resolves to, or ``None`` if the ref is absent."""

    result = _run_git(
        runner,
        ["git", "-C", checkout, "rev-parse", "--verify", "--quiet", ref],
        timeout=DEFAULT_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    if not sha:
        return None
    return sha


def _current_branch(checkout: str, runner: Callable) -> Optional[str]:
    """Return the checked-out branch name, or ``None`` if not a branch."""

    result = _run_git(
        runner,
        ["git", "-C", checkout, "rev-parse", "--abbrev-ref", "HEAD"],
        timeout=DEFAULT_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _fetch_integration_branch(
    checkout: str,
    branch: str,
    runner: Callable,
) -> None:
    """Fetch the configured integration branch into its remote-tracking ref.

    The fetch uses an unambiguous refspec
    (``refs/heads/<branch>:refs/remotes/origin/<branch>``) so the freshly
    fetched remote ref is exactly the one compared against the verified base
    SHA. Failures (timeout, runner error, non-zero exit) are converted into
    the module's single recoverable error type.
    """

    result = _run_git(
        runner,
        [
            "git", "-C", checkout, "fetch", "--no-tags",
            "--", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}",
        ],
        timeout=DEFAULT_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise CheckoutError(
            f"fetch of integration branch {branch!r} failed "
            f"(exit {result.returncode}); verify the remote is reachable and "
            "the checkout state, then retry"
        )


def _clone(
    checkout: str,
    url: str,
    branch: str,
    runner: Callable,
) -> None:
    """Clone ``url`` at ``branch`` into ``checkout``.

    The clone is a bounded, non-shell invocation. ``--branch`` makes the new
    checkout track the configured integration branch. Failures (timeout,
    runner error, non-zero exit) are converted into the module's single
    recoverable error type. On failure nothing is written: ``git clone``
    either produces a complete directory or aborts before creating one.
    """

    result = _run_git(
        runner,
        ["git", "clone", "--branch", branch, url, checkout],
        timeout=DEFAULT_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise CheckoutError(
            f"clone of {url!r} failed "
            f"(exit {result.returncode}); verify the repository is reachable "
            "and the runner is available, then retry"
        )


def _validate_sha(sha: str) -> bool:
    """Return True when ``sha`` is a 40- or 64-character hexadecimal digest."""

    sha = sha.strip()
    if len(sha) not in (40, 64):
        return False
    try:
        int(sha, 16)
    except ValueError:
        return False
    return True


def establish_canonical_checkout(
    checkout: str,
    repository: str,
    integration_branch: str,
    base_sha: str,
    checkout_root: str,
    git_runner: Optional[Callable] = None,
) -> CheckoutEvidence:
    """Establish or revalidate the canonical checkout on disk.

    Parameters
    ----------
    checkout:
        The canonical checkout path (``<root>/<repo>``), as produced by the
        proposal seam.
    repository:
        The exact ``owner/repository`` verified by S2C2a. A bare name is
        rejected; this primitive receives the full verified identity.
    integration_branch:
        The integration branch verified by S2C2a.
    base_sha:
        The remote branch SHA verified by S2C2a.
    checkout_root:
        The controlled approved checkout root.
    git_runner:
        An injected command runner (defaults to :func:`subprocess.run`).

    Returns
    -------
    CheckoutEvidence
        Structured evidence sufficient for a later journal checkpoint.

    Raises
    ------
    CheckoutError
        On any recoverable failure: bad inputs, containment violation,
        clone failure, mismatched origin, dirty checkout, branch
        relationship mismatch, or a moved remote branch.
    """

    runner = git_runner or subprocess.run

    # -- validate inputs before any mutation ------------------------------
    root = _validate_checkout_root(checkout_root)
    checkout = _validate_checkout_path(checkout, root)
    normalized_repo = _validate_repository(repository)
    try:
        branch = validate_integration_branch(integration_branch)
    except ValueError as exc:
        raise CheckoutError(f"invalid integration branch: {exc}") from exc
    base_sha = _require_nonblank(base_sha, "base SHA")
    if not _validate_sha(base_sha):
        raise CheckoutError(
            f"base SHA '{base_sha}' is not a valid 40- or 64-character "
            "hexadecimal SHA; verify the verified remote branch SHA and retry"
        )
    base_sha = base_sha.strip()

    ssh_url = _ssh_url_for(normalized_repo)

    if not os.path.exists(checkout):
        # Absent: clone.
        _clone(checkout, ssh_url, branch, runner)
        return _finalize_new_checkout(
            checkout, normalized_repo, branch, base_sha, runner
        )

    # -- existing path: validate, never rewrite --------------------------
    if not os.path.isdir(checkout):
        raise CheckoutError(
            f"{checkout} exists but is not a directory; remove the "
            "conflicting entry or verify the approved checkout path, then "
            "retry"
        )

    existing_origin = _resolve_origin(checkout, normalized_repo, runner)
    if existing_origin.state == _OriginState.NOT_GIT:
        raise CheckoutError(
            f"{checkout} exists but is not a Git checkout; the checkout "
            "path is not a repository, verify the approved checkout path "
            "and retry"
        )
    if existing_origin.state == _OriginState.NON_GITHUB:
        raise CheckoutError(
            f"{checkout} is a Git checkout but its origin is not a GitHub "
            f"repository; verify the checkout is the {normalized_repo} "
            "repository and retry"
        )
    if existing_origin.state == _OriginState.MISMATCH:
        raise CheckoutError(
            f"origin {existing_origin.normalized!r} does not match the "
            f"requested repository {normalized_repo!r}; the existing "
            "checkout is not the target repository, verify the approved "
            "checkout path and retry"
        )

    # Branch relationship: the checkout must be on the integration branch.
    current_branch = _current_branch(checkout, runner)
    if current_branch is None:
        raise CheckoutError(
            f"{checkout} is not on a branch (detached HEAD); the checkout "
            f"must be on {branch}, verify the checkout state and retry"
        )
    if current_branch != branch:
        raise CheckoutError(
            f"checkout is on branch {current_branch!r}, not the "
            f"integration branch {branch!r}; the branch relationship "
            "mismatches, verify the checkout state and retry"
        )

    # Cleanliness: never discard or reset local state.
    status_result = _run_git(
        runner,
        ["git", "-C", checkout, "status", "--porcelain"],
        timeout=DEFAULT_GIT_TIMEOUT_SECONDS,
    )
    if status_result.returncode != 0:
        raise CheckoutError(
            f"git status of {checkout} failed "
            f"(exit {status_result.returncode}); verify the checkout is "
            "clean and accessible, then retry"
        )
    if status_result.stdout.strip():
        raise CheckoutError(
            f"{checkout} has uncommitted changes; the checkout must be "
            "clean before reuse, verify the checkout state and retry"
        )

    # Fetch the configured branch into its remote-tracking ref, then compare
    # the freshly fetched ref to the previously verified base SHA. A moved
    # branch is a recoverable mismatch; it is never silently accepted.
    _fetch_integration_branch(checkout, branch, runner)
    remote_ref = REMOTE_TRACKING_REF.format(branch=branch)
    remote_sha = _git_ref_sha(checkout, remote_ref, runner)
    if remote_sha is None:
        raise CheckoutError(
            f"{remote_ref} is missing; the integration branch ref was not "
            "tracked, verify the remote branch exists and the checkout "
            "state, then retry"
        )
    if remote_sha != base_sha:
        raise CheckoutError(
            f"integration branch moved: {remote_ref} is at "
            f"{remote_sha}, but the verified base SHA is {base_sha}; the "
            "base has changed, do not accept a new base automatically, "
            "re-verify the remote branch SHA and retry"
        )

    return CheckoutEvidence(
        canonical_checkout=checkout,
        normalized_origin=normalized_repo,
        integration_branch=branch,
        remote_ref=remote_ref,
        base_sha=base_sha,
        created_by_operation=False,
    )


def _finalize_new_checkout(
    checkout: str,
    normalized_repo: str,
    branch: str,
    base_sha: str,
    runner: Callable,
) -> CheckoutEvidence:
    """Validate the freshly cloned checkout and return its evidence.

    A ``git clone --branch <branch>`` already leaves the checkout on the
    integration branch and clean, so this verifies that relationship, that
    the origin resolves to the exact requested repository, and that the
    remote-tracking ref matches the verified SHA. No local state is
    rewritten: this only runs on a path we just created.
    """

    # The clone must resolve to the exact requested repository.
    origin_state = _resolve_origin(checkout, normalized_repo, runner)
    if origin_state.state != _OriginState.MATCH:
        raise CheckoutError(
            f"clone origin {origin_state.normalized!r} does not match the "
            f"requested repository {normalized_repo!r}; the cloned checkout "
            "is not the target repository, verify the repository and retry"
        )

    # The clone is on the integration branch; verify it.
    current_branch = _current_branch(checkout, runner)
    if current_branch is None:
        raise CheckoutError(
            f"{checkout} is not on a branch after clone (detached HEAD); "
            "verify the checkout is writable and retry"
        )
    if current_branch != branch:
        raise CheckoutError(
            f"clone is on branch {current_branch!r}, not the integration "
            f"branch {branch!r}; verify the checkout state and retry"
        )

    # The clone was requested at the verified branch tip; verify the
    # remote-tracking ref now exists and matches the verified SHA.
    remote_ref = REMOTE_TRACKING_REF.format(branch=branch)
    remote_sha = _git_ref_sha(checkout, remote_ref, runner)
    if remote_sha is None:
        raise CheckoutError(
            f"{remote_ref} is missing after clone; the integration branch "
            "ref was not tracked, verify the remote branch exists and retry"
        )
    if remote_sha != base_sha:
        raise CheckoutError(
            f"integration branch moved: {remote_ref} is at "
            f"{remote_sha}, but the verified base SHA is {base_sha}; the "
            "base has changed, do not accept a new base automatically, "
            "re-verify the remote branch SHA and retry"
        )

    # Verify the checkout is clean.
    status_result = _run_git(
        runner,
        ["git", "-C", checkout, "status", "--porcelain"],
        timeout=DEFAULT_GIT_TIMEOUT_SECONDS,
    )
    if status_result.returncode != 0:
        raise CheckoutError(
            f"git status of {checkout} failed "
            f"(exit {status_result.returncode}); verify the checkout is "
            "clean, then retry"
        )
    if status_result.stdout.strip():
        raise CheckoutError(
            f"{checkout} is not clean after clone; the checkout must be "
            "clean, verify the checkout state and retry"
        )

    return CheckoutEvidence(
        canonical_checkout=checkout,
        normalized_origin=normalized_repo,
        integration_branch=branch,
        remote_ref=remote_ref,
        base_sha=base_sha,
        created_by_operation=True,
    )
