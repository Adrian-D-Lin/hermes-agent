"""Repository binding utilities for the Adrian Kanban plugin."""

import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple


class RepositoryBindingError(Exception):
    """Raised when repository binding operations fail."""


@dataclass(frozen=True)
class RepositoryBindingResult:
    """Immutable result of a repository binding resolution."""

    head_sha: str
    state: str
    remote_ref: str
    remote_error: Optional[str] = None


def normalize_github_repository(remote: str) -> str:
    """Normalize a supported GitHub remote URL to lowercase owner/repo."""
    if not remote or not isinstance(remote, str):
        raise ValueError("Invalid GitHub repository remote")

    canonical_match = re.match(
        r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", remote
    )
    if canonical_match:
        owner = canonical_match.group(1)
        repo = canonical_match.group(2)
        return f"{owner.lower()}/{repo.lower()}"

    remote = remote.strip()
    if remote.startswith("file://"):
        raise ValueError(
            "Invalid GitHub repository remote: file URLs are not supported"
        )

    normalized = remote
    scp_match = re.match(r"^git@github\.com:(.+)$", normalized)
    if scp_match:
        path = scp_match.group(1)
        if path.endswith(".git"):
            path = path[:-4]
        normalized = f"https://github.com/{path}"

    ssh_match = re.match(r"^ssh://git@github\.com/(.+)$", normalized)
    if ssh_match:
        path = ssh_match.group(1)
        if path.endswith(".git"):
            path = path[:-4]
        normalized = f"https://github.com/{path}"

    https_match = re.match(r"^https://github\.com/(.+)$", normalized)
    if https_match:
        path = https_match.group(1)
        if path.endswith("/"):
            path = path[:-1]
        if path.endswith(".git"):
            path = path[:-4]
        normalized = f"https://github.com/{path}"

    github_match = re.match(r"^https://github\.com/([^/]+)/([^/]+)$", normalized)
    if not github_match:
        raise ValueError(f"Invalid GitHub repository remote: {remote}")

    owner = github_match.group(1)
    repo = github_match.group(2)
    if not owner or not repo:
        raise ValueError(f"Invalid GitHub repository remote: {remote}")
    return f"{owner.lower()}/{repo.lower()}"


def validate_github_repository_name(name: Any) -> str:
    """Validate and return a bare GitHub repository name."""
    if not isinstance(name, str):
        raise ValueError("repository name must be text")
    text = name.strip()
    if not text:
        raise ValueError("repository name is required")
    if len(text) > 100:
        raise ValueError("repository name must be at most 100 characters")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        raise ValueError(
            "repository name contains invalid characters "
            "(letters, digits, dots, hyphens, underscores only)"
        )
    return text


def validate_integration_branch(branch: Any) -> str:
    """Validate and return a Git-safe integration branch name."""
    if not isinstance(branch, str):
        raise ValueError("integration branch must be text")
    text = branch.strip()
    if not text:
        raise ValueError("integration branch is required")
    if len(text) > 100:
        raise ValueError("integration branch must be at most 100 characters")
    if any(ord(char) < 32 or char.isspace() for char in text):
        raise ValueError("integration branch contains invalid characters")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", text):
        raise ValueError("integration branch contains invalid characters")
    if text.startswith("/") or text.endswith("/") or "//" in text:
        raise ValueError("integration branch contains an invalid slash pattern")
    if ".." in text:
        raise ValueError("integration branch contains an invalid dot pattern")
    if text == "@" or "@{" in text:
        raise ValueError("integration branch contains invalid @ syntax")
    if text.endswith(".") or any(
        part.endswith(".lock") for part in text.split("/")
    ):
        raise ValueError("integration branch has an invalid suffix")
    return text


class RepositoryBindingResolver:
    """Resolve a registered repository's configured integration head."""

    def __init__(self, registration: Any, runner: Callable = subprocess.run):
        self.registration = registration
        self.runner = runner

    def _run_git(self, *args: str) -> Tuple[int, str, str]:
        try:
            result = self.runner(
                ["git", *args],
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                encoding="utf-8",
                errors="replace",
                cwd=self.registration.repository_root,
                timeout=30,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            raise
        except FileNotFoundError as exc:
            raise RepositoryBindingError(f"Git executable not found: {exc}") from exc
        except OSError as exc:
            raise RepositoryBindingError(f"Failed to launch git process: {exc}") from exc

    @staticmethod
    def _is_connectivity_failure(stderr: str) -> bool:
        stderr_lower = stderr.lower()
        connectivity_indicators = (
            "could not resolve host",
            "name or service not known",
            "connection timed out",
            "connection refused",
            "network is unreachable",
            "no route to host",
            "temporary failure in name resolution",
            "failed to connect",
            "connection reset by peer",
            "broken pipe",
            "timeout",
            "dns",
        )
        return any(indicator in stderr_lower for indicator in connectivity_indicators)

    @staticmethod
    def _is_auth_failure(stderr: str) -> bool:
        stderr_lower = stderr.lower()
        auth_indicators = (
            "authentication failed",
            "could not read username",
            "could not read password",
            "invalid username or password",
            "authentication required",
            "access denied",
            "permission denied",
            "401",
            "403",
        )
        return any(indicator in stderr_lower for indicator in auth_indicators)

    @staticmethod
    def _validate_sha(sha: str) -> bool:
        sha = sha.strip()
        if len(sha) not in (40, 64):
            return False
        try:
            int(sha, 16)
            return True
        except ValueError:
            return False

    def resolve_integration_head(
        self, allow_offline: bool = True
    ) -> RepositoryBindingResult:
        rc, _stdout, stderr = self._run_git("rev-parse", "--git-common-dir")
        if rc != 0:
            raise RepositoryBindingError(f"Failed to get git common dir: {stderr}")

        rc, stdout, stderr = self._run_git(
            "config", "--get", "remote.origin.url"
        )
        if rc != 0:
            raise RepositoryBindingError(f"Failed to get remote origin URL: {stderr}")

        try:
            normalized_origin = normalize_github_repository(stdout.strip())
        except ValueError as exc:
            raise RepositoryBindingError(f"Invalid origin URL: {exc}") from exc
        if normalized_origin != self.registration.github_repository:
            raise RepositoryBindingError(
                f"origin {normalized_origin} does not match "
                f"{self.registration.github_repository}"
            )

        fetch_ref = (
            self.registration.remote_branch_ref
            + ":"
            + self.registration.remote_tracking_ref
        )
        try:
            rc, _stdout, stderr = self._run_git(
                "fetch", "--no-tags", "origin", fetch_ref
            )
        except subprocess.TimeoutExpired:
            if not allow_offline:
                raise RepositoryBindingError("remote evidence is required")
            state = "degraded_offline"
            remote_error = "GitHub is unreachable"
        else:
            if rc == 0:
                state = "online"
                remote_error = None
            elif self._is_auth_failure(stderr):
                raise RepositoryBindingError(
                    f"authentication failed during fetch: {stderr}"
                )
            elif self._is_connectivity_failure(stderr):
                if not allow_offline:
                    raise RepositoryBindingError("remote evidence is required")
                state = "degraded_offline"
                remote_error = "GitHub is unreachable"
            else:
                raise RepositoryBindingError(f"Fetch failed: {stderr}")

        tracking_ref = self.registration.remote_tracking_ref
        rc, stdout, stderr = self._run_git("rev-parse", tracking_ref)
        if rc != 0:
            raise RepositoryBindingError(f"Failed to resolve tracking ref: {stderr}")
        head_sha = stdout.strip()
        if not self._validate_sha(head_sha):
            raise RepositoryBindingError(f"Invalid SHA: {head_sha}")

        if state == "degraded_offline":
            rc, _stdout, stderr = self._run_git(
                "cat-file", "-e", f"{head_sha}^{{commit}}"
            )
            if rc != 0:
                raise RepositoryBindingError(
                    f"Commit {head_sha} not found locally: {stderr}"
                )

        return RepositoryBindingResult(
            head_sha=head_sha,
            state=state,
            remote_ref=tracking_ref,
            remote_error=remote_error,
        )
