from __future__ import annotations

import json
import sqlite3
from typing import Any


def _json_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return False


def _get_lifecycle_contract(conn: sqlite3.Connection, task_card_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT contract_id, contract_version, step, segment_id, workspace_id, "
        "execution_profile, canonical_contract_payload, registry_hash, "
        "skill_id, skill_version, skill_hash, created_at "
        "FROM task_lifecycle_contracts WHERE task_card_id = ?",
        (task_card_id,),
    ).fetchone()
    if row is None:
        return None
    data = _row_to_dict(row)
    data["canonical_contract_payload"] = _json_or_none(data["canonical_contract_payload"])
    return data


def _get_input_manifest(conn: sqlite3.Connection, task_card_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT canonical_payload, declared_inputs_accessible, created_at "
        "FROM task_input_manifests WHERE task_card_id = ?",
        (task_card_id,),
    ).fetchone()
    if row is None:
        return None
    data = _row_to_dict(row)
    data["canonical_payload"] = _json_or_none(data["canonical_payload"])
    data["declared_inputs_accessible"] = _parse_bool(data["declared_inputs_accessible"])

    entries = conn.execute(
        "SELECT workspace_path, sha256, source_kind, source_locator, context_guidance "
        "FROM task_input_entries WHERE task_card_id = ? ORDER BY workspace_path",
        (task_card_id,),
    ).fetchall()
    data["entries"] = [_row_to_dict(e) for e in entries]
    return data


def _get_handoff_requirement(conn: sqlite3.Connection, task_card_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT version, execution_profile, reviewer, canonical_payload, created_at "
        "FROM task_handoff_requirements WHERE task_card_id = ?",
        (task_card_id,),
    ).fetchone()
    if row is None:
        return None
    data = _row_to_dict(row)
    data["canonical_payload"] = _json_or_none(data["canonical_payload"])
    return data


def _get_latest_candidate(conn: sqlite3.Connection, task_card_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT candidate_id, execution_run_id, reviewer, summary, metadata_json, "
        "submitted_by, created_at "
        "FROM task_candidate_handoffs WHERE task_card_id = ? "
        "ORDER BY created_at DESC, candidate_id DESC LIMIT 1",
        (task_card_id,),
    ).fetchone()
    if row is None:
        return None
    data = _row_to_dict(row)
    data["metadata_json"] = _json_or_none(data["metadata_json"])
    return data


def _get_accepted_handoff(conn: sqlite3.Connection, task_card_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT v.verdict_id, v.candidate_id, v.review_run_id, v.reviewer, "
        "v.summary, v.created_at, c.metadata_json "
        "FROM task_reviewer_verdicts v "
        "JOIN task_candidate_handoffs c ON c.candidate_id = v.candidate_id "
        "WHERE v.task_card_id = ? AND v.verdict = 'accepted' "
        "ORDER BY v.created_at DESC LIMIT 1",
        (task_card_id,),
    ).fetchone()
    if row is None:
        return None
    data = _row_to_dict(row)
    data["metadata_json"] = _json_or_none(data["metadata_json"])
    return data


def _get_task_attachments(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, filename, content_type, size, uploaded_by, created_at "
        "FROM task_attachments WHERE task_id = ? ORDER BY created_at, id",
        (task_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _get_cleanup_status(conn, replacement_id, cleanup_required):
    if not cleanup_required:
        return {'state': 'retained', 'items': []}
    rows = conn.execute(
        "SELECT source_path, destination_path, expected_inventory FROM task_purge_cleanup_items WHERE replacement_id=? ORDER BY source_path",
        (replacement_id,),
    ).fetchall()
    items = []
    for row in rows:
        source_path = row[0]
        destination_path = row[1]
        expected_inventory_raw = row[2]
        evidence = None
        error = None
        state = 'failed'
        event_row = conn.execute(
            "SELECT status, evidence_json, error FROM task_purge_cleanup_events WHERE replacement_id=? AND source_path=? ORDER BY event_id DESC LIMIT 1",
            (replacement_id, source_path),
        ).fetchone()
        if event_row is None:
            state = "failed"
            evidence = None
            error = "No cleanup event found for source_path"
        else:
            status = event_row[0]
            evidence_raw = event_row[1]
            error = event_row[2]
            if status == "prepared":
                state = "pending"
                evidence = None
            elif status == "failed":
                state = "failed"
                evidence = None
            elif status == "verified":
                try:
                    evidence = json.loads(evidence_raw) if evidence_raw else None
                except (json.JSONDecodeError, TypeError):
                    evidence = None
                if not isinstance(evidence, dict) or evidence.get("verified") is not True:
                    state = "failed"
                    error = "Malformed or missing verified evidence"
                else:
                    try:
                        expected_inv = json.loads(expected_inventory_raw) if expected_inventory_raw else None
                    except (json.JSONDecodeError, TypeError):
                        expected_inv = None
                    if not isinstance(expected_inv, dict):
                        state = "failed"
                        error = "Malformed expected_inventory"
                    else:
                        ev_source = evidence.get("source")
                        ev_archive = evidence.get("archive")
                        ev_sha = evidence.get("sha256")
                        exp_sha = expected_inv.get("sha256")
                        if not isinstance(ev_sha, str) or not ev_sha.strip() or ev_sha != exp_sha:
                            state = "failed"
                            error = "Evidence mismatch with expected inventory"
                        elif ev_source != source_path or ev_archive != destination_path:
                            state = "failed"
                            error = "Evidence mismatch with expected inventory"
                        else:
                            state = "verified"
            else:
                state = "failed"
                error = f"Unknown status: {status}"
        items.append({
            "source": source_path,
            "archive": destination_path,
            "state": state,
            "evidence": evidence,
            "error": error,
        })
    if not items:
        overall_state = "failed"
    elif all(item["state"] == "verified" for item in items):
        overall_state = "verified"
    elif any(item["state"] == "failed" for item in items):
        overall_state = "failed"
    else:
        overall_state = "pending"
    result = {"state": overall_state, "items": items}
    if overall_state not in ("verified", "retained"):
        result["remediation"] = "replay original approved request"
    return result


def _get_purge_replacements(
    conn: sqlite3.Connection,
    board: str,
    initiative_id: str,
    successor_task_id: str | None = None,
) -> list[dict[str, Any]]:
    query = """
        SELECT replacement_id, initiative_card_id, initiative_id, board_slug,
               predecessor_task_card_id, predecessor_task_id,
               successor_task_card_id, successor_task_id,
               eligibility_classification, eligibility_evidence,
               authorization_approval_id, requester_evidence,
               repository_disposition_reference, repository_disposition_action,
               repository_disposition_preservation_ref, transferred_relations,
               cleanup_required, predecessor_workspace_snapshot, created_at
        FROM task_purge_replacements
        WHERE board_slug = ? AND initiative_id = ?
    """
    params: list[Any] = [board, initiative_id]
    if successor_task_id is not None:
        query += " AND successor_task_id = ?"
        params.append(successor_task_id)
    query += " ORDER BY created_at, replacement_id"
    rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        d = _row_to_dict(row)
        d["board"] = d.pop("board_slug")
        d["eligibility_evidence"] = _json_or_none(d["eligibility_evidence"])
        d["requester_evidence"] = _json_or_none(d["requester_evidence"])
        d["transferred_relations"] = _json_or_none(d["transferred_relations"])
        d["predecessor_workspace_snapshot"] = _json_or_none(d["predecessor_workspace_snapshot"])
        d["cleanup_required"] = _parse_bool(d["cleanup_required"])
        d['cleanup'] = _get_cleanup_status(conn, d['replacement_id'], d['cleanup_required'])
        result.append(d)
    return result


def _collect_open_findings(
    phase_results: list[dict[str, Any]],
    handoff_rejections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    for pr in phase_results:
        payload = pr.get("canonical_payload")
        if isinstance(payload, dict):
            f_list = payload.get("findings")
            if isinstance(f_list, list):
                for item in f_list:
                    if not isinstance(item, dict):
                        continue
                    status = str(item.get("status", "")).lower()
                    disposition = str(item.get("disposition", "")).lower()
                    if status in ("closed", "resolved", "accepted") or disposition in ("closed", "resolved", "accepted"):
                        continue
                    findings.append(item)

    for rej in handoff_rejections:
        f_json = rej.get("findings_json")
        parsed = _json_or_none(f_json)
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", "")).lower()
                disposition = str(item.get("disposition", "")).lower()
                if status in ("closed", "resolved", "accepted") or disposition in ("closed", "resolved", "accepted"):
                    continue
                findings.append(item)

    return findings


def show_projection(
    conn: sqlite3.Connection,
    task_id: str | None,
    board: str,
    initiative_id: str | None = None,
) -> dict[str, Any]:
    if task_id is not None:
        card_row = conn.execute(
            "SELECT id, card_type, initiative_id, task_id, title, board_slug, record_version "
            "FROM adrian_kanban_cards WHERE task_id = ? AND board_slug = ? AND card_type = 'task'",
            (task_id, board),
        ).fetchone()
        if card_row is None:
            raise ValueError(f"task card not found: {task_id}")

        card = _row_to_dict(card_row)
        card["board"] = card.pop("board_slug")
        card.pop("id")

        native = conn.execute(
            "SELECT status, assignee, body, priority, tenant, created_at, started_at, "
            "completed_at, current_run_id, workspace_kind, workspace_path, result "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if native is None:
            raise ValueError(f"native task not found: {task_id}")
        task_data = _row_to_dict(native)

        lifecycle = _get_lifecycle_contract(conn, card_row["id"])
        legacy = lifecycle is None

        manifest = _get_input_manifest(conn, card_row["id"])
        handoff_req = _get_handoff_requirement(conn, card_row["id"])
        latest_candidate = _get_latest_candidate(conn, card_row["id"])
        accepted_handoff = _get_accepted_handoff(conn, card_row["id"])
        attachments = _get_task_attachments(conn, task_id)

        purge_replacement = None
        replacements = _get_purge_replacements(conn, board, card_row["initiative_id"], task_id)
        if replacements:
            purge_replacement = replacements[-1]

        return {
            "card": card,
            "task": task_data,
            "legacy": legacy,
            "lifecycle_contract": lifecycle,
            "input_manifest": manifest,
            "handoff_requirement": handoff_req,
            "latest_candidate": latest_candidate,
            "accepted_handoff": accepted_handoff,
            "attachments": attachments,
            "purge_replacement": purge_replacement,
        }

    if initiative_id is not None:
        card_row = conn.execute(
            "SELECT id, card_type, initiative_id, task_id, title, board_slug, record_version "
            "FROM adrian_kanban_cards WHERE initiative_id = ? AND board_slug = ? AND card_type = 'initiative'",
            (initiative_id, board),
        ).fetchone()
        if card_row is None:
            raise ValueError(f"initiative card not found: {initiative_id}")

        card = _row_to_dict(card_row)
        card["board"] = card.pop("board_slug")
        card.pop("id")

        transitions = conn.execute(
            "SELECT previous_transition_id, transition_id, from_phase, from_segment_id, "
            "to_phase, to_segment_id, canon_route, repository_reconciliation_ref, "
            "trigger, actor_evidence, canonical_payload, rendered_history_ref, created_at "
            "FROM initiative_transitions WHERE initiative_id = ? "
            "ORDER BY transition_id",
            (initiative_id,),
        ).fetchall()
        transition_list = []
        for t in transitions:
            d = _row_to_dict(t)
            d["actor_evidence"] = _json_or_none(d["actor_evidence"])
            d["canonical_payload"] = _json_or_none(d["canonical_payload"])
            transition_list.append(d)

        current_transition = transition_list[-1] if transition_list else None

        phase_results = conn.execute(
            "SELECT result_id, phase, segment_id, iteration, result_kind, "
            "contract_id, contract_version, canonical_payload, accepted_task_refs, "
            "accepted_checkpoint_refs, actor_evidence, idempotency_key, accepted, created_at "
            "FROM initiative_phase_results WHERE initiative_id = ? "
            "ORDER BY created_at, result_id",
            (initiative_id,),
        ).fetchall()
        pr_list = []
        for pr in phase_results:
            d = _row_to_dict(pr)
            d["canonical_payload"] = _json_or_none(d["canonical_payload"])
            d["accepted_task_refs"] = _json_or_none(d["accepted_task_refs"])
            d["accepted_checkpoint_refs"] = _json_or_none(d["accepted_checkpoint_refs"])
            d["actor_evidence"] = _json_or_none(d["actor_evidence"])
            d["accepted"] = _parse_bool(d["accepted"])
            pr_list.append(d)

        proj_row = conn.execute(
            "SELECT projection_id, projection_version, manifest_path, manifest_sha, "
            "content_digest, parsed_segment_definitions, readiness_refs, validation_result, projected_at "
            "FROM initiative_segment_projections WHERE initiative_id = ? "
            "ORDER BY projection_version DESC LIMIT 1",
            (initiative_id,),
        ).fetchone()
        segment_projection = None
        if proj_row:
            segment_projection = _row_to_dict(proj_row)
            segment_projection["parsed_segment_definitions"] = _json_or_none(
                segment_projection["parsed_segment_definitions"]
            )
            segment_projection["readiness_refs"] = _json_or_none(
                segment_projection["readiness_refs"]
            )

        workspaces = conn.execute(
            "SELECT workspace_id, segment_id, projection_id, lifecycle_state, "
            "controller_binding_ref, active, created_at, updated_at "
            "FROM segment_workspaces WHERE initiative_id = ?",
            (initiative_id,),
        ).fetchall()
        ws_list = []
        for ws in workspaces:
            d = _row_to_dict(ws)
            d["active"] = _parse_bool(d["active"])
            members = conn.execute(
                "SELECT repository_identity, relative_path, branch, required_base_sha, "
                "observed_head, member_state, observed_at "
                "FROM segment_workspace_members WHERE workspace_id = ? "
                "ORDER BY repository_identity",
                (d["workspace_id"],),
            ).fetchall()
            d["members"] = [_row_to_dict(m) for m in members]
            ws_list.append(d)

        task_cards = conn.execute(
            "SELECT c.id, c.task_id, c.title, c.record_version, "
            "t.status, t.assignee "
            "FROM adrian_kanban_cards c "
            "LEFT JOIN tasks t ON t.id = c.task_id "
            "WHERE c.initiative_id = ? AND c.board_slug = ? AND c.card_type = 'task' "
            "ORDER BY c.created_at, c.id",
            (initiative_id, board),
        ).fetchall()

        all_replacements = _get_purge_replacements(conn, board, initiative_id)
        replacement_by_successor = {r["successor_task_id"]: r for r in all_replacements}
        tasks_list = []
        for tc in task_cards:
            tc_dict = _row_to_dict(tc)
            lifecycle = _get_lifecycle_contract(conn, tc_dict["id"])
            latest_candidate = _get_latest_candidate(conn, tc_dict["id"])
            accepted_handoff = _get_accepted_handoff(conn, tc_dict["id"])
            replaces_task_id = None
            if tc_dict["task_id"] in replacement_by_successor:
                replaces_task_id = replacement_by_successor[tc_dict["task_id"]]["predecessor_task_id"]
            tasks_list.append({
                "task_id": tc_dict["task_id"],
                "title": tc_dict["title"],
                "record_version": tc_dict["record_version"],
                "status": tc_dict["status"],
                "assignee": tc_dict["assignee"],
                "lifecycle_contract": lifecycle,
                "latest_candidate": latest_candidate,
                "accepted_handoff": accepted_handoff,
                "replaces_task_id": replaces_task_id,
            })

        rejections = conn.execute(
            "SELECT rejection_id, findings_json, created_at "
            "FROM task_handoff_rejections WHERE task_card_id IN "
            "(SELECT id FROM adrian_kanban_cards WHERE initiative_id = ? AND card_type = 'task') "
            "ORDER BY created_at",
            (initiative_id,),
        ).fetchall()
        rej_list = [_row_to_dict(r) for r in rejections]

        open_findings = _collect_open_findings(pr_list, rej_list)

        next_permitted_routes: list[str] = []
        if current_transition:
            payload = current_transition.get("canonical_payload")
            if isinstance(payload, dict):
                routes = payload.get("next_permitted_routes")
                if isinstance(routes, list) and all(
                    isinstance(r, str) and r.strip() for r in routes
                ):
                    next_permitted_routes = routes
                else:
                    canon = current_transition.get("canon_route")
                    if isinstance(canon, str) and canon.strip():
                        next_permitted_routes = [canon]

        return {
            "card": card,
            "current_transition": current_transition,
            "transition_history": transition_list,
            "phase_results": pr_list,
            "segment_projection": segment_projection,
            "workspaces": ws_list,
            "tasks": tasks_list,
            "open_findings": open_findings,
            "next_permitted_routes": next_permitted_routes,
            "purge_replacements": all_replacements,
        }

    raise ValueError("must provide task_id or initiative_id")


def list_projection(
    conn: sqlite3.Connection,
    board: str | None = None,
    initiative_id: str | None = None,
    assignee: str | None = None,
    status: str | None = None,
    tenant: str | None = None,
    include_archived: bool | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    init_clauses = []
    init_params: list[Any] = []

    if board:
        init_clauses.append("c.board_slug = ?")
        init_params.append(board)
    if initiative_id:
        init_clauses.append("c.initiative_id = ?")
        init_params.append(initiative_id)

    task_clauses = []
    task_params: list[Any] = []

    if board:
        task_clauses.append("c.board_slug = ?")
        task_params.append(board)
    if initiative_id:
        task_clauses.append("c.initiative_id = ?")
        task_params.append(initiative_id)
    if assignee:
        task_clauses.append("t.assignee = ?")
        task_params.append(assignee)
    if status:
        task_clauses.append("t.status = ?")
        task_params.append(status)
    if tenant:
        task_clauses.append("t.tenant = ?")
        task_params.append(tenant)
    if include_archived is False:
        task_clauses.append("t.status != 'archived'")

    init_query = """
        SELECT c.id, c.card_type, c.initiative_id, c.task_id, c.title, c.board_slug, c.record_version, c.created_at,
               NULL as status, NULL as assignee
        FROM adrian_kanban_cards c
        WHERE c.card_type = 'initiative'
    """
    if init_clauses:
        init_query += " AND " + " AND ".join(init_clauses)

    task_query = """
        SELECT c.id, c.card_type, c.initiative_id, c.task_id, c.title, c.board_slug, c.record_version, c.created_at,
               t.status, t.assignee
        FROM adrian_kanban_cards c
        LEFT JOIN tasks t ON t.id = c.task_id
        WHERE c.card_type = 'task'
    """
    if task_clauses:
        task_query += " AND " + " AND ".join(task_clauses)

    init_rows = conn.execute(init_query, init_params).fetchall()
    task_rows = conn.execute(task_query, task_params).fetchall()

    latest_transitions: dict[str, tuple[Any, Any]] = {}
    for r in init_rows:
        row = conn.execute(
            "SELECT to_phase, to_segment_id FROM initiative_transitions WHERE initiative_id = ? ORDER BY transition_id DESC LIMIT 1",
            (r["initiative_id"],),
        ).fetchone()
        if row is not None:
            latest_transitions[r["initiative_id"]] = (row[0], row[1])

    lifecycle_contracts: dict[Any, tuple[Any, Any, Any]] = {}
    for r in task_rows:
        row = conn.execute(
            "SELECT step, segment_id, workspace_id FROM task_lifecycle_contracts WHERE task_card_id = ? LIMIT 1",
            (r["id"],),
        ).fetchone()
        if row is not None:
            lifecycle_contracts[r["id"]] = (row[0], row[1], row[2])

    initiatives = []
    for r in init_rows:
        d = _row_to_dict(r)
        d["board"] = d.pop("board_slug")
        d.pop("id")
        trans = latest_transitions.get(r["initiative_id"])
        d["current_phase"] = trans[0] if trans else None
        d["current_segment_id"] = trans[1] if trans else None
        initiatives.append(d)

    tasks = []
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in task_rows:
        key = (r["board_slug"], r["initiative_id"])
        if key not in groups:
            groups[key] = []
        groups[key].append(r)
    replacement_map: dict[tuple[str, str], dict[str, str]] = {}
    for (b, init_id), rows in groups.items():
        reps = _get_purge_replacements(conn, b, init_id)
        m = {}
        for rep in reps:
            m[rep["successor_task_id"]] = rep["predecessor_task_id"]
        replacement_map[(b, init_id)] = m
    for r in task_rows:
        d = _row_to_dict(r)
        d["board"] = d.pop("board_slug")
        d.pop("id")
        key = (r["board_slug"], r["initiative_id"])
        replaces_task_id = None
        if key in replacement_map:
            replaces_task_id = replacement_map[key].get(d["task_id"])
        d["replaces_task_id"] = replaces_task_id
        contract = lifecycle_contracts.get(r["id"])
        d["lifecycle_phase"] = contract[0] if contract else None
        d["segment_id"] = contract[1] if contract else None
        d["segment_workspace_id"] = contract[2] if contract else None
        tasks.append(d)

    initiatives.sort(key=lambda x: (x["created_at"], x["initiative_id"]))
    tasks.sort(key=lambda x: (x["created_at"], x["task_id"]))

    combined = initiatives + tasks

    if limit is not None:
        combined = combined[:limit]

    return {
        "initiatives": [c for c in combined if c["card_type"] == "initiative"],
        "tasks": [c for c in combined if c["card_type"] == "task"],
        "count": len(combined),
    }


def attachments_projection(
    conn: sqlite3.Connection,
    task_id: str,
    board: str,
) -> dict[str, Any]:
    card_row = conn.execute(
        "SELECT id, task_id, board_slug FROM adrian_kanban_cards "
        "WHERE task_id = ? AND board_slug = ? AND card_type = 'task'",
        (task_id, board),
    ).fetchone()
    if card_row is None:
        raise ValueError(f"task card not found: {task_id}")

    attachments = _get_task_attachments(conn, task_id)
    return {
        "task_id": task_id,
        "board": board,
        "attachments": attachments,
    }
