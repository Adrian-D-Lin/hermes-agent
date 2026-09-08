"""Initiative mutation handlers for the Adrian Kanban command boundary.

These handlers consume a Write-Gate approval inside the outer SQLite
transaction, so consumption and mutation roll back together. The plugin
never sets approval state directly; it only loads the named approval and
consumes it through ``writegate.kanban_approvals``.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from writegate.kanban_approvals import (
    KanbanInitiativeApprovalConsumption,
    consume_approved,
)

from .initiative_checkpoints import admit_orchestration_checkpoint

INITIATIVE_PHASES = frozenset(
    {
        "D1",
        "D2",
        "D3",
        "D4",
        "DEV1",
        "DEV2",
        "DEV3",
        "DEV4",
        "PC1",
    }
)

_INITIATIVE_LEDGER_MARKER = "# [[INITIATIVE_LEDGER]]"

_INITIATIVE_REQUIRED_HEADINGS = (
    "## Initiative",
    "### Objective",
    "### Board and workspace context",
    "### Authoritative artifacts",
    "### Cleared outcomes",
    "### Open items",
    "### Related task cards",
    "### Constraints",
    "### Cold-session continuation",
)


def _validate_initiative_body(body: str) -> None:
    """Validate the initiative ledger body contract.

    The body must begin with the exact marker and then declare each required
    heading exactly once, in this order. A malformed body raises ``ValueError``.
    """
    lines = body.splitlines()
    if not lines or lines[0] != _INITIATIVE_LEDGER_MARKER:
        raise ValueError("initiative body must start with the ledger marker")
    headings: list[str] = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            headings.append(stripped)
    for expected in _INITIATIVE_REQUIRED_HEADINGS:
        if headings.count(expected) != 1:
            raise ValueError(f"heading {expected} must appear exactly once")
    seen: list[str] = []
    for heading in headings:
        if heading in _INITIATIVE_REQUIRED_HEADINGS:
            seen.append(heading)
    if seen != list(_INITIATIVE_REQUIRED_HEADINGS):
        raise ValueError("required headings must appear in order")


def _approval_digest(payload: dict[str, Any]) -> str:
    approved_payload = {
        key: value for key, value in payload.items() if key != "approval_id"
    }
    return hashlib.sha256(
        json.dumps(
            approved_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _load_approval(conn: Any, approval_id: str) -> Any:
    row = conn.execute(
        "SELECT approval_id, operation, initiative_id, proposed_creation_id, "
        "expected_version, canonical_digest, authorizer_evidence, session_id, "
        "expires_at FROM write_gate_kanban_approvals WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown approval")
    return row


def _consume_approval(
    conn: Any,
    context: Any,
    approval_id: str,
    operation: str,
    expected_version: int,
    payload: dict[str, Any],
    initiative_id: str | None,
    proposed_creation_id: str | None,
) -> None:
    row = _load_approval(conn, approval_id)
    consumption = KanbanInitiativeApprovalConsumption(
        approval_id=approval_id,
        request_id=context.attempt_id,
        operation=operation,
        initiative_id=initiative_id,
        proposed_creation_id=proposed_creation_id,
        expected_version=expected_version,
        canonical_digest=_approval_digest(payload),
        canonicalization_version=1,
        authorizer_evidence=row["authorizer_evidence"],
        session_id=context.binding.session_id,
        expires_at=row["expires_at"],
        consumed_mutation_id=context.attempt_id,
        consumed_idempotency_ref=context.idempotency_key,
    )
    consume_approved(conn, consumption, now=int(time.time()))


def _advance_initiative_version(
    conn: Any, context: Any, initiative_id: str, expected_version: int
) -> int:
    now = int(time.time())
    actor_evidence = json.dumps(
        {
            "session_id": context.binding.session_id,
            "actor_profile": context.binding.actor_profile,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    cursor = conn.execute(
        "UPDATE adrian_kanban_cards SET record_version = record_version + 1 "
        "WHERE initiative_id = ? AND record_version = ? "
        "AND card_type = 'initiative' AND task_id IS NULL AND closed_at IS NULL",
        (initiative_id, expected_version),
    )
    if cursor.rowcount != 1:
        raise ValueError("stale initiative version")
    return expected_version + 1


def _handle_create_initiative(context: Any) -> dict[str, Any]:
    payload = context.payload
    unknown = set(payload.keys()) - {
        "initiative_id",
        "title",
        "body",
        "approval_id",
        "board",
    }
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    initiative_id = payload.get("initiative_id")
    title = payload.get("title")
    body = payload.get("body")
    approval_id = payload.get("approval_id")
    board = payload.get("board")
    if not (type(initiative_id) is str and initiative_id.strip()):
        raise ValueError("initiative_id must be a nonblank string")
    if not (type(title) is str and title.strip()):
        raise ValueError("title must be a nonblank string")
    if not (type(body) is str and body.strip()):
        raise ValueError("body must be a nonblank string")
    if not (type(approval_id) is str and approval_id.strip()):
        raise ValueError("approval_id must be a nonblank string")
    if not (type(board) is str and board.strip()):
        raise ValueError("board must be a nonblank string")
    initiative_id = initiative_id.strip()
    title = title.strip()
    board = board.strip()
    _validate_initiative_body(body)

    if context.binding.expected_version != 0:
        raise ValueError("expected_version must be 0 for create")

    card = context.connection.execute(
        "SELECT id FROM adrian_kanban_cards WHERE initiative_id = ? AND board_slug = ? "
        "AND card_type = 'initiative' AND task_id IS NULL AND closed_at IS NULL",
        (initiative_id, board),
    ).fetchone()
    if card is not None:
        raise ValueError("initiative card already exists")
    if context.connection.execute(
        "SELECT 1 FROM adrian_kanban_initiatives WHERE initiative_id = ?",
        (initiative_id,),
    ).fetchone() is not None:
        raise ValueError("initiative identity already exists")

    expected_version = 0
    _consume_approval(
        context.connection,
        context,
        approval_id,
        "kanban_create_initiative",
        expected_version,
        payload,
        None,
        initiative_id,
    )
    context.connection.execute(
        "INSERT INTO adrian_kanban_initiatives (initiative_id) VALUES (?)",
        (initiative_id,),
    )
    now = int(time.time())
    card_id = context.connection.execute(
        "INSERT INTO adrian_kanban_cards "
        "(card_type, initiative_id, task_id, title, body, created_at, "
        "board_slug, record_version, closed_at) VALUES "
        "('initiative', ?, NULL, ?, ?, ?, ?, ?, NULL)",
        (initiative_id, title, body, now, board, expected_version),
    ).lastrowid
    actor_evidence = json.dumps(
        {
            "session_id": context.binding.session_id,
            "actor_profile": context.binding.actor_profile,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    context.connection.execute(
        "INSERT INTO initiative_transitions "
        "(initiative_card_id, initiative_id, previous_transition_id, "
        "transition_id, from_phase, from_segment_id, to_phase, to_segment_id, "
        "trigger, actor_evidence, canonical_payload, created_at) VALUES "
        "(?, ?, NULL, 1, NULL, NULL, 'D1', NULL, 'initialization', ?, ?, ?)",
        (
            card_id,
            initiative_id,
            actor_evidence,
            json.dumps(
                {
                    "from_phase": None,
                    "from_segment_id": None,
                    "to_phase": "D1",
                    "to_segment_id": None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            now,
        ),
    )
    return {
        "initiative_id": initiative_id,
        "phase": "D1",
        "record_version": expected_version,
    }


def _handle_update_initiative(context: Any) -> dict[str, Any]:
    payload = context.payload
    unknown = set(payload.keys()) - {
        "initiative_id",
        "update_kind",
        "update",
        "approval_id",
        "board",
    }
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    initiative_id = payload.get("initiative_id")
    update_kind = payload.get("update_kind")
    update = payload.get("update")
    approval_id = payload.get("approval_id")
    board = payload.get("board")
    if not (type(initiative_id) is str and initiative_id.strip()):
        raise ValueError("initiative_id must be a nonblank string")
    if not (type(update_kind) is str and update_kind.strip()):
        raise ValueError("update_kind must be a nonblank string")
    if not isinstance(update, dict):
        raise ValueError("update must be an object")
    if not (type(approval_id) is str and approval_id.strip()):
        raise ValueError("approval_id must be a nonblank string")
    if not (type(board) is str and board.strip()):
        raise ValueError("board must be a nonblank string")
    initiative_id = initiative_id.strip()
    update_kind = update_kind.strip()
    board = board.strip()
    if update_kind not in {
        "body_update",
        "phase_result",
        "orchestration_checkpoint",
    }:
        raise ValueError("unsupported update_kind")

    card = context.connection.execute(
        "SELECT id, record_version FROM adrian_kanban_cards "
        "WHERE initiative_id = ? AND board_slug = ? "
        "AND card_type = 'initiative' AND task_id IS NULL AND closed_at IS NULL",
        (initiative_id, board),
    ).fetchone()
    if card is None:
        raise ValueError("open initiative card not found")
    card_id = card["id"]
    current_version = card["record_version"]
    expected_version = context.binding.expected_version
    if current_version != expected_version:
        raise ValueError("stale initiative version")

    if update_kind == "orchestration_checkpoint":
        admission = admit_orchestration_checkpoint(
            context.connection,
            initiative_card_id=card_id,
            initiative_id=initiative_id,
            actor_profile=context.binding.actor_profile,
            update=update,
        )
        _consume_approval(
            context.connection,
            context,
            approval_id,
            "kanban_update_initiative",
            expected_version,
            payload,
            initiative_id,
            None,
        )
        canonical_payload = json.dumps(
            admission.result, sort_keys=True, separators=(",", ":")
        )
        accepted_task_refs_json = json.dumps(
            admission.accepted_task_refs,
            sort_keys=True,
            separators=(",", ":"),
        )
        accepted_checkpoint_refs_json = json.dumps(
            admission.accepted_checkpoint_refs,
            sort_keys=True,
            separators=(",", ":"),
        )
        actor_evidence = json.dumps(
            {
                "session_id": context.binding.session_id,
                "actor_profile": context.binding.actor_profile,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        now = int(time.time())
        context.connection.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                admission.result_id,
                card_id,
                initiative_id,
                admission.phase,
                None,
                admission.iteration,
                "orchestration_checkpoint",
                admission.contract_id,
                admission.contract_version,
                canonical_payload,
                accepted_task_refs_json,
                accepted_checkpoint_refs_json,
                actor_evidence,
                context.idempotency_key,
                now,
            ),
        )
        record_version = _advance_initiative_version(
            context.connection, context, initiative_id, expected_version
        )
        return {
            "initiative_id": initiative_id,
            "update_kind": update_kind,
            "record_version": record_version,
            "result_id": admission.result_id,
            "step": admission.step,
        }

    if update_kind == "body_update":
        if set(update.keys()) != {"body"}:
            raise ValueError("body_update update must contain only body")
        new_body = update["body"]
        if not (type(new_body) is str and new_body.strip()):
            raise ValueError("body must be a nonblank string")
        _validate_initiative_body(new_body)
        _consume_approval(
            context.connection,
            context,
            approval_id,
            "kanban_update_initiative",
            expected_version,
            payload,
            initiative_id,
            None,
        )
        context.connection.execute(
            "UPDATE adrian_kanban_cards SET body = ? WHERE id = ?",
            (new_body, card_id),
        )
        record_version = _advance_initiative_version(
            context.connection, context, initiative_id, expected_version
        )
        return {
            "initiative_id": initiative_id,
            "update_kind": update_kind,
            "record_version": record_version,
        }

    if update_kind == "phase_result":
        required_keys = {
            "result_id",
            "phase",
            "segment_id",
            "iteration",
            "result_kind",
            "contract_id",
            "contract_version",
            "result",
            "accepted_task_refs",
            "accepted_checkpoint_refs",
        }
        if set(update.keys()) != required_keys:
            raise ValueError("phase_result update must contain exact fields")
        result_id = update.get("result_id")
        phase = update.get("phase")
        segment_id = update.get("segment_id")
        iteration = update.get("iteration")
        result_kind = update.get("result_kind")
        contract_id = update.get("contract_id")
        contract_version = update.get("contract_version")
        result = update.get("result")
        accepted_task_refs = update.get("accepted_task_refs")
        accepted_checkpoint_refs = update.get("accepted_checkpoint_refs")
        if not (type(result_id) is str and result_id.strip()):
            raise ValueError("result_id must be a nonblank string")
        if not (type(phase) is str and phase.strip()):
            raise ValueError("phase must be a nonblank string")
        if phase not in INITIATIVE_PHASES:
            raise ValueError("phase must be a recognized initiative phase")
        if segment_id is not None and not (
            type(segment_id) is str and segment_id.strip()
        ):
            raise ValueError("segment_id must be a nonblank string")
        if not (
            type(iteration) is int
            and not isinstance(iteration, bool)
            and iteration > 0
        ):
            raise ValueError("iteration must be a positive integer")
        if not (type(result_kind) is str and result_kind.strip()):
            raise ValueError("result_kind must be a nonblank string")
        if not (type(contract_id) is str and contract_id.strip()):
            raise ValueError("contract_id must be a nonblank string")
        if not (type(contract_version) is str and contract_version.strip()):
            raise ValueError("contract_version must be a nonblank string")
        if not isinstance(result, dict):
            raise ValueError("result must be an object")
        if not isinstance(accepted_task_refs, list):
            raise ValueError("accepted_task_refs must be a list")
        if not isinstance(accepted_checkpoint_refs, list):
            raise ValueError("accepted_checkpoint_refs must be a list")
        for ref in accepted_task_refs:
            if not (type(ref) is str and ref.strip()):
                raise ValueError("accepted_task_refs must contain nonblank strings")
        for ref in accepted_checkpoint_refs:
            if not (type(ref) is str and ref.strip()):
                raise ValueError(
                    "accepted_checkpoint_refs must contain nonblank strings"
                )
        if len(accepted_task_refs) != len(set(accepted_task_refs)):
            raise ValueError("accepted_task_refs must be unique")
        if len(accepted_checkpoint_refs) != len(set(accepted_checkpoint_refs)):
            raise ValueError("accepted_checkpoint_refs must be unique")
        if result_kind == "repository_reconciliation":
            _validate_reconciliation_result_candidate(
                context,
                card_id,
                initiative_id,
                board,
                phase,
                segment_id,
                result,
            )
        _consume_approval(
            context.connection,
            context,
            approval_id,
            "kanban_update_initiative",
            expected_version,
            payload,
            initiative_id,
            None,
        )
        canonical_payload = json.dumps(
            result, sort_keys=True, separators=(",", ":")
        )
        accepted_task_refs_json = json.dumps(
            accepted_task_refs, sort_keys=True, separators=(",", ":")
        )
        accepted_checkpoint_refs_json = json.dumps(
            accepted_checkpoint_refs, sort_keys=True, separators=(",", ":")
        )
        actor_evidence = json.dumps(
            {
                "session_id": context.binding.session_id,
                "actor_profile": context.binding.actor_profile,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        now = int(time.time())
        context.connection.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                result_id,
                card_id,
                initiative_id,
                phase,
                segment_id,
                iteration,
                result_kind,
                contract_id,
                contract_version,
                canonical_payload,
                accepted_task_refs_json,
                accepted_checkpoint_refs_json,
                actor_evidence,
                context.idempotency_key,
                now,
            ),
        )
        record_version = _advance_initiative_version(
            context.connection, context, initiative_id, expected_version
        )
        return {
            "initiative_id": initiative_id,
            "update_kind": update_kind,
            "record_version": record_version,
            "result_id": result_id,
        }


def _validate_reconciliation(
    conn: Any,
    card_id: int,
    initiative_id: str,
    board: str,
    reconciliation_ref: str,
    previous_transition_id: int,
    from_phase: str,
    from_segment_id: str | None,
    to_phase: str,
    to_segment_id: str | None,
) -> None:
    row = conn.execute(
        "SELECT initiative_card_id, initiative_id, phase, segment_id, accepted, "
        "result_kind, actor_evidence, canonical_payload "
        "FROM initiative_phase_results WHERE result_id = ?",
        (reconciliation_ref,),
    ).fetchone()
    if row is None:
        raise ValueError("reconciliation result not found")
    if row["initiative_card_id"] != card_id:
        raise ValueError("reconciliation card mismatch")
    if row["initiative_id"] != initiative_id:
        raise ValueError("reconciliation initiative mismatch")
    if row["accepted"] != 1:
        raise ValueError("reconciliation not accepted")
    if row["result_kind"] != "repository_reconciliation":
        raise ValueError("reconciliation kind mismatch")
    if row["phase"] != from_phase:
        raise ValueError("reconciliation phase mismatch")
    if row["segment_id"] != from_segment_id:
        raise ValueError("reconciliation segment mismatch")

    try:
        actor_evidence = json.loads(row["actor_evidence"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("invalid reconciliation actor evidence")
    if not isinstance(actor_evidence, dict):
        raise ValueError("reconciliation actor evidence must be an object")
    if actor_evidence.get("actor_profile") != "default":
        raise ValueError("reconciliation actor profile must be default")

    try:
        canonical_payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("invalid reconciliation payload")
    if not isinstance(canonical_payload, dict):
        raise ValueError("reconciliation payload must be an object")
    required_keys = {
        "initiative_id",
        "board",
        "previous_transition_id",
        "from_phase",
        "from_segment_id",
        "to_phase",
        "to_segment_id",
        "verification_result",
        "canon_route",
        "exit_gate_ref",
    }
    if not required_keys.issubset(canonical_payload):
        raise ValueError("reconciliation payload is incomplete")
    expected = {
        "initiative_id": initiative_id,
        "board": board,
        "previous_transition_id": previous_transition_id,
        "from_phase": from_phase,
        "from_segment_id": from_segment_id,
        "to_phase": to_phase,
        "to_segment_id": to_segment_id,
        "verification_result": "accepted",
    }
    for key, value in expected.items():
        if canonical_payload.get(key) != value:
            raise ValueError(f"reconciliation {key} mismatch")
    for key in ("canon_route", "exit_gate_ref"):
        value = canonical_payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"reconciliation {key} must be nonblank")


def _validate_reconciliation_result_candidate(
    context: Any,
    card_id: int,
    initiative_id: str,
    board: str,
    phase: str,
    segment_id: str | None,
    result: dict[str, Any],
) -> None:
    if context.binding.actor_profile != "default":
        raise ValueError("reconciliation actor profile must be default")
    expected_keys = {
        "initiative_id",
        "board",
        "previous_transition_id",
        "from_phase",
        "from_segment_id",
        "to_phase",
        "to_segment_id",
        "canon_route",
        "exit_gate_ref",
        "verification_result",
    }
    if set(result) != expected_keys:
        raise ValueError("invalid reconciliation result fields")
    latest = context.connection.execute(
        "SELECT transition_id, to_phase, to_segment_id "
        "FROM initiative_transitions WHERE initiative_id = ? "
        "ORDER BY transition_id DESC LIMIT 1",
        (initiative_id,),
    ).fetchone()
    if latest is None:
        raise ValueError("no initiative transition found")
    if result["initiative_id"] != initiative_id or result["board"] != board:
        raise ValueError("reconciliation target mismatch")
    if result["previous_transition_id"] != latest["transition_id"]:
        raise ValueError("reconciliation predecessor mismatch")
    if result["from_phase"] != latest["to_phase"] or result["from_phase"] != phase:
        raise ValueError("reconciliation source phase mismatch")
    if (
        result["from_segment_id"] != latest["to_segment_id"]
        or result["from_segment_id"] != segment_id
    ):
        raise ValueError("reconciliation source segment mismatch")
    if result["to_phase"] not in INITIATIVE_PHASES:
        raise ValueError("invalid reconciliation target phase")
    target_segment = result["to_segment_id"]
    if result["to_phase"] in {"DEV2", "DEV3", "DEV4"}:
        if not isinstance(target_segment, str) or not target_segment.strip():
            raise ValueError("reconciliation target segment must be nonblank")
    elif target_segment is not None:
        raise ValueError("reconciliation target segment must be absent")
    for key in ("canon_route", "exit_gate_ref"):
        value = result[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"reconciliation {key} must be nonblank")
    if result["verification_result"] != "accepted":
        raise ValueError("reconciliation verification was not accepted")


def _validate_segment_projection(
    conn: Any,
    card_id: int,
    to_segment_id: str,
) -> None:
    row = conn.execute(
        "SELECT parsed_segment_definitions, readiness_refs, validation_result "
        "FROM initiative_segment_projections WHERE initiative_card_id = ? "
        "ORDER BY projection_version DESC LIMIT 1",
        (card_id,),
    ).fetchone()
    if row is None:
        raise ValueError("no segment projection found")
    if row["validation_result"] != "accepted":
        raise ValueError("segment projection not accepted")
    try:
        parsed_definitions = json.loads(row["parsed_segment_definitions"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("invalid segment definitions")
    if not isinstance(parsed_definitions, list) or not parsed_definitions:
        raise ValueError("segment definitions must be a nonempty list")
    segment_ids: set[str] = set()
    for definition in parsed_definitions:
        if not isinstance(definition, dict):
            raise ValueError("segment definition must be an object")
        segment_id = definition.get("segment_id")
        if not isinstance(segment_id, str) or not segment_id.strip():
            raise ValueError("segment_id must be a nonblank string")
        if segment_id in segment_ids:
            raise ValueError("duplicate segment_id")
        segment_ids.add(segment_id)
    if to_segment_id not in segment_ids:
        raise ValueError("target segment not found")

    try:
        readiness_refs = json.loads(row["readiness_refs"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("invalid readiness references")
    if isinstance(readiness_refs, dict):
        readiness = readiness_refs.get(to_segment_id)
        if not isinstance(readiness, str) or not readiness.strip():
            raise ValueError("target segment has no readiness reference")
    elif isinstance(readiness_refs, list):
        if not any(
            isinstance(item, str)
            and item.strip()
            and item.rsplit(":", 1)[-1] == to_segment_id
            for item in readiness_refs
        ):
            raise ValueError("target segment has no readiness reference")
    else:
        raise ValueError("readiness references have an unsupported shape")


def _handle_transition_initiative(context: Any) -> dict[str, Any]:
    payload = context.payload
    unknown = set(payload.keys()) - {
        "initiative_id",
        "to_phase",
        "to_segment_id",
        "reconciliation_ref",
        "approval_id",
        "board",
    }
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    initiative_id = payload.get("initiative_id")
    to_phase = payload.get("to_phase")
    to_segment_id = payload.get("to_segment_id")
    reconciliation_ref = payload.get("reconciliation_ref")
    approval_id = payload.get("approval_id")
    board = payload.get("board")
    if not (type(initiative_id) is str and initiative_id.strip()):
        raise ValueError("initiative_id must be a nonblank string")
    if not (type(to_phase) is str and to_phase.strip()):
        raise ValueError("to_phase must be a nonblank string")
    if to_segment_id is not None and not (
        type(to_segment_id) is str and to_segment_id.strip()
    ):
        raise ValueError("to_segment_id must be a nonblank string")
    if not (type(reconciliation_ref) is str and reconciliation_ref.strip()):
        raise ValueError("reconciliation_ref must be a nonblank string")
    if not (type(approval_id) is str and approval_id.strip()):
        raise ValueError("approval_id must be a nonblank string")
    if not (type(board) is str and board.strip()):
        raise ValueError("board must be a nonblank string")
    initiative_id = initiative_id.strip()
    to_phase = to_phase.strip()
    to_segment_id = to_segment_id.strip() if to_segment_id is not None else None
    reconciliation_ref = reconciliation_ref.strip()
    board = board.strip()
    if to_phase not in INITIATIVE_PHASES:
        raise ValueError("to_phase must be a recognized initiative phase")
    if to_phase in {"DEV2", "DEV3", "DEV4"} and to_segment_id is None:
        raise ValueError("to_segment_id is required for DEV2, DEV3, DEV4")
    if to_phase not in {"DEV2", "DEV3", "DEV4"} and to_segment_id is not None:
        raise ValueError("to_segment_id must be absent for non-DEV2/3/4 phases")

    card = context.connection.execute(
        "SELECT id, record_version FROM adrian_kanban_cards "
        "WHERE initiative_id = ? AND board_slug = ? "
        "AND card_type = 'initiative' AND task_id IS NULL AND closed_at IS NULL",
        (initiative_id, board),
    ).fetchone()
    if card is None:
        raise ValueError("open initiative card not found")
    card_id = card["id"]
    current_version = card["record_version"]
    expected_version = context.binding.expected_version
    if current_version != expected_version:
        raise ValueError("stale initiative version")

    latest = context.connection.execute(
        "SELECT transition_id, from_phase, from_segment_id, to_phase, to_segment_id "
        "FROM initiative_transitions WHERE initiative_id = ? "
        "ORDER BY transition_id DESC LIMIT 1",
        (initiative_id,),
    ).fetchone()
    if latest is None:
        raise ValueError("no initiative transition found")
    predecessor_id = latest["transition_id"]
    if not (type(predecessor_id) is int and predecessor_id > 0):
        raise ValueError("latest transition ID must be a positive integer")
    from_phase = latest["to_phase"]
    from_segment_id = latest["to_segment_id"]
    _validate_reconciliation(
        context.connection,
        card_id,
        initiative_id,
        board,
        reconciliation_ref,
        predecessor_id,
        from_phase,
        from_segment_id,
        to_phase,
        to_segment_id,
    )
    if to_phase in {"DEV2", "DEV3", "DEV4"}:
        _validate_segment_projection(
            context.connection,
            card_id,
            to_segment_id,
        )
    _consume_approval(
        context.connection,
        context,
        approval_id,
        "kanban_transition_initiative",
        expected_version,
        payload,
        initiative_id,
        None,
    )
    now = int(time.time())
    actor_evidence = json.dumps(
        {
            "session_id": context.binding.session_id,
            "actor_profile": context.binding.actor_profile,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    canonical_payload = json.dumps(
        {
            "from_phase": from_phase,
            "from_segment_id": from_segment_id,
            "to_phase": to_phase,
            "to_segment_id": to_segment_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    context.connection.execute(
        "INSERT INTO initiative_transitions "
        "(initiative_card_id, initiative_id, previous_transition_id, "
        "transition_id, from_phase, from_segment_id, to_phase, to_segment_id, "
        "trigger, actor_evidence, canonical_payload, repository_reconciliation_ref, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            card_id,
            initiative_id,
            predecessor_id,
            predecessor_id + 1,
            from_phase,
            from_segment_id,
            to_phase,
            to_segment_id,
            "reconciliation",
            actor_evidence,
            canonical_payload,
            reconciliation_ref,
            now,
        ),
    )
    record_version = _advance_initiative_version(
        context.connection, context, initiative_id, expected_version
    )
    result = {
        "initiative_id": initiative_id,
        "from_phase": from_phase,
        "to_phase": to_phase,
        "transition_id": predecessor_id + 1,
        "record_version": record_version,
    }
    if from_segment_id is not None or to_segment_id is not None:
        result["from_segment_id"] = from_segment_id
        result["to_segment_id"] = to_segment_id
    return result


def _handle_close_initiative(context: Any) -> dict[str, Any]:
    payload = context.payload
    unknown = set(payload.keys()) - {
        "initiative_id",
        "closure_result_ref",
        "approval_id",
        "board",
    }
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    initiative_id = payload.get("initiative_id")
    closure_result_ref = payload.get("closure_result_ref")
    approval_id = payload.get("approval_id")
    board = payload.get("board")
    if not (type(initiative_id) is str and initiative_id.strip()):
        raise ValueError("initiative_id must be a nonblank string")
    if not (type(closure_result_ref) is str and closure_result_ref.strip()):
        raise ValueError("closure_result_ref must be a nonblank string")
    if not (type(approval_id) is str and approval_id.strip()):
        raise ValueError("approval_id must be a nonblank string")
    if not (type(board) is str and board.strip()):
        raise ValueError("board must be a nonblank string")
    initiative_id = initiative_id.strip()
    closure_result_ref = closure_result_ref.strip()
    board = board.strip()

    if context.binding.actor_profile != "default":
        raise ValueError("closure actor profile must be default")

    card = context.connection.execute(
        "SELECT id, record_version FROM adrian_kanban_cards "
        "WHERE initiative_id = ? AND board_slug = ? "
        "AND card_type = 'initiative' AND task_id IS NULL AND closed_at IS NULL",
        (initiative_id, board),
    ).fetchone()
    if card is None:
        raise ValueError("open initiative card not found")
    card_id = card["id"]
    current_version = card["record_version"]
    expected_version = context.binding.expected_version
    if current_version != expected_version:
        raise ValueError("stale initiative version")

    latest = context.connection.execute(
        "SELECT transition_id, to_phase, to_segment_id "
        "FROM initiative_transitions WHERE initiative_id = ? "
        "ORDER BY transition_id DESC LIMIT 1",
        (initiative_id,),
    ).fetchone()
    if latest is None:
        raise ValueError("no initiative transition found")
    current_phase = latest["to_phase"]
    current_segment_id = latest["to_segment_id"]
    if current_phase not in {"DEV4", "PC1"}:
        raise ValueError("closure requires DEV4 or PC1")
    if current_phase == "PC1" and current_segment_id is not None:
        raise ValueError(
            "PC1 closure must be milestone-scoped (segment_id must be NULL)"
        )

    closure_row = context.connection.execute(
        "SELECT initiative_card_id, initiative_id, phase, segment_id, accepted, "
        "result_kind, contract_id, contract_version, actor_evidence, canonical_payload "
        "FROM initiative_phase_results WHERE result_id = ?",
        (closure_result_ref,),
    ).fetchone()
    if closure_row is None:
        raise ValueError("closure result not found")
    if closure_row["initiative_card_id"] != card_id:
        raise ValueError("closure card mismatch")
    if closure_row["initiative_id"] != initiative_id:
        raise ValueError("closure initiative mismatch")
    if closure_row["accepted"] != 1:
        raise ValueError("closure result not accepted")
    if closure_row["result_kind"] != "initiative_closure":
        raise ValueError("closure kind mismatch")
    if closure_row["phase"] != current_phase:
        raise ValueError("closure phase mismatch")
    if closure_row["segment_id"] != current_segment_id:
        raise ValueError("closure segment mismatch")
    expected_contract = f"adrian-kanban.lifecycle.{current_phase.lower()}"
    if closure_row["contract_id"] != expected_contract:
        raise ValueError("closure contract mismatch")
    if closure_row["contract_version"] != "1":
        raise ValueError("closure contract version mismatch")

    try:
        closure_actor = json.loads(closure_row["actor_evidence"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("invalid closure actor evidence")
    if not isinstance(closure_actor, dict):
        raise ValueError("closure actor evidence must be an object")
    if closure_actor.get("actor_profile") != "default":
        raise ValueError("closure actor profile must be default")

    try:
        closure_payload = json.loads(closure_row["canonical_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("invalid closure payload")
    if not isinstance(closure_payload, dict):
        raise ValueError("closure payload must be an object")
    required_keys = {
        "final_summary_ref",
        "user_approval_ref",
        "repository_reconciliation_ref",
        "resolved_phase_result_refs",
        "cancelled_task_refs",
        "closure_conclusion",
    }
    if set(closure_payload.keys()) != required_keys:
        raise ValueError("closure payload must contain exact fields")
    for key in ("final_summary_ref", "user_approval_ref"):
        value = closure_payload.get(key)
        if not (type(value) is str and value.strip()):
            raise ValueError(f"closure {key} must be nonblank")
    if closure_payload.get("closure_conclusion") != "approved":
        raise ValueError("closure conclusion must be approved")
    resolved_refs = closure_payload.get("resolved_phase_result_refs")
    cancelled_refs = closure_payload.get("cancelled_task_refs")
    if not isinstance(resolved_refs, list):
        raise ValueError("resolved_phase_result_refs must be a list")
    if not isinstance(cancelled_refs, list):
        raise ValueError("cancelled_task_refs must be a list")
    for ref in resolved_refs:
        if not (type(ref) is str and ref.strip()):
            raise ValueError("resolved_phase_result_refs must contain nonblank strings")
    for ref in cancelled_refs:
        if not (type(ref) is str and ref.strip()):
            raise ValueError("cancelled_task_refs must contain nonblank strings")
    if len(resolved_refs) != len(set(resolved_refs)):
        raise ValueError("resolved_phase_result_refs must be unique")
    if len(cancelled_refs) != len(set(cancelled_refs)):
        raise ValueError("cancelled_task_refs must be unique")

    reconciliation_ref = closure_payload["repository_reconciliation_ref"]
    if not (type(reconciliation_ref) is str and reconciliation_ref.strip()):
        raise ValueError("repository_reconciliation_ref must be nonblank")
    _validate_reconciliation(
        context.connection,
        card_id,
        initiative_id,
        board,
        reconciliation_ref,
        latest["transition_id"],
        current_phase,
        current_segment_id,
        "closed",
        None,
    )

    projection_row = context.connection.execute(
        "SELECT parsed_segment_definitions, validation_result "
        "FROM initiative_segment_projections WHERE initiative_card_id = ? "
        "ORDER BY projection_version DESC LIMIT 1",
        (card_id,),
    ).fetchone()
    if projection_row is None:
        raise ValueError("no segment projection found")
    if projection_row["validation_result"] != "accepted":
        raise ValueError("segment projection not accepted")
    try:
        parsed_definitions = json.loads(projection_row["parsed_segment_definitions"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("invalid segment definitions")
    if not isinstance(parsed_definitions, list) or not parsed_definitions:
        raise ValueError("segment definitions must be a nonempty list")
    segment_ids: set[str] = set()
    ordinals: set[int] = set()
    max_ordinal = 0
    max_segment_id: str | None = None
    for definition in parsed_definitions:
        if not isinstance(definition, dict):
            raise ValueError("segment definition must be an object")
        segment_id = definition.get("segment_id")
        if not isinstance(segment_id, str) or not segment_id.strip():
            raise ValueError("segment_id must be a nonblank string")
        if segment_id in segment_ids:
            raise ValueError("duplicate segment_id")
        segment_ids.add(segment_id)
        ordinal = definition.get("ordinal")
        if not (type(ordinal) is int and not isinstance(ordinal, bool) and ordinal > 0):
            raise ValueError("segment ordinal must be a positive integer")
        if ordinal in ordinals:
            raise ValueError("duplicate segment ordinal")
        ordinals.add(ordinal)
        if ordinal > max_ordinal:
            max_ordinal = ordinal
            max_segment_id = segment_id

    if current_phase == "DEV4":
        if current_segment_id is None:
            raise ValueError("DEV4 closure requires a segment")
        if current_segment_id != max_segment_id:
            raise ValueError("DEV4 closure requires the final registered segment")

    required_coverage: set[tuple[str, str | None]] = set()
    for phase in ("D1", "D2", "D3", "D4", "DEV1"):
        required_coverage.add((phase, None))
    for segment_id in segment_ids:
        for phase in ("DEV2", "DEV3", "DEV4"):
            required_coverage.add((phase, segment_id))

    covered: set[tuple[str, str | None]] = set()
    for ref in resolved_refs:
        row = context.connection.execute(
            "SELECT initiative_card_id, initiative_id, phase, segment_id, result_kind, contract_id, contract_version, accepted "
            "FROM initiative_phase_results WHERE result_id = ?",
            (ref,),
        ).fetchone()
        if row is None:
            raise ValueError("resolved result not found")
        if row["initiative_card_id"] != card_id:
            raise ValueError("resolved result card mismatch")
        if row["initiative_id"] != initiative_id:
            raise ValueError("resolved result initiative mismatch")
        if row["accepted"] != 1:
            raise ValueError("resolved result not accepted")
        if row["result_kind"] in {"repository_reconciliation", "initiative_closure"}:
            continue
        phase = row["phase"]
        segment_id = row["segment_id"]
        expected_contract = f"adrian-kanban.lifecycle.{phase.lower()}"
        if row["contract_id"] != expected_contract:
            raise ValueError("resolved result contract mismatch")
        if row["contract_version"] != "1":
            raise ValueError("resolved result contract version mismatch")
        covered.add((phase, segment_id))
    if not required_coverage.issubset(covered):
        raise ValueError("incomplete phase coverage")

    tasks = context.connection.execute(
        "SELECT c.task_id, c.id AS task_card_id, t.status FROM adrian_kanban_cards c "
        "JOIN tasks t ON c.task_id = t.id "
        "WHERE c.initiative_id = ? AND c.card_type = 'task'",
        (initiative_id,),
    ).fetchall()
    archived_task_ids: set[str] = set()
    for task in tasks:
        task_id = task["task_id"]
        status = task["status"]
        if status not in {"done", "archived"}:
            raise ValueError("task not resolved")
        if status == "archived":
            archived_task_ids.add(task_id)
        elif status == "done":
            task_card_id = task["task_card_id"]
            contract_row = context.connection.execute(
                "SELECT 1 FROM task_lifecycle_contracts WHERE task_card_id = ?",
                (task_card_id,),
            ).fetchone()
            if contract_row is not None:
                verdict_row = context.connection.execute(
                    "SELECT 1 FROM task_reviewer_verdicts WHERE task_card_id = ? AND verdict = 'accepted'",
                    (task_card_id,),
                ).fetchone()
                if verdict_row is None:
                    raise ValueError("lifecycle task done without accepted verdict")
    cancelled_set = set(cancelled_refs)
    if not archived_task_ids.issubset(cancelled_set):
        raise ValueError("archived task not cancelled")
    if not cancelled_set.issubset(archived_task_ids):
        raise ValueError("cancelled ref not archived")

    workspaces = context.connection.execute(
        "SELECT workspace_id, active, lifecycle_state FROM segment_workspaces "
        "WHERE initiative_card_id = ?",
        (card_id,),
    ).fetchall()
    for ws in workspaces:
        if ws["active"] != 0:
            raise ValueError("workspace active")
        if ws["lifecycle_state"] != "retired":
            raise ValueError("workspace not retired")
        members = context.connection.execute(
            "SELECT member_state FROM segment_workspace_members WHERE workspace_id = ?",
            (ws["workspace_id"],),
        ).fetchall()
        for member in members:
            if member["member_state"] not in {"merged", "retired"}:
                raise ValueError("workspace member not resolved")

    _consume_approval(
        context.connection,
        context,
        approval_id,
        "kanban_close_initiative",
        expected_version,
        payload,
        initiative_id,
        None,
    )
    now = int(time.time())
    cursor = context.connection.execute(
        "UPDATE adrian_kanban_cards SET closed_at = ?, record_version = record_version + 1 "
        "WHERE id = ? AND record_version = ? AND closed_at IS NULL",
        (now, card_id, expected_version),
    )
    if cursor.rowcount != 1:
        raise ValueError("stale initiative version")
    return {
        "initiative_id": initiative_id,
        "closure_result_ref": closure_result_ref,
        "closed_at": now,
        "record_version": expected_version + 1,
    }
