"""Transactional notification projection for Adrian Kanban.

The notification outbox is a read-only projection pointer created in the same
SQLite transaction as an accepted command receipt. This module exposes the
single read path over that outbox: it parses only internally generated
accepted receipt envelopes and never looks up the current card version.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_OUTBOX_TABLE = "adrian_kanban_notification_outbox"
_RECEIPT_TABLE = "adrian_kanban_command_receipts"
_MAX_LIMIT = 500


def list_notifications(
    conn: sqlite3.Connection,
    after_event_id: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Return the next page of committed notification events.

    Events are the outbox rows joined to their receipts in increasing
    ``event_id`` order. Each event carries exactly ``event_id``,
    ``receipt_id``, ``operation``, ``target``, ``committed_at``, and
    ``committed_records``. The cursor is the last returned event ID, or the
    supplied cursor when no events are returned. A malformed internally
    generated row fails closed instead of being silently skipped.
    """
    if type(after_event_id) is not int or isinstance(after_event_id, bool) or after_event_id < 0:
        raise ValueError("after_event_id must be a non-negative integer")
    if type(limit) is not int or isinstance(limit, bool) or limit < 1 or limit > _MAX_LIMIT:
        raise ValueError("limit must be a positive integer no greater than 500")

    rows = conn.execute(
        f"SELECT o.event_id, r.idempotency_key, r.operation, r.target, "
        f"r.created_at, r.response_json, o.committed_records_json "
        f"FROM {_OUTBOX_TABLE} o "
        f"JOIN {_RECEIPT_TABLE} r ON r.idempotency_key = o.idempotency_key "
        f"WHERE o.event_id > ? "
        f"ORDER BY o.event_id ASC LIMIT ?",
        (after_event_id, limit + 1),
    ).fetchall()

    has_more = len(rows) > limit
    events: list[dict[str, Any]] = []
    for row in rows[:limit]:
        events.append(_parse_event(row))

    cursor = events[-1]["event_id"] if events else after_event_id
    return {"events": events, "cursor": cursor, "has_more": has_more}


def _parse_event(row: sqlite3.Row) -> dict[str, Any]:
    event_id = row["event_id"]
    receipt_id = row["idempotency_key"]
    operation = row["operation"]
    target = row["target"]
    committed_at = row["created_at"]
    for field_name, value in (
        ("event_id", event_id),
        ("receipt_id", receipt_id),
        ("operation", operation),
        ("target", target),
        ("committed_at", committed_at),
    ):
        if field_name == "event_id":
            if type(value) is not int or isinstance(value, bool) or value <= 0:
                raise ValueError(f"malformed notification outbox row: {field_name}")
        elif field_name == "committed_at":
            if type(value) is not int or isinstance(value, bool):
                raise ValueError(f"malformed notification outbox row: {field_name}")
        else:
            if not (type(value) is str and value.strip()):
                raise ValueError(f"malformed notification outbox row: {field_name}")

    try:
        envelope = json.loads(row["response_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("malformed notification outbox row: receipt JSON") from exc
    if type(envelope) is not dict or envelope.get("result") != "ACCEPTED":
        raise ValueError("malformed notification outbox row: receipt envelope")
    try:
        committed_records = json.loads(row["committed_records_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("malformed notification outbox row: committed_records") from exc
    if type(committed_records) is not list:
        raise ValueError("malformed notification outbox row: committed_records")
    for record in committed_records:
        if type(record) is not dict:
            raise ValueError("malformed notification outbox row: committed record")
        card_type = record.get("card_type")
        initiative_id = record.get("initiative_id")
        task_id = record.get("task_id")
        record_version = record.get("record_version")
        if card_type not in ("initiative", "task"):
            raise ValueError("malformed notification outbox row: card_type")
        if not (type(initiative_id) is str and initiative_id.strip()):
            raise ValueError("malformed notification outbox row: initiative_id")
        if card_type == "initiative" and task_id is not None:
            raise ValueError("malformed notification outbox row: task_id")
        if card_type == "task" and not (type(task_id) is str and task_id.strip()):
            raise ValueError("malformed notification outbox row: task_id")
        if (
            type(record_version) is not int
            or isinstance(record_version, bool)
            or record_version < 0
        ):
            raise ValueError("malformed notification outbox row: record_version")

    return {
        "event_id": event_id,
        "receipt_id": receipt_id,
        "operation": operation,
        "target": target,
        "committed_at": committed_at,
        "committed_records": committed_records,
    }
