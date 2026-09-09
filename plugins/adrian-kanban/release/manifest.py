"""Release manifest construction and persistence for the Adrian Kanban plugin."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..migration.rollback import hash_path
from ..versioning import release_identity
from .models import ReleaseManifest, ReleaseRejected, canonical_json, manifest_from_payload


def build_release_manifest(
    *,
    package_root: Path,
    desktop_build_path: Path,
    database_path: Path,
    source_commit: str,
    profile_names: tuple[str, ...],
    component_names: tuple[str, ...],
    created_at: int,
) -> ReleaseManifest:
    """Build a strict typed release manifest for one closed Desktop build.

    All input paths must already be absolute before resolution. The database
    contents are never hashed; only the package directory and the closed
    Desktop build file are digested.
    """
    if not isinstance(package_root, Path) or not package_root.is_absolute():
        raise ReleaseRejected(
            "package_root must be an absolute Path; accepted format is a resolved "
            "absolute path to the package directory; remediation: resolve the "
            "package directory before building the manifest"
        )
    if not isinstance(desktop_build_path, Path) or not desktop_build_path.is_absolute():
        raise ReleaseRejected(
            "desktop_build_path must be an absolute Path; accepted format is a "
            "resolved absolute path to the closed Desktop build file; remediation: "
            "resolve the Desktop build path before building the manifest"
        )
    if not isinstance(database_path, Path) or not database_path.is_absolute():
        raise ReleaseRejected(
            "database_path must be an absolute Path; accepted format is a resolved "
            "absolute path to the database identity file; remediation: resolve the "
            "database path before building the manifest"
        )
    package_root = package_root.resolve()
    desktop_build_path = desktop_build_path.resolve()
    database_path = database_path.resolve()
    if not package_root.is_dir():
        raise ReleaseRejected(
            "package_root must name an existing directory; accepted format is a "
            "path to the package directory; remediation: point at the built "
            "package directory"
        )
    if not desktop_build_path.is_file():
        raise ReleaseRejected(
            "desktop_build_path must name an existing file; accepted format is a "
            "path to the closed Desktop build file; remediation: point at the "
            "closed Desktop build artifact"
        )
    if not database_path.is_file():
        raise ReleaseRejected(
            "database_path must name an existing file; accepted format is a path "
            "to the database identity file; remediation: point at the database "
            "file used as the release identity"
        )
    versions_json = canonical_json(release_identity())
    return ReleaseManifest(
        package_root=package_root,
        desktop_build_path=desktop_build_path,
        database_path=database_path,
        source_commit=source_commit,
        profile_names=profile_names,
        component_names=component_names,
        created_at=created_at,
        versions_json=versions_json,
        package_digest=hash_path(package_root),
        desktop_digest=hash_path(desktop_build_path),
    )


def write_release_manifest(manifest: ReleaseManifest, target_path: Path) -> None:
    """Publish the canonical payload directly as UTF-8 with no decoration.

    Exact replay of an identical target is a no-op. A different existing target
    is rejected. A new target is published atomically through a same-directory
    temporary file.
    """
    if not isinstance(manifest, ReleaseManifest):
        raise ReleaseRejected(
            "manifest must be a ReleaseManifest; accepted format is the strict "
            "typed record produced by build_release_manifest; remediation: build "
            "the manifest first"
        )
    if not isinstance(target_path, Path) or not target_path.is_absolute():
        raise ReleaseRejected(
            "target_path must be an absolute Path; accepted format is a resolved "
            "absolute path for the manifest file; remediation: resolve the target "
            "path before writing"
        )
    target_path = target_path.resolve()
    payload = manifest.payload
    if target_path.exists():
        try:
            existing = target_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReleaseRejected(
                "existing manifest target could not be read; accepted format is a "
                "UTF-8 file containing the exact canonical payload; remediation: "
                "restore the manifest target and retry"
            ) from exc
        if existing != payload:
            raise ReleaseRejected(
                "existing manifest target differs from the canonical payload; "
                "accepted format is the exact canonical payload text; remediation: "
                "remove or replace the differing target before publishing"
            )
        return
    target_parent = target_path.parent
    try:
        target_parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReleaseRejected(
            "manifest target parent could not be created; accepted format is a "
            "path whose parent directory can be created; remediation: ensure the "
            "target parent is creatable and retry"
        ) from exc
    fd, tmp_name = tempfile.mkstemp(dir=str(target_parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(payload)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, target_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_release_manifest(source_path: Path) -> ReleaseManifest:
    """Load a manifest requiring exact keys and canonical source text."""
    if not isinstance(source_path, Path) or not source_path.is_absolute():
        raise ReleaseRejected(
            "source_path must be an absolute Path; accepted format is a resolved "
            "absolute path to the manifest file; remediation: resolve the source "
            "path before loading"
        )
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise ReleaseRejected(
            "source_path must name an existing file; accepted format is a path to "
            "the manifest file; remediation: point at the published manifest file"
        )
    try:
        payload = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseRejected(
            "manifest source could not be read; accepted format is a UTF-8 file "
            "containing the exact canonical payload; remediation: restore the "
            "manifest file and retry"
        ) from exc
    return manifest_from_payload(payload)


__all__ = [
    "build_release_manifest",
    "load_release_manifest",
    "write_release_manifest",
]

