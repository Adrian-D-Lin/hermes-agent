from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import (
    DryRunCardClassification,
    DryRunReport,
    ForwardMigrationResult,
    MigrationDisposition,
    MigrationRejected,
    ReconciliationFinding,
    ReconciliationReport,
    RollbackSetManifest,
    StoppedStateEvidence,
    canonical_json,
    digest_json,
)
from .rollback import verify_rollback_set


def _validate_database_path(database_path: Path) -> None:
    if not isinstance(database_path, Path):
        raise MigrationRejected("database_path must be Path")
    if not database_path.is_absolute():
        raise MigrationRejected("database_path must be absolute")
    if not database_path.is_file():
        raise MigrationRejected("database_path must be an existing file")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _snapshot_table(
    conn: sqlite3.Connection, table: str, order_by: str
) -> tuple[int, str]:
    try:
        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
        rows = cursor.fetchall()
    except sqlite3.OperationalError as exc:
        raise MigrationRejected(f"failed to snapshot {table}: {exc}") from exc
    dicts = [_row_to_dict(r) for r in rows]
    return len(dicts), digest_json(dicts)


def _snapshot_review(
    conn: sqlite3.Connection,
) -> tuple[int, str]:
    try:
        handoffs = conn.execute(
            "SELECT * FROM task_candidate_handoffs ORDER BY candidate_id"
        ).fetchall()
        verdicts = conn.execute(
            "SELECT * FROM task_reviewer_verdicts ORDER BY verdict_id"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise MigrationRejected(f"failed to snapshot review: {exc}") from exc
    handoff_dicts = [_row_to_dict(r) for r in handoffs]
    verdict_dicts = [_row_to_dict(r) for r in verdicts]
    snapshot = {
        "candidate_handoffs": handoff_dicts,
        "reviewer_verdicts": verdict_dicts,
    }
    total = len(handoff_dicts) + len(verdict_dicts)
    return total, digest_json(snapshot)


def _compute_dry_run(conn: sqlite3.Connection) -> DryRunReport:
    source_count, source_hash = _snapshot_table(conn, "tasks", "id")
    dep_count, dep_hash = _snapshot_table(conn, "task_links", "parent_id, child_id")
    run_count, run_hash = _snapshot_table(conn, "task_runs", "id")
    review_count, review_hash = _snapshot_review(conn)

    try:
        tasks = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    except sqlite3.OperationalError as exc:
        raise MigrationRejected(f"failed to read tasks: {exc}") from exc

    cards: list[DryRunCardClassification] = []
    for task in tasks:
        task_dict = _row_to_dict(task)
        task_id = task_dict["id"]
        title = task_dict.get("title", "")
        status = task_dict.get("status", "")
        source_digest = digest_json(task_dict)

        try:
            map_rows = conn.execute(
                "SELECT * FROM adrian_kanban_migration_map "
                "WHERE source_task_id = ? AND action = 'migrate'",
                (task_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            map_rows = []

        if len(map_rows) == 1:
            map_row = _row_to_dict(map_rows[0])
            task_card_id = map_row.get("task_card_id")
            if task_card_id is None:
                raise MigrationRejected(f"mapped task {task_id} has null task_card_id")
            try:
                card = conn.execute(
                    "SELECT * FROM adrian_kanban_cards WHERE id = ?",
                    (task_card_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                card = None
            if card is None:
                raise MigrationRejected(f"mapped task {task_id} card not found")
            card_dict = _row_to_dict(card)
            if card_dict.get("task_id") != task_id:
                raise MigrationRejected(f"mapped task {task_id} card task_id mismatch")
            cards.append(
                DryRunCardClassification(
                    task_id=task_id,
                    title=title,
                    status=status,
                    has_plugin_mapping=True,
                    classification="mapped",
                    is_triage=(status == "triage"),
                    missing_target_identifiers=(),
                    source_digest=source_digest,
                )
            )
        elif len(map_rows) == 0:
            cards.append(
                DryRunCardClassification(
                    task_id=task_id,
                    title=title,
                    status=status,
                    has_plugin_mapping=False,
                    classification="legacy_pending_migration",
                    is_triage=(status == "triage"),
                    missing_target_identifiers=("initiative_id",),
                    source_digest=source_digest,
                )
            )
        else:
            raise MigrationRejected(f"task {task_id} has duplicate migration_map rows")

    cards.sort(key=lambda c: c.task_id)
    return DryRunReport(
        cards=tuple(cards),
        source_count=source_count,
        source_hash=source_hash,
        native_dependency_count=dep_count,
        native_dependency_hash=dep_hash,
        native_run_count=run_count,
        native_run_hash=run_hash,
        native_review_count=review_count,
        native_review_hash=review_hash,
    )


def _reconcile_internal(
    conn: sqlite3.Connection,
    report: DryRunReport,
    dispositions: tuple[MigrationDisposition, ...],
) -> ReconciliationReport:
    findings: list[ReconciliationFinding] = []

    source_count, source_hash = _snapshot_table(conn, "tasks", "id")
    passed = source_count == report.source_count and source_hash == report.source_hash
    findings.append(
        ReconciliationFinding(
            code="source_unchanged",
            passed=passed,
            expected_json=canonical_json({
                "count": report.source_count,
                "hash": report.source_hash,
            }),
            observed_json=canonical_json({"count": source_count, "hash": source_hash}),
            remediation="Restore native tasks table from rollback set",
        )
    )

    dep_count, dep_hash = _snapshot_table(conn, "task_links", "parent_id, child_id")
    passed = (
        dep_count == report.native_dependency_count
        and dep_hash == report.native_dependency_hash
    )
    findings.append(
        ReconciliationFinding(
            code="dependency_unchanged",
            passed=passed,
            expected_json=canonical_json({
                "count": report.native_dependency_count,
                "hash": report.native_dependency_hash,
            }),
            observed_json=canonical_json({"count": dep_count, "hash": dep_hash}),
            remediation="Restore native task_links table from rollback set",
        )
    )

    run_count, run_hash = _snapshot_table(conn, "task_runs", "id")
    passed = run_count == report.native_run_count and run_hash == report.native_run_hash
    findings.append(
        ReconciliationFinding(
            code="run_unchanged",
            passed=passed,
            expected_json=canonical_json({
                "count": report.native_run_count,
                "hash": report.native_run_hash,
            }),
            observed_json=canonical_json({"count": run_count, "hash": run_hash}),
            remediation="Restore native task_runs table from rollback set",
        )
    )

    review_count, review_hash = _snapshot_review(conn)
    passed = (
        review_count == report.native_review_count
        and review_hash == report.native_review_hash
    )
    findings.append(
        ReconciliationFinding(
            code="review_unchanged",
            passed=passed,
            expected_json=canonical_json({
                "count": report.native_review_count,
                "hash": report.native_review_hash,
            }),
            observed_json=canonical_json({"count": review_count, "hash": review_hash}),
            remediation="Restore native review tables from rollback set",
        )
    )

    try:
        map_rows = conn.execute(
            "SELECT * FROM adrian_kanban_migration_map ORDER BY source_task_id"
        ).fetchall()
    except sqlite3.OperationalError:
        map_rows = []
    map_dicts = [_row_to_dict(r) for r in map_rows]
    map_count = len(map_dicts)
    map_hash = digest_json(map_dicts)

    expected_map_count = len(dispositions)
    passed = map_count == expected_map_count
    findings.append(
        ReconciliationFinding(
            code="map_count",
            passed=passed,
            expected_json=canonical_json({"count": expected_map_count}),
            observed_json=canonical_json({"count": map_count}),
            remediation="Verify migration_map rows match dispositions",
        )
    )

    expected_map_ids = sorted(d.task_id for d in dispositions)
    observed_map_ids = sorted(r["source_task_id"] for r in map_dicts)
    passed = observed_map_ids == expected_map_ids
    findings.append(
        ReconciliationFinding(
            code="map_exact_one_per_source",
            passed=passed,
            expected_json=canonical_json({"ids": expected_map_ids}),
            observed_json=canonical_json({"ids": observed_map_ids}),
            remediation="Ensure exactly one migration_map row per source task",
        )
    )

    migrate_findings_passed = True
    for d in dispositions:
        if d.action != "migrate":
            continue
        matching = [r for r in map_dicts if r["source_task_id"] == d.task_id]
        if len(matching) != 1:
            migrate_findings_passed = False
            continue
        row = matching[0]
        if row["initiative_id"] != d.initiative_id:
            migrate_findings_passed = False
            continue
        if row["task_card_id"] is None:
            migrate_findings_passed = False
            continue
        try:
            card = conn.execute(
                "SELECT * FROM adrian_kanban_cards WHERE id = ?",
                (row["task_card_id"],),
            ).fetchone()
        except sqlite3.OperationalError:
            card = None
        if card is None:
            migrate_findings_passed = False
            continue
        card_dict = _row_to_dict(card)
        if card_dict.get("task_id") != d.task_id:
            migrate_findings_passed = False
            continue
        if card_dict.get("initiative_id") != d.initiative_id:
            migrate_findings_passed = False
            continue
        try:
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (d.task_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            task = None
        if task is None:
            migrate_findings_passed = False
            continue
        task_dict = _row_to_dict(task)
        if card_dict.get("title") != task_dict.get("title"):
            migrate_findings_passed = False
            continue
        if card_dict.get("body") != task_dict.get("body"):
            migrate_findings_passed = False
            continue

    findings.append(
        ReconciliationFinding(
            code="migrate_shape",
            passed=migrate_findings_passed,
            expected_json=canonical_json({
                "migrated": [d.task_id for d in dispositions if d.action == "migrate"]
            }),
            observed_json=canonical_json({
                "migrated": [d.task_id for d in dispositions if d.action == "migrate"]
            }),
            remediation="Verify migrated task cards match source tasks",
        )
    )

    retain_findings_passed = True
    for d in dispositions:
        if d.action != "retain_legacy":
            continue
        matching = [r for r in map_dicts if r["source_task_id"] == d.task_id]
        if len(matching) != 1:
            retain_findings_passed = False
            continue
        row = matching[0]
        if row["initiative_id"] is not None or row["task_card_id"] is not None:
            retain_findings_passed = False

    findings.append(
        ReconciliationFinding(
            code="retain_null_targets",
            passed=retain_findings_passed,
            expected_json=canonical_json({
                "retained": [
                    d.task_id for d in dispositions if d.action == "retain_legacy"
                ]
            }),
            observed_json=canonical_json({
                "retained": [
                    d.task_id for d in dispositions if d.action == "retain_legacy"
                ]
            }),
            remediation="Verify retained tasks have null targets in migration_map",
        )
    )

    transition_findings_passed = True
    migrated_initiative_ids = set(
        d.initiative_id for d in dispositions if d.action == "migrate"
    )
    for init_id in migrated_initiative_ids:
        try:
            transitions = conn.execute(
                "SELECT * FROM initiative_transitions "
                "WHERE initiative_id = ? ORDER BY transition_id",
                (init_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            transitions = []
        if not transitions:
            transition_findings_passed = False
            continue
        first = _row_to_dict(transitions[0])
        if first["transition_id"] != 1:
            transition_findings_passed = False
            continue
        if first["previous_transition_id"] is not None:
            transition_findings_passed = False
            continue
        for i, t in enumerate(transitions):
            t_dict = _row_to_dict(t)
            if t_dict["transition_id"] != i + 1:
                transition_findings_passed = False
                break
            if i > 0:
                prev = _row_to_dict(transitions[i - 1])
                if t_dict["previous_transition_id"] != prev["transition_id"]:
                    transition_findings_passed = False
                    break

    findings.append(
        ReconciliationFinding(
            code="transition_chain_linear",
            passed=transition_findings_passed,
            expected_json=canonical_json({"linear": True}),
            observed_json=canonical_json({"linear": transition_findings_passed}),
            remediation=(
                "Verify initiative transition chains are linear from transition_id 1"
            ),
        )
    )

    migrated_ids = [d.task_id for d in dispositions if d.action == "migrate"]
    lifecycle_count = 0
    if migrated_ids:
        placeholders = ",".join("?" for _ in migrated_ids)
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM task_lifecycle_contracts WHERE task_id IN ({placeholders})",
                migrated_ids,
            ).fetchone()
            lifecycle_count = row["cnt"] if row else 0
        except sqlite3.OperationalError:
            lifecycle_count = 0
    passed = lifecycle_count == 0
    findings.append(
        ReconciliationFinding(
            code="zero_lifecycle_contracts",
            passed=passed,
            expected_json=canonical_json({"count": 0}),
            observed_json=canonical_json({"count": lifecycle_count}),
            remediation="Remove lifecycle contracts for migrated task IDs",
        )
    )

    return ReconciliationReport(
        findings=tuple(findings),
        source_count=source_count,
        source_hash=source_hash,
        map_count=map_count,
        map_hash=map_hash,
    )


def _parse_reconciliation_report(payload: str) -> ReconciliationReport:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MigrationRejected("invalid reconciliation payload") from exc
    if not isinstance(data, dict):
        raise MigrationRejected("reconciliation payload must be object")
    expected_keys = {
        "findings",
        "source_count",
        "source_hash",
        "map_count",
        "map_hash",
    }
    if set(data.keys()) != expected_keys:
        raise MigrationRejected("reconciliation payload has wrong keys")
    findings = []
    for f in data["findings"]:
        if not isinstance(f, dict):
            raise MigrationRejected("finding must be object")
        f_keys = {"code", "passed", "expected_json", "observed_json", "remediation"}
        if set(f.keys()) != f_keys:
            raise MigrationRejected("finding has wrong keys")
        findings.append(
            ReconciliationFinding(
                code=f["code"],
                passed=f["passed"],
                expected_json=f["expected_json"],
                observed_json=f["observed_json"],
                remediation=f["remediation"],
            )
        )
    return ReconciliationReport(
        findings=tuple(findings),
        source_count=data["source_count"],
        source_hash=data["source_hash"],
        map_count=data["map_count"],
        map_hash=data["map_hash"],
    )


def _parse_forward_result(payload: str) -> ForwardMigrationResult:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MigrationRejected("invalid result payload") from exc
    if not isinstance(data, dict):
        raise MigrationRejected("result payload must be object")
    expected_keys = {
        "operation_id",
        "dry_run_digest",
        "disposition_digest",
        "rollback_set_digest",
        "replayed",
        "migrated_task_ids",
        "retained_task_ids",
        "reconciliation",
    }
    if set(data.keys()) != expected_keys:
        raise MigrationRejected("result payload has wrong keys")
    recon = _parse_reconciliation_report(canonical_json(data["reconciliation"]))
    return ForwardMigrationResult(
        operation_id=data["operation_id"],
        dry_run_digest=data["dry_run_digest"],
        disposition_digest=data["disposition_digest"],
        rollback_set_digest=data["rollback_set_digest"],
        replayed=data["replayed"],
        migrated_task_ids=tuple(data["migrated_task_ids"]),
        retained_task_ids=tuple(data["retained_task_ids"]),
        reconciliation=recon,
    )


def dry_run(database_path: Path) -> DryRunReport:
    _validate_database_path(database_path)
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return _compute_dry_run(conn)
    finally:
        conn.close()


def reconcile(database_path: Path, baseline: DryRunReport) -> ReconciliationReport:
    _validate_database_path(database_path)
    conn = sqlite3.connect(str(database_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        dispositions: tuple[MigrationDisposition, ...] = ()
        return _reconcile_internal(conn, baseline, dispositions)
    finally:
        conn.close()


def apply_forward(
    *,
    database_path: Path,
    report: DryRunReport,
    dispositions: tuple[MigrationDisposition, ...],
    rollback_set: RollbackSetManifest,
    stopped_state: StoppedStateEvidence,
    operation_id: str,
    applied_at: int,
) -> ForwardMigrationResult:
    _validate_database_path(database_path)
    if not isinstance(dispositions, tuple):
        raise MigrationRejected("dispositions must be tuple")
    for d in dispositions:
        if not isinstance(d, MigrationDisposition):
            raise MigrationRejected("dispositions must contain MigrationDisposition")
    ids = [d.task_id for d in dispositions]
    if len(ids) != len(set(ids)):
        raise MigrationRejected("dispositions must have unique task_ids")
    if ids != sorted(ids):
        raise MigrationRejected("dispositions must be sorted")
    report_ids = [c.task_id for c in report.cards]
    if set(ids) != set(report_ids):
        raise MigrationRejected("dispositions must cover every report card")
    if not isinstance(rollback_set, RollbackSetManifest):
        raise MigrationRejected("rollback_set must be RollbackSetManifest")
    if not isinstance(stopped_state, StoppedStateEvidence):
        raise MigrationRejected("stopped_state must be StoppedStateEvidence")
    if not stopped_state.is_stopped:
        raise MigrationRejected("system must be stopped")
    if not isinstance(operation_id, str):
        raise MigrationRejected("operation_id must be str")
    if operation_id != operation_id.strip() or not operation_id:
        raise MigrationRejected("operation_id must be stripped nonblank")
    if isinstance(applied_at, bool) or not isinstance(applied_at, int):
        raise MigrationRejected("applied_at must be int")
    if applied_at <= 0:
        raise MigrationRejected("applied_at must be positive")

    findings = verify_rollback_set(rollback_set)
    for f in findings:
        if not f.passed:
            raise MigrationRejected(f"rollback set verification failed: {f.code}")

    disposition_digest = digest_json([d.canonical_dict for d in dispositions])
    rollback_digest = rollback_set.digest

    conn = sqlite3.connect(str(database_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        op_row = conn.execute(
            "SELECT * FROM adrian_kanban_migration_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if op_row is not None:
            op_dict = _row_to_dict(op_row)
            if (
                op_dict["dry_run_digest"] != report.digest
                or op_dict["disposition_digest"] != disposition_digest
                or op_dict["rollback_set_digest"] != rollback_digest
            ):
                raise MigrationRejected("replay digest mismatch")
            if op_dict["result_payload"] is None:
                raise MigrationRejected("replay has no result payload")
            stored_result = _parse_forward_result(op_dict["result_payload"])
            if stored_result.replayed:
                raise MigrationRejected("replay of replay not allowed")
            return ForwardMigrationResult(
                operation_id=operation_id,
                dry_run_digest=report.digest,
                disposition_digest=disposition_digest,
                rollback_set_digest=rollback_digest,
                replayed=True,
                migrated_task_ids=stored_result.migrated_task_ids,
                retained_task_ids=stored_result.retained_task_ids,
                reconciliation=stored_result.reconciliation,
            )

        dup_row = conn.execute(
            """
            SELECT operation_id FROM adrian_kanban_migration_operations
            WHERE dry_run_digest = ? AND disposition_digest = ? AND rollback_set_digest = ?
            AND operation_id != ?
            """,
            (report.digest, disposition_digest, rollback_digest, operation_id),
        ).fetchone()
        if dup_row is not None:
            raise MigrationRejected("digest triple already used by another operation")

        current_report = _compute_dry_run(conn)
        if current_report.digest != report.digest:
            raise MigrationRejected("dry run digest mismatch")

        conn.execute("BEGIN IMMEDIATE")
        try:
            revalidated = _compute_dry_run(conn)
            if revalidated.digest != report.digest:
                raise MigrationRejected("dry run digest changed in transaction")

            conn.execute(
                """
                INSERT INTO adrian_kanban_migration_operations (
                    operation_id, dry_run_digest, disposition_digest,
                    rollback_set_digest, result_payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    report.digest,
                    disposition_digest,
                    rollback_digest,
                    "{}",
                    applied_at,
                ),
            )

            for d in dispositions:
                if d.action == "retain_legacy":
                    conn.execute(
                        """
                            INSERT INTO adrian_kanban_migration_map (
                            source_task_id, action, initiative_id, task_card_id,
                            source_digest, disposition_payload, disposition_digest,
                            operation_id, applied_at
                        ) VALUES (?, 'retain_legacy', NULL, NULL, ?, ?, ?, ?, ?)
                        """,
                        (
                            d.task_id,
                            next(
                                card.source_digest
                                for card in report.cards
                                if card.task_id == d.task_id
                            ),
                            d.payload,
                            d.digest,
                            operation_id,
                            applied_at,
                        ),
                    )
                else:
                    task = conn.execute(
                        "SELECT * FROM tasks WHERE id = ?", (d.task_id,)
                    ).fetchone()
                    if task is None:
                        raise MigrationRejected(f"task {d.task_id} not found")
                    task_dict = _row_to_dict(task)
                    source_digest = digest_json(task_dict)

                    init_row = conn.execute(
                        "SELECT * FROM adrian_kanban_initiatives "
                        "WHERE initiative_id = ?",
                        (d.initiative_id,),
                    ).fetchone()
                    if init_row is None:
                        conn.execute(
                            "INSERT INTO adrian_kanban_initiatives "
                            "(initiative_id) VALUES (?)",
                            (d.initiative_id,),
                        )

                    init_card_row = conn.execute(
                        "SELECT * FROM adrian_kanban_cards "
                        "WHERE card_type = 'initiative' AND initiative_id = ?",
                        (d.initiative_id,),
                    ).fetchone()
                    if init_card_row is None:
                        conn.execute(
                            """
                            INSERT INTO adrian_kanban_cards (
                                card_type, initiative_id, task_id, title,
                                created_at, board_slug, record_version, body,
                                closed_at
                            ) VALUES (
                                'initiative', ?, NULL, ?, ?, ?, 1, NULL, NULL
                            )
                            """,
                            (
                                d.initiative_id,
                                d.initiative_title,
                                applied_at,
                                d.board_slug,
                            ),
                        )
                        init_card_id = conn.execute(
                            "SELECT last_insert_rowid()"
                        ).fetchone()[0]
                    else:
                        init_card_dict = _row_to_dict(init_card_row)
                        if (
                            init_card_dict["title"] != d.initiative_title
                            or init_card_dict["board_slug"] != d.board_slug
                        ):
                            raise MigrationRejected(
                                f"initiative card for {d.initiative_id} "
                                "identity mismatch"
                            )
                        init_card_id = init_card_dict["id"]

                    first_trans = conn.execute(
                        "SELECT * FROM initiative_transitions "
                        "WHERE initiative_id = ? AND transition_id = 1",
                        (d.initiative_id,),
                    ).fetchone()
                    if first_trans is None:
                        canonical_payload = canonical_json({
                            "initiative_id": d.initiative_id,
                            "to_phase": d.initial_lifecycle_phase,
                            "approval_reference": d.approval_reference,
                            "evidence_reference": d.evidence_reference,
                        })
                        conn.execute(
                            """
                            INSERT INTO initiative_transitions (
                                initiative_card_id, initiative_id,
                                previous_transition_id, transition_id,
                                from_phase, from_segment_id, to_phase,
                                to_segment_id, canon_route,
                                repository_reconciliation_ref, trigger,
                                actor_evidence, canonical_payload,
                                rendered_history_ref, created_at
                            ) VALUES (
                                ?, ?, NULL, 1, NULL, NULL, ?, NULL, NULL,
                                NULL, 'migration', ?, ?, NULL, ?
                            )
                            """,
                            (
                                init_card_id,
                                d.initiative_id,
                                d.initial_lifecycle_phase,
                                d.payload,
                                canonical_payload,
                                applied_at,
                            ),
                        )
                    else:
                        trans_dict = _row_to_dict(first_trans)
                        if trans_dict["to_phase"] != d.initial_lifecycle_phase:
                            raise MigrationRejected(
                                f"initiative {d.initiative_id} phase mismatch"
                            )

                    card_row = conn.execute(
                        "SELECT * FROM adrian_kanban_cards "
                        "WHERE task_id = ? AND card_type = 'task'",
                        (d.task_id,),
                    ).fetchone()
                    if card_row is None:
                        conn.execute(
                            """
                            INSERT INTO adrian_kanban_cards (
                                card_type, initiative_id, task_id, title,
                                created_at, board_slug, record_version, body,
                                closed_at
                            ) VALUES ('task', ?, ?, ?, ?, ?, 1, ?, NULL)
                            """,
                            (
                                d.initiative_id,
                                d.task_id,
                                task_dict.get("title"),
                                applied_at,
                                d.board_slug,
                                task_dict.get("body"),
                            ),
                        )
                        card_id = conn.execute("SELECT last_insert_rowid()").fetchone()[
                            0
                        ]
                    else:
                        card_dict = _row_to_dict(card_row)
                        if (
                            card_dict["initiative_id"] != d.initiative_id
                            or card_dict["title"] != task_dict.get("title")
                            or card_dict["body"] != task_dict.get("body")
                            or card_dict["board_slug"] != d.board_slug
                        ):
                            raise MigrationRejected(
                                f"card for task {d.task_id} shape mismatch"
                            )
                        card_id = card_dict["id"]

                    conn.execute(
                        """
                            INSERT INTO adrian_kanban_migration_map (
                            source_task_id, action, initiative_id, task_card_id,
                            source_digest, disposition_payload, disposition_digest,
                            operation_id, applied_at
                        ) VALUES (?, 'migrate', ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            d.task_id,
                            d.initiative_id,
                            card_id,
                            source_digest,
                            d.payload,
                            d.digest,
                            operation_id,
                            applied_at,
                        ),
                    )

            recon = _reconcile_internal(conn, report, dispositions)
            if not recon.all_passed:
                raise MigrationRejected("reconciliation failed")

            migrated = tuple(d.task_id for d in dispositions if d.action == "migrate")
            retained = tuple(
                d.task_id for d in dispositions if d.action == "retain_legacy"
            )
            result = ForwardMigrationResult(
                operation_id=operation_id,
                dry_run_digest=report.digest,
                disposition_digest=disposition_digest,
                rollback_set_digest=rollback_digest,
                replayed=False,
                migrated_task_ids=migrated,
                retained_task_ids=retained,
                reconciliation=recon,
            )
            conn.execute(
                "UPDATE adrian_kanban_migration_operations "
                "SET result_payload = ? WHERE operation_id = ?",
                (result.payload, operation_id),
            )
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise MigrationRejected(f"sqlite error: {exc}") from exc
    finally:
        conn.close()
