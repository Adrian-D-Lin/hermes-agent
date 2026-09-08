"""Narrow read-only helper for historical D4 execution lease verification."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .segment_manifest import _validate_manifest_path


@dataclass(frozen=True)
class VerifiedExecutionLease:
    approval_id: str
    lease_id: str
    session_id: str
    worktree_path: str
    approved_folder: str
    execution_started_at: str
    execution_finished_at: str


def _normalize_path(value: str) -> str:
    """Lexically normalize an absolute path without resolving symlinks."""
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a non-empty string")
    if not Path(value).is_absolute():
        raise ValueError("path must be absolute")
    return os.path.normcase(os.path.normpath(value))


def _parse_aware_iso(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty ISO timestamp string")
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(f"{field} must be a valid ISO timestamp") from None
    if dt.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return dt


def _require_nonblank(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    return value


def verify_execution_lease(
    registry,
    *,
    approval_id: str,
    lease_id: str,
    session_id: str,
    worktree_path: str,
    document_paths: list[str],
    execution_started_at: str,
    execution_finished_at: str,
) -> VerifiedExecutionLease:
    approval_id = _require_nonblank(approval_id, "approval_id")
    lease_id = _require_nonblank(lease_id, "lease_id")
    session_id = _require_nonblank(session_id, "session_id")
    worktree_norm = _normalize_path(worktree_path)
    if not isinstance(document_paths, list) or not document_paths:
        raise ValueError("document_paths must be a non-empty list")
    seen_docs: set[str] = set()
    for doc in document_paths:
        if not isinstance(doc, str) or not doc.strip():
            raise ValueError("document_paths entries must be non-blank strings")
        _validate_manifest_path(doc)
        if doc in seen_docs:
            raise ValueError("document_paths must be unique")
        seen_docs.add(doc)
    started_dt = _parse_aware_iso(execution_started_at, "execution_started_at")
    finished_dt = _parse_aware_iso(execution_finished_at, "execution_finished_at")
    try:
        request = registry.get_request(approval_id)
    except Exception:
        raise ValueError(
            "Unable to load Write-Gate authorization records; verify registry availability and retry."
        ) from None
    if request is None:
        raise ValueError("approval request not found")
    try:
        lease = registry.get_lease(lease_id)
    except Exception:
        raise ValueError(
            "Unable to load Write-Gate authorization records; verify registry availability and retry."
        ) from None
    if lease is None:
        raise ValueError("lease not found")
    if request.request_id != approval_id:
        raise ValueError("approval request identity mismatch")
    if lease.lease_id != lease_id:
        raise ValueError("lease identity mismatch")
    req_session = _require_nonblank(request.session_id, "request.session_id")
    if req_session != session_id:
        raise ValueError("request session mismatch")
    req_worktree = _normalize_path(request.confirmed_worktree)
    if req_worktree != worktree_norm:
        raise ValueError("request worktree mismatch")
    if request.decision != "once":
        raise ValueError("request decision must be 'once'")
    req_folder = _normalize_path(request.narrowest_folder)
    req_decision_at = _parse_aware_iso(request.decision_at, "request.decision_at")
    lease_session = _require_nonblank(lease.session_id, "lease.session_id")
    if lease_session != session_id:
        raise ValueError("lease session mismatch")
    lease_worktree = _normalize_path(lease.confirmed_worktree)
    if lease_worktree != worktree_norm:
        raise ValueError("lease worktree mismatch")
    if lease.approval_reference != approval_id:
        raise ValueError("lease approval reference mismatch")
    lease_folder = _normalize_path(lease.approved_folder)
    lease_approved_at = _parse_aware_iso(lease.approved_at, "lease.approved_at")
    lease_expires_at = _parse_aware_iso(lease.expires_at, "lease.expires_at")
    if req_folder != lease_folder:
        raise ValueError("request and lease folders mismatch")
    if lease.status not in ("active", "expired"):
        raise ValueError(
            "lease status must be active or expired; historical revocation timing cannot be verified"
        )
    if not (
        req_decision_at
        <= lease_approved_at
        <= started_dt
        <= finished_dt
        <= lease_expires_at
    ):
        raise ValueError("timestamp ordering violated")
    for doc in document_paths:
        target = os.path.normcase(os.path.normpath(os.path.join(worktree_norm, doc)))
        if os.path.commonpath([target, lease_folder]) != lease_folder:
            raise ValueError("document path escapes approved folder")
    return VerifiedExecutionLease(
        approval_id=approval_id,
        lease_id=lease_id,
        session_id=session_id,
        worktree_path=worktree_norm,
        approved_folder=lease_folder,
        execution_started_at=execution_started_at,
        execution_finished_at=execution_finished_at,
    )
