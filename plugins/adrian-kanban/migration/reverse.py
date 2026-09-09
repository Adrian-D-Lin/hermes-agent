from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .models import (
    MigrationRejected,
    ReconciliationFinding,
    ReconciliationReport,
    ReverseDisposition,
    ReverseMigrationResult,
    RollbackSetManifest,
    StoppedStateEvidence,
    canonical_json,
    digest_json,
)
from .rollback import restore_rollback_set, verify_rollback_set


def _require_path(value, name: str) -> Path:
    if not isinstance(value, Path):
        raise MigrationRejected(f"{name} must be Path")
    return value


def _abs_path(value: Path, name: str) -> Path:
    path = value.expanduser()
    if not path.is_absolute():
        raise MigrationRejected(f"{name} must be absolute")
    return path


def _require_exists(path: Path, name: str) -> None:
    if not path.exists():
        raise MigrationRejected(f"{name} does not exist")


def _require_file(path: Path, name: str) -> None:
    _require_exists(path, name)
    if not path.is_file():
        raise MigrationRejected(f"{name} must be a file")


def _parse_mutation_authority(config_path: Path) -> str:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MigrationRejected(f"cannot read config: {exc}") from exc

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            authority = data.get("mutation_authority")
            if isinstance(authority, str):
                return authority
            kanban = data.get("kanban")
            if isinstance(kanban, dict):
                authority = kanban.get("mutation_authority")
                if isinstance(authority, str):
                    return authority
    except json.JSONDecodeError:
        pass

    authority = None
    in_kanban = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" ") and not line.startswith("\t"):
            in_kanban = False
            if stripped == "kanban:":
                in_kanban = True
            continue
        if in_kanban and stripped.startswith("mutation_authority:"):
            value = stripped.split(":", 1)[1].strip()
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'") and len(value) >= 2:
                value = value[1:-1]
            authority = value
            break

    if authority is None:
        raise MigrationRejected("mutation_authority not found in config")
    return authority


def _enumerate_plugin_receipts(
    database_path: Path, captured_at: int
) -> tuple[str, ...]:
    uri = f"file:{database_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT idempotency_key FROM adrian_kanban_command_receipts "
            "WHERE created_at > ? ORDER BY idempotency_key",
            (captured_at,),
        ).fetchall()
        return tuple(row[0] for row in rows)
    finally:
        conn.close()


def _get_tasks_snapshot(database_path: Path) -> tuple[int, str]:
    uri = f"file:{database_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        rows = conn.execute(
            "SELECT id, title, body, status, created_at FROM tasks ORDER BY id"
        ).fetchall()
        tasks = [
            {
                "id": row[0],
                "title": row[1],
                "body": row[2],
                "status": row[3],
                "created_at": row[4],
            }
            for row in rows
        ]
        return count, digest_json(tasks)
    finally:
        conn.close()


def _get_captured_tasks_snapshot(manifest: RollbackSetManifest) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(manifest.root_path)
        source_db = root / manifest.database_relative_path
        target_db = Path(temporary_directory) / "database.sqlite"
        shutil.copy2(source_db, target_db)

        if manifest.wal_present:
            source_wal = root / manifest.wal_relative_path
            target_wal = Path(temporary_directory) / "database.sqlite-wal"
            shutil.copy2(source_wal, target_wal)

        if manifest.shm_present:
            source_shm = root / manifest.shm_relative_path
            target_shm = Path(temporary_directory) / "database.sqlite-shm"
            shutil.copy2(source_shm, target_shm)

        return _get_tasks_snapshot(target_db)


def reverse_migration(
    *,
    manifest: RollbackSetManifest,
    database_path: Path,
    config_path: Path,
    active_release_pointer_path: Path,
    stopped_state: StoppedStateEvidence,
    reverse_disposition: ReverseDisposition | None = None,
) -> ReverseMigrationResult:
    if not stopped_state.is_stopped:
        raise MigrationRejected("state is not stopped")

    database_path = _abs_path(
        _require_path(database_path, "database_path"), "database_path"
    )
    config_path = _abs_path(_require_path(config_path, "config_path"), "config_path")
    active_release_pointer_path = _abs_path(
        _require_path(active_release_pointer_path, "active_release_pointer_path"),
        "active_release_pointer_path",
    )
    _require_file(database_path, "database_path")
    _require_file(config_path, "config_path")
    _require_exists(active_release_pointer_path, "active_release_pointer_path")

    authority = _parse_mutation_authority(config_path)
    if authority != "plugin":
        raise MigrationRejected("plugin authority required for reverse migration")

    receipt_keys = _enumerate_plugin_receipts(database_path, manifest.captured_at)
    if receipt_keys:
        if reverse_disposition is None:
            raise MigrationRejected(
                "receipts exist but no reverse_disposition provided"
            )
        if reverse_disposition.approver_identity != "Adrian":
            raise MigrationRejected("approver_identity must be Adrian")
        if reverse_disposition.receipt_keys != receipt_keys:
            raise MigrationRejected(
                "reverse_disposition.receipt_keys must name every receipt exactly"
            )
    elif reverse_disposition is not None:
        raise MigrationRejected("no receipts exist, reverse_disposition must be None")

    findings = verify_rollback_set(manifest)
    if not all(finding.passed for finding in findings):
        failed = [finding.code for finding in findings if not finding.passed]
        raise MigrationRejected(f"rollback verification failed: {', '.join(failed)}")

    source_count, source_hash = _get_captured_tasks_snapshot(manifest)

    restored_db_digest, restored_config_digest, restored_pointer_digest = (
        restore_rollback_set(
            manifest=manifest,
            database_path=database_path,
            config_path=config_path,
            active_release_pointer_path=active_release_pointer_path,
            stopped_state=stopped_state,
        )
    )
    restored_count, restored_hash = _get_tasks_snapshot(database_path)

    reconciliation = ReconciliationReport(
        findings=(
            ReconciliationFinding(
                code="restored_database_digest",
                passed=restored_db_digest == manifest.database_digest,
                expected_json=canonical_json({"digest": manifest.database_digest}),
                observed_json=canonical_json({"digest": restored_db_digest}),
                remediation="Re-restore database from rollback set",
            ),
            ReconciliationFinding(
                code="restored_config_digest",
                passed=restored_config_digest == manifest.config_digest,
                expected_json=canonical_json({"digest": manifest.config_digest}),
                observed_json=canonical_json({"digest": restored_config_digest}),
                remediation="Re-restore config from rollback set",
            ),
            ReconciliationFinding(
                code="restored_pointer_digest",
                passed=restored_pointer_digest == manifest.pointer_digest,
                expected_json=canonical_json({"digest": manifest.pointer_digest}),
                observed_json=canonical_json({"digest": restored_pointer_digest}),
                remediation="Re-restore pointer from rollback set",
            ),
            ReconciliationFinding(
                code="restored_source_snapshot",
                passed=(
                    restored_count == source_count and restored_hash == source_hash
                ),
                expected_json=canonical_json({
                    "count": source_count,
                    "hash": source_hash,
                }),
                observed_json=canonical_json({
                    "count": restored_count,
                    "hash": restored_hash,
                }),
                remediation="Re-restore database from rollback set",
            ),
        ),
        source_count=restored_count,
        source_hash=restored_hash,
        map_count=0,
        map_hash=digest_json([]),
    )

    return ReverseMigrationResult(
        rollback_set_digest=manifest.digest,
        restored_database_digest=restored_db_digest,
        restored_config_digest=restored_config_digest,
        restored_pointer_digest=restored_pointer_digest,
        replayed=False,
        reconciliation=reconciliation,
    )
