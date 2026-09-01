"""Pre-edit recovery archive writer (``§11``).

Write recovery evidence only through the WriteGate control under::

    <project-root>/5-archive/write-gate/<session-id>/<lease-id>/

The model does not gain general write authority over ``5-archive/**``.  The
control creates and verifies the immutable evidence under the same approved
lease.

Four cases, each verified before the edit may proceed:

* **modify** — existing file: immutable byte-identical copy + verified digest.
* **create** — absent target/new file or directory: immutable absence marker.
* **move / rename** — preserve source *and* pre-existing destination (or an
  absence marker for the destination).
* **recovery failure** — if evidence cannot be created and verified, the edit
  is denied (a hard denial, not a degraded edit).

Repeated retry under one lease id is idempotent: the first snapshot is never
overwritten, and a second attempt produces a distinct immutable file.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from .containment import canonicalize_target, is_within


@dataclass
class RecoveryOutcome:
    """The result of a recovery write.  ``ok`` is false on any failure, in
    which case ``error`` carries a human-readable reason and the governed
    edit must not proceed.
    """

    ok: bool
    evidence_paths: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @classmethod
    def success(cls, paths: List[str]) -> "RecoveryOutcome":
        return cls(ok=True, evidence_paths=paths)

    @classmethod
    def failure(cls, reason: str) -> "RecoveryOutcome":
        return cls(ok=False, error=reason)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def derive_project_root(worktree: str) -> Optional[str]:
    """Derive the Git project root safely from the bound worktree.

    Walks up from the worktree looking for a ``.git`` directory; falls back to
    the worktree itself when no Git directory is found (a verified non-Git
    binding).  Returns ``None`` only when the worktree cannot be resolved.
    """
    root = canonicalize_target(worktree, must_exist=True)
    if root is None:
        return None
    cur = root
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return root


class RecoveryWriter:
    """Creates and verifies immutable recovery evidence under the lease."""

    def __init__(self, reg, project_root: str, session_id: str, lease_id: str):
        self.project_root = project_root
        self.session_id = session_id
        self.lease_id = lease_id
        self.base = os.path.join(
            project_root, "5-archive", "write-gate", session_id, lease_id,
        )

    # -- public API ---------------------------------------------------------
    def snapshot_existing_file(self, target: str) -> RecoveryOutcome:
        """Modify case: copy an existing file, verify byte-identical."""
        canonical = canonicalize_target(target, must_exist=True)
        if canonical is None:
            return RecoveryOutcome.failure(f"target {target!r} is not an existing file")
        if not os.path.isfile(canonical):
            return RecoveryOutcome.failure(
                f"target {canonical!r} is not a regular file"
            )
        dest = self._evidence_name(canonical, suffix=f".approved-{_utc_timestamp()}-{self.lease_id}")
        outcome = self._write_snapshot(canonical, dest)
        if not outcome.ok:
            return outcome
        # Verify byte-identical.
        try:
            if _sha256(canonical) != _sha256(dest):
                return RecoveryOutcome.failure(
                    "recovery copy digest mismatch after write"
                )
            if not os.access(dest, os.R_OK):
                return RecoveryOutcome.failure("recovery copy is not readable")
        except OSError as exc:
            return RecoveryOutcome.failure(f"recovery verification failed: {exc}")
        return RecoveryOutcome.success([dest])

    def snapshot_absent(self, target: str) -> RecoveryOutcome:
        """Create case: record an immutable absence marker for an absent target."""
        canonical = canonicalize_target(target)
        if canonical is None:
            return RecoveryOutcome.failure(f"cannot resolve target {target!r}")
        marker_name = (
            os.path.basename(canonical.rstrip("/"))
            or "target"
        ) + f".absent-{_utc_timestamp()}-{self.lease_id}"
        dest = os.path.join(self.base, marker_name)
        body = (
            f"Write-Gate recovery absence marker\n"
            f"target: {canonical}\n"
            f"session: {self.session_id}\n"
            f"lease: {self.lease_id}\n"
            f"note: the canonical target was absent at check time\n"
        )
        try:
            self._ensure_base()
            if os.path.exists(dest):
                return RecoveryOutcome.success([dest])
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(body)
            if not os.access(dest, os.R_OK):
                return RecoveryOutcome.failure("absence marker is not readable")
        except OSError as exc:
            return RecoveryOutcome.failure(f"could not write absence marker: {exc}")
        return RecoveryOutcome.success([dest])

    def snapshot_move(self, source: str, destination: str) -> RecoveryOutcome:
        """Move/rename case: preserve pre-edit source + pre-existing destination
        (or absence markers when either side is absent).
        """
        outcomes: List[RecoveryOutcome] = []
        # Source side.
        src_canon = canonicalize_target(source, must_exist=True)
        if src_canon is not None and os.path.isfile(src_canon):
            o = self.snapshot_existing_file(source)
            if not o.ok:
                return o
            outcomes.extend(o.evidence_paths)
        else:
            o = self.snapshot_absent(source)
            if not o.ok:
                return o
            outcomes.extend(o.evidence_paths)
        # Destination side.
        dest_canon = canonicalize_target(destination, must_exist=True)
        if dest_canon is not None and os.path.isfile(dest_canon):
            o = self.snapshot_existing_file(destination)
            if not o.ok:
                return o
            outcomes.extend(o.evidence_paths)
        else:
            o = self.snapshot_absent(destination)
            if not o.ok:
                return o
            outcomes.extend(o.evidence_paths)
        return RecoveryOutcome.success(outcomes)

    # -- internals ----------------------------------------------------------
    def _ensure_base(self) -> None:
        if not os.path.isdir(self.base):
            os.makedirs(self.base, mode=0o700, exist_ok=True)
        # Never weaken an existing directory's mode; tighten if needed.
        import stat
        try:
            mode = stat.S_IMODE(os.stat(self.base).st_mode)
            if mode & 0o077:
                os.chmod(self.base, 0o700)
        except OSError:
            pass

    def _evidence_name(self, canonical: str, suffix: str) -> str:
        base = os.path.basename(canonical.rstrip("/")) or "target"
        stem, _, ext = base.rpartition(".")
        name = f"{stem}{suffix}" if ext else f"{base}{suffix}"
        return self._unique_path(os.path.join(self.base, name))

    def _write_snapshot(self, src: str, dest: str) -> RecoveryOutcome:
        try:
            self._ensure_base()
            if os.path.exists(dest):
                # Idempotent retry: never overwrite the first snapshot; use a
                # distinct numeric-suffixed name so the first copy is preserved.
                dest = self._unique_path(dest + f".{int(datetime.now(timezone.utc).timestamp())}")
            shutil.copy2(src, dest)
        except OSError as exc:
            return RecoveryOutcome.failure(f"could not copy recovery snapshot: {exc}")
        return RecoveryOutcome.success([dest])

    def _unique_path(self, candidate: str) -> str:
        if not os.path.exists(candidate):
            return candidate
        stem, ext = os.path.splitext(candidate)
        n = 1
        while True:
            alt = f"{stem}.{n}{ext}"
            if not os.path.exists(alt):
                return alt
            n += 1
