from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..migration.models import canonical_json, digest_json

__all__ = [
    "DarkInstallPlan",
    "ReleaseFinding",
    "ReleaseManifest",
    "ReleaseRejected",
    "ReleaseVerification",
    "manifest_from_payload",
]


class ReleaseRejected(ValueError):
    """Raised for invalid public release types or shapes.

    Messages identify the field, accepted format, and remediation without
    echoing raw values.
    """


def _require_abs_path(value: Any, name: str) -> None:
    if not isinstance(value, Path):
        raise ReleaseRejected(
            f"{name} must be a pathlib.Path; accepted format is an absolute "
            "pathlib.Path; remediation: pass the resolved path"
        )
    if not value.is_absolute():
        raise ReleaseRejected(
            f"{name} must be an absolute path; accepted format is an absolute "
            "pathlib.Path; remediation: resolve the path before constructing the "
            "record"
        )


def _require_existing_dir(value: Any, name: str) -> None:
    _require_abs_path(value, name)
    if not value.is_dir():
        raise ReleaseRejected(
            f"{name} must name an existing directory; accepted format is an "
            "absolute path to the package directory; remediation: point at the "
            "built package directory"
        )


def _require_existing_file(value: Any, name: str) -> None:
    _require_abs_path(value, name)
    if not value.is_file():
        raise ReleaseRejected(
            f"{name} must name an existing file; accepted format is an absolute "
            "path to the build or database file; remediation: point at the "
            "existing artifact"
        )


def _require_sorted_unique_ids(value: Any, name: str) -> None:
    if not isinstance(value, tuple) or len(value) == 0:
        raise ReleaseRejected(
            f"{name} must be a nonempty tuple of names; accepted format is a "
            "tuple[str, ...]; remediation: pass the exact release names"
        )
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise ReleaseRejected(
                f"{name} elements must be nonblank stripped strings; accepted "
                "format is str; remediation: pass clean release names"
            )
        if item in seen:
            raise ReleaseRejected(
                f"{name} must contain unique names; accepted format is a tuple "
                "of distinct names; remediation: remove duplicates"
            )
        seen.add(item)
    if list(value) != sorted(value):
        raise ReleaseRejected(
            f"{name} must be sorted; accepted format is a lexicographically "
            "sorted tuple; remediation: sort the names before use"
        )


def _require_finding_str(value: Any, name: str, json_value: bool = False) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseRejected(
            f"{name} must be a nonblank string; accepted format is str; "
            "remediation: supply a descriptive finding field"
        )
    if json_value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ReleaseRejected(
                f"{name} must be valid JSON text; accepted format is a JSON "
                "string; remediation: serialize the expected/observed value with "
                "canonical_json"
            ) from exc
        if canonical_json(parsed) != value:
            raise ReleaseRejected(
                f"{name} must be canonical JSON of its decoded value; accepted "
                "format is the exact result of canonical_json; remediation: "
                "serialize the expected/observed value with canonical_json"
            )


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    package_root: Path
    desktop_build_path: Path
    database_path: Path
    source_commit: str
    profile_names: tuple[str, ...]
    component_names: tuple[str, ...]
    created_at: int
    versions_json: str
    package_digest: str
    desktop_digest: str

    def __post_init__(self) -> None:
        _require_existing_dir(self.package_root, "package_root")
        _require_existing_file(self.desktop_build_path, "desktop_build_path")
        _require_existing_file(self.database_path, "database_path")
        if not isinstance(self.source_commit, str) or not re.fullmatch(
            r"[0-9a-f]{40}", self.source_commit
        ):
            raise ReleaseRejected(
                "source_commit must be a 40-character lowercase hex commit; "
                "accepted format is [0-9a-f]{40}; remediation: pass the sealed "
                "source commit"
            )
        _require_sorted_unique_ids(self.profile_names, "profile_names")
        _require_sorted_unique_ids(self.component_names, "component_names")
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, int) or self.created_at <= 0:
            raise ReleaseRejected(
                "created_at must be a positive integer timestamp; accepted format "
                "is an int epoch seconds value; remediation: pass the build "
                "timestamp"
            )
        if not isinstance(self.versions_json, str):
            raise ReleaseRejected(
                "versions_json must be str; accepted format is the canonical JSON "
                "text of the release identity object; remediation: use "
                "canonical_json(release_identity())"
            )
        try:
            data = json.loads(self.versions_json)
        except json.JSONDecodeError as exc:
            raise ReleaseRejected(
                "versions_json must be valid JSON; accepted format is the "
                "canonical JSON text of the release identity object; remediation: "
                "regenerate versions_json from release_identity()"
            ) from exc
        if not isinstance(data, dict) or not data:
            raise ReleaseRejected(
                "versions_json must decode to a nonempty JSON object; accepted "
                "format is the canonical JSON text of the release identity object; "
                "remediation: regenerate versions_json from release_identity()"
            )
        plugin_version = data.get("plugin_version")
        if (
            not isinstance(plugin_version, str)
            or not plugin_version.strip()
            or plugin_version != plugin_version.strip()
        ):
            raise ReleaseRejected(
                "versions_json must contain a nonblank stripped exact-string "
                "plugin_version; accepted format is a stripped string value for "
                "the plugin_version key; remediation: regenerate versions_json "
                "from release_identity()"
            )
        if canonical_json(data) != self.versions_json:
            raise ReleaseRejected(
                "versions_json must be canonical JSON of its decoded object; "
                "accepted format is the exact result of canonical_json; "
                "remediation: regenerate versions_json with canonical_json"
            )
        for field in ("package_digest", "desktop_digest"):
            value = getattr(self, field)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ReleaseRejected(
                    f"{field} must be a 64-character lowercase hex sha256 digest; "
                    "accepted format is [0-9a-f]{64}; remediation: recompute the "
                    f"{field} with hash_path"
                )

    @property
    def release_id(self) -> str:
        data = json.loads(self.versions_json)
        return f"adrian-kanban-{data['plugin_version']}-{self.package_digest[:12]}"

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "package_root": str(self.package_root),
            "desktop_build_path": str(self.desktop_build_path),
            "database_path": str(self.database_path),
            "source_commit": self.source_commit,
            "profile_names": list(self.profile_names),
            "component_names": list(self.component_names),
            "created_at": self.created_at,
            "versions_json": self.versions_json,
            "package_digest": self.package_digest,
            "desktop_digest": self.desktop_digest,
        }

    @property
    def payload(self) -> str:
        return canonical_json(self.canonical_dict())

    @property
    def digest(self) -> str:
        return digest_json(self.canonical_dict())


def manifest_from_payload(payload: str) -> ReleaseManifest:
    """Reconstruct a strict typed manifest from its exact canonical payload."""
    if not isinstance(payload, str):
        raise ReleaseRejected(
            "manifest payload must be str; accepted format is the canonical JSON "
            "text of a ReleaseManifest.canonical_dict(); remediation: regenerate "
            "the manifest with build_release_manifest"
        )
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ReleaseRejected(
            "manifest payload must be valid JSON; accepted format is the canonical "
            "JSON text of a ReleaseManifest.canonical_dict(); remediation: restore "
            "an unmodified manifest file"
        ) from exc
    if not isinstance(data, dict):
        raise ReleaseRejected(
            "manifest payload must be a JSON object; accepted format is the "
            "canonical JSON text of a ReleaseManifest.canonical_dict(); "
            "remediation: restore an unmodified manifest file"
        )
    expected_keys = set(ReleaseManifest.__dataclass_fields__)
    if set(data.keys()) != expected_keys:
        raise ReleaseRejected(
            "manifest payload keys must exactly match the ReleaseManifest fields; "
            "accepted format is the canonical JSON text of a "
            "ReleaseManifest.canonical_dict(); remediation: restore an unmodified "
            "manifest file"
        )
    if canonical_json(data) != payload:
        raise ReleaseRejected(
            "manifest payload must be canonical source text; accepted format is the "
            "exact result of canonical_json over the manifest fields; remediation: "
            "restore an unmodified manifest file"
        )
    for list_field in ("profile_names", "component_names"):
        if not isinstance(data[list_field], list):
            raise ReleaseRejected(
                f"{list_field} must be a JSON array of names; accepted format is a "
                "list of strings; remediation: restore an unmodified manifest file"
            )
    return ReleaseManifest(
        package_root=Path(data["package_root"]),
        desktop_build_path=Path(data["desktop_build_path"]),
        database_path=Path(data["database_path"]),
        source_commit=data["source_commit"],
        profile_names=tuple(data["profile_names"]),
        component_names=tuple(data["component_names"]),
        created_at=data["created_at"],
        versions_json=data["versions_json"],
        package_digest=data["package_digest"],
        desktop_digest=data["desktop_digest"],
    )


@dataclass(frozen=True, slots=True)
class ReleaseFinding:
    code: str
    passed: bool
    expected_json: str
    observed_json: str
    remediation: str

    def __post_init__(self) -> None:
        _require_finding_str(self.code, "code")
        if not isinstance(self.passed, bool):
            raise ReleaseRejected(
                "passed must be a bool; accepted format is True or False; "
                "remediation: set the finding outcome explicitly"
            )
        _require_finding_str(self.expected_json, "expected_json", json_value=True)
        _require_finding_str(self.observed_json, "observed_json", json_value=True)
        _require_finding_str(self.remediation, "remediation")


@dataclass(frozen=True, slots=True)
class ReleaseVerification:
    release_id: str
    findings: tuple[ReleaseFinding, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.release_id, str) or not self.release_id.strip():
            raise ReleaseRejected(
                "release_id must be a nonblank string; accepted format is the "
                "adrian-kanban-{plugin_version}-{package_digest[:12]} identifier; "
                "remediation: use the manifest release_id"
            )
        if not isinstance(self.findings, tuple):
            raise ReleaseRejected(
                "findings must be a tuple of ReleaseFinding; accepted format is "
                "tuple[ReleaseFinding, ...]; remediation: pass the preflight "
                "finding tuple"
            )
        codes: set[str] = set()
        for finding in self.findings:
            if not isinstance(finding, ReleaseFinding):
                raise ReleaseRejected(
                    "findings must contain only ReleaseFinding records; accepted "
                    "format is tuple[ReleaseFinding, ...]; remediation: build each "
                    "finding with ReleaseFinding"
                )
            if finding.code in codes:
                raise ReleaseRejected(
                    "findings must have unique codes; accepted format is one "
                    "finding per code; remediation: remove duplicate finding "
                    "codes"
                )
            codes.add(finding.code)

    @property
    def all_passed(self) -> bool:
        return all(finding.passed for finding in self.findings)


@dataclass(frozen=True, slots=True)
class DarkInstallPlan:
    release_id: str
    package_path: Path
    profile_candidate_links: dict[str, Path]
    activation_permitted: bool
    mutation_authority: str

    def __post_init__(self) -> None:
        if not isinstance(self.release_id, str) or not self.release_id.strip():
            raise ReleaseRejected(
                "release_id must be a nonblank string; accepted format is the "
                "adrian-kanban-{plugin_version}-{package_digest[:12]} identifier; "
                "remediation: use the manifest release_id"
            )
        _require_abs_path(self.package_path, "package_path")
        if not isinstance(self.profile_candidate_links, dict) or len(self.profile_candidate_links) == 0:
            raise ReleaseRejected(
                "profile_candidate_links must be a nonempty mapping; accepted "
                "format is dict[str, Path] keyed by every profile name; "
                "remediation: supply one absolute candidate link per profile"
            )
        seen: set[str] = set()
        for key, value in self.profile_candidate_links.items():
            if not isinstance(key, str) or not key.strip() or key != key.strip():
                raise ReleaseRejected(
                    "profile_candidate_links keys must be nonblank stripped "
                    "strings; accepted format is str; remediation: pass clean "
                    "profile names"
                )
            if key in seen:
                raise ReleaseRejected(
                    "profile_candidate_links keys must be unique; accepted format "
                    "is distinct profile names; remediation: remove duplicate "
                    "keys"
                )
            seen.add(key)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ReleaseRejected(
                    "profile_candidate_links values must be absolute paths; "
                    "accepted format is an absolute pathlib.Path; remediation: "
                    "resolve each candidate link path"
                )
        if list(self.profile_candidate_links) != sorted(self.profile_candidate_links):
            raise ReleaseRejected(
                "profile_candidate_links keys must be in sorted insertion order; "
                "accepted format is a dict whose keys equal their sorted order; "
                "remediation: insert the candidate links keyed by sorted profile "
                "names"
            )
        if self.activation_permitted is not False:
            raise ReleaseRejected(
                "activation_permitted must be False; accepted format is the bool "
                "False; remediation: dark-install plans never permit activation"
            )
        if self.mutation_authority != "native":
            raise ReleaseRejected(
                "mutation_authority must be exactly 'native'; accepted format is "
                "the string 'native'; remediation: keep the native authority for "
                "dark installs"
            )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "package_path": str(self.package_path),
            "profile_candidate_links": {
                key: str(value)
                for key, value in self.profile_candidate_links.items()
            },
            "activation_permitted": self.activation_permitted,
            "mutation_authority": self.mutation_authority,
        }
