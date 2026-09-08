"""Segment manifest preparation for Adrian Kanban."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

_HEX_40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_UNRESERVED = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def _percent_encode(value: str) -> str:
    encoded = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        if char in _UNRESERVED:
            encoded.append(char)
        else:
            encoded.append(f"%{byte:02X}")
    return "".join(encoded)


def _normalize_id(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value:
        raise ValueError(f"{field} must be nonblank")
    if value != value.strip():
        raise ValueError(f"{field} must not have leading/trailing whitespace")
    return value


def _validate_manifest_path(path: str) -> str:
    if not isinstance(path, str):
        raise ValueError("manifest_path must be a string")
    if path != path.strip():
        raise ValueError("manifest_path must not have leading/trailing whitespace")
    if not path:
        raise ValueError("manifest_path must be nonempty")
    if path.startswith("/"):
        raise ValueError("manifest_path must not be slash-rooted")
    if path.startswith("~"):
        raise ValueError("manifest_path must not start with tilde")
    if "\\" in path:
        raise ValueError("manifest_path must not contain backslash")
    if "?" in path or "#" in path:
        raise ValueError("manifest_path must not contain query/fragment characters")
    if re.match(r"^[A-Za-z]:", path):
        raise ValueError("manifest_path must not be Windows drive path")
    if path.startswith("//"):
        raise ValueError("manifest_path must not be UNC path")
    components = path.split("/")
    for component in components:
        if component in ("", ".", ".."):
            raise ValueError("manifest_path must not contain empty/dot/dot-dot components")
    return path


def _validate_sha(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not (_HEX_40_RE.match(value) or _HEX_64_RE.match(value)):
        raise ValueError(f"{field} must be 40 or 64 lowercase hex")
    return value


def _validate_sha256(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not _HEX_64_RE.match(value):
        raise ValueError(f"{field} must be 64 lowercase hex")
    return value


def _validate_commit(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not (_HEX_40_RE.match(value) or _HEX_64_RE.match(value)):
        raise ValueError(f"{field} must be 40 or 64 lowercase hex")
    return value


def _validate_nonblank_str(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value:
        raise ValueError(f"{field} must be nonblank")
    if value != value.strip():
        raise ValueError(f"{field} must not have leading/trailing whitespace")
    return value


def _validate_positive_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int")
    if value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _validate_strict_dict(value, field: str, expected_keys: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a dict")
    actual_keys = set(value.keys())
    if actual_keys != expected_keys:
        raise ValueError(f"{field} has unknown or missing fields")
    return value


def _validate_design_ref(value, field: str) -> dict:
    ref = _validate_strict_dict(value, field, {"path", "commit", "sha256"})
    _validate_manifest_path(ref["path"])
    _validate_commit(ref["commit"], f"{field}.commit")
    _validate_sha256(ref["sha256"], f"{field}.sha256")
    return ref


def _validate_scope_ref(value, field: str) -> dict:
    ref = _validate_strict_dict(value, field, {"path", "sha256", "kanban_card"})
    _validate_manifest_path(ref["path"])
    _validate_sha256(ref["sha256"], f"{field}.sha256")
    _validate_nonblank_str(ref["kanban_card"], f"{field}.kanban_card")
    return ref


def _validate_segmentation_ref(value, field: str) -> dict:
    ref = _validate_strict_dict(value, field, {"path", "sha256", "kanban_card"})
    _validate_manifest_path(ref["path"])
    _validate_sha256(ref["sha256"], f"{field}.sha256")
    _validate_nonblank_str(ref["kanban_card"], f"{field}.kanban_card")
    return ref


def _validate_segment_review_ref(value, field: str) -> dict:
    ref = _validate_strict_dict(value, field, {"path", "sha256", "kanban_card"})
    _validate_manifest_path(ref["path"])
    _validate_sha256(ref["sha256"], f"{field}.sha256")
    _validate_nonblank_str(ref["kanban_card"], f"{field}.kanban_card")
    return ref


def _validate_repository_registry(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a dict")
    if not value:
        raise ValueError(f"{field} must be nonempty")
    for repo_id, repo in value.items():
        _validate_nonblank_str(repo_id, f"{field} key")
        _validate_strict_dict(repo, f"{field}.{repo_id}", {
            "canonical_remote", "main_ref", "controlled_role"
        })
        _validate_nonblank_str(repo["canonical_remote"], f"{field}.{repo_id}.canonical_remote")
        _validate_nonblank_str(repo["main_ref"], f"{field}.{repo_id}.main_ref")
        _validate_nonblank_str(repo["controlled_role"], f"{field}.{repo_id}.controlled_role")
    return value


def _validate_materialization_policy(value, field: str) -> dict:
    expected = {
        "workspace_id_rule",
        "isolation_mechanism",
        "path_authority",
        "base_rule",
        "writer_rule",
        "integration_rule",
    }
    expected_values = {
        "workspace_id_rule": (
            "<initiative_id>:<segment_id> after canonical escaping"
        ),
        "isolation_mechanism": (
            "trusted-registry-derived Git worktree per declared repository member"
        ),
        "path_authority": (
            "trusted workspace registry; no caller-supplied or agent-selected "
            "absolute path"
        ),
        "base_rule": (
            "materialize each segment immediately before DEV2 from each "
            "member's then-current origin/main after every required predecessor "
            "is merged"
        ),
        "writer_rule": (
            "exactly one active writer binding per logical segment workspace; "
            "all DEV2-DEV4 tasks share the same member set"
        ),
        "integration_rule": (
            "ancestry-preserving merge commit to every declared member's "
            "origin/main before successor admission"
        ),
    }
    policy = _validate_strict_dict(value, field, expected)
    for key in expected:
        _validate_nonblank_str(policy[key], f"{field}.{key}")
        if policy[key] != expected_values[key]:
            raise ValueError(f"{field}.{key} must be {expected_values[key]!r}")
    return policy


def _validate_segment(value, index: int, declared_repos: set[str], trusted_repos: frozenset[str]) -> dict:
    segment = _validate_strict_dict(value, f"segments[{index}]", {
        "segment_id", "ordinal", "title", "boundary", "dependency_ids",
        "dispatch_package_ref", "isolation_mechanism", "repository_members",
        "segment_workspace_id", "primary_scope_items", "readiness_ref"
    })
    _validate_nonblank_str(segment["segment_id"], f"segments[{index}].segment_id")
    _validate_positive_int(segment["ordinal"], f"segments[{index}].ordinal")
    _validate_nonblank_str(segment["title"], f"segments[{index}].title")
    boundary = _validate_strict_dict(segment["boundary"], f"segments[{index}].boundary", {
        "in_scope", "out_of_scope"
    })
    _validate_nonblank_str(boundary["in_scope"], f"segments[{index}].boundary.in_scope")
    _validate_nonblank_str(boundary["out_of_scope"], f"segments[{index}].boundary.out_of_scope")
    if not isinstance(segment["dependency_ids"], list):
        raise ValueError(f"segments[{index}].dependency_ids must be a list")
    for dep in segment["dependency_ids"]:
        _validate_nonblank_str(dep, f"segments[{index}].dependency_ids item")
    dependency_ids = set(segment["dependency_ids"])
    if len(dependency_ids) != len(segment["dependency_ids"]):
        raise ValueError(f"segments[{index}].dependency_ids must be unique")
    dispatch = _validate_strict_dict(segment["dispatch_package_ref"], f"segments[{index}].dispatch_package_ref", {
        "path", "sha256"
    })
    _validate_manifest_path(dispatch["path"])
    _validate_sha256(dispatch["sha256"], f"segments[{index}].dispatch_package_ref.sha256")
    if segment["isolation_mechanism"] != "git_worktree":
        raise ValueError(f"segments[{index}].isolation_mechanism must be git_worktree")
    if not isinstance(segment["repository_members"], list) or not segment["repository_members"]:
        raise ValueError(f"segments[{index}].repository_members must be nonempty list")
    members = set(segment["repository_members"])
    if len(members) != len(segment["repository_members"]):
        raise ValueError(f"segments[{index}].repository_members must be unique")
    for member in members:
        if member not in declared_repos:
            raise ValueError(f"segments[{index}].repository_members contains undeclared repository")
        if member not in trusted_repos:
            raise ValueError(f"segments[{index}].repository_members contains untrusted repository")
    _validate_nonblank_str(segment["segment_workspace_id"], f"segments[{index}].segment_workspace_id")
    if not isinstance(segment["primary_scope_items"], list) or not segment["primary_scope_items"]:
        raise ValueError(f"segments[{index}].primary_scope_items must be nonempty list")
    scope_items = set()
    for item in segment["primary_scope_items"]:
        _validate_positive_int(item, f"segments[{index}].primary_scope_items item")
        scope_items.add(item)
    if len(scope_items) != len(segment["primary_scope_items"]):
        raise ValueError(f"segments[{index}].primary_scope_items must be unique")
    _validate_nonblank_str(segment["readiness_ref"], f"segments[{index}].readiness_ref")
    return segment


@dataclass(frozen=True)
class PreparedSegment:
    segment_id: str
    ordinal: int
    workspace_id: str
    repository_members: tuple[str, ...]
    readiness_ref: str


@dataclass(frozen=True)
class PreparedSegmentManifest:
    result_id: str
    projection_id: str
    manifest_path: str
    manifest_sha: str
    content_digest: str
    initiative_id: str
    segments: tuple[PreparedSegment, ...]
    parsed_segment_definitions: str
    readiness_refs: str
    scope_ref_kanban_card: str
    segmentation_ref_kanban_card: str
    segment_review_ref_kanban_card: str


def prepare_segment_manifest(
    raw_request: dict,
    read_git_blob,
    trusted_repository_ids: frozenset[str],
    expected_initiative_id: str | None = None,
) -> PreparedSegmentManifest:
    if not isinstance(raw_request, dict):
        raise ValueError("raw_request must be a dict")
    _validate_strict_dict(raw_request, "raw_request", {
        "result_id", "projection_id", "manifest_path", "manifest_sha"
    })
    result_id = _normalize_id(raw_request["result_id"], "result_id")
    projection_id = _normalize_id(raw_request["projection_id"], "projection_id")
    manifest_path = _validate_manifest_path(raw_request["manifest_path"])
    manifest_sha = _validate_sha(raw_request["manifest_sha"], "manifest_sha")

    if not isinstance(trusted_repository_ids, frozenset) or not trusted_repository_ids:
        raise ValueError("trusted_repository_ids must be a nonempty frozenset")
    for repo in trusted_repository_ids:
        _normalize_id(repo, "trusted_repository_ids item")

    raw_bytes = read_git_blob(manifest_sha, manifest_path)
    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise ValueError("read_git_blob must return nonempty bytes")

    try:
        manifest_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("manifest bytes must be valid UTF-8")

    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError:
        raise ValueError("manifest must be valid JSON")

    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")

    content_digest = hashlib.sha256(raw_bytes).hexdigest()

    expected_top_keys = {
        "manifest_version", "initiative_id", "initiative_title",
        "design_ref", "scope_ref", "segmentation_ref", "segment_review_ref",
        "repository_registry", "materialization_policy", "segments"
    }
    _validate_strict_dict(manifest, "manifest", expected_top_keys)

    if manifest["manifest_version"] != "1.0.0":
        raise ValueError("manifest_version must be 1.0.0")

    initiative_id = _normalize_id(manifest["initiative_id"], "initiative_id")
    if expected_initiative_id is not None:
        expected = _normalize_id(expected_initiative_id, "expected_initiative_id")
        if expected != initiative_id:
            raise ValueError("initiative mismatch")

    _validate_nonblank_str(manifest["initiative_title"], "initiative_title")
    _validate_design_ref(manifest["design_ref"], "design_ref")
    _validate_scope_ref(manifest["scope_ref"], "scope_ref")
    _validate_segmentation_ref(manifest["segmentation_ref"], "segmentation_ref")
    _validate_segment_review_ref(
        manifest["segment_review_ref"], "segment_review_ref"
    )
    scope_ref_kanban_card = manifest["scope_ref"]["kanban_card"]
    segmentation_ref_kanban_card = manifest["segmentation_ref"]["kanban_card"]
    segment_review_ref_kanban_card = manifest["segment_review_ref"]["kanban_card"]

    registry = _validate_repository_registry(manifest["repository_registry"], "repository_registry")
    declared_repos = set(registry.keys())
    for repo in declared_repos:
        if repo not in trusted_repository_ids:
            raise ValueError("repository not in trusted set")

    _validate_materialization_policy(manifest["materialization_policy"], "materialization_policy")

    if not isinstance(manifest["segments"], list) or not manifest["segments"]:
        raise ValueError("segments must be a nonempty list")

    segments = []
    seen_ids = set()
    seen_ordinals = set()
    seen_readiness = set()
    for i, seg in enumerate(manifest["segments"]):
        validated = _validate_segment(seg, i, declared_repos, trusted_repository_ids)
        seg_id = validated["segment_id"]
        if seg_id in seen_ids:
            raise ValueError(f"duplicate segment_id {seg_id}")
        seen_ids.add(seg_id)
        ordinal = validated["ordinal"]
        if ordinal in seen_ordinals:
            raise ValueError(f"duplicate ordinal {ordinal}")
        seen_ordinals.add(ordinal)
        if ordinal != i + 1:
            raise ValueError(f"segment ordinal must be {i + 1}, got {ordinal}")
        for dep in validated["dependency_ids"]:
            if dep not in seen_ids:
                raise ValueError(f"dependency {dep} not declared earlier")
        readiness = validated["readiness_ref"]
        if readiness in seen_readiness:
            raise ValueError(f"duplicate readiness_ref {readiness}")
        seen_readiness.add(readiness)

        expected_ws = f"{_percent_encode(initiative_id)}:{_percent_encode(seg_id)}"
        if validated["segment_workspace_id"] != expected_ws:
            raise ValueError(f"segment_workspace_id mismatch for {seg_id}")

        segments.append(PreparedSegment(
            segment_id=seg_id,
            ordinal=ordinal,
            workspace_id=validated["segment_workspace_id"],
            repository_members=tuple(validated["repository_members"]),
            readiness_ref=readiness,
        ))

    prepared_segments = tuple(segments)
    parsed_segment_definitions = json.dumps(
        manifest["segments"], sort_keys=True, separators=(",", ":")
    )
    readiness_map = {seg.segment_id: seg.readiness_ref for seg in segments}
    readiness_refs = json.dumps(readiness_map, sort_keys=True, separators=(",", ":"))

    return PreparedSegmentManifest(
        result_id=result_id,
        projection_id=projection_id,
        manifest_path=manifest_path,
        manifest_sha=manifest_sha,
        content_digest=content_digest,
        initiative_id=initiative_id,
        segments=prepared_segments,
        parsed_segment_definitions=parsed_segment_definitions,
        readiness_refs=readiness_refs,
        scope_ref_kanban_card=scope_ref_kanban_card,
        segmentation_ref_kanban_card=segmentation_ref_kanban_card,
        segment_review_ref_kanban_card=segment_review_ref_kanban_card,
    )
