"""Durable GitHub provisioning primitives for session-startup onboarding (S2C2a).

Implements the first four steps of the complete provisioning sequence —
operation identity, GitHub repository create/validate, integration branch
establish/validate, and SSH Git reachability — as one narrow
:class:`RepositoryProvisioner` with one injected HTTP seam and one injected
command-runner seam. It is NOT wired into the plugin runtime in this slice;
later executor work (S2C2b+) consumes it.

* The owner is fixed to :data:`PROVISIONING_OWNER` (Adrian-D-Lin), a personal
  account: create operations use the user APIs (``GET /user`` to verify the
  authenticated login, ``POST /user/repos`` to create), and bind operations
  inspect the exact ``GET /repos/Adrian-D-Lin/<repo>`` identity — public
  repositories need no token, and there is no repository-list credential
  probe.
* The operation ID is generated once when absent, persisted through the
  onboarding executor's ``persist`` callback before any external side effect,
  and reused on retries. Durable state lives in the
  ``journal["provisioning"]`` block of the existing onboarding journal.
* On every run the provisioner re-verifies external state: the exact
  repository is re-read and re-validated (identity and visibility), the exact
  SSH identity is probed, and the configured branch is re-read. If a journaled
  repository has disappeared or changed identity/visibility the run fails
  recoverably; if the branch has moved the verified SHA evidence is refreshed
  through persistence, never returned stale.
* Any failure raises :class:`ProvisioningError`, the single actionable
  exception the coordinator turns into ``blocked_recoverable``. This slice
  never marks a project ready and never returns the coordinator's final
  ``created`` status.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from .repository_binding import (
    validate_github_repository_name,
    validate_integration_branch,
)

PROVISIONING_OWNER = "Adrian-D-Lin"

JOURNAL_BLOCK_KEY = "provisioning"

STEP_OPERATION_ID = "operation_id"
STEP_CREDENTIALS = "credentials_verified"
STEP_SSH = "ssh_git_reachable"
STEP_REPOSITORY = "repository_verified"
STEP_BRANCH = "integration_branch_verified"

_DEFAULT_GITHUB_TIMEOUT_SECONDS = 10.0
_DEFAULT_GIT_TIMEOUT_SECONDS = 15.0

_GITHUB_USER_API = "https://api.github.com/user"
_GITHUB_USER_REPOS_API = "https://api.github.com/user/repos"
_GITHUB_REPO_API = "https://api.github.com/repos/{repo}"
_GITHUB_REFS_API = (
    "https://api.github.com/repos/{repo}/git/refs/heads/{branch}"
)

_GITHUB_REQUIRED_HEADERS = (
    ("Accept", "application/vnd.github+json"),
    ("X-GitHub-Api-Version", "2022-11-28"),
    ("User-Agent", "hermes-adrian-kanban"),
)

_GIT_SSH_COMMAND = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"


def make_provisioner(
    transport: Optional[Callable] = None,
    git_runner: Optional[Callable] = None,
    **kwargs: Any,
) -> "RepositoryProvisioner":
    """Create a RepositoryProvisioner with the given injected seams."""
    return RepositoryProvisioner(
        transport=transport, git_runner=git_runner, **kwargs
    )


def operation_id() -> str:
    """Generate a new deterministic-format operation ID."""
    return f"provision-{uuid.uuid4().hex[:12]}"


def ssh_git_command(owner: str, repo_name: str) -> str:
    """Return the exact ``git@github.com:owner/repo.git`` SSH identity."""
    return f"git@github.com:{owner}/{repo_name}.git"


def default_git_runner(
    args: List[str], timeout: float, extra_env: Optional[Dict[str, str]]
) -> Tuple[int, str, str]:
    """Run one fixed git/ssh argv, returning ``(returncode, stdout, stderr)``."""
    env = {**os.environ, **(extra_env or {})}
    try:
        result = subprocess.run(
            args, env=env, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        raise
    except Exception as exc:
        return 1, "", str(exc)


class ProvisioningError(Exception):
    """Single actionable exception for provisioning failures.

    The message names the failing condition and gives a remedy; the
    coordinator places onboarding into ``blocked_recoverable`` on it.
    """


def _require_nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvisioningError(f"provisioning requires nonblank {name}")
    return value.strip()


def _parse_repository_identity(repository: str) -> Tuple[str, str]:
    """Return ``(owner, name)`` for a fixed-owner ``owner/name`` identity."""
    text = _require_nonblank(repository, "repository")
    parts = text.split("/")
    if len(parts) != 2 or not all(p.strip() for p in parts):
        raise ProvisioningError(
            "repository must be owner/name with exactly two nonblank "
            "components"
        )
    owner, name = parts[0].strip(), parts[1].strip()
    if owner.lower() != PROVISIONING_OWNER.lower():
        raise ProvisioningError(
            f"owner must be {PROVISIONING_OWNER}, got '{owner}'"
        )
    try:
        validate_github_repository_name(name)
    except ValueError as exc:
        raise ProvisioningError(str(exc)) from exc
    return owner, name


def _http_error(exc: urllib.error.HTTPError) -> ProvisioningError:
    """Classify an HTTPError into a concise actionable provisioning error."""
    status = exc.code
    body_text = ""
    body = exc.read()
    if body:
        try:
            body_text = body.decode("utf-8", errors="replace")[:2000]
        except Exception:  # pragma: no cover - defensive
            body_text = repr(body[:200])
    if status in (401, 403):
        if "rate limit" in body_text.lower() or "rate limit" in str(exc).lower():
            return ProvisioningError(
                f"github api request failed with {status}: rate limit "
                "exceeded; wait for the limit to reset"
            )
        if status == 401:
            return ProvisioningError("authentication failed")
        return ProvisioningError("forbidden")
    if status == 404:
        return ProvisioningError("github api request failed with 404: not found")
    if status == 422:
        detail = ""
        if body_text:
            try:
                detail = json.loads(body_text).get("message", "")
            except Exception:  # pragma: no cover - defensive
                detail = body_text[:500]
        return ProvisioningError(
            f"github api request failed with 422: unprocessable entity"
            + (f" — {detail}" if detail else "")
        )
    if 500 <= status < 600:
        return ProvisioningError(
            f"github api request failed with {status}: github server "
            "error; retry shortly"
        )
    return ProvisioningError(
        f"github api request failed with {status}: unexpected http status"
    )


def _classify_url_error(exc: Exception) -> ProvisioningError:
    """Classify a connectivity/timeout error (URLError, OSError, TimeoutError).

    A single classifier is used by both the default transport and the
    provisioner methods so every injected or default transport failure
    yields one consistent ``ProvisioningError``.
    """
    if isinstance(exc, TimeoutError):
        return ProvisioningError("github api request timed out")
    reason = getattr(exc, "reason", None)
    if isinstance(reason, socket.timeout):
        return ProvisioningError("github api request timed out; retry shortly")
    if reason:
        return ProvisioningError(f"github api connectivity error: {reason}")
    return ProvisioningError("github api connectivity error; retry shortly")


def _classify_http(exc: urllib.error.HTTPError) -> ProvisioningError:
    """Classify an HTTPError, tolerating a read-only error stream.

    ``exc.read()`` on a closed stream raises ``http.client.IncompleteRead``
    (a ``ValueError``); a transport that already consumed the body must
    still yield a classifiable, non-crashing result.
    """
    try:
        return _http_error(exc)
    except ValueError:  # pragma: no cover - defensive
        status = exc.code
        if status in (401, 403):
            return ProvisioningError(
                "authentication failed" if status == 401 else "forbidden"
            )
        if status == 404:
            return ProvisioningError("github api request failed with 404: not found")
        if 500 <= status < 600:
            return ProvisioningError(
                f"github api request failed with {status}: github server "
                "error; retry shortly"
            )
        return ProvisioningError(
            f"github api request failed with {status}: unexpected http status"
        )


def default_github_transport(
    url: str, headers: Dict[str, str], method: str, body: Optional[bytes], timeout: float
) -> bytes:
    """Fetch ``url`` with the GitHub headers, classifying failures."""
    request = urllib.request.Request(url, data=body, method=method)
    for name, value in _GITHUB_REQUIRED_HEADERS:
        request.add_header(name, value)
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise _classify_http(exc) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise _classify_url_error(exc) from exc


class _Journal:
    """Narrow view over the ``journal["provisioning"]`` block.

    The block's top-level keys are the verified results (each journaled once,
    keyed); ``steps`` records the ordered steps completed so far.
    """

    def __init__(self, onboarding: Dict[str, Any], persist: Callable):
        self._onboarding = onboarding
        self._journal = onboarding.get("journal", {})
        self._block = self._journal.get(JOURNAL_BLOCK_KEY, {})
        self._persist = persist

    @property
    def block(self) -> Dict[str, Any]:
        return self._block

    @property
    def operation_id(self) -> Optional[str]:
        return self._block.get("operation_id")

    @property
    def branch_sha(self) -> Optional[str]:
        return (self._block.get("branch") or {}).get("sha")

    def completed_steps(self) -> List[str]:
        return list(self._block.get("steps", []))

    def checkpoint(self, step: str) -> None:
        steps = list(self._block.get("steps", []))
        if step not in steps:
            steps.append(step)
        self._block["steps"] = steps
        self._journal[JOURNAL_BLOCK_KEY] = self._block
        self._persist(self._onboarding)

    def record_repository(
        self, repo: str, visibility: str, default_branch: str, created: bool
    ) -> None:
        self._block["repository"] = {
            "repository": repo,
            "visibility": visibility,
            "default_branch": default_branch,
            "created": created,
        }

    def record_ssh(self, identity: str) -> None:
        self._block["ssh"] = {"ssh_identity": identity, "verified": True}

    def record_branch(self, branch: str, sha: str, established: bool) -> None:
        self._block["branch"] = {
            "branch": branch,
            "sha": sha,
            "established": established,
        }
        self._block.setdefault("step_evidence", {})[STEP_BRANCH] = {
            "branch": branch,
            "branch_sha": sha,
        }


class RepositoryProvisioner:
    """Execute the durable create/bind/establish/verify provisioning steps.

    One instance carries one injected HTTP seam and one injected
    command-runner seam. All state is read from / written through the
    onboarding journal block; no external resource is mutated more than
    once, and every retry re-verifies the external state.
    """

    def __init__(
        self,
        transport: Optional[
            Callable[[str, Dict[str, str], float], bytes]
        ] = None,
        git_runner: Optional[
            Callable[[List[str], float, Optional[Dict[str, str]]], Tuple[int, str, str]]
        ] = None,
        github_timeout: float = _DEFAULT_GITHUB_TIMEOUT_SECONDS,
        git_timeout: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
        github_token_env: str = "GH_TOKEN",
    ) -> None:
        if not (isinstance(github_timeout, (int, float)) and github_timeout > 0):
            raise ValueError("github timeout must be positive")
        if not (isinstance(git_timeout, (int, float)) and git_timeout >= 0):
            raise ValueError("git timeout must be non-negative")
        self._transport = transport or default_github_transport
        self._git_runner = git_runner or default_git_runner
        self._github_timeout = float(github_timeout)
        self._git_timeout = float(git_timeout)
        self._github_token_env = github_token_env

    # ------------------------------------------------------------------
    # Credential helpers
    # ------------------------------------------------------------------

    def resolve_github_token(self) -> Optional[str]:
        """Return a nonblank token from the token env var, else ``None``."""
        token = os.environ.get(self._github_token_env, "")
        token = token.strip() if isinstance(token, str) else ""
        return token or None

    def fetch_authenticated_login(self, token: str) -> Optional[str]:
        """Return the authenticated GitHub login, or ``None`` on 404."""
        try:
            body = self._transport(
                _GITHUB_USER_API,
                {"Authorization": f"Bearer {token}"},
                "GET",
                None,
                self._github_timeout,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise _classify_http(exc) from exc
        except ProvisioningError as exc:
            if "404" in str(exc):
                return None
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise _classify_url_error(exc) from exc
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProvisioningError(
                "github api returned malformed json for /user"
            ) from None
        if not isinstance(payload, dict):
            raise ProvisioningError(
                "github api returned a malformed /user object"
            )
        login = payload.get("login")
        if not isinstance(login, str) or not login:
            raise ProvisioningError(
                "github api returned a malformed /user object without a usable "
                "login"
            )
        return login

    # ------------------------------------------------------------------
    # Repository helpers
    # ------------------------------------------------------------------

    def fetch_repository(self, owner: str, repo_name: str) -> Optional[Dict[str, Any]]:
        """Return the exact repository payload, or ``None`` on 404."""
        full_repo = f"{owner}/{repo_name}"
        url = _GITHUB_REPO_API.format(repo=full_repo)
        headers: Dict[str, str] = {}
        token = self.resolve_github_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            body = self._transport(url, headers, "GET", None, self._github_timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise _classify_http(exc) from exc
        except ProvisioningError as exc:
            if "404" in str(exc):
                return None
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise _classify_url_error(exc) from exc
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProvisioningError(
                f"github api returned malformed json for /repos/{full_repo}"
            ) from None
        if not isinstance(payload, dict):
            raise ProvisioningError(
                f"malformed repository object for /repos/{full_repo}"
            )
        return payload

    def create_repository(
        self, owner: str, repo_name: str, visibility: str
    ) -> Dict[str, Any]:
        """Create ``owner/repo_name`` via ``POST /user/repos``."""
        full_repo = f"{owner}/{repo_name}"
        token = self.resolve_github_token()
        if not token:
            raise ProvisioningError(
                f"creating {full_repo} requires a GitHub token; set the "
                f"{self._github_token_env} environment variable"
            )
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = json.dumps(
            {"name": repo_name, "private": visibility != "public", "auto_init": True}
        ).encode("utf-8")
        try:
            response_body = self._transport(
                _GITHUB_USER_REPOS_API, headers, "POST", body, self._github_timeout
            )
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProvisioningError(
                f"github api returned malformed json when creating {full_repo}"
            ) from None
        except urllib.error.HTTPError as exc:
            raise _classify_http(exc) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise _classify_url_error(exc) from exc
        if not isinstance(payload, dict):
            raise ProvisioningError(
                f"github api returned a malformed repository object "
                f"when creating {full_repo}"
            )
        return payload

    def verify_repository(
        self, repo: Dict[str, Any], owner: str, repo_name: str, visibility: str
    ) -> None:
        """Verify an exact repository payload's identity and visibility."""
        full_repo = f"{owner}/{repo_name}"
        name = repo.get("name")
        if not isinstance(name, str) or name != repo_name:
            raise ProvisioningError(
                f"github repository identity mismatch: expected name "
                f"'{repo_name}', got {name!r}"
            )
        full_name = repo.get("full_name")
        if not isinstance(full_name, str) or full_name != full_repo:
            raise ProvisioningError(
                f"github repository identity mismatch: expected full_name "
                f"'{full_repo}', got {full_name!r}"
            )
        is_private = repo.get("private")
        requested_private = visibility == "private"
        requested_public = visibility == "public"
        if requested_private or requested_public:
            expected_private = requested_private
            if is_private != expected_private:
                raise ProvisioningError(
                    f"github repository visibility mismatch: expected "
                    f"'{visibility}', got {'private' if is_private else 'public'}"
                )
        # bind mode sets visibility to None: accept the repository's
        # actual visibility without enforcing a requested value.

    # ------------------------------------------------------------------
    # Branch helpers
    # ------------------------------------------------------------------

    def read_branch_ref(
        self, owner: str, repo_name: str, branch: str, token: Optional[str]
    ) -> Optional[str]:
        """Return the configured branch's current SHA, or ``None`` on 404."""
        full_repo = f"{owner}/{repo_name}"
        url = _GITHUB_REFS_API.format(repo=full_repo, branch=branch)
        headers: Dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            body = self._transport(url, headers, "GET", None, self._github_timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise _classify_http(exc) from exc
        except ProvisioningError as exc:
            if "404" in str(exc):
                return None
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise _classify_url_error(exc) from exc
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProvisioningError(
                f"github api returned malformed json for ref "
                f"{full_repo} {branch}"
            ) from None
        if not isinstance(payload, dict):
            raise ProvisioningError(
                f"github api returned malformed json for ref "
                f"{full_repo} {branch}"
            )
        obj = payload.get("object")
        if not isinstance(obj, dict):
            raise ProvisioningError(
                f"github api returned a malformed ref object for "
                f"{full_repo} {branch}"
            )
        sha = obj.get("sha")
        if not isinstance(sha, str) or not sha:
            raise ProvisioningError(
                f"github api returned a malformed ref object without a usable "
                f"object sha for {full_repo} {branch}"
            )
        return sha

    def create_branch_ref(
        self,
        owner: str,
        repo_name: str,
        branch: str,
        token: str,
        repo_payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create the branch ref at the default branch tip; return its SHA."""
        full_repo = f"{owner}/{repo_name}"
        ref_url = _GITHUB_REFS_API.format(repo=full_repo, branch=branch)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if repo_payload is None:
            repo_payload = self.fetch_repository(owner, repo_name)
        if repo_payload is None:
            raise ProvisioningError(f"repository {full_repo} not found")
        default_branch = repo_payload.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise ProvisioningError(
                f"cannot create branch {branch!r} for {full_repo}: the "
                "repository has no usable default_branch"
            )
        base_sha = self.read_branch_ref(owner, repo_name, default_branch, token)
        if not base_sha:
            raise ProvisioningError(
                f"cannot create branch {branch!r} for {full_repo}: the "
                f"default branch '{default_branch}' has no resolvable ref"
            )
        body = json.dumps({"ref": f"refs/heads/{branch}", "sha": base_sha}).encode(
            "utf-8"
        )
        try:
            response_body = self._transport(
                ref_url, headers, "POST", body, self._github_timeout
            )
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProvisioningError(
                f"github api returned malformed json when creating branch "
                f"{branch!r} for {full_repo}"
            ) from None
        except urllib.error.HTTPError as exc:
            raise _classify_http(exc) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise _classify_url_error(exc) from exc
        if not isinstance(payload, dict):
            raise ProvisioningError(
                f"malformed branch creation response for "
                f"{full_repo} branch {branch!r}"
            )
        obj = payload.get("object") if isinstance(payload, dict) else None
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if not isinstance(sha, str) or not sha:
            raise ProvisioningError(
                f"github api created branch {branch!r} for {full_repo} but "
                "returned no object sha"
            )
        return sha

    # ------------------------------------------------------------------
    # SSH helpers
    # ------------------------------------------------------------------

    def probe_ssh_reachability(
        self, owner: str, repo_name: str
    ) -> Tuple[str, Dict[str, str], float]:
        """Run the fixed SSH ls-remote argv; return the journaled evidence."""
        identity = ssh_git_command(owner, repo_name)
        extra_env = {
            "GIT_SSH_COMMAND": _GIT_SSH_COMMAND,
            "GIT_TERMINAL_PROMPT": "0",
        }
        args = ["git", "ls-remote", "--heads", identity]
        try:
            rc, stdout, stderr = self._git_runner(
                args, self._git_timeout, extra_env
            )
        except subprocess.TimeoutExpired as exc:
            raise ProvisioningError(
                "ssh git reachability probe timed out"
            ) from exc
        if rc != 0:
            raise ProvisioningError(
                f"ssh git access failed for {identity}: "
                f"{stderr or 'no stderr available'}"
            )
        return identity, extra_env, self._git_timeout

    def run_provisioning(
        self,
        record: Dict[str, Any],
        persist: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Execute the durable provisioning steps against the journal block.

        ``record`` is the onboarding record (repository, integration_branch,
        mode, visibility). ``persist(onboarding_mapping)`` CAS-writes the whole
        onboarding mapping and returns the newest record; the executor reuses
        that newest record for its later checkpoints. Durable state lives in
        the ``journal[provisioning]`` block; any failure raises
        :class:`ProvisioningError`.
        """
        onboarding = json.loads(record.get("onboarding_json", "{}"))
        journal = _Journal(onboarding, persist)

        # Extract the proposal from the record's onboarding JSON.
        onboarding = json.loads(record.get("onboarding_json", "{}"))
        proposal = (onboarding.get("journal") or {}).get("proposal", {})

        repo_str = proposal.get("repository", "")
        owner, repo_name = _parse_repository_identity(repo_str)
        branch = _require_nonblank(
            proposal.get("integration_branch", "main"), "integration_branch"
        )
        try:
            validate_integration_branch(branch)
        except ValueError as exc:
            raise ProvisioningError(
                f"invalid integration branch: {exc}"
            ) from exc
        mode = proposal.get("mode")
        if mode not in ("create", "bind"):
            raise ProvisioningError(
                f"mode must be 'create' or 'bind', got {mode!r}"
            )
        create_mode = mode == "create"
        visibility = proposal.get("visibility")
        if create_mode and visibility not in ("private", "public"):
            raise ProvisioningError(
                f"visibility must be 'private' or 'public', got {visibility!r}"
            )
        if not create_mode:
            visibility = None

        # Step 1: operation identity — persisted before any external
        # side effect, reused on retries.
        existing_op_id = journal.operation_id
        if existing_op_id is None:
            new_op_id = operation_id()
            journal.block["operation_id"] = new_op_id
            journal.checkpoint(STEP_OPERATION_ID)
        else:
            new_op_id = existing_op_id
        operation_id_val = new_op_id

        # Step 2: GitHub API credentials (create verifies the login;
        # bind tolerates an absent token for a public repository).
        token = self.resolve_github_token()
        if create_mode:
            if not token:
                raise ProvisioningError(
                    "credentials are required; set the "
                    f"{self._github_token_env} environment variable"
                )
            login = self.fetch_authenticated_login(token)
            if login is None:
                raise ProvisioningError(
                    "create mode requires a valid GitHub token that "
                    "authenticates to a personal account"
                )
            if login.lower() != PROVISIONING_OWNER.lower():
                raise ProvisioningError(
                    f"create mode requires the authenticated login to be "
                    f"'{PROVISIONING_OWNER}', got '{login}' — the token "
                    f"authenticates as a different account"
                )
        journal.checkpoint(STEP_CREDENTIALS)

        # Step 3: repository create/validate. The exact repository is
        # re-read and re-validated on every run, retry included.
        repo = self.fetch_repository(owner, repo_name)
        if repo is None:
            if not create_mode:
                raise ProvisioningError(
                    f"bind mode requires {owner}/{repo_name} to already exist"
                )
            # A create-mode repository that disappeared after its
            # checkpoint is not silently re-created: the prior attempt
            # journaled a verified repository, so its absence is
            # reported for investigation instead of a second mutation.
            if STEP_REPOSITORY in journal.completed_steps():
                raise ProvisioningError(
                    f"{owner}/{repo_name} no longer exists on github although "
                    "the journal records it as verified; investigate the "
                    "remote state, then retry"
                )
            repo = self.create_repository(owner, repo_name, visibility)
        self.verify_repository(repo, owner, repo_name, visibility)
        journal.record_repository(
            f"{owner}/{repo_name}",
            "public" if not repo.get("private") else "private",
            repo.get("default_branch", ""),
            STEP_REPOSITORY not in journal.completed_steps(),
        )
        journal.checkpoint(STEP_REPOSITORY)

        # Step 4: SSH Git reachability — exact-repository ls-remote probe.
        # This runs after the repository exists so it can actually succeed.
        identity, ssh_env, ssh_timeout = self.probe_ssh_reachability(
            owner, repo_name
        )
        journal.record_ssh(identity)
        journal.checkpoint(STEP_SSH)

        # Step 5: integration branch establish/validate. The branch is
        # re-read on every run; if it moved, the verified SHA evidence is
        # refreshed through persistence.
        current_sha = self.read_branch_ref(
            owner, repo_name, branch, token
        )
        if current_sha is None:
            if not create_mode:
                raise ProvisioningError(f"branch '{branch}' does not exist")
            if not token:
                raise ProvisioningError(
                    f"establishing branch '{branch}' requires a GitHub "
                    "token"
                )
            current_sha = self.create_branch_ref(
                owner, repo_name, branch, token, repo_payload=repo
            )
            established = True
        else:
            established = False
        journal.record_branch(branch, current_sha, established)
        journal.checkpoint(STEP_BRANCH)

        return {
            "operation_id": operation_id_val,
            "repository": f"{owner}/{repo_name}",
            "mode": mode,
            "visibility": visibility,
            "integration_branch": branch,
            "branch_sha": current_sha,
        }
