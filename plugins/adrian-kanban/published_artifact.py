"""Immutable published artifact validation for Adrian Kanban."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .segment_manifest import (
    _validate_manifest_path,
    _validate_sha,
    _validate_sha256,
)


@dataclass(frozen=True)
class VerifiedArtifact:
    path: str
    commit: str
    sha256: str


def verify_published_artifact(
    reference: dict,
    read_published_blob,
) -> VerifiedArtifact:
    if not isinstance(reference, dict):
        raise ValueError("reference must be a dict")
    if set(reference.keys()) != {"path", "commit", "sha256"}:
        raise ValueError("reference must have exactly keys path, commit, sha256")

    path = _validate_manifest_path(reference["path"])
    commit = _validate_sha(reference["commit"], "reference.commit")
    sha256 = _validate_sha256(reference["sha256"], "reference.sha256")

    if not callable(read_published_blob):
        raise TypeError("read_published_blob must be callable")

    blob = read_published_blob(commit, path)
    if not isinstance(blob, bytes):
        raise ValueError("read_published_blob must return bytes")

    actual = hashlib.sha256(blob).digest()
    declared = bytes.fromhex(sha256)
    if actual != declared:
        raise ValueError(
            "sha256 mismatch: verify exact pinned artifact and declared sha256, then retry"
        )

    return VerifiedArtifact(path=path, commit=commit, sha256=sha256)
