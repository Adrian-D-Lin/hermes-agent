"""Live derivation of task-card gate-override proposals (design v0.28 §7.13)."""

from __future__ import annotations

import json

from .override_proposals import consume_revalidated_override, store_prepared_override

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
        child_card = conn.execute(
            """
            SELECT c.record_version AS record_version,
                   t.current_run_id AS current_run_id,
                   t.claim_lock AS claim_lock
            FROM adrian_kanban_cards c
            JOIN tasks t ON t.id = c.task_id
            WHERE c.task_id = ? AND c.card_type = 'task'
              AND c.board_slug = ? AND c.closed_at IS NULL
            """,
            (child_id, board),
        ).fetchall()
        if len(child_card) != 1:
            raise ValueError(
                f"no open task card found for dependent task {child_id!r}"
            )
        child_card = child_card[0]
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
                        "expected_version": child_card["record_version"],
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
                        "expected_version": child_card["record_version"],
                    }
                )
            elif child_status in ("running",) or child_status in TERMINAL_STATUSES:
                downstream_exceptions.append(
                    {
                        "task_id": child_id,
                        "status": child_status,
                        "expected_version": child_card["record_version"],
                        "current_run_id": child_card["current_run_id"],
                        "claim_lock": child_card["claim_lock"],
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


def execute_approved_task_status_override(
    conn,
    *,
    request_id,
    executor_session_id,
    executor_profile,
    mutation_id,
    idempotency_key,
    now,
):
    """Atomically execute an approved task gate override on the caller's open transaction.

    Never begins, commits, or rolls back; a late failure propagates so the
    caller's rollback reverses approval consumption, version advance, run
    closure, task movement, dependency changes, and records.
    """
    if not request_id or not isinstance(request_id, str):
        raise ValueError("request_id is required")
    if not executor_session_id or not isinstance(executor_session_id, str):
        raise ValueError("executor_session_id is required")
    if executor_profile != "default":
        raise ValueError("only the default executor profile may execute overrides")
    if not mutation_id or not isinstance(mutation_id, str):
        raise ValueError("mutation_id is required")
    if not idempotency_key or not isinstance(idempotency_key, str):
        raise ValueError("idempotency_key is required")
    if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
        raise ValueError("now must be a positive integer")

    row = conn.execute(
        "SELECT canonical_payload FROM gate_override_proposals WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown override request {request_id!r}")
    payload = json.loads(row["canonical_payload"])
    stored_proposal = payload["proposal"]
    executor = payload["executor"]
    if executor.get("session_id") != executor_session_id:
        raise ValueError("executor session does not match the approved override")
    if executor.get("profile") != executor_profile:
        raise ValueError("executor profile does not match the approved override")

    rederived = derive_task_status_override(
        conn,
        task_id=stored_proposal["target"],
        board=stored_proposal["board"],
        to_status=stored_proposal["destination"]["status"],
        override_reason=stored_proposal["override_reason"],
        executor_session_id=executor_session_id,
        executor_profile=executor_profile,
    )
    if rederived != stored_proposal:
        raise ValueError("task record version no longer matches the approved override")

    consumed = consume_revalidated_override(
        conn,
        request_id=request_id,
        operation=stored_proposal["operation"],
        target=stored_proposal["target"],
        executor_session_id=executor_session_id,
        executor_profile=executor_profile,
        current_proposal=rederived,
        mutation_id=mutation_id,
        idempotency_key=idempotency_key,
        now=now,
    )

    task_id = stored_proposal["target"]
    board = stored_proposal["board"]
    source_status = rederived["source"]["status"]
    to_status = rederived["destination"]["status"]
    expected_version = rederived["expected_version"]

    task_card = conn.execute(
        "SELECT id, record_version FROM adrian_kanban_cards "
        "WHERE task_id = ? AND card_type = 'task' AND board_slug = ? AND closed_at IS NULL",
        (task_id, board),
    ).fetchone()
    if task_card is None:
        raise ValueError(f"no open task card found for task {task_id!r}")
    task_card_id = task_card["id"]
    if task_card["record_version"] != expected_version:
        raise ValueError("task record version no longer matches the approved override")

    initiative_card = conn.execute(
        "SELECT id FROM adrian_kanban_cards "
        "WHERE initiative_id = ? AND task_id IS NULL AND card_type = 'initiative' "
        "AND board_slug = ? AND closed_at IS NULL",
        (rederived["initiative_id"], board),
    ).fetchone()
    if initiative_card is None:
        raise ValueError(f"no open parent initiative found for {rederived['initiative_id']!r}")
    initiative_card_id = initiative_card["id"]

    updated = conn.execute(
        "UPDATE adrian_kanban_cards SET record_version = ? WHERE id = ? AND record_version = ?",
        (expected_version + 1, task_card_id, expected_version),
    )
    if updated.rowcount != 1:
        raise ValueError("task record version no longer matches the approved override")

    for closure in rederived["claim_run_closures"]:
        run_id = closure["run_id"]
        run = conn.execute(
            "SELECT status, ended_at, claim_lock FROM task_runs WHERE id = ? AND task_id = ?",
            (run_id, task_id),
        ).fetchone()
        if (
            run is None
            or run["status"] != closure["run_status"]
            or run["ended_at"] is not None
            or run["claim_lock"] != closure["claim_lock"]
        ):
            raise ValueError(
                f"run integrity failure: task {task_id!r} references "
                f"run {run_id!r} that has no live task_runs row with a matching claim_lock"
            )
        run_updated = conn.execute(
            "UPDATE task_runs SET status = 'released', outcome = 'overridden', "
            "ended_at = ?, claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND task_id = ? AND status = ? AND ended_at IS NULL AND claim_lock = ?",
            (now, run_id, task_id, closure["run_status"], closure["claim_lock"]),
        )
        if run_updated.rowcount != 1:
            raise ValueError(
                f"run integrity failure: task {task_id!r} references "
                f"run {run_id!r} that has no live task_runs row with a matching claim_lock"
            )

    source = rederived["source"]
    task_updated = conn.execute(
        "UPDATE tasks SET status = ?, current_run_id = NULL, claim_lock = NULL, "
        "claim_expires = NULL, worker_pid = NULL, completed_at = ? "
        "WHERE id = ? AND status = ? AND current_run_id IS ? AND claim_lock IS ? "
        "AND claim_expires IS ? AND worker_pid IS ?",
        (
            to_status,
            now if to_status == "done" else None,
            task_id,
            source_status,
            source["current_run_id"],
            source["claim_lock"],
            source["claim_expires"],
            source["worker_pid"],
        ),
    )
    if task_updated.rowcount != 1:
        raise ValueError("task record version no longer matches the approved override")

    for change in rederived["dependency_changes"]:
        child_id = change["task_id"]
        child_updated = conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND status = ?",
            (change["to_status"], child_id, change["from_status"]),
        )
        if child_updated.rowcount != 1:
            raise ValueError("task record version no longer matches the approved override")
        child_card = conn.execute(
            "SELECT id, record_version FROM adrian_kanban_cards "
            "WHERE task_id = ? AND card_type = 'task' AND board_slug = ? AND closed_at IS NULL",
            (child_id, board),
        ).fetchall()
        if len(child_card) != 1:
            raise ValueError(f"no open task card found for task {child_id!r}")
        child_card = child_card[0]
        child_expected_version = change["expected_version"]
        if child_card["record_version"] != child_expected_version:
            raise ValueError("task record version no longer matches the approved override")
        child_card_updated = conn.execute(
            "UPDATE adrian_kanban_cards SET record_version = ? WHERE id = ? AND record_version = ?",
            (child_expected_version + 1, child_card["id"], child_expected_version),
        )
        if child_card_updated.rowcount != 1:
            raise ValueError("task record version no longer matches the approved override")
        unsatisfied_gates = [
            gate for gate in rederived["gates"] if gate["result"] != "met"
        ]
        event_kind = (
            "gate_override_dependency_release"
            if change["action"] == "release"
            else "gate_override_dependency_regate"
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                child_id,
                event_kind,
                json.dumps(
                    {
                        "override_request_id": request_id,
                        "unsatisfied_gates": unsatisfied_gates,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                now,
            ),
        )

    unsatisfied_gates = [gate for gate in rederived["gates"] if gate["result"] != "met"]
    canonical_payload = json.dumps(
        {
            **rederived,
            "movement_basis": "adrian_gate_override",
            "result": "accepted_with_active_decision",
            "override_request_id": request_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    actor_evidence = json.dumps(
        {
            "executor_session_id": executor_session_id,
            "executor_profile": executor_profile,
            "approval_evidence": consumed["approval_evidence"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    conn.execute(
        "INSERT INTO task_gate_override_records ("
        "request_id, approval_id, mutation_id, initiative_card_id, initiative_id, "
        "task_card_id, task_id, from_status, to_status, "
        "override_authority_ref, override_reason, unsatisfied_gates, movement_basis, "
        "result, actor_evidence, canonical_payload, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            request_id,
            consumed["approval_id"],
            mutation_id,
            initiative_card_id,
            rederived["initiative_id"],
            task_card_id,
            task_id,
            source_status,
            to_status,
            request_id,
            rederived["override_reason"],
            json.dumps(unsatisfied_gates, sort_keys=True, separators=(",", ":")),
            "adrian_gate_override",
            "accepted_with_active_decision",
            actor_evidence,
            canonical_payload,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO task_active_decisions ("
        "decision_id, task_card_id, task_id, initiative_id, decision_kind, "
        "authority_ref, canonical_payload, active, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            f"decision:{request_id}",
            task_card_id,
            task_id,
            rederived["initiative_id"],
            "adrian_gate_override",
            request_id,
            canonical_payload,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, 'adrian-kanban', ?, ?)",
        (task_id, rederived["commentary"], now),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, 'gate_override_applied', ?, ?)",
        (
            task_id,
            json.dumps(
                {
                    "override_request_id": request_id,
                    "active_decision_id": f"decision:{request_id}",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            now,
        ),
    )

    return {
        "task_id": task_id,
        "from_status": source_status,
        "to_status": to_status,
        "record_version": expected_version + 1,
        "request_id": request_id,
        "result": "accepted_with_active_decision",
    }
