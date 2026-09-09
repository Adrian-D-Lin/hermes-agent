from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

from .models import (
    MigrationRejected,
    ReconciliationFinding,
    RollbackSetManifest,
    StoppedStateEvidence,
    canonical_json,
)

__all__ = [
    "capture_rollback_set",
    "verify_rollback_set",
    "restore_rollback_set",
    "hash_path",
]


def _require_path(value, name: str) -> Path:
    if not isinstance(value, Path):
        raise MigrationRejected(f"{name} must be Path")
    return value


def _abs_path(value: Path, name: str) -> Path:
    p = value.expanduser()
    if not p.is_absolute():
        raise MigrationRejected(f"{name} must be absolute")
    return p


def _require_exists(p: Path, name: str) -> None:
    if not p.exists():
        raise MigrationRejected(f"{name} does not exist")


def _require_file(p: Path, name: str) -> None:
    _require_exists(p, name)
    if not p.is_file():
        raise MigrationRejected(f"{name} must be a file")


def _require_dir(p: Path, name: str) -> None:
    _require_exists(p, name)
    if not p.is_dir():
        raise MigrationRejected(f"{name} must be a directory")


def _require_empty_dir(p: Path, name: str) -> None:
    _require_dir(p, name)
    if any(p.iterdir()):
        raise MigrationRejected(f"{name} must be empty")


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _symlink_digest(p: Path) -> str:
    target = os.readlink(p)
    payload = canonical_json({"kind": "symlink", "target": target})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dir_digest(p: Path) -> str:
    entries = []
    for root, dirs, files in os.walk(p, followlinks=False):
        root_path = Path(root)
        for name in list(dirs):
            full = root_path / name
            rel = full.relative_to(p).as_posix()
            if full.is_symlink():
                target = os.readlink(full)
                entries.append({"path": rel, "kind": "symlink", "target": target})
                dirs.remove(name)
            else:
                entries.append({"path": rel, "kind": "directory"})
        for name in files:
            full = root_path / name
            rel = full.relative_to(p).as_posix()
            if full.is_symlink():
                target = os.readlink(full)
                entries.append({"path": rel, "kind": "symlink", "target": target})
            else:
                entries.append({
                    "path": rel,
                    "kind": "file",
                    "digest": _sha256_file(full),
                })
    entries.sort(key=lambda item: (item["path"], item["kind"]))
    return hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest()


def hash_path(p: Path) -> str:
    if p.is_symlink():
        return _symlink_digest(p)
    if p.is_file():
        return _sha256_file(p)
    if p.is_dir():
        return _dir_digest(p)
    raise MigrationRejected(f"cannot hash {p}")


def _copy_tree_preserving_symlinks(src: Path, dst: Path) -> None:
    if src.is_symlink():
        target = os.readlink(src)
        os.symlink(target, dst)
        return
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            _copy_tree_preserving_symlinks(item, dst / item.name)
        return
    if src.is_file():
        shutil.copy2(src, dst)
        return
    raise MigrationRejected(f"cannot copy {src}")


def _read_sqlite_metadata(db_path: Path) -> tuple[dict, dict]:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, type, sql FROM sqlite_master ORDER BY type, name")
        rows = cur.fetchall()
        schema_meta = {
            "tables": [{"name": r[0], "type": r[1], "sql": r[2]} for r in rows]
        }
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'adrian_kanban_migration_%' ORDER BY name"
        )
        mig_tables = [r[0] for r in cur.fetchall()]
        migration_meta = {"migration_tables": mig_tables}
    finally:
        conn.close()
    return schema_meta, migration_meta


def _prepare_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        _require_dir(output_dir, "output_dir")
        _require_empty_dir(output_dir, "output_dir")
    else:
        output_dir.mkdir(parents=True)


def _atomic_copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as tmp_f:
            with src.open("rb") as src_f:
                shutil.copyfileobj(src_f, tmp_f)
        try:
            os.replace(tmp_name, dst)
        except PermissionError:
            with open(tmp_name, "rb") as tmp_f, open(dst, "wb") as dst_f:
                shutil.copyfileobj(tmp_f, dst_f)
                dst_f.flush()
                os.fsync(dst_f.fileno())
            os.unlink(tmp_name)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_copy_symlink(target: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
    try:
        os.symlink(target, temp)
        os.replace(temp, dst)
    except BaseException:
        try:
            if temp.is_file() or temp.is_symlink():
                os.unlink(temp)
        except OSError:
            pass
        raise


def _atomic_copy_tree(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = dst.with_name(dst.name + ".tmp_tree")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    try:
        _copy_tree_preserving_symlinks(src, tmp_dir)
        os.replace(tmp_dir, dst)
    except BaseException:
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
        except OSError:
            pass
        raise


def _remove_if_exists(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        os.unlink(p)
    elif p.is_dir():
        raise MigrationRejected("cannot remove directory")
    elif p.exists():
        raise MigrationRejected("cannot remove path")


def capture_rollback_set(
    *,
    database_path: Path,
    config_path: Path,
    active_release_pointer_path: Path,
    package_path: Path,
    desktop_build_path: Path,
    output_dir: Path,
    health_result: dict,
    stopped_state: StoppedStateEvidence,
    captured_at: int,
) -> RollbackSetManifest:
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
    package_path = _abs_path(
        _require_path(package_path, "package_path"), "package_path"
    )
    desktop_build_path = _abs_path(
        _require_path(desktop_build_path, "desktop_build_path"),
        "desktop_build_path",
    )
    output_dir = _abs_path(_require_path(output_dir, "output_dir"), "output_dir")

    _require_file(database_path, "database_path")
    _require_file(config_path, "config_path")
    _require_exists(active_release_pointer_path, "active_release_pointer_path")
    _require_exists(package_path, "package_path")
    _require_exists(desktop_build_path, "desktop_build_path")

    _prepare_output_dir(output_dir)

    wal_src = database_path.with_name(database_path.name + "-wal")
    shm_src = database_path.with_name(database_path.name + "-shm")
    wal_present = wal_src.exists()
    shm_present = shm_src.exists()

    db_rel = "database.sqlite"
    db_dst = output_dir / db_rel
    shutil.copy2(database_path, db_dst)
    db_digest = _sha256_file(db_dst)

    wal_rel = None
    wal_digest = None
    if wal_present:
        wal_rel = "database.sqlite-wal"
        wal_dst = output_dir / wal_rel
        shutil.copy2(wal_src, wal_dst)
        wal_digest = _sha256_file(wal_dst)

    shm_rel = None
    shm_digest = None
    if shm_present:
        shm_rel = "database.sqlite-shm"
        shm_dst = output_dir / shm_rel
        shutil.copy2(shm_src, shm_dst)
        shm_digest = _sha256_file(shm_dst)

    schema_meta, migration_meta = _read_sqlite_metadata(database_path)
    schema_meta["live_database_path"] = str(database_path)
    schema_meta["live_config_path"] = str(config_path)
    schema_meta["live_pointer_path"] = str(active_release_pointer_path)

    if not wal_present:
        _remove_if_exists(wal_src)
    if not shm_present:
        _remove_if_exists(shm_src)

    config_rel = "config.json"
    config_dst = output_dir / config_rel
    shutil.copy2(config_path, config_dst)
    config_digest = _sha256_file(config_dst)

    pointer_rel = "active_release_pointer"
    pointer_dst = output_dir / pointer_rel
    if active_release_pointer_path.is_symlink():
        pointer_kind = "symlink"
        pointer_target = os.readlink(active_release_pointer_path)
        pointer_payload = canonical_json({"kind": "symlink", "target": pointer_target})
        pointer_dst.write_text(pointer_payload, encoding="utf-8")
        pointer_digest = hashlib.sha256(pointer_payload.encode("utf-8")).hexdigest()
    else:
        pointer_kind = "file"
        pointer_target = None
        shutil.copy2(active_release_pointer_path, pointer_dst)
        pointer_digest = _sha256_file(pointer_dst)

    package_rel = "package"
    package_dst = output_dir / package_rel
    _copy_tree_preserving_symlinks(package_path, package_dst)
    package_digest = hash_path(package_dst)

    desktop_rel = "desktop_build"
    desktop_dst = output_dir / desktop_rel
    _copy_tree_preserving_symlinks(desktop_build_path, desktop_dst)
    desktop_digest = hash_path(desktop_dst)

    schema_metadata_json = canonical_json(schema_meta)
    migration_metadata_json = canonical_json(migration_meta)
    health_result_json = canonical_json(health_result)

    manifest_path = output_dir / "manifest.json"
    manifest = RollbackSetManifest(
        root_path=str(output_dir),
        manifest_path=str(manifest_path),
        database_relative_path=db_rel,
        database_digest=db_digest,
        wal_present=wal_present,
        wal_relative_path=wal_rel,
        wal_digest=wal_digest,
        shm_present=shm_present,
        shm_relative_path=shm_rel,
        shm_digest=shm_digest,
        config_relative_path=config_rel,
        config_digest=config_digest,
        pointer_relative_path=pointer_rel,
        pointer_kind=pointer_kind,
        pointer_target=pointer_target,
        pointer_digest=pointer_digest,
        package_relative_path=package_rel,
        package_digest=package_digest,
        desktop_relative_path=desktop_rel,
        desktop_digest=desktop_digest,
        schema_metadata_json=schema_metadata_json,
        migration_metadata_json=migration_metadata_json,
        health_result_json=health_result_json,
        captured_at=captured_at,
    )
    manifest_path.write_text(manifest.payload, encoding="utf-8")
    return manifest


def verify_rollback_set(
    manifest: RollbackSetManifest,
) -> tuple[ReconciliationFinding, ...]:
    findings: list[ReconciliationFinding] = []
    root = Path(manifest.root_path)
    manifest_file = Path(manifest.manifest_path)

    try:
        observed_payload = manifest_file.read_text(encoding="utf-8")
    except OSError as exc:
        findings.append(
            ReconciliationFinding(
                code="manifest_file",
                passed=False,
                expected_json=canonical_json({"path": manifest.manifest_path}),
                observed_json=canonical_json({"error": "read_failed"}),
                remediation="Restore manifest.json from backup",
            )
        )
        return tuple(findings)

    manifest_passed = observed_payload == manifest.payload
    findings.append(
        ReconciliationFinding(
            code="manifest_file",
            passed=manifest_passed,
            expected_json=canonical_json({"digest": manifest.digest}),
            observed_json=canonical_json({
                "digest": hashlib.sha256(observed_payload.encode("utf-8")).hexdigest()
            }),
            remediation="Re-capture rollback set if manifest mismatch",
        )
    )

    def check_file(rel: str, expected_digest: str, code: str) -> None:
        p = root / rel
        if not p.exists():
            findings.append(
                ReconciliationFinding(
                    code=code,
                    passed=False,
                    expected_json=canonical_json({"digest": expected_digest}),
                    observed_json=canonical_json({"error": "missing"}),
                    remediation=f"Restore {rel} from backup",
                )
            )
            return
        obs_digest = hash_path(p)
        passed = obs_digest == expected_digest
        findings.append(
            ReconciliationFinding(
                code=code,
                passed=passed,
                expected_json=canonical_json({"digest": expected_digest}),
                observed_json=canonical_json({"digest": obs_digest}),
                remediation=f"Restore {rel} from backup",
            )
        )

    check_file(manifest.database_relative_path, manifest.database_digest, "database")

    if manifest.wal_present:
        check_file(manifest.wal_relative_path, manifest.wal_digest, "wal")
    else:
        wal_p = root / "database.sqlite-wal"
        passed = not wal_p.exists()
        findings.append(
            ReconciliationFinding(
                code="wal",
                passed=passed,
                expected_json=canonical_json({"present": False}),
                observed_json=canonical_json({"present": wal_p.exists()}),
                remediation="Remove stray WAL file if present",
            )
        )

    if manifest.shm_present:
        check_file(manifest.shm_relative_path, manifest.shm_digest, "shm")
    else:
        shm_p = root / "database.sqlite-shm"
        passed = not shm_p.exists()
        findings.append(
            ReconciliationFinding(
                code="shm",
                passed=passed,
                expected_json=canonical_json({"present": False}),
                observed_json=canonical_json({"present": shm_p.exists()}),
                remediation="Remove stray SHM file if present",
            )
        )

    check_file(manifest.config_relative_path, manifest.config_digest, "config")
    check_file(manifest.pointer_relative_path, manifest.pointer_digest, "pointer")
    check_file(manifest.package_relative_path, manifest.package_digest, "package")
    check_file(manifest.desktop_relative_path, manifest.desktop_digest, "desktop")

    return tuple(findings)


def restore_rollback_set(
    *,
    manifest: RollbackSetManifest,
    database_path: Path,
    config_path: Path,
    active_release_pointer_path: Path,
    stopped_state: StoppedStateEvidence,
) -> tuple[str, str, str]:
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

    findings = verify_rollback_set(manifest)
    if not all(f.passed for f in findings):
        failed = [f.code for f in findings if not f.passed]
        raise MigrationRejected(f"rollback verification failed: {', '.join(failed)}")

    schema_meta = json.loads(manifest.schema_metadata_json)
    live_db = schema_meta.get("live_database_path")
    live_cfg = schema_meta.get("live_config_path")
    live_ptr = schema_meta.get("live_pointer_path")

    if live_db != str(database_path):
        raise MigrationRejected("database path mismatch with capture metadata")
    if live_cfg != str(config_path):
        raise MigrationRejected("config path mismatch with capture metadata")
    if live_ptr != str(active_release_pointer_path):
        raise MigrationRejected("pointer path mismatch with capture metadata")

    root = Path(manifest.root_path)
    db_src = root / manifest.database_relative_path
    cfg_src = root / manifest.config_relative_path
    ptr_src = root / manifest.pointer_relative_path

    _atomic_copy_file(db_src, database_path)
    db_digest = _sha256_file(database_path)

    wal_src = root / manifest.wal_relative_path if manifest.wal_present else None
    shm_src = root / manifest.shm_relative_path if manifest.shm_present else None

    wal_dst = database_path.with_name(database_path.name + "-wal")
    shm_dst = database_path.with_name(database_path.name + "-shm")

    if manifest.wal_present:
        _atomic_copy_file(wal_src, wal_dst)
    else:
        _remove_if_exists(wal_dst)

    if manifest.shm_present:
        _atomic_copy_file(shm_src, shm_dst)
    else:
        _remove_if_exists(shm_dst)

    _atomic_copy_file(cfg_src, config_path)
    cfg_digest = _sha256_file(config_path)

    if manifest.pointer_kind == "symlink":
        _atomic_copy_symlink(manifest.pointer_target, active_release_pointer_path)
        ptr_digest = _symlink_digest(active_release_pointer_path)
    else:
        _atomic_copy_file(ptr_src, active_release_pointer_path)
        ptr_digest = _sha256_file(active_release_pointer_path)

    return db_digest, cfg_digest, ptr_digest
