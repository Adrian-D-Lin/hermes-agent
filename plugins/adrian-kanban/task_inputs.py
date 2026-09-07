"""Task input manifest preparation and finalization for the Adrian Kanban verified slice."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "PreparedEntry",
    "PreparedManifest",
    "FinalizedEntry",
    "FinalizedManifest",
    "prepare_task_input_manifest",
    "finalize_task_input_manifest",
    "validate_contract_input_coverage",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonblank_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _validate_workspace_path(path: Any) -> str:
    if not _is_nonblank_str(path):
        raise ValueError("workspace_path must be a nonblank string")
    if path.startswith("/"):
        raise ValueError("workspace_path must not be absolute")
    if "\\" in path:
        raise ValueError("workspace_path must not contain backslash")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError("workspace_path must not contain empty, dot, or dot-dot components")
    return path


def _validate_sha256(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise ValueError("sha256 must be exactly 64 lowercase hexadecimal characters")
    return value


def _validate_commit(value: Any) -> str:
    if not isinstance(value, str) or not _COMMIT_RE.match(value):
        raise ValueError("source_locator must be exactly 40 or 64 lowercase hexadecimal characters")
    return value


@dataclass(frozen=True)
class PreparedEntry:
    workspace_path: str
    sha256: str
    source_kind: str
    source_locator: str | None
    snapshot_bytes: bytes | None
    filename: str | None
    content_type: str | None
    context_guidance: str


@dataclass(frozen=True)
class PreparedManifest:
    version: int
    entries: tuple[PreparedEntry, ...]
    context_ref: dict[str, str]
    declared_inputs_accessible: bool


@dataclass(frozen=True)
class FinalizedEntry:
    workspace_path: str
    sha256: str
    source_kind: str
    source_locator: str
    context_guidance: str


@dataclass(frozen=True)
class FinalizedManifest:
    version: int
    entries: tuple[FinalizedEntry, ...]
    context_ref: dict[str, str]
    canonical_payload: str


def prepare_task_input_manifest(
    raw_manifest: dict[str, Any],
    read_git_blob: Callable[[str, str], bytes],
) -> PreparedManifest:
    if not isinstance(raw_manifest, dict):
        raise ValueError("manifest must be a dict")
    if set(raw_manifest.keys()) != {"version", "entries", "context_ref", "snapshots"}:
        raise ValueError("manifest must have exactly version, entries, context_ref, snapshots")
    if not isinstance(raw_manifest["version"], int) or isinstance(raw_manifest["version"], bool) or raw_manifest["version"] != 1:
        raise ValueError("version must be 1")
    entries_raw = raw_manifest["entries"]
    if not isinstance(entries_raw, list):
        raise ValueError("entries must be a list")
    if len(entries_raw) == 0:
        raise ValueError("entries must be nonempty")
    context_ref_raw = raw_manifest["context_ref"]
    if not isinstance(context_ref_raw, dict):
        raise ValueError("context_ref must be a dict")
    snapshots_raw = raw_manifest["snapshots"]
    if not isinstance(snapshots_raw, dict):
        raise ValueError("snapshots must be a dict")

    seen_paths: set[str] = set()
    git_commit: str | None = None
    prepared_entries: list[PreparedEntry] = []

    for entry in entries_raw:
        if not isinstance(entry, dict):
            raise ValueError("entry must be a dict")
        if entry.get("source_kind") == "git_commit":
            expected_keys = {"workspace_path", "sha256", "source_kind", "source_locator"}
        elif entry.get("source_kind") == "snapshot_attachment":
            expected_keys = {"workspace_path", "sha256", "source_kind"}
        else:
            raise ValueError("source_kind must be git_commit or snapshot_attachment")
        if set(entry.keys()) != expected_keys:
            raise ValueError(f"entry has unknown or missing fields for source_kind {entry.get('source_kind')}")
        path = _validate_workspace_path(entry["workspace_path"])
        if path in seen_paths:
            raise ValueError(f"duplicate workspace_path: {path}")
        seen_paths.add(path)
        sha = _validate_sha256(entry["sha256"])
        kind = entry["source_kind"]
        guidance = context_ref_raw.get(path)
        if not _is_nonblank_str(guidance):
            raise ValueError(f"context_ref missing or blank for {path}")

        if kind == "git_commit":
            commit = _validate_commit(entry["source_locator"])
            if git_commit is None:
                git_commit = commit
            elif commit != git_commit:
                raise ValueError("all Git entries must use the same commit")
            raw_bytes = read_git_blob(commit, path)
            if not isinstance(raw_bytes, bytes):
                raise ValueError("read_git_blob must return bytes")
            actual_sha = hashlib.sha256(raw_bytes).hexdigest()
            if actual_sha != sha:
                raise ValueError("digest mismatch for git entry")
            prepared_entries.append(
                PreparedEntry(
                    workspace_path=path,
                    sha256=sha,
                    source_kind=kind,
                    source_locator=commit,
                    snapshot_bytes=None,
                    filename=None,
                    content_type=None,
                    context_guidance=guidance,
                )
            )
        else:
            snap = snapshots_raw.get(path)
            if snap is None:
                raise ValueError(f"missing snapshot for {path}")
            if not isinstance(snap, dict):
                raise ValueError(f"snapshot for {path} must be a dict")
            if set(snap.keys()) != {"filename", "content_type", "content_base64"}:
                raise ValueError(f"snapshot for {path} has unknown or missing fields")
            filename = snap["filename"]
            content_type = snap["content_type"]
            content_b64 = snap["content_base64"]
            if not _is_nonblank_str(filename):
                raise ValueError("filename must be a nonblank string")
            if not _is_nonblank_str(content_type):
                raise ValueError("content_type must be a nonblank string")
            if not isinstance(content_b64, str):
                raise ValueError("content_base64 must be a string")
            try:
                raw_bytes = base64.b64decode(content_b64, validate=True)
            except Exception:
                raise ValueError("content_base64 is not valid base64") from None
            if len(raw_bytes) == 0:
                raise ValueError("decoded snapshot bytes must be nonempty")
            actual_sha = hashlib.sha256(raw_bytes).hexdigest()
            if actual_sha != sha:
                raise ValueError("digest mismatch for snapshot entry")
            prepared_entries.append(
                PreparedEntry(
                    workspace_path=path,
                    sha256=sha,
                    source_kind=kind,
                    source_locator=None,
                    snapshot_bytes=raw_bytes,
                    filename=filename,
                    content_type=content_type,
                    context_guidance=guidance,
                )
            )

    snapshot_paths = {e.workspace_path for e in prepared_entries if e.source_kind == "snapshot_attachment"}
    if set(snapshots_raw.keys()) != snapshot_paths:
        raise ValueError("snapshots must have exactly one object for every snapshot path and no extra paths")

    context_paths = set(context_ref_raw.keys())
    if context_paths != seen_paths:
        raise ValueError("context_ref keys must exactly equal entry paths")

    return PreparedManifest(
        version=1,
        entries=tuple(prepared_entries),
        context_ref=dict(context_ref_raw),
        declared_inputs_accessible=True,
    )


def finalize_task_input_manifest(
    prepared: PreparedManifest,
    snapshot_attachment_ids: dict[str, int],
) -> FinalizedManifest:
    snapshot_paths = {e.workspace_path for e in prepared.entries if e.source_kind == "snapshot_attachment"}
    if set(snapshot_attachment_ids.keys()) != snapshot_paths:
        raise ValueError("attachment mapping must have exactly one entry for every snapshot path")
    for path, att_id in snapshot_attachment_ids.items():
        if not _is_positive_int(att_id):
            raise ValueError("attachment id must be a positive integer")

    finalized_entries: list[FinalizedEntry] = []
    for entry in prepared.entries:
        if entry.source_kind == "git_commit":
            locator = entry.source_locator
        else:
            locator = str(snapshot_attachment_ids[entry.workspace_path])
        finalized_entries.append(
            FinalizedEntry(
                workspace_path=entry.workspace_path,
                sha256=entry.sha256,
                source_kind=entry.source_kind,
                source_locator=locator,
                context_guidance=entry.context_guidance,
            )
        )

    payload = {
        "version": 1,
        "entries": [
            {
                "workspace_path": e.workspace_path,
                "sha256": e.sha256,
                "source_kind": e.source_kind,
                "source_locator": e.source_locator,
            }
            for e in finalized_entries
        ],
        "context_ref": dict(prepared.context_ref),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return FinalizedManifest(
        version=1,
        entries=tuple(finalized_entries),
        context_ref=dict(prepared.context_ref),
        canonical_payload=canonical,
    )


def validate_contract_input_coverage(
    snapshot: Any,
    prepared_or_finalized: Any,
) -> bool:
    baseline = set(snapshot.baseline_refs)
    governing = set(snapshot.governing_source_refs)
    prior = set(snapshot.prior_record_refs)
    required = baseline | governing | prior
    if isinstance(prepared_or_finalized, PreparedManifest):
        actual = {e.workspace_path for e in prepared_or_finalized.entries}
    elif isinstance(prepared_or_finalized, FinalizedManifest):
        actual = {e.workspace_path for e in prepared_or_finalized.entries}
    else:
        raise ValueError("must be a PreparedManifest or FinalizedManifest")
    if required != actual:
        raise ValueError("coverage mismatch: contract refs do not match manifest paths")
    return True
