"""Disposable migration/rollback tests derived from S3 brief sections 8-10."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture(scope="module")
def migration_modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_migration"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    modules = {
        name: importlib.import_module(f"{package_name}.migration.{name}")
        for name in ("models", "rollback", "engine", "reverse")
    }
    modules["schema"] = importlib.import_module(f"{package_name}.schema")
    yield modules
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


def _database(tmp_path: Path, modules, tasks=("task-a", "task-b")) -> Path:
    database_path = (tmp_path / "kanban.sqlite3").resolve()
    with kb.connect_closing(database_path) as conn:
        modules["schema"].create_schema(conn)
        for index, task_id in enumerate(tasks, start=1):
            conn.execute(
                "INSERT INTO tasks (id, title, body, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    f"Title {task_id}",
                    f"Body {task_id}",
                    "triage" if index == 1 else "ready",
                    index,
                ),
            )
        if len(tasks) > 1:
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (tasks[0], tasks[1]),
            )
        conn.commit()
    return database_path


def _rollback_inputs(tmp_path: Path, database_path: Path):
    config_path = (tmp_path / "config.yaml").resolve()
    config_path.write_text("kanban:\n  mutation_authority: native\n", encoding="utf-8")
    pointer_path = (tmp_path / "current-release").resolve()
    pointer_path.write_text("release-v1", encoding="utf-8")
    package_path = (tmp_path / "release-v1").resolve()
    package_path.mkdir()
    (package_path / "plugin.py").write_text("VERSION = 1\n", encoding="utf-8")
    (package_path / "empty").mkdir()
    desktop_path = (tmp_path / "Hermes.exe").resolve()
    desktop_path.write_bytes(b"desktop-v1")
    output_dir = (tmp_path / "rollback-set").resolve()
    return {
        "database_path": database_path,
        "config_path": config_path,
        "active_release_pointer_path": pointer_path,
        "package_path": package_path,
        "desktop_build_path": desktop_path,
        "output_dir": output_dir,
        "health_result": {"healthy": True, "authority": "native"},
    }


def _stopped(models):
    return models.StoppedStateEvidence(True, 0, 0)


def _capture(tmp_path: Path, database_path: Path, modules):
    values = _rollback_inputs(tmp_path, database_path)
    manifest = modules["rollback"].capture_rollback_set(
        **values,
        stopped_state=_stopped(modules["models"]),
        captured_at=1_700_000_000,
    )
    return manifest, values


def _disposition(models, task_id: str, action: str):
    target = action == "migrate"
    return models.MigrationDisposition(
        task_id=task_id,
        action=action,
        initiative_id="initiative-1" if target else None,
        initiative_title="Initiative One" if target else None,
        initial_lifecycle_phase="D1" if target else None,
        board_slug="default" if target else None,
        approver_identity="Adrian",
        approval_reference=f"approval:{task_id}",
        evidence_reference=f"evidence:{task_id}",
    )


def test_dry_run_is_deterministic_read_only_and_reports_triage(
    tmp_path, migration_modules
):
    engine = migration_modules["engine"]
    database_path = _database(tmp_path, migration_modules)
    before = database_path.read_bytes()

    first = engine.dry_run(database_path)
    second = engine.dry_run(database_path)

    assert first.payload == second.payload
    assert first.digest == second.digest
    assert database_path.read_bytes() == before
    assert [card.task_id for card in first.cards] == ["task-a", "task-b"]
    assert first.cards[0].is_triage is True
    assert first.cards[1].is_triage is False
    assert all(
        card.classification == "legacy_pending_migration" for card in first.cards
    )
    assert all(
        card.missing_target_identifiers == ("initiative_id",) for card in first.cards
    )


def test_rollback_set_captures_and_detects_tampering(tmp_path, migration_modules):
    rollback = migration_modules["rollback"]
    database_path = _database(tmp_path, migration_modules)
    wal_was_present = database_path.with_name(database_path.name + "-wal").exists()
    shm_was_present = database_path.with_name(database_path.name + "-shm").exists()
    manifest, _ = _capture(tmp_path, database_path, migration_modules)

    assert manifest.wal_present is wal_was_present
    assert manifest.shm_present is shm_was_present
    assert database_path.with_name(database_path.name + "-wal").exists() is wal_was_present
    assert database_path.with_name(database_path.name + "-shm").exists() is shm_was_present
    assert all(item.passed for item in rollback.verify_rollback_set(manifest))
    package_copy = Path(manifest.root_path) / manifest.package_relative_path
    (package_copy / "plugin.py").write_text("VERSION = 2\n", encoding="utf-8")

    findings = rollback.verify_rollback_set(manifest)
    assert any(item.code == "package" and not item.passed for item in findings)


def test_rollback_capture_rejects_nonstopped_state(tmp_path, migration_modules):
    rollback = migration_modules["rollback"]
    models = migration_modules["models"]
    database_path = _database(tmp_path, migration_modules)
    values = _rollback_inputs(tmp_path, database_path)

    with pytest.raises(models.MigrationRejected, match="stopped"):
        rollback.capture_rollback_set(
            **values,
            stopped_state=models.StoppedStateEvidence(True, 1, 0),
            captured_at=1_700_000_000,
        )


def test_forward_migration_is_atomic_explicit_and_idempotent(
    tmp_path, migration_modules
):
    engine = migration_modules["engine"]
    models = migration_modules["models"]
    database_path = _database(tmp_path, migration_modules)
    report = engine.dry_run(database_path)
    manifest, _ = _capture(tmp_path, database_path, migration_modules)
    dispositions = (
        _disposition(models, "task-a", "migrate"),
        _disposition(models, "task-b", "retain_legacy"),
    )

    result = engine.apply_forward(
        database_path=database_path,
        report=report,
        dispositions=dispositions,
        rollback_set=manifest,
        stopped_state=_stopped(models),
        operation_id="migration-1",
        applied_at=1_700_000_100,
    )

    assert result.replayed is False
    assert result.reconciliation.all_passed
    assert result.migrated_task_ids == ("task-a",)
    assert result.retained_task_ids == ("task-b",)
    with closing(sqlite3.connect(database_path)) as conn:
        conn.row_factory = sqlite3.Row
        maps = conn.execute(
            "SELECT * FROM adrian_kanban_migration_map ORDER BY source_task_id"
        ).fetchall()
        assert [(row["source_task_id"], row["action"]) for row in maps] == [
            ("task-a", "migrate"),
            ("task-b", "retain_legacy"),
        ]
        card = conn.execute(
            "SELECT * FROM adrian_kanban_cards WHERE task_id = 'task-a'"
        ).fetchone()
        assert card["card_type"] == "task"
        assert card["initiative_id"] == "initiative-1"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_lifecycle_contracts WHERE task_id = 'task-a'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2

    replay = engine.apply_forward(
        database_path=database_path,
        report=report,
        dispositions=dispositions,
        rollback_set=manifest,
        stopped_state=_stopped(models),
        operation_id="migration-1",
        applied_at=1_700_000_100,
    )
    assert replay.replayed is True
    with closing(sqlite3.connect(database_path)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM adrian_kanban_migration_map").fetchone()[
                0
            ]
            == 2
        )


def test_forward_requires_one_approved_disposition_per_source(
    tmp_path, migration_modules
):
    engine = migration_modules["engine"]
    models = migration_modules["models"]
    database_path = _database(tmp_path, migration_modules)
    report = engine.dry_run(database_path)
    manifest, _ = _capture(tmp_path, database_path, migration_modules)

    with pytest.raises(models.MigrationRejected, match="disposition"):
        engine.apply_forward(
            database_path=database_path,
            report=report,
            dispositions=(_disposition(models, "task-a", "migrate"),),
            rollback_set=manifest,
            stopped_state=_stopped(models),
            operation_id="migration-incomplete",
            applied_at=1_700_000_100,
        )
    with closing(sqlite3.connect(database_path)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_migration_operations"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM adrian_kanban_migration_map").fetchone()[
                0
            ]
            == 0
        )


def test_forward_requires_adrian_approval(tmp_path, migration_modules):
    models = migration_modules["models"]
    with pytest.raises(models.MigrationRejected, match="Adrian"):
        replace(
            _disposition(models, "task-a", "migrate"),
            approver_identity="not-Adrian",
        )


def test_mid_operation_conflict_rolls_back_every_prior_write(
    tmp_path, migration_modules
):
    engine = migration_modules["engine"]
    models = migration_modules["models"]
    database_path = _database(tmp_path, migration_modules)
    report = engine.dry_run(database_path)
    manifest, _ = _capture(tmp_path, database_path, migration_modules)
    dispositions = (
        _disposition(models, "task-a", "retain_legacy"),
        _disposition(models, "task-b", "migrate"),
    )
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute(
            "INSERT INTO adrian_kanban_initiatives (initiative_id) "
            "VALUES ('initiative-1')"
        )
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, created_at, board_slug) "
            "VALUES ('initiative', 'initiative-1', NULL, 'Wrong title', 1, 'default')"
        )
        conn.commit()

    with pytest.raises(models.MigrationRejected, match="initiative"):
        engine.apply_forward(
            database_path=database_path,
            report=report,
            dispositions=dispositions,
            rollback_set=manifest,
            stopped_state=_stopped(models),
            operation_id="migration-conflict",
            applied_at=1_700_000_100,
        )
    with closing(sqlite3.connect(database_path)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_migration_operations"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM adrian_kanban_migration_map").fetchone()[
                0
            ]
            == 0
        )


def test_reconciliation_detects_native_history_drift(tmp_path, migration_modules):
    engine = migration_modules["engine"]
    database_path = _database(tmp_path, migration_modules)
    baseline = engine.dry_run(database_path)
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute("UPDATE tasks SET body = 'changed' WHERE id = 'task-a'")
        conn.commit()

    report = engine.reconcile(database_path, baseline)
    assert not report.all_passed
    assert any(
        item.code == "source_unchanged" and not item.passed for item in report.findings
    )


def test_schema_metadata_is_idempotent(tmp_path, migration_modules):
    database_path = _database(tmp_path, migration_modules, tasks=())
    with closing(sqlite3.connect(database_path)) as conn:
        migration_modules["schema"].create_schema(conn)
        migration_modules["schema"].create_schema(conn)
        rows = dict(
            conn.execute(
                "SELECT metadata_key, metadata_value FROM adrian_kanban_schema_metadata"
            ).fetchall()
        )
    assert rows == {"schema_version": "1", "migration_format_version": "1"}


def test_manifest_payload_is_canonical_json(tmp_path, migration_modules):
    database_path = _database(tmp_path, migration_modules)
    manifest, _ = _capture(tmp_path, database_path, migration_modules)
    manifest_path = Path(manifest.manifest_path)

    assert manifest_path.read_text(encoding="utf-8") == manifest.payload
    assert (
        json.dumps(
            json.loads(manifest.payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        == manifest.payload
    )


def _set_plugin_authority(config_path: Path) -> None:
    config_path.write_text("kanban:\n  mutation_authority: plugin\n", encoding="utf-8")


def test_reverse_requires_stopped_plugin_authority(tmp_path, migration_modules):
    reverse = migration_modules["reverse"]
    models = migration_modules["models"]
    database_path = _database(tmp_path, migration_modules)
    manifest, values = _capture(tmp_path, database_path, migration_modules)

    with pytest.raises(models.MigrationRejected, match="plugin authority"):
        reverse.reverse_migration(
            manifest=manifest,
            database_path=database_path,
            config_path=values["config_path"],
            active_release_pointer_path=values["active_release_pointer_path"],
            stopped_state=_stopped(models),
        )

    _set_plugin_authority(values["config_path"])
    with pytest.raises(models.MigrationRejected, match="stopped"):
        reverse.reverse_migration(
            manifest=manifest,
            database_path=database_path,
            config_path=values["config_path"],
            active_release_pointer_path=values["active_release_pointer_path"],
            stopped_state=models.StoppedStateEvidence(True, 1, 0),
        )


def test_reverse_restores_verified_set_without_intervening_receipts(
    tmp_path, migration_modules
):
    reverse = migration_modules["reverse"]
    database_path = _database(tmp_path, migration_modules)
    manifest, values = _capture(tmp_path, database_path, migration_modules)
    _set_plugin_authority(values["config_path"])
    values["active_release_pointer_path"].write_text("release-v2", encoding="utf-8")
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute("UPDATE tasks SET body = 'plugin-era' WHERE id = 'task-a'")
        conn.commit()

    result = reverse.reverse_migration(
        manifest=manifest,
        database_path=database_path,
        config_path=values["config_path"],
        active_release_pointer_path=values["active_release_pointer_path"],
        stopped_state=_stopped(migration_modules["models"]),
    )

    assert result.replayed is False
    assert result.reconciliation.all_passed
    assert result.restored_database_digest == manifest.database_digest
    assert result.restored_config_digest == manifest.config_digest
    assert result.restored_pointer_digest == manifest.pointer_digest
    source_finding = next(
        item
        for item in result.reconciliation.findings
        if item.code == "restored_source_snapshot"
    )
    assert source_finding.passed
    assert "mutation_authority: native" in values["config_path"].read_text(
        encoding="utf-8"
    )
    with closing(sqlite3.connect(database_path)) as conn:
        assert (
            conn.execute("SELECT body FROM tasks WHERE id = 'task-a'").fetchone()[0]
            == "Body task-a"
        )


def test_reverse_rejects_unapproved_or_partially_approved_plugin_receipts(
    tmp_path, migration_modules
):
    reverse = migration_modules["reverse"]
    models = migration_modules["models"]
    database_path = _database(tmp_path, migration_modules)
    manifest, values = _capture(tmp_path, database_path, migration_modules)
    _set_plugin_authority(values["config_path"])
    with closing(sqlite3.connect(database_path)) as conn:
        for offset, key in enumerate(("receipt-a", "receipt-b"), start=1):
            conn.execute(
                "INSERT INTO adrian_kanban_command_receipts "
                "(idempotency_key, operation, target, request_digest, "
                "response_json, created_at) VALUES (?, 'edit_task', 'task-a', "
                "'digest', '{}', ?)",
                (key, manifest.captured_at + offset),
            )
        conn.commit()

    with pytest.raises(models.MigrationRejected, match="receipt"):
        reverse.reverse_migration(
            manifest=manifest,
            database_path=database_path,
            config_path=values["config_path"],
            active_release_pointer_path=values["active_release_pointer_path"],
            stopped_state=_stopped(models),
        )
    with pytest.raises(models.MigrationRejected, match="every receipt"):
        reverse.reverse_migration(
            manifest=manifest,
            database_path=database_path,
            config_path=values["config_path"],
            active_release_pointer_path=values["active_release_pointer_path"],
            stopped_state=_stopped(models),
            reverse_disposition=models.ReverseDisposition(
                receipt_keys=("receipt-a",),
                approver_identity="Adrian",
                approval_reference="approval:reverse",
                evidence_reference="evidence:reverse",
            ),
        )

    result = reverse.reverse_migration(
        manifest=manifest,
        database_path=database_path,
        config_path=values["config_path"],
        active_release_pointer_path=values["active_release_pointer_path"],
        stopped_state=_stopped(models),
        reverse_disposition=models.ReverseDisposition(
            receipt_keys=("receipt-a", "receipt-b"),
            approver_identity="Adrian",
            approval_reference="approval:reverse",
            evidence_reference="evidence:reverse",
        ),
    )
    assert result.reconciliation.all_passed
