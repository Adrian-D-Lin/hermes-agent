import re
import subprocess

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


def read_published_blob(root: str, commit: str, path: str) -> bytes:
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ValueError(
            "Invalid commit reference; expected a 40- or 64-character lowercase hex SHA."
        )

    heads = _git(root, "ls-remote", "--heads", "origin", "refs/heads/main")
    if heads.returncode != 0:
        raise ValueError(
            "Unable to read origin/main from the remote; verify the 'origin' remote "
            "is reachable and retry."
        )
    try:
        decoded = heads.stdout.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(
            "origin/main returned a malformed reference; verify the remote state and retry."
        ) from None

    lines = [line for line in decoded.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(
            "origin/main did not resolve to a single reference; verify the remote "
            "state and retry."
        )
    parts = lines[0].split()
    if len(parts) != 2:
        raise ValueError(
            "origin/main returned a malformed reference; verify the remote state "
            "and retry."
        )
    remote_tip, ref = parts
    if ref != "refs/heads/main" or not _COMMIT_RE.fullmatch(remote_tip):
        raise ValueError(
            "origin/main returned a malformed reference; verify the remote state "
            "and retry."
        )

    for label, sha in (
        ("pinned commit", commit),
        ("remote origin/main tip", remote_tip),
    ):
        tip = _git(root, "cat-file", "-t", sha)
        if tip.returncode != 0 or tip.stdout.strip() != b"commit":
            raise ValueError(
                f"{label} is not a commit object; fetch origin/main through the "
                "authorized workflow then retry."
            )

    merge = _git(root, "merge-base", "--is-ancestor", commit, remote_tip)
    if merge.returncode == 1:
        raise ValueError(
            "The pinned commit is not an ancestor of origin/main; publish or merge "
            "the manifest to origin/main, then retry."
        )
    if merge.returncode != 0:
        raise ValueError(
            "Ancestry check failed; fetch origin/main through the authorized workflow "
            "then retry."
        )

    blob = _git(root, "cat-file", "blob", f"{commit}:{path}")
    if blob.returncode != 0:
        raise ValueError(
            "The pinned manifest could not be read from the commit; verify the exact "
            "pinned commit and path, then retry."
        )
    return blob.stdout
