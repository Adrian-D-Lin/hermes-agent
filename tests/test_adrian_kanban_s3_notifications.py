"""Transactional plugin notification projection derived from committed commands."""

from __future__ import annotations

import importlib
import json
import sqlite3

from tests.test_adrian_kanban_s3_read_attachments import (
    _database,
    _seed_cards,
    commands_module,  # noqa: F401
)


def _boundary(commands_module, database_path, provider, handler):
    return commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_block": handler},
    )


def _submit(boundary, *, attempt_id="notify-attempt", key="notify-key"):
    return boundary.submit(
        "kanban_block",
        attempt_id=attempt_id,
        idempotency_key=key,
        target="task-read",
        expected_version=0,
        session_id="notify-session",
        workspace_id=None,
        execution_context="test",
        payload={"task_id": "task-read", "board": "orchestrator"},
    )


def test_accepted_mutation_captures_commit_time_record_and_one_outbox_event(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_cards(database_path)

    def handler(context):
        context.connection.execute(
            "UPDATE adrian_kanban_cards SET record_version = record_version + 1 "
            "WHERE task_id = 'task-read'"
        )
        return {"task_id": "task-read", "blocked": True}

    boundary = _boundary(commands_module, database_path, provider, handler)
    first = _submit(boundary)
    replay = _submit(boundary, attempt_id="notify-replay")

    assert replay == first
    assert first == {
        "result": "ACCEPTED",
        "state_changed": True,
        "attempt_id": "notify-attempt",
        "operation": "kanban_block",
        "value": {"task_id": "task-read", "blocked": True},
    }
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT committed_records_json "
            "FROM adrian_kanban_notification_outbox"
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == [
            {
                "card_type": "task",
                "initiative_id": "initiative-read",
                "task_id": "task-read",
                "record_version": 1,
            }
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_notification_outbox"
        ).fetchone()[0] == 1


def test_failed_mutation_creates_neither_receipt_nor_notification(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_cards(database_path)

    def handler(_context):
        raise ValueError("no change")

    result = _submit(_boundary(commands_module, database_path, provider, handler))

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_notification_outbox"
        ).fetchone()[0] == 0


def test_notification_projection_is_monotonic_and_uses_immutable_receipt_state(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_cards(database_path)

    def handler(context):
        context.connection.execute(
            "UPDATE adrian_kanban_cards SET record_version = record_version + 1 "
            "WHERE task_id = 'task-read'"
        )
        return {"task_id": "task-read", "blocked": True}

    first = _submit(_boundary(commands_module, database_path, provider, handler))
    assert first["result"] == "ACCEPTED"
    with sqlite3.connect(database_path, isolation_level=None) as conn:
        conn.execute(
            "UPDATE adrian_kanban_cards SET record_version = 9 "
            "WHERE task_id = 'task-read'"
        )

    notifications = importlib.import_module(
        f"{commands_module.__package__}.notifications"
    )
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        page = notifications.list_notifications(conn, after_event_id=0, limit=10)

    assert page["cursor"] == 1
    assert page["has_more"] is False
    assert page["events"] == [
        {
            "event_id": 1,
            "receipt_id": "notify-key",
            "operation": "kanban_block",
            "target": "task-read",
            "committed_at": page["events"][0]["committed_at"],
            "committed_records": [
                {
                    "card_type": "task",
                    "initiative_id": "initiative-read",
                    "task_id": "task-read",
                    "record_version": 1,
                }
            ],
        }
    ]
    assert type(page["events"][0]["committed_at"]) is int

    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        empty = notifications.list_notifications(
            conn, after_event_id=page["cursor"], limit=10
        )
    assert empty == {"events": [], "cursor": 1, "has_more": False}
