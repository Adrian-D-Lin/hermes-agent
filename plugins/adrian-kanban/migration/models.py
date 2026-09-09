from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


class MigrationRejected(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise MigrationRejected(f"{name} must be str")
    if value != value.strip():
        raise MigrationRejected(f"{name} must be stripped")
    if not value:
        raise MigrationRejected(f"{name} must be nonblank")
    return value


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise MigrationRejected(f"{name} must be int, not bool")
    if not isinstance(value, int):
        raise MigrationRejected(f"{name} must be int")
    return value


def _require_nonnegative_int(value: Any, name: str) -> int:
    v = _require_int(value, name)
    if v < 0:
        raise MigrationRejected(f"{name} must be nonnegative")
    return v


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise MigrationRejected(f"{name} must be bool")
    return value


def _require_tuple_of_ids(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise MigrationRejected(f"{name} must be tuple")
    for item in value:
        _require_str(item, f"{name} element")
    if len(value) != len(set(value)):
        raise MigrationRejected(f"{name} must have unique elements")
    if list(value) != sorted(value):
        raise MigrationRejected(f"{name} must be sorted")
    return value


def _require_canonical_json_object(value: Any, name: str) -> str:
    _require_str(value, name)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MigrationRejected(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise MigrationRejected(f"{name} must be a JSON object")
    if canonical_json(parsed) != value:
        raise MigrationRejected(f"{name} must be canonical JSON")
    return value


def _require_canonical_json_value(value: Any, name: str) -> str:
    _require_str(value, name)
    try:
        json.loads(value)
    except json.JSONDecodeError as exc:
        raise MigrationRejected(f"{name} must be valid JSON") from exc
    return value


@dataclass(frozen=True, slots=True)
class StoppedStateEvidence:
    all_clients_stopped: bool
    active_claims: int
    writable_processes: int

    def __post_init__(self) -> None:
        _require_bool(self.all_clients_stopped, "all_clients_stopped")
        _require_nonnegative_int(self.active_claims, "active_claims")
        _require_nonnegative_int(self.writable_processes, "writable_processes")

    @property
    def is_stopped(self) -> bool:
        return (
            self.all_clients_stopped
            and self.active_claims == 0
            and self.writable_processes == 0
        )


@dataclass(frozen=True, slots=True)
class MigrationDisposition:
    task_id: str
    action: str
    initiative_id: str | None
    initiative_title: str | None
    initial_lifecycle_phase: str | None
    board_slug: str | None
    approver_identity: str
    approval_reference: str
    evidence_reference: str

    def __post_init__(self) -> None:
        _require_str(self.task_id, "task_id")
        if self.action not in ("retain_legacy", "migrate"):
            raise MigrationRejected("action must be retain_legacy or migrate")
        if self.approver_identity != "Adrian":
            raise MigrationRejected("approver_identity must be Adrian")
        _require_str(self.approver_identity, "approver_identity")
        _require_str(self.approval_reference, "approval_reference")
        _require_str(self.evidence_reference, "evidence_reference")
        target_fields = (
            self.initiative_id,
            self.initiative_title,
            self.initial_lifecycle_phase,
            self.board_slug,
        )
        if self.action == "migrate":
            for i, f in enumerate(target_fields):
                _require_str(f, f"target field {i}")
        else:
            for f in target_fields:
                if f is not None:
                    raise MigrationRejected(
                        "retain_legacy requires all target fields to be None"
                    )

    @property
    def canonical_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "action": self.action,
            "initiative_id": self.initiative_id,
            "initiative_title": self.initiative_title,
            "initial_lifecycle_phase": self.initial_lifecycle_phase,
            "board_slug": self.board_slug,
            "approver_identity": self.approver_identity,
            "approval_reference": self.approval_reference,
            "evidence_reference": self.evidence_reference,
        }

    @property
    def payload(self) -> str:
        return canonical_json(self.canonical_dict)

    @property
    def digest(self) -> str:
        return digest_json(self.canonical_dict)


@dataclass(frozen=True, slots=True)
class DryRunCardClassification:
    task_id: str
    title: str
    status: str
    has_plugin_mapping: bool
    classification: str
    is_triage: bool
    missing_target_identifiers: tuple[str, ...]
    source_digest: str

    def __post_init__(self) -> None:
        _require_str(self.task_id, "task_id")
        _require_str(self.title, "title")
        _require_str(self.status, "status")
        _require_bool(self.has_plugin_mapping, "has_plugin_mapping")
        _require_str(self.classification, "classification")
        _require_bool(self.is_triage, "is_triage")
        _require_tuple_of_ids(
            self.missing_target_identifiers, "missing_target_identifiers"
        )
        _require_str(self.source_digest, "source_digest")
        if self.has_plugin_mapping:
            if self.missing_target_identifiers != ():
                raise MigrationRejected(
                    "mapped card must have empty missing_target_identifiers"
                )
            if self.classification != "mapped":
                raise MigrationRejected("mapped card must have classification 'mapped'")
        else:
            if self.missing_target_identifiers != ("initiative_id",):
                raise MigrationRejected(
                    "legacy card must have missing_target_identifiers "
                    "('initiative_id',)"
                )
            if self.classification != "legacy_pending_migration":
                raise MigrationRejected(
                    "legacy card must have classification 'legacy_pending_migration'"
                )

    @property
    def canonical_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "has_plugin_mapping": self.has_plugin_mapping,
            "classification": self.classification,
            "is_triage": self.is_triage,
            "missing_target_identifiers": list(self.missing_target_identifiers),
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True, slots=True)
class DryRunReport:
    cards: tuple[DryRunCardClassification, ...]
    source_count: int
    source_hash: str
    native_dependency_count: int
    native_dependency_hash: str
    native_run_count: int
    native_run_hash: str
    native_review_count: int
    native_review_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.cards, tuple):
            raise MigrationRejected("cards must be tuple")
        for card in self.cards:
            if not isinstance(card, DryRunCardClassification):
                raise MigrationRejected("cards must contain DryRunCardClassification")
        ids = [c.task_id for c in self.cards]
        if len(ids) != len(set(ids)):
            raise MigrationRejected("cards must have unique task_ids")
        if ids != sorted(ids):
            raise MigrationRejected("cards must be strictly ascending by task_id")
        if len(self.cards) != self.source_count:
            raise MigrationRejected("cards count must match source_count")
        _require_nonnegative_int(self.source_count, "source_count")
        _require_str(self.source_hash, "source_hash")
        _require_nonnegative_int(
            self.native_dependency_count, "native_dependency_count"
        )
        _require_str(self.native_dependency_hash, "native_dependency_hash")
        _require_nonnegative_int(self.native_run_count, "native_run_count")
        _require_str(self.native_run_hash, "native_run_hash")
        _require_nonnegative_int(self.native_review_count, "native_review_count")
        _require_str(self.native_review_hash, "native_review_hash")

    @property
    def canonical_dict(self) -> dict[str, Any]:
        return {
            "cards": [c.canonical_dict for c in self.cards],
            "source_count": self.source_count,
            "source_hash": self.source_hash,
            "native_dependency_count": self.native_dependency_count,
            "native_dependency_hash": self.native_dependency_hash,
            "native_run_count": self.native_run_count,
            "native_run_hash": self.native_run_hash,
            "native_review_count": self.native_review_count,
            "native_review_hash": self.native_review_hash,
        }

    @property
    def payload(self) -> str:
        return canonical_json(self.canonical_dict)

    @property
    def digest(self) -> str:
        return digest_json(self.canonical_dict)


@dataclass(frozen=True, slots=True)
class RollbackSetManifest:
    root_path: str
    manifest_path: str
    database_relative_path: str
    database_digest: str
    wal_present: bool
    wal_relative_path: str | None
    wal_digest: str | None
    shm_present: bool
    shm_relative_path: str | None
    shm_digest: str | None
    config_relative_path: str
    config_digest: str
    pointer_relative_path: str
    pointer_kind: str
    pointer_target: str | None
    pointer_digest: str
    package_relative_path: str
    package_digest: str
    desktop_relative_path: str
    desktop_digest: str
    schema_metadata_json: str
    migration_metadata_json: str
    health_result_json: str
    captured_at: int

    def __post_init__(self) -> None:
        _require_str(self.root_path, "root_path")
        _require_str(self.manifest_path, "manifest_path")
        _require_str(self.database_relative_path, "database_relative_path")
        _require_str(self.database_digest, "database_digest")
        _require_bool(self.wal_present, "wal_present")
        _require_bool(self.shm_present, "shm_present")
        _require_str(self.config_relative_path, "config_relative_path")
        _require_str(self.config_digest, "config_digest")
        _require_str(self.pointer_relative_path, "pointer_relative_path")
        if self.pointer_kind not in ("symlink", "file"):
            raise MigrationRejected("pointer_kind must be symlink or file")
        _require_str(self.pointer_digest, "pointer_digest")
        _require_str(self.package_relative_path, "package_relative_path")
        _require_str(self.package_digest, "package_digest")
        _require_str(self.desktop_relative_path, "desktop_relative_path")
        _require_str(self.desktop_digest, "desktop_digest")
        _require_canonical_json_object(
            self.schema_metadata_json, "schema_metadata_json"
        )
        _require_canonical_json_object(
            self.migration_metadata_json, "migration_metadata_json"
        )
        _require_canonical_json_object(self.health_result_json, "health_result_json")
        _require_int(self.captured_at, "captured_at")
        if self.captured_at <= 0:
            raise MigrationRejected("captured_at must be positive int")
        if self.wal_present:
            _require_str(self.wal_relative_path, "wal_relative_path")
            _require_str(self.wal_digest, "wal_digest")
        else:
            if self.wal_relative_path is not None:
                raise MigrationRejected(
                    "wal_relative_path must be None when wal_present is False"
                )
            if self.wal_digest is not None:
                raise MigrationRejected(
                    "wal_digest must be None when wal_present is False"
                )
        if self.shm_present:
            _require_str(self.shm_relative_path, "shm_relative_path")
            _require_str(self.shm_digest, "shm_digest")
        else:
            if self.shm_relative_path is not None:
                raise MigrationRejected(
                    "shm_relative_path must be None when shm_present is False"
                )
            if self.shm_digest is not None:
                raise MigrationRejected(
                    "shm_digest must be None when shm_present is False"
                )
        if self.pointer_kind == "symlink":
            _require_str(self.pointer_target, "pointer_target")
        else:
            if self.pointer_target is not None:
                raise MigrationRejected(
                    "pointer_target must be None when pointer_kind is file"
                )

    @property
    def canonical_dict(self) -> dict[str, Any]:
        return {
            "root_path": self.root_path,
            "manifest_path": self.manifest_path,
            "database_relative_path": self.database_relative_path,
            "database_digest": self.database_digest,
            "wal_present": self.wal_present,
            "wal_relative_path": self.wal_relative_path,
            "wal_digest": self.wal_digest,
            "shm_present": self.shm_present,
            "shm_relative_path": self.shm_relative_path,
            "shm_digest": self.shm_digest,
            "config_relative_path": self.config_relative_path,
            "config_digest": self.config_digest,
            "pointer_relative_path": self.pointer_relative_path,
            "pointer_kind": self.pointer_kind,
            "pointer_target": self.pointer_target,
            "pointer_digest": self.pointer_digest,
            "package_relative_path": self.package_relative_path,
            "package_digest": self.package_digest,
            "desktop_relative_path": self.desktop_relative_path,
            "desktop_digest": self.desktop_digest,
            "schema_metadata_json": self.schema_metadata_json,
            "migration_metadata_json": self.migration_metadata_json,
            "health_result_json": self.health_result_json,
            "captured_at": self.captured_at,
        }

    @property
    def payload(self) -> str:
        return canonical_json(self.canonical_dict)

    @property
    def digest(self) -> str:
        return digest_json(self.canonical_dict)


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    code: str
    passed: bool
    expected_json: str
    observed_json: str
    remediation: str

    def __post_init__(self) -> None:
        _require_str(self.code, "code")
        _require_bool(self.passed, "passed")
        _require_canonical_json_value(self.expected_json, "expected_json")
        _require_canonical_json_value(self.observed_json, "observed_json")
        _require_str(self.remediation, "remediation")

    @property
    def canonical_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "passed": self.passed,
            "expected_json": self.expected_json,
            "observed_json": self.observed_json,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    findings: tuple[ReconciliationFinding, ...]
    source_count: int
    source_hash: str
    map_count: int
    map_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.findings, tuple):
            raise MigrationRejected("findings must be tuple")
        for f in self.findings:
            if not isinstance(f, ReconciliationFinding):
                raise MigrationRejected("findings must contain ReconciliationFinding")
        codes = [f.code for f in self.findings]
        if len(codes) != len(set(codes)):
            raise MigrationRejected("findings must have unique codes")
        _require_nonnegative_int(self.source_count, "source_count")
        _require_str(self.source_hash, "source_hash")
        _require_nonnegative_int(self.map_count, "map_count")
        _require_str(self.map_hash, "map_hash")

    @property
    def all_passed(self) -> bool:
        return all(f.passed for f in self.findings)

    @property
    def canonical_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.canonical_dict for f in self.findings],
            "source_count": self.source_count,
            "source_hash": self.source_hash,
            "map_count": self.map_count,
            "map_hash": self.map_hash,
        }

    @property
    def payload(self) -> str:
        return canonical_json(self.canonical_dict)

    @property
    def digest(self) -> str:
        return digest_json(self.canonical_dict)


@dataclass(frozen=True, slots=True)
class ForwardMigrationResult:
    operation_id: str
    dry_run_digest: str
    disposition_digest: str
    rollback_set_digest: str
    replayed: bool
    migrated_task_ids: tuple[str, ...]
    retained_task_ids: tuple[str, ...]
    reconciliation: ReconciliationReport

    def __post_init__(self) -> None:
        _require_str(self.operation_id, "operation_id")
        _require_str(self.dry_run_digest, "dry_run_digest")
        _require_str(self.disposition_digest, "disposition_digest")
        _require_str(self.rollback_set_digest, "rollback_set_digest")
        _require_bool(self.replayed, "replayed")
        _require_tuple_of_ids(self.migrated_task_ids, "migrated_task_ids")
        _require_tuple_of_ids(self.retained_task_ids, "retained_task_ids")
        if not isinstance(self.reconciliation, ReconciliationReport):
            raise MigrationRejected("reconciliation must be ReconciliationReport")
        migrated_set = set(self.migrated_task_ids)
        retained_set = set(self.retained_task_ids)
        if migrated_set & retained_set:
            raise MigrationRejected(
                "migrated_task_ids and retained_task_ids must be disjoint"
            )

    @property
    def canonical_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "dry_run_digest": self.dry_run_digest,
            "disposition_digest": self.disposition_digest,
            "rollback_set_digest": self.rollback_set_digest,
            "replayed": self.replayed,
            "migrated_task_ids": list(self.migrated_task_ids),
            "retained_task_ids": list(self.retained_task_ids),
            "reconciliation": self.reconciliation.canonical_dict,
        }

    @property
    def payload(self) -> str:
        return canonical_json(self.canonical_dict)

    @property
    def digest(self) -> str:
        return digest_json(self.canonical_dict)


@dataclass(frozen=True, slots=True)
class ReverseDisposition:
    receipt_keys: tuple[str, ...]
    approver_identity: str
    approval_reference: str
    evidence_reference: str

    def __post_init__(self) -> None:
        _require_tuple_of_ids(self.receipt_keys, "receipt_keys")
        if self.approver_identity != "Adrian":
            raise MigrationRejected("approver_identity must be Adrian")
        _require_str(self.approver_identity, "approver_identity")
        _require_str(self.approval_reference, "approval_reference")
        _require_str(self.evidence_reference, "evidence_reference")

    @property
    def canonical_dict(self) -> dict[str, Any]:
        return {
            "receipt_keys": list(self.receipt_keys),
            "approver_identity": self.approver_identity,
            "approval_reference": self.approval_reference,
            "evidence_reference": self.evidence_reference,
        }

    @property
    def payload(self) -> str:
        return canonical_json(self.canonical_dict)

    @property
    def digest(self) -> str:
        return digest_json(self.canonical_dict)


@dataclass(frozen=True, slots=True)
class ReverseMigrationResult:
    rollback_set_digest: str
    restored_database_digest: str
    restored_config_digest: str
    restored_pointer_digest: str
    replayed: bool
    reconciliation: ReconciliationReport

    def __post_init__(self) -> None:
        _require_str(self.rollback_set_digest, "rollback_set_digest")
        _require_str(self.restored_database_digest, "restored_database_digest")
        _require_str(self.restored_config_digest, "restored_config_digest")
        _require_str(self.restored_pointer_digest, "restored_pointer_digest")
        _require_bool(self.replayed, "replayed")
        if not isinstance(self.reconciliation, ReconciliationReport):
            raise MigrationRejected("reconciliation must be ReconciliationReport")

    @property
    def canonical_dict(self) -> dict[str, Any]:
        return {
            "rollback_set_digest": self.rollback_set_digest,
            "restored_database_digest": self.restored_database_digest,
            "restored_config_digest": self.restored_config_digest,
            "restored_pointer_digest": self.restored_pointer_digest,
            "replayed": self.replayed,
            "reconciliation": self.reconciliation.canonical_dict,
        }

    @property
    def payload(self) -> str:
        return canonical_json(self.canonical_dict)

    @property
    def digest(self) -> str:
        return digest_json(self.canonical_dict)
