"""Read-only dark-install preflight for the Adrian Kanban release."""

from __future__ import annotations

import json
from pathlib import Path

from ..migration.rollback import hash_path
from ..migration.models import canonical_json
from .models import (
    DarkInstallPlan,
    ReleaseFinding,
    ReleaseManifest,
    ReleaseRejected,
    ReleaseVerification,
)


def _require_manifest(manifest: ReleaseManifest) -> None:
    if not isinstance(manifest, ReleaseManifest):
        raise ReleaseRejected(
            "manifest must be a ReleaseManifest; accepted format is the strict "
            "typed record produced by build_release_manifest; remediation: build "
            "the manifest before running preflight"
        )


def _require_name_map(value: object, names: tuple[str, ...], field: str) -> dict[str, Path]:
    if not isinstance(value, dict):
        raise ReleaseRejected(
            f"{field} must be a mapping; accepted format is a dict keyed by every "
            "release name with absolute Path values; remediation: supply one "
            "absolute path per release name"
        )
    if set(value.keys()) != set(names):
        raise ReleaseRejected(
            f"{field} keys must exactly match the release names; accepted format "
            "is a dict keyed by every release name; remediation: include every "
            "release name exactly once"
        )
    result: dict[str, Path] = {}
    for name in names:
        path = value[name]
        if not isinstance(path, Path) or not path.is_absolute():
            raise ReleaseRejected(
                f"{field}[{name}] must be an absolute Path; accepted format is a "
                "resolved absolute path; remediation: resolve the path before "
                "running preflight"
            )
        result[name] = path.resolve()
    return result


def verify_dark_install(
    *,
    manifest: ReleaseManifest,
    profile_package_paths: dict[str, Path],
    component_versions: dict[str, dict],
    component_database_paths: dict[str, Path],
    mutation_authority: str,
) -> ReleaseVerification:
    """Verify a dark install read-only and fail closed on any mismatch.

    Package/Desktop tampering appears as failed findings rather than mutation.
    """
    _require_manifest(manifest)
    expected_profile_paths = _require_name_map(
        profile_package_paths, manifest.profile_names, "profile_package_paths"
    )
    expected_component_paths = _require_name_map(
        component_database_paths, manifest.component_names, "component_database_paths"
    )
    expected_versions = json.loads(manifest.versions_json)
    if not isinstance(component_versions, dict):
        raise ReleaseRejected(
            "component_versions must be a mapping; accepted format is a dict "
            "keyed by every component name with the decoded versions object as "
            "each value; remediation: supply one version object per component"
        )
    if set(component_versions.keys()) != set(manifest.component_names):
        raise ReleaseRejected(
            "component_versions keys must exactly match the component names; "
            "accepted format is a dict keyed by every component name; remediation: "
            "include every component name exactly once"
        )

    findings: list[ReleaseFinding] = []

    try:
        observed_package_digest = hash_path(expected_profile_paths[manifest.profile_names[0]])
    except Exception:
        observed_package_digest = None
    package_passed = observed_package_digest == manifest.package_digest
    findings.append(
        ReleaseFinding(
            code="package_digest",
            passed=package_passed,
            expected_json=canonical_json({"digest": manifest.package_digest}),
            observed_json=canonical_json(
                {"digest": observed_package_digest}
                if observed_package_digest is not None
                else {"error": "unhashable"}
            ),
            remediation="Restore the untampered package directory from the sealed release",
        )
    )

    try:
        observed_desktop_digest = hash_path(manifest.desktop_build_path)
    except Exception:
        observed_desktop_digest = None
    desktop_passed = observed_desktop_digest == manifest.desktop_digest
    findings.append(
        ReleaseFinding(
            code="desktop_digest",
            passed=desktop_passed,
            expected_json=canonical_json({"digest": manifest.desktop_digest}),
            observed_json=canonical_json(
                {"digest": observed_desktop_digest}
                if observed_desktop_digest is not None
                else {"error": "unhashable"}
            ),
            remediation="Restore the untampered closed Desktop build file",
        )
    )

    profile_mismatches = [
        name
        for name in manifest.profile_names
        if expected_profile_paths[name] != manifest.package_root
    ]
    profile_passed = not profile_mismatches
    findings.append(
        ReleaseFinding(
            code="profile_release_identity",
            passed=profile_passed,
            expected_json=canonical_json(
                {name: str(manifest.package_root) for name in manifest.profile_names}
            ),
            observed_json=canonical_json(
                {
                    name: "match" if name not in profile_mismatches else "mismatch"
                    for name in manifest.profile_names
                }
            ),
            remediation="Point every profile at the manifest package directory",
        )
    )

    version_mismatches = [
        name
        for name in manifest.component_names
        if component_versions[name] != expected_versions
    ]
    version_passed = not version_mismatches
    findings.append(
        ReleaseFinding(
            code="component_version_identity",
            passed=version_passed,
            expected_json=canonical_json(expected_versions),
            observed_json=canonical_json(
                {
                    name: "match" if name not in version_mismatches else "mismatch"
                    for name in manifest.component_names
                }
            ),
            remediation="Use the exact decoded versions_json for every component",
        )
    )

    database_mismatches = [
        name
        for name in manifest.component_names
        if expected_component_paths[name] != manifest.database_path
    ]
    database_passed = not database_mismatches
    findings.append(
        ReleaseFinding(
            code="component_database_identity",
            passed=database_passed,
            expected_json=canonical_json(
                {name: str(manifest.database_path) for name in manifest.component_names}
            ),
            observed_json=canonical_json(
                {
                    name: "match" if name not in database_mismatches else "mismatch"
                    for name in manifest.component_names
                }
            ),
            remediation="Point every component at the manifest database identity",
        )
    )

    authority_passed = mutation_authority == "native"
    findings.append(
        ReleaseFinding(
            code="dark_authority",
            passed=authority_passed,
            expected_json=canonical_json({"authority": "native"}),
            observed_json=canonical_json({"authority": mutation_authority}),
            remediation="Select the native authority for dark installs",
        )
    )

    return ReleaseVerification(release_id=manifest.release_id, findings=tuple(findings))


def plan_dark_install(
    *,
    manifest: ReleaseManifest,
    release_root: Path,
    profile_roots: dict[str, Path],
) -> DarkInstallPlan:
    """Return a pure dark-install plan without mutating anything.

    No mkdir, copy, symlink, configuration, database, or authority mutation is
    performed. Future destination/link paths need not exist.
    """
    _require_manifest(manifest)
    if not isinstance(release_root, Path) or not release_root.is_absolute():
        raise ReleaseRejected(
            "release_root must be an absolute Path; accepted format is a resolved "
            "absolute path to the release root; remediation: resolve the release "
            "root before planning"
        )
    release_root = release_root.resolve()
    if not isinstance(profile_roots, dict):
        raise ReleaseRejected(
            "profile_roots must be a mapping; accepted format is a dict keyed by "
            "every profile name with absolute Path values; remediation: supply one "
            "absolute profile root per profile"
        )
    if set(profile_roots.keys()) != set(manifest.profile_names):
        raise ReleaseRejected(
            "profile_roots keys must exactly match the profile names; accepted "
            "format is a dict keyed by every profile name; remediation: include "
            "every profile name exactly once"
        )
    resolved_roots: dict[str, Path] = {}
    for name in manifest.profile_names:
        root = profile_roots[name]
        if not isinstance(root, Path) or not root.is_absolute():
            raise ReleaseRejected(
                f"profile_roots[{name}] must be an absolute Path; accepted format "
                "is a resolved absolute path; remediation: resolve the profile "
                "root before planning"
            )
        resolved_roots[name] = root.resolve()

    package_path = release_root / manifest.release_id
    candidate_links = {
        name: resolved_roots[name] / "dark-candidates" / manifest.release_id
        for name in sorted(manifest.profile_names)
    }
    return DarkInstallPlan(
        release_id=manifest.release_id,
        package_path=package_path,
        profile_candidate_links=candidate_links,
        activation_permitted=False,
        mutation_authority="native",
    )


__all__ = ["plan_dark_install", "verify_dark_install"]

