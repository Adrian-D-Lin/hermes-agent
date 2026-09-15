"""Project proposal derivation and GitHub inspection for session startup.

This module is the S2C1 read/derive seam of the project-repository-binding
work. It contains only deterministic derivation and read-only inspection:

* :func:`make_proposal_preparer` builds a proposal preparer compatible with
  ``OnboardingCoordinator._validate_proposal``. It consumes the coordinator
  payload produced by ``OnboardingCoordinator._collect`` and returns exactly
  the required proposal field set. It performs no Git, client, config writer,
  Project DB, or board mutation.
* :class:`GitHubDefaultBranchInspector` is a read-only inspector that obtains a
  GitHub repository's default branch via the GitHub API using Python stdlib
  HTTP with an injected transport. It never infers or mutates the configured
  integration branch; it only supplies the initial bind suggestion.
* :func:`make_onboarding_callbacks` reads the current trusted kanban config at
  call time and returns the wired callbacks, without caching a stale registry.

No general service layer, plugin framework, background worker, registry, or
model tool is introduced. The canonical checkout root is the spec-mandated
``/home/progenitor/AI-main``; the controlled worktree root is read from the
current trusted kanban config, not duplicated here.
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from hermes_cli import config as _config

from .repository_binding import (
    validate_github_repository_name,
    validate_integration_branch,
)

# Every GitHub association is Adrian-D-Lin. Alternate owners are rejected, not
# offered as a selection: the owner is fixed, not derived from user input.
PROPOSAL_OWNER = "Adrian-D-Lin"

# Canonical checkout root. This is a distinct root from the controlled
# worktree root (read from kanban config): canonical checkouts live directly
# beneath ``AI-main``, not beneath the worktree root.
AI_MAIN_ROOT = "/home/progenitor/AI-main"

# GitHub API: the default branch is reported on the repository object.
_GITHUB_REPO_API = "https://api.github.com/repos/{repo}"

# Required GitHub API headers. The Accept header is mandated by the API; the
# User-Agent is required or the API returns 403. The Authorization header is
# added only when a token is present.
_GITHUB_REQUIRED_HEADERS = (
    ("Accept", "application/vnd.github+json"),
    ("X-GitHub-Api-Version", "2022-11-28"),
    ("User-Agent", "hermes-adrian-kanban"),
)

# Seconds to wait for the GitHub API to respond before treating the call as a
# connectivity/timeout failure.
_DEFAULT_TIMEOUT_SECONDS = 10


class GitHubInspectionError(ValueError):
    """Raised when the GitHub repository inspection cannot be completed.

    The message is a plain actionable error string; callers may match on
    the text (for example "missing github credentials", "authentication
    failed", "not found") but no stable kind prefix is guaranteed.
    """


def _require_nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank text")
    return value


def _project_slug_from_display(display_name: str) -> str:
    """Derive a stable lowercase project slug from a display name.

    Reuses the Hermes project-slug convention: lowercase, collapse runs of
    non-alphanumeric characters to single hyphens, trim leading/trailing
    hyphens, and cap at 64 characters. Falls back to ``project`` when the
    display name yields nothing usable.
    """

    slug = str(display_name or "").strip().lower()
    slug = "".join(
        ch if (ch.isalnum() or ch in "-_") else "-" for ch in slug
    )
    # Collapse runs of separator characters to a single hyphen, then trim.
    slug = re.sub(r"[-_]+", "-", slug).strip("-")
    slug = slug[:64].strip("-")
    return slug or "project"


def _board_slug_from_display(display_name: str) -> str:
    """Derive a board slug from the same display name.

    Boards and projects share one slug space in Hermes, so the board slug is
    the same normalized token as the project slug.
    """

    return _project_slug_from_display(display_name)


def _validate_containment(project_slug: str) -> str:
    """Return the canonical checkout, strictly beneath ``AI-main``.

    The checkout is ``/home/progenitor/AI-main/<project-slug>``. The result
    must stay strictly inside ``AI-main``; any escape (which a malformed slug
    cannot produce, but which is checked defensively) is rejected.
    """

    checkout = os.path.normpath(os.path.join(AI_MAIN_ROOT, project_slug))
    root = os.path.normpath(AI_MAIN_ROOT)
    if checkout != root and not checkout.startswith(root + os.sep):
        raise ValueError(
            "canonical checkout must stay within the AI-main root"
        )
    return checkout


def _read_controlled_worktree_root(config_loader: Optional[Callable[[], Any]] = None) -> str:
    """Read the controlled worktree root from the current trusted config.

    The loader is injected for tests; the default loader is
    ``hermes_cli.config.load_config_readonly``. The value must be a nonblank
    absolute path (after ``expanduser``/``normpath``).
    """

    loader = config_loader if config_loader is not None else _config.load_config_readonly
    config = loader()
    if not isinstance(config, dict):
        raise ValueError("kanban config must be a mapping")
    kanban_config = config.get("kanban")
    if not isinstance(kanban_config, dict):
        raise ValueError("kanban config must be a mapping")
    worktree_root = kanban_config.get("controlled_worktree_root")
    if not isinstance(worktree_root, str) or not worktree_root.strip():
        raise ValueError("kanban.controlled_worktree_root must be nonblank")
    root = os.path.normpath(os.path.expanduser(worktree_root))
    if not os.path.isabs(root):
        raise ValueError(
            "kanban.controlled_worktree_root must be an absolute path"
        )
    return root


@dataclass(frozen=True)
class _ProposalPreparer:
    """Builds a proposal from the coordinator payload.

    The preparer is a plain callable so it can be injected directly into
    ``OnboardingCoordinator`` as ``proposal_preparer``.
    """

    controlled_worktree_root: str

    def __call__(self, payload: Dict[str, Any]) -> Dict[str, str]:
        mode = payload.get("mode")
        if mode not in ("create", "bind"):
            raise ValueError(f"unknown proposal mode {mode!r}")

        display_name = _require_nonblank(
            payload.get("display_name"), "display_name"
        )
        # The owner is fixed, not derived from user input. The coordinator
        # payload carries it separately; validate it against the fixed
        # owner before using it.
        owner = _require_nonblank(payload.get("owner"), "owner")
        if owner != PROPOSAL_OWNER:
            raise ValueError(
                f"repository owner must be {PROPOSAL_OWNER}; got {owner!r}"
            )
        # The coordinator payload carries the bare repository name; validate
        # it with the shared repository-name validator, then construct the
        # normalized owner/name.
        bare_name = _require_nonblank(
            payload.get("repository"), "repository"
        )
        try:
            validated_name = validate_github_repository_name(bare_name)
        except ValueError as exc:
            raise ValueError(
                f"repository name is not a valid GitHub repository name: {exc}"
            ) from exc
        # The typed branch is validated by the same complete validator as
        # the inspector's default_branch.
        branch = _require_nonblank(payload.get("branch"), "integration branch")
        try:
            validated_branch = validate_integration_branch(branch)
        except ValueError as exc:
            raise ValueError(f"invalid integration branch: {exc}") from exc

        canonical_repo = f"{PROPOSAL_OWNER.lower()}/{validated_name.lower()}"
        project_slug = _project_slug_from_display(display_name)
        board_slug = _board_slug_from_display(display_name)
        checkout = _validate_containment(project_slug)

        proposal: Dict[str, str] = {
            "mode": mode,
            "display_name": display_name,
            "project_slug": project_slug,
            "repository": canonical_repo,
            "integration_branch": validated_branch,
            "canonical_checkout": checkout,
            "controlled_worktree_root": self.controlled_worktree_root,
            "board_slug": board_slug,
        }
        if mode == "create":
            visibility = payload.get("visibility", "private")
            if visibility not in ("private", "public"):
                raise ValueError("visibility must be 'private' or 'public'")
            proposal["visibility"] = visibility
        return proposal


@dataclass(frozen=True)
class GitHubDefaultBranchInspector:
    """Read-only inspector that obtains a GitHub repository's default branch.

    The inspector issues a single ``GET`` against the GitHub repository API via
    an injected transport (``transport`` defaults to a stdlib opener). It
    distinguishes missing credentials, auth denial, not-found,
    connectivity/timeout, malformed JSON, and a missing/invalid
    ``default_branch`` with actionable errors. It never infers or mutates the
    configured integration branch.
    """

    timeout: float = _DEFAULT_TIMEOUT_SECONDS
    transport: Optional[Callable[[str, Dict[str, str], float], bytes]] = None

    def __post_init__(self) -> None:
        if not (
            isinstance(self.timeout, (int, float)) and self.timeout > 0
        ):
            raise ValueError("GitHubDefaultBranchInspector timeout must be positive")
        object.__setattr__(self, "timeout", float(self.timeout))

    def _open(self, url: str) -> bytes:
        """Fetch ``url`` and return the response body as bytes.

        The transport is injected so tests can supply a fixed body or raise
        a typed failure. Both the default transport and the injected
        transport classify 401/403/404, timeout/connectivity, and other
        HTTP failures clearly. The default transport uses
        ``urllib.request`` with the required GitHub headers and the
        configured timeout.
        """

        transport = self.transport
        if transport is None:
            token = _github_token_from_env()
            if not token:
                raise GitHubInspectionError(
                    "missing github credentials: set GH_TOKEN or GITHUB_TOKEN"
                )
            headers = dict(_GITHUB_REQUIRED_HEADERS)
            headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(
                url, headers=headers, method="GET"
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                raise _http_error(exc) from exc
            except urllib.error.URLError as exc:
                raise _url_error(exc) from exc
            except socket.timeout as exc:
                raise GitHubInspectionError(
                    "github api request timed out"
                ) from exc
            except TimeoutError as exc:
                raise GitHubInspectionError(
                    f"github api request timed out: {exc}"
                ) from exc
            except OSError as exc:
                raise GitHubInspectionError(
                    f"github api connectivity error: {exc}"
                ) from exc
        try:
            return transport(url, dict(_GITHUB_REQUIRED_HEADERS), self.timeout)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise _url_error(exc) from exc
        except socket.timeout as exc:
            raise GitHubInspectionError(
                "github api request timed out"
            ) from exc
        except TimeoutError as exc:
            raise GitHubInspectionError(
                f"github api request timed out: {exc}"
            ) from exc
        except OSError as exc:
            raise GitHubInspectionError(
                f"github api connectivity error: {exc}"
            ) from exc

    def inspect(self, repository: str) -> str:
        """Return the repository's default branch, or raise on failure.

        ``repository`` is the live onboarding input: a bare repository name
        (validated with :func:`validate_github_repository_name`). The owner
        is fixed to :data:`PROPOSAL_OWNER`, not derived from user input; a
        canonical fixed-owner ``owner/repo`` is also accepted for direct
        reuse and tests. Any other owner (including a bare-name shape with
        a slash for a different owner) is rejected — there is no owner
        selection.

        The returned branch is validated by the same complete validator as
        a user-typed branch (``validate_integration_branch``).
        """

        stripped = repository.strip() if isinstance(repository, str) else ""
        if re.fullmatch(r"[A-Za-z0-9._-]+", stripped):
            # Live onboarding path: bare repository name, fixed owner.
            try:
                validated_name = validate_github_repository_name(stripped)
            except ValueError as exc:
                raise GitHubInspectionError(
                    f"repository is not a valid GitHub repository name: {exc}"
                ) from exc
            canonical = f"{PROPOSAL_OWNER.lower()}/{validated_name.lower()}"
        else:
            # Optional canonical owner/repo (tests / direct reuse). Parse
            # exactly two nonblank components: validate the owner
            # case-insensitively against the fixed owner and the repo
            # component with the shared repository-name validator. This
            # path does not reuse normalize_github_repository's permissive
            # canonical regex.
            parts = stripped.split("/")
            if len(parts) != 2 or not all(p.strip() for p in parts):
                raise GitHubInspectionError(
                    "repository must be a bare repository name or "
                    "owner/repo with exactly two nonblank components"
                )
            owner, repo = parts
            if owner.strip().lower() != PROPOSAL_OWNER.lower():
                raise GitHubInspectionError(
                    f"repository owner must be {PROPOSAL_OWNER}; "
                    f"got {owner.strip()!r}"
                )
            try:
                validated_repo = validate_github_repository_name(repo)
            except ValueError as exc:
                raise GitHubInspectionError(
                    f"repository is not a valid GitHub repository name: {exc}"
                ) from exc
            canonical = f"{PROPOSAL_OWNER.lower()}/{validated_repo.lower()}"

        url = _GITHUB_REPO_API.format(repo=canonical)
        try:
            body = self._open(url)
        except GitHubInspectionError:
            raise
        except socket.timeout:
            raise GitHubInspectionError(
                "github api request timed out"
            ) from None
        except OSError as exc:
            raise GitHubInspectionError(
                f"github api connectivity error: {exc}"
            ) from exc

        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubInspectionError(
                "github api returned a malformed response"
            ) from exc
        if not isinstance(data, dict):
            raise GitHubInspectionError(
                "github api returned a malformed response"
            )

        default_branch = data.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch.strip():
            raise GitHubInspectionError(
                "github repository default_branch is missing"
            )
        try:
            return validate_integration_branch(default_branch)
        except ValueError as exc:
            raise GitHubInspectionError(
                f"github default_branch is not a valid branch: {exc}"
            ) from exc


def _http_error(exc: urllib.error.HTTPError) -> GitHubInspectionError:
    """Classify an HTTPError into a clear, actionable inspection error."""

    status = getattr(exc, "code", None)
    reason = getattr(exc, "reason", "unknown")
    if status == 401:
        return GitHubInspectionError(
            f"github api authentication failed: {reason}"
        )
    if status == 403:
        return GitHubInspectionError(
            f"github api forbidden: {reason}"
        )
    if status == 404:
        return GitHubInspectionError(
            f"github repository not found: {reason}"
        )
    return GitHubInspectionError(
        f"github api http error {status}: {reason}"
    )


def _url_error(exc: urllib.error.URLError) -> GitHubInspectionError:
    """Classify a URLError into a clear, actionable inspection error."""

    reason = getattr(exc, "reason", "unknown")
    if isinstance(reason, socket.timeout):
        return GitHubInspectionError("github api request timed out")
    return GitHubInspectionError(
        f"github api connectivity error: {reason}"
    )


def _github_token_from_env() -> Optional[str]:
    """Return a GitHub token from the standard env vars, or ``None``.

    The token is read at call time so a token supplied after import is still
    honored. Missing credentials are reported by the caller, not silently
    treated as anonymous.
    """

    for var in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"):
        token = os.environ.get(var)
        if token and token.strip():
            return token.strip()
    return None


def make_proposal_preparer(
    controlled_worktree_root: str,
) -> Callable[[Dict[str, Any]], Dict[str, str]]:
    """Build a proposal preparer for the given controlled worktree root.

    The controlled worktree root must be a nonblank absolute path; it is the
    value read from the current trusted kanban config at factory-call time
    (see :func:`make_onboarding_callbacks`). The canonical checkout root is
    the spec-mandated ``/home/progenitor/AI-main``.
    """

    root = os.path.normpath(os.path.expanduser(controlled_worktree_root))
    if not root or not root.strip():
        raise ValueError("controlled_worktree_root must be nonblank")
    if not os.path.isabs(root):
        raise ValueError("controlled_worktree_root must be an absolute path")
    return _ProposalPreparer(controlled_worktree_root=root)


def make_github_inspector(
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> GitHubDefaultBranchInspector:
    """Build a GitHub default branch inspector with the given timeout."""

    return GitHubDefaultBranchInspector(timeout=timeout)


def make_onboarding_callbacks(
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    config_loader: Optional[Callable[[], Any]] = None,
    transport: Optional[Callable[[str, Dict[str, str], float], bytes]] = None,
) -> Dict[str, Any]:
    """Return the wired onboarding callbacks for the current config.

    The factory reads the current trusted kanban config at call time (via
    ``hermes_cli.config.load_config_readonly`` or an injected zero-arg
    loader), validates ``kanban.controlled_worktree_root``, and returns
    fresh, independent inspector and preparer callbacks. The returned
    mapping matches the ``OnboardingCoordinator`` injection keys
    (``repo_inspector`` and ``proposal_preparer``). ``transport`` is
    forwarded to the inspector for tests; the production default is the
    stdlib opener. External mutations and runtime wiring are intentionally
    out of scope for this slice.
    """

    root = _read_controlled_worktree_root(config_loader)
    return {
        "repo_inspector": GitHubDefaultBranchInspector(
            timeout=timeout, transport=transport
        ).inspect,
        "proposal_preparer": make_proposal_preparer(root),
    }
