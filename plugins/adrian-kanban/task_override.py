"""Live derivation of task-card gate-override proposals (design v0.28 §7.13)."""

from __future__ import annotations

from .override_proposals import store_prepared_override

SUPPORTED_DESTINATIONS = ("todo", "ready", "review", "done")
TERMINAL_STATUSES = ("done", "archived")


def _require_nonblank(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value


def _check(code, result, evidence_ref=None, observed=None):
    return {
        "code": code,
        "result": result,
        "evidence_ref": evidence_ref,
        "observed": observed,
    }


def derive_task_status_override(
    conn,
    *,
    task_id,
    board,
    to_status,
    override_reason,
    executor_session_id,
    executor_profile,
):
    _require_nonblank(task_id, "task_id")
    _require_nonblank(board, "board")
    _require_nonblank(to_status, "to_status")
    _require_nonblank(override_reason, "override_reason")
    _require_nonblank(executor_session_id, "executor_session_id")
    if executor_profile != "default":
        raise ValueError("only the default executor profile may execute overrides")
    if to_status not in SUPPORTED_DESTINATIONS:
        raise ValueError(
            f"unsupported override destination {to_status!r}; "
            f"supported destinations are {', '.join(SUPPORTED_DESTINATIONS)}"
        )

    row = conn.execute(
        """
        SELECT c.task_id AS task_id,
               c.initiative_id AS initiative_id,
               c.record_version AS record_version,
               t.status AS status,
               t.assignee AS assignee,
               t.current_run_id AS current_run_id,
               t.claim_lock AS claim_lock,
               t.claim_expires AS claim_expires,
               t.worker_pid AS worker_pid
        FROM adrian_kanban_cards c
        JOIN tasks t ON t.id = c.task_id
        WHERE c.task_id = ? AND c.card_type = 'task'
          AND c.board_slug = ? AND c.closed_at IS NULL
        """,
        (task_id, board),
    ).fetchall()
    if len(row) != 1:
        raise ValueError(f"no open task card found for task {task_id!r}")
    row = row[0]
    initiative_id = row["initiative_id"]
    initiative = conn.execute(
        """
        SELECT 1
        FROM adrian_kanban_cards i
        WHERE i.initiative_id = ?
          AND i.task_id IS NULL
          AND i.card_type = 'initiative'
          AND i.board_slug = ?
          AND i.closed_at IS NULL
        """,
        (initiative_id, board),
    ).fetchall()
    if len(initiative) != 1:
        raise ValueError(f"no open parent initiative found for {initiative_id!r}")

    source_status = row["status"]
    if source_status == to_status:
        raise ValueError(
            f"no-op override: task {task_id!r} is already in {to_status!r}"
        )

    current_run_id = row["current_run_id"]
    if current_run_id is not None:
        if (
            isinstance(current_run_id, bool)
            or not isinstance(current_run_id, int)
            or current_run_id <= 0
        ):
            raise ValueError(
                f"run integrity failure: task {task_id!r} references "
                f"run {current_run_id!r} that is not a positive integer"
            )
    claim_lock = row["claim_lock"]
    claim_expires = row["claim_expires"]
    worker_pid = row["worker_pid"]
    claim_run_closures = []
    if current_run_id:
        task_claim_lock = claim_lock
        if source_status != "running" or not (
            isinstance(task_claim_lock, str) and task_claim_lock.strip()
        ):
            raise ValueError(
                f"claim integrity failure: task {task_id!r} has a live "
                f"current_run_id {current_run_id!r} but no running task with a "
                f"nonblank claim_lock"
            )
        run = conn.execute(
            "SELECT status, ended_at, claim_lock FROM task_runs "
            "WHERE id = ? AND task_id = ?",
            (current_run_id, task_id),
        ).fetchone()
        if (
            run is None
            or run["status"] != "running"
            or run["ended_at"] is not None
            or run["claim_lock"] != task_claim_lock
        ):
            raise ValueError(
                f"run integrity failure: task {task_id!r} references "
                f"run {current_run_id!r} that has no live task_runs row with a "
                f"matching claim_lock"
            )
        claim_run_closures.append(
            {
                "task_id": task_id,
                "run_id": current_run_id,
                "run_status": run["status"],
                "outcome": "overridden",
                "claim_lock": task_claim_lock,
            }
        )
    elif claim_lock is not None or claim_expires is not None or worker_pid is not None:
        raise ValueError(
            f"claim integrity failure: task {task_id!r} has no current_run_id "
            f"but carries corrupt claim state"
        )

    gates = []
    governed = conn.execute(
        "SELECT 1 FROM task_handoff_requirements WHERE task_id = ? LIMIT 1",
        (task_id,),
    ).fetchone()
    if to_status == "done" and governed is not None:
        verdict = conn.execute(
            """
            SELECT verdict_id
            FROM task_reviewer_verdicts
            WHERE task_id = ? AND verdict = 'accepted'
            ORDER BY verdict_id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if verdict is None:
            gates.append(
                _check(
                    "accepted_reviewer_verdict",
                    "unmet",
                    None,
                    "no accepted reviewer verdict authorizes completion",
                )
            )
        else:
            gates.append(
                _check(
                    "accepted_reviewer_verdict",
                    "met",
                    verdict["verdict_id"],
                    "latest accepted reviewer verdict authorizes completion",
                )
            )
    else:
        gates.append(
            _check(
                "ordinary_task_transition",
                "unmet",
                None,
                f"no ordinary gate evidence authorizes {source_status} -> {to_status}",
            )
        )

    dependency_changes = []
    downstream_exceptions = []
    children = conn.execute(
        """
        SELECT t.id AS id, t.status AS status
        FROM task_links l
        JOIN tasks t ON t.id = l.child_id
        WHERE l.parent_id = ?
        ORDER BY t.id
        """,
        (task_id,),
    ).fetchall()
    for child in children:
        child_id = child["id"]
        child_status = child["status"]
        if to_status == "done":
            if child_status != "todo":
                continue
            other_parents = conn.execute(
                """
                SELECT 1
                FROM task_links pl
                JOIN tasks pt ON pt.id = pl.parent_id
                WHERE pl.child_id = ? AND pl.parent_id != ?
                  AND pt.status NOT IN ('done', 'archived')
                LIMIT 1
                """,
                (child_id, task_id),
            ).fetchone()
            if other_parents is None:
                dependency_changes.append(
                    {
                        "task_id": child_id,
                        "action": "release",
                        "from_status": "todo",
                        "to_status": "ready",
                    }
                )
        elif source_status == "done" and to_status in ("todo", "ready", "review"):
            if child_status in ("ready", "review", "blocked"):
                dependency_changes.append(
                    {
                        "task_id": child_id,
                        "action": "re_gate",
                        "from_status": child_status,
                        "to_status": "todo",
                    }
                )
            elif child_status in ("running",) or child_status in TERMINAL_STATUSES:
                downstream_exceptions.append(
                    {
                        "task_id": child_id,
                        "status": child_status,
                        "reason": "active_or_terminal_dependent_requires_explicit_disposition",
                    }
                )

    non_bypassable_checks = [
        _check("task_identity", "met", None, f"task {task_id!r} resolved to a single open card"),
        _check("card_identity", "met", None, f"card record_version {row['record_version']} preserved"),
        _check("open_parent_initiative", "met", None, f"open parent initiative {initiative_id!r}"),
        _check("destination_shape", "met", None, f"destination {to_status!r} is a supported human override destination"),
        _check("run_integrity", "met", None, "current run reference verified against live task_runs"),
    ]

    return {
        "operation": "kanban_execute_task_gate_override",
        "initiative_id": initiative_id,
        "target": task_id,
        "target_kind": "task",
        "board": board,
        "expected_version": row["record_version"],
        "source": {
            "status": source_status,
            "assignee": row["assignee"],
            "current_run_id": current_run_id,
            "claim_lock": claim_lock,
            "claim_expires": claim_expires,
            "worker_pid": worker_pid,
        },
        "destination": {"status": to_status},
        "gates": gates,
        "non_bypassable_checks": non_bypassable_checks,
        "claim_run_closures": claim_run_closures,
        "dependency_changes": dependency_changes,
        "downstream_exceptions": downstream_exceptions,
        "reconciliation_ref": None,
        "override_reason": override_reason,
        "commentary": (
            f"Per Adrian's explicit instruction, {task_id} moved from "
            f"{source_status} to {to_status}; gate criteria overridden, not satisfied."
        ),
    }


def prepare_task_status_override(
    conn,
    *,
    initial_authorizer,
    initial_session_id,
    initial_message_id,
    initial_quote,
    executor_session_id,
    executor_profile,
    task_id,
    board,
    to_status,
    override_reason,
    now,
    expires_at,
):
    """Prepare one task gate override on the caller's open transaction.

    Thin coordinator: derives the proposal from live state, then stores it and
    prepares the Write-Gate approval via ``store_prepared_override``. Never
    begins, commits, or rolls back; never mints evidence, displays UI, approves,
    consumes, or mutates a task.
    """
    if initial_session_id != executor_session_id:
        raise ValueError("initial and executor sessions must be identical")

    request_id = f"kanban-task-gate-override:{initial_message_id}"
    approval_id = f"kanban-task-gate-override-approval:{initial_message_id}"

    proposal = derive_task_status_override(
        conn,
        task_id=task_id,
        board=board,
        to_status=to_status,
        override_reason=override_reason,
        executor_session_id=executor_session_id,
        executor_profile=executor_profile,
    )

    return store_prepared_override(
        conn,
        request_id=request_id,
        approval_id=approval_id,
        initial_authorizer=initial_authorizer,
        initial_session_id=initial_session_id,
        initial_message_id=initial_message_id,
        initial_quote=initial_quote,
        executor_session_id=executor_session_id,
        executor_profile=executor_profile,
        proposal=proposal,
        now=now,
        expires_at=expires_at,
    )
