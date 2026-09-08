import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path


def _sha256_file(path: Path, chunk_size: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _canonical_json(entries: list) -> bytes:
    return json.dumps(entries, sort_keys=True, ensure_ascii=True).encode("utf-8")


def _sha256_hash(entries: list) -> str:
    return hashlib.sha256(_canonical_json(entries)).hexdigest()


def _readlink_str(path: Path) -> str:
    return os.readlink(str(path))


def inventory(path: Path, *, worktree: bool = False) -> dict:
    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError(f"inventory path does not exist: {path}")

    entries: list[dict] = []

    def _walk(node: Path) -> None:
        rel = node.relative_to(path) if node != path else Path(".")
        try:
            st = node.lstat()
        except OSError as exc:
            raise ValueError(f"cannot stat {node}: {exc}") from exc

        mode = st.st_mode
        if stat.S_ISLNK(mode):
            entries.append({
                "name": str(rel),
                "type": "symlink",
                "target": _readlink_str(node),
                "mode": oct(mode & 0o7777),
            })
        elif stat.S_ISDIR(mode):
            entries.append({
                "name": str(rel),
                "type": "directory",
                "mode": oct(mode & 0o7777),
            })
        elif stat.S_ISREG(mode):
            entries.append({
                "name": str(rel),
                "type": "file",
                "mode": oct(mode & 0o7777),
                "sha256": _sha256_file(node),
            })
        else:
            raise ValueError(f"unsupported special file: {node}")

        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            for child in sorted(node.iterdir(), key=lambda p: p.name):
                _walk(child)

    _walk(path)

    if worktree and entries:
        entries = [
            e for e in entries if not (e["name"] == ".git" and e["type"] == "file")
        ]

    entries.sort(key=lambda e: e["name"])
    return {"entries": entries, "sha256": _sha256_hash(entries)}


def archive_path(
    source: Path,
    destination: Path,
    expected: dict,
    *,
    worktree: bool = False,
) -> dict:
    source = Path(os.path.abspath(str(source)))
    destination = Path(os.path.abspath(str(destination)))

    if source.parent == source or destination.parent == destination:
        raise ValueError("cannot move a filesystem root")

    if (
        source == destination
        or source in destination.parents
        or destination in source.parents
    ):
        raise ValueError("source and destination are identical")

    src_exists = source.exists() or source.is_symlink()
    dst_exists = destination.exists() or destination.is_symlink()

    if src_exists and dst_exists:
        raise ValueError("source and destination both exist; refusing to overwrite")

    if not src_exists and not dst_exists:
        raise ValueError("source and destination both absent")

    if src_exists:
        actual = inventory(source, worktree=worktree)
        if actual != expected:
            raise ValueError(
                f"inventory mismatch for source {source}; expected sha256 "
                f"{expected['sha256']}, got {actual['sha256']}"
            )

    if dst_exists:
        dst_inv = inventory(destination, worktree=worktree)
        if worktree:
            _verify_git_worktree(destination)
        if dst_inv != expected:
            raise ValueError(
                f"destination {destination} inventory mismatch; "
                f"expected sha256 {expected['sha256']}, got {dst_inv['sha256']}"
            )
        return {
            "source": str(source),
            "archive": str(destination),
            "sha256": expected["sha256"],
            "verified": True,
        }

    if worktree:
        _verify_git_worktree(source)
        _perform_git_move(source, destination)
    else:
        os.rename(str(source), str(destination))

    if source.exists() or source.is_symlink():
        raise ValueError(f"source {source} still present after move")

    final = inventory(destination, worktree=worktree)
    if final != expected:
        raise ValueError(
            f"destination inventory mismatch after move; "
            f"expected sha256 {expected['sha256']}, got {final['sha256']}"
        )

    return {
        "source": str(source),
        "archive": str(destination),
        "sha256": expected["sha256"],
        "verified": True,
    }


def _verify_git_worktree(source: Path) -> None:
    toplevel = _git(source, "rev-parse", "--show-toplevel")
    if Path(toplevel) != source:
        raise ValueError(
            f"{source} is not a git worktree toplevel "
            f"(rev-parse --show-toplevel={toplevel})"
        )
    git_file = source / ".git"
    if not git_file.is_file() or git_file.is_symlink():
        raise ValueError(f"{git_file} is not a regular file")
    common_dir_raw = _git(str(source), "rev-parse", "--git-common-dir").strip()
    common_dir = Path(common_dir_raw)
    if not common_dir.is_absolute():
        common_dir = source / common_dir
    if not (common_dir / "HEAD").exists():
        raise ValueError(f"git common dir {common_dir} not located")


def _perform_git_move(source: Path, destination: Path) -> None:
    try:
        common_dir_raw = _git(str(source), "rev-parse", "--git-common-dir").strip()
        common_dir = Path(common_dir_raw)
        if not common_dir.is_absolute():
            common_dir = source / common_dir
        common_dir = common_dir.resolve()
        source_resolved = source.resolve()
        if common_dir == source_resolved or source_resolved in common_dir.parents:
            raise ValueError(
                f"git common dir {common_dir} is not safe to use as cwd for move "
                f"(overlaps source {source})"
            )

        try:
            _git(str(common_dir), "worktree", "move", str(source), str(destination))
        except subprocess.CalledProcessError as exc:
            raise ValueError(
                f"git worktree move failed for {source}: "
                f"{(exc.stderr or exc.stdout or '').strip()[:200]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"git worktree move timed out for {source}") from exc
    except (ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise

    toplevel = _git(str(destination), "rev-parse", "--show-toplevel")
    if Path(toplevel) != destination:
        raise ValueError(
            f"after move, {destination} rev-parse --show-toplevel={toplevel}"
        )


def _git(cwd: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return proc.stdout.strip()
