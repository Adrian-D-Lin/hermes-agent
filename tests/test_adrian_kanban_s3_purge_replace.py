"""Controlled purge-and-replace tests derived from Kanban design v0.28 section 8.4."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from writegate.kanban_approvals import create_kanban_approval_schema


@pytest.fixture(scope="module")
def plugin_modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_purge_replace"
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
        name: importlib.import_module(f"{package_name}.{name}")
        for name in (
            "commands",
            "provider",
            "schema",
            "contracts",
            "lifecycle",
            "skill_bundle",
            "task_inputs",
            "projections",
            "purge_cleanup",
        )
    }
    yield modules
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


def _database(tmp_path, monkeypatch, modules):
    database_path = (tmp_path / "kanban.sqlite3").resolve()
    with kb.connect_closing(database_path) as conn:
        modules["schema"].create_schema(conn)
        create_kanban_approval_schema(conn)
        conn.commit()
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        f"  database_path: {database_path.as_posix()}\n",
        encoding="utf-8",
    )
    provider = modules["provider"].AdrianKanbanAuthorityProvider(str(database_path))
    modules["provider"].register_provider(provider)
    return database_path, provider


def _seed_native_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str = "ready",
    assignee: str = "independent-reviewer",
    body: str = "body",
    current_run_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tasks "
        "(id, title, body, assignee, status, priority, created_by, created_at, "
        "workspace_kind, goal_mode, current_run_id) VALUES "
        "(?, ?, ?, ?, ?, 0, 'seed', 1, 'scratch', 1, ?)",
        (task_id, task_id, body, assignee, status, current_run_id),
    )


def _seed_card(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    initiative_id: str = "initiative-1",
    board: str = "orchestrator",
    version: int = 0,
) -> int:
    return int(
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, "
            "board_slug, record_version) VALUES "
            "('task', ?, ?, ?, 'body', 1, ?, ?)",
            (initiative_id, task_id, task_id, board, version),
        ).lastrowid
    )


def _seed_creation_noncompliant_graph(database_path: Path) -> int:
    with sqlite3.connect(database_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')")
        initiative_card_id = int(
            conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, body, created_at, "
                "board_slug, record_version) VALUES "
                "('initiative', 'initiative-1', NULL, 'Initiative', 'body', 1, "
                "'orchestrator', 0)"
            ).lastrowid
        )
        for task_id in ("parent-a", "predecessor", "child-a"):
            _seed_native_task(
                conn,
                task_id,
                status="todo" if task_id == "child-a" else "ready",
            )
            _seed_card(conn, task_id)
        predecessor_card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE task_id = 'predecessor'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO task_handoff_requirements "
            "(task_card_id, task_id, version, execution_profile, reviewer, "
            "canonical_payload, created_at) VALUES "
            "(?, 'predecessor', 1, 'independent-reviewer', 'default', "
            "'{\"malformed\":true}', 1)",
            (predecessor_card_id,),
        )
        conn.execute("INSERT INTO task_links VALUES ('parent-a', 'predecessor')")
        conn.execute("INSERT INTO task_links VALUES ('predecessor', 'child-a')")
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES ('predecessor', 'seed', 'historical', 2)"
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES ('predecessor', 'blocked', '{}', 2)"
        )
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at, outcome) VALUES "
            "('predecessor', 'independent-reviewer', 'failed', 1, 2, 'gave_up')"
        )
        conn.execute(
            "INSERT INTO kanban_notify_subs "
            "(task_id, platform, chat_id, thread_id, created_at) VALUES "
            "('predecessor', 'desktop', 'chat-1', '', 2)"
        )
        conn.commit()
        return initiative_card_id


def _successor_payload() -> dict:
    return {
        "task_id": "successor",
        "initiative_id": "initiative-1",
        "title": "Corrected successor",
        "assignee": "independent-reviewer",
        "body": "Corrected work",
        "parents": ["parent-a"],
        "goal_mode": True,
        "handoff_requirements_v1": {
            "version": 1,
            "reviewer": "default",
            "fields": {"evidence": {"type": "text"}},
        },
        "board": "orchestrator",
    }


def _update() -> dict:
    return {
        "replacement_id": "purge-replacement:1",
        "predecessor_task_id": "predecessor",
        "predecessor_record_version": 0,
        "eligibility_classification": "creation_non_compliance",
        "eligibility_evidence_ref": "evidence:invalid-creation:1",
        "repository_worktree_disposition": {
            "reference": "disposition:retain:1",
            "action": "retain",
            "preservation_evidence_ref": "evidence:preserved:1",
        },
        "expected_relations": {
            "prerequisite_task_ids": ["parent-a"],
            "dependent_task_ids": ["child-a"],
        },
        "successor_payload": _successor_payload(),
    }


def _payload(update: dict | None = None) -> dict:
    return {
        "initiative_id": "initiative-1",
        "update_kind": "purge_replace_task",
        "update": update or _update(),
        "approval_id": "approval:purge:1",
        "board": "orchestrator",
    }


def _approval_digest(payload: dict) -> str:
    approved = {key: value for key, value in payload.items() if key != "approval_id"}
    return hashlib.sha256(
        json.dumps(approved, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _approve(database_path: Path, payload: dict, *, version: int = 0) -> None:
    now = int(time.time())
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO write_gate_kanban_approvals ("
            "approval_id, approval_type, state, request_id, operation, "
            "initiative_id, proposed_creation_id, expected_version, "
            "canonical_digest, canonicalization_version, authorizer_evidence, "
            "session_id, prepared_at, approved_at, expires_at, approval_evidence"
            ") VALUES (?, 'kanban_initiative_mutation', 'approved', ?, "
            "'kanban_update_initiative', 'initiative-1', NULL, ?, ?, 1, ?, "
            "'session-purge', ?, ?, ?, 'approved-on-second-action')",
            (
                payload["approval_id"],
                f"attempt:{payload['approval_id']}",
                version,
                _approval_digest(payload),
                '{"peer_identity":"adrian@tailnet"}',
                now - 2,
                now - 1,
                now + 600,
            ),
        )
        conn.commit()


def _boundary(modules, database_path, provider, preparer=None):
    commands = modules["commands"]
    return commands._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_update_initiative": commands._handle_update_initiative,
            "kanban_create": commands._handle_create,
        },
        known_profiles={"default", "independent-reviewer"},
        task_input_preparer=preparer,
    )


def _submit(
    boundary, payload: dict, *, profile: str = "default", version: int = 0
) -> dict:
    return boundary.submit(
        "kanban_update_initiative",
        attempt_id=f"attempt:{payload['approval_id']}",
        idempotency_key=f"key:{payload['approval_id']}",
        target="initiative-1",
        expected_version=version,
        session_id="session-purge",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile=profile,
        payload=payload,
    )


def test_controlled_purge_replaces_atomically_and_keeps_dependents_gated(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    initiative_card_id = _seed_creation_noncompliant_graph(database_path)
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {
        "initiative_id": "initiative-1",
        "update_kind": "purge_replace_task",
        "record_version": 1,
        "replacement_id": "purge-replacement:1",
        "predecessor_task_id": "predecessor",
        "successor_task_id": "successor",
        "cleanup_required": False,
    }
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'predecessor'"
            ).fetchone()[0]
            == 0
        )
        successor = conn.execute(
            "SELECT status, current_run_id, claim_lock, worker_pid, "
            "consecutive_failures FROM tasks WHERE id = 'successor'"
        ).fetchone()
        assert tuple(successor) == ("todo", None, None, None, 0)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_cards WHERE task_id = 'predecessor'"
            ).fetchone()[0]
            == 0
        )
        successor_card = conn.execute(
            "SELECT initiative_id, record_version FROM adrian_kanban_cards "
            "WHERE task_id = 'successor'"
        ).fetchone()
        assert tuple(successor_card) == ("initiative-1", 0)
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
            ).fetchall()
        ] == [
            ("parent-a", "successor"),
            ("successor", "child-a"),
        ]
        assert (
            conn.execute("SELECT status FROM tasks WHERE id = 'child-a'").fetchone()[0]
            == "todo"
        )
        for table in (
            "task_comments",
            "task_events",
            "task_runs",
            "task_handoff_requirements",
        ):
            key = "task_id"
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {key} = 'predecessor'"
                ).fetchone()[0]
                == 0
            )
        lineage = conn.execute(
            "SELECT * FROM task_purge_replacements WHERE replacement_id = ?",
            ("purge-replacement:1",),
        ).fetchone()
        assert lineage["initiative_card_id"] == initiative_card_id
        assert lineage["predecessor_task_id"] == "predecessor"
        assert lineage["successor_task_id"] == "successor"
        assert lineage["eligibility_classification"] == "creation_non_compliance"
        assert lineage["authorization_approval_id"] == "approval:purge:1"
        assert json.loads(lineage["transferred_relations"]) == {
            "dependent_task_ids": ["child-a"],
            "prerequisite_task_ids": ["parent-a"],
        }
        assert json.loads(lineage["requester_evidence"]) == {
            "actor_profile": "default",
            "session_id": "session-purge",
        }
        assert lineage["cleanup_required"] == 0
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "consumed"
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE id = ?",
                (initiative_card_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = 'successor'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda update: update.update(eligibility_classification="worker_crash"),
        lambda update: update["expected_relations"].update(dependent_task_ids=[]),
        lambda update: update["successor_payload"].update(task_id="predecessor"),
        lambda update: update["successor_payload"].update(goal_mode=False),
        lambda update: update["repository_worktree_disposition"].update(
            action="delete_now"
        ),
    ],
)
def test_invalid_replacement_rolls_back_everything(
    plugin_modules, tmp_path, monkeypatch, mutate
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    update = _update()
    mutate(update)
    payload = _payload(update)
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'predecessor'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'successor'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        ).fetchall() == [
            ("parent-a", "predecessor"),
            ("predecessor", "child-a"),
        ]
        assert (
            conn.execute("SELECT COUNT(*) FROM task_purge_replacements").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "approved"
        )


def test_valid_handoff_card_is_not_creation_noncompliant(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    valid = json.dumps(
        {
            "fields": {"evidence": {"type": "text"}},
            "reviewer": "default",
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE task_handoff_requirements SET canonical_payload = ? "
            "WHERE task_id = 'predecessor'",
            (valid,),
        )
        conn.commit()
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'predecessor'"
            ).fetchone()[0]
            == 1
        )


def test_accepted_handoff_or_lifecycle_evidence_makes_task_ineligible(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    initiative_card_id = _seed_creation_noncompliant_graph(database_path)
    with sqlite3.connect(database_path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE task_id = 'predecessor'"
        ).fetchone()[0]
        run_id = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, "
            "ended_at, outcome) VALUES "
            "('predecessor', 'independent-reviewer', 'done', 3, 4, 'completed')"
        ).lastrowid
        conn.execute(
            "INSERT INTO task_candidate_handoffs "
            "(candidate_id, task_card_id, task_id, execution_run_id, reviewer, "
            "summary, metadata_json, submitted_by, created_at) VALUES "
            "('candidate:accepted', ?, 'predecessor', ?, 'default', '', '{}', "
            "'independent-reviewer', 4)",
            (card_id, run_id),
        )
        review_run_id = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, "
            "ended_at, outcome) VALUES "
            "('predecessor', 'default', 'done', 5, 6, 'completed')"
        ).lastrowid
        conn.execute(
            "INSERT INTO task_reviewer_verdicts "
            "(verdict_id, task_card_id, task_id, candidate_id, review_run_id, "
            "reviewer, verdict, summary, created_at) VALUES "
            "('verdict:accepted', ?, 'predecessor', 'candidate:accepted', ?, "
            "'default', 'accepted', '', 6)",
            (card_id, review_run_id),
        )
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, canonical_payload, accepted_task_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "('result:accepted', ?, 'initiative-1', 'D1', NULL, 1, 'closure', "
            "'{}', '[\"candidate:accepted\"]', '{}', 'seed', 1, 7)",
            (initiative_card_id,),
        )
        conn.commit()
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)

    assert result["result"] == "REJECTED"


def test_permanent_bug_requires_blocked_idle_card_and_complete_recovery_evidence(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    update = _update()
    update["eligibility_classification"] = "permanent_bug_blockage"
    update["eligibility_evidence_ref"] = {
        "defect_ref": "bug:123",
        "verification_ref": "verification:123",
        "supported_recovery_attempt_refs": {
            "unblock": "attempt:unblock",
            "requeue": "attempt:requeue",
            "retry": "attempt:retry",
            "input_correction": "attempt:input",
            "requested_changes": "attempt:changes",
        },
    }
    payload = _payload(update)
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)
    assert result["result"] == "REJECTED"

    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE tasks SET status = 'blocked', current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = 'predecessor'"
        )
        conn.commit()
    payload["approval_id"] = "approval:purge:2"
    _approve(database_path, payload)
    result = _submit(_boundary(plugin_modules, database_path, provider), payload)
    assert result["result"] == "ACCEPTED"


def test_wrong_profile_and_receipt_replay_are_fail_closed_and_idempotent(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    payload = _payload()
    _approve(database_path, payload)
    boundary = _boundary(plugin_modules, database_path, provider)

    rejected = _submit(boundary, payload, profile="independent-reviewer")
    assert rejected["result"] == "REJECTED"
    accepted = _submit(boundary, payload)
    replay = _submit(boundary, payload)

    assert accepted["result"] == "ACCEPTED"
    assert replay == accepted
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM task_purge_replacements").fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'successor'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("status", ["running", "review"])
def test_creation_defect_does_not_allow_purging_an_active_execution(
    plugin_modules, tmp_path, monkeypatch, status
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = 'predecessor'", (status,))
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "approved"
        )
        assert (
            conn.execute(
                "SELECT status FROM tasks WHERE id = 'predecessor'"
            ).fetchone()[0]
            == status
        )


def test_late_deletion_failure_rolls_back_successor_links_and_authorization(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    initiative_card_id = _seed_creation_noncompliant_graph(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "CREATE TRIGGER simulate_storage_failure BEFORE DELETE ON tasks "
            "WHEN OLD.id = 'predecessor' BEGIN "
            "SELECT RAISE(ABORT, 'simulated storage failure'); END"
        )
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'successor'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM task_purge_replacements").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE id = ?",
                (initiative_card_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "approved"
        )
        assert conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id"
        ).fetchall() == [
            ("parent-a", "predecessor"),
            ("predecessor", "child-a"),
        ]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = 'predecessor'"
            ).fetchone()[0]
            == 1
        )


def test_valid_cross_initiative_dependency_is_preserved(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-2')")
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, created_at, board_slug) "
            "VALUES ('initiative', 'initiative-2', NULL, 'Other initiative', "
            "1, 'orchestrator')"
        )
        conn.execute(
            "UPDATE adrian_kanban_cards SET initiative_id = 'initiative-2' "
            "WHERE task_id = 'parent-a'"
        )
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)

    assert result["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = 'successor'"
        ).fetchall() == [("parent-a",)]
        assert (
            conn.execute("SELECT status FROM tasks WHERE id = 'successor'").fetchone()[
                0
            ]
            == "todo"
        )


def test_replacement_lineage_is_visible_without_implying_acceptance(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    payload = _payload()
    _approve(database_path, payload)
    assert (
        _submit(_boundary(plugin_modules, database_path, provider), payload)["result"]
        == "ACCEPTED"
    )
    projections = plugin_modules["projections"]
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES ('successor', 'reviewer', 'detail-visible comment', 10)"
        )
        shown = projections.show_projection(conn, "successor", "orchestrator")
        lineage = shown["purge_replacement"]
        assert lineage["predecessor_task_id"] == "predecessor"
        assert lineage["successor_task_id"] == "successor"
        assert lineage["cleanup_required"] is False
        assert isinstance(lineage["requester_evidence"], dict)
        assert lineage["transferred_relations"]["dependent_task_ids"] == ["child-a"]
        assert shown["accepted_handoff"] is None
        assert shown["task"]["status"] == "todo"
        assert shown["comments"] == [
            {
                "id": shown["comments"][0]["id"],
                "author": "reviewer",
                "body": "detail-visible comment",
                "created_at": 10,
            }
        ]
        initiative = projections.show_projection(
            conn, None, "orchestrator", "initiative-1"
        )
        assert initiative["purge_replacements"] == [lineage]
        assert (
            next(t for t in initiative["tasks"] if t["task_id"] == "successor")[
                "replaces_task_id"
            ]
            == "predecessor"
        )
        listed = projections.list_projection(conn, board="orchestrator")
        assert (
            next(t for t in listed["tasks"] if t["task_id"] == "successor")[
                "replaces_task_id"
            ]
            == "predecessor"
        )
        assert (
            projections.show_projection(conn, "parent-a", "orchestrator")[
                "purge_replacement"
            ]
            is None
        )
        assert projections.list_projection(conn, board="different-board")["tasks"] == []
        with pytest.raises(ValueError):
            projections.show_projection(conn, "successor", "different-board")
        conn.execute(
            "UPDATE adrian_kanban_cards SET closed_at = 1234 "
            "WHERE initiative_id = 'initiative-1'"
        )
        closed_detail = projections.show_projection(
            conn, None, "orchestrator", "initiative-1"
        )
        assert closed_detail["card"]["closed_at"] == 1234
        closed_list = projections.list_projection(conn, board="orchestrator")
        assert next(
            card
            for card in closed_list["initiatives"]
            if card["initiative_id"] == "initiative-1"
        )["closed_at"] == 1234
        assert next(
            card for card in closed_list["tasks"] if card["task_id"] == "successor"
        )["closed_at"] == 1234
        json.dumps(shown)
        json.dumps(initiative)


def test_repeated_replacement_keeps_full_lineage_without_completing_dependencies(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    first = _payload()
    _approve(database_path, first)
    boundary = _boundary(plugin_modules, database_path, provider)
    assert _submit(boundary, first)["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE task_handoff_requirements SET canonical_payload = '{}' "
            "WHERE task_id = 'successor'"
        )
    update = _update()
    update["replacement_id"] = "purge-replacement:2"
    update["predecessor_task_id"] = "successor"
    update["successor_payload"]["task_id"] = "successor-2"
    second = _payload(update)
    second["approval_id"] = "approval:purge:2"
    _approve(database_path, second, version=1)
    result = _submit(boundary, second, version=1)
    assert result["result"] == "ACCEPTED"
    assert _submit(boundary, second, version=1) == result
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        initiative = plugin_modules["projections"].show_projection(
            conn, None, "orchestrator", "initiative-1"
        )
        assert [
            (r["predecessor_task_id"], r["successor_task_id"])
            for r in initiative["purge_replacements"]
        ] == [("predecessor", "successor"), ("successor", "successor-2")]
        assert {r["task_id"] for r in initiative["tasks"]} == {
            "parent-a",
            "successor-2",
            "child-a",
        }
        assert [
            tuple(r)
            for r in conn.execute(
                "SELECT parent_id, child_id FROM task_links ORDER BY parent_id"
            )
        ] == [("parent-a", "successor-2"), ("successor-2", "child-a")]
        assert (
            conn.execute("SELECT status FROM tasks WHERE id = 'child-a'").fetchone()[0]
            == "todo"
        )


def _cleanup_payload(database_path, tmp_path, monkeypatch):
    managed_root = tmp_path / "managed-workspaces"
    source = managed_root / "predecessor"
    source.mkdir(parents=True)
    (source / "notes.txt").write_text("preserve me", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(managed_root))
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'scratch', workspace_path = ? "
            "WHERE id = 'predecessor'",
            (str(source),),
        )
    update = _update()
    update["repository_worktree_disposition"]["action"] = "cleanup_after_commit"
    return source, _payload(update)


def test_cleanup_archives_only_after_commit_and_replay_is_idempotent(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    source, payload = _cleanup_payload(database_path, tmp_path, monkeypatch)
    _approve(database_path, payload)
    cleanup = plugin_modules["purge_cleanup"]
    real_archive = cleanup.archive_path
    observations = []

    def observed_archive(*args, **kwargs):
        with sqlite3.connect(database_path, timeout=1) as other:
            assert (
                other.execute(
                    "SELECT COUNT(*) FROM tasks WHERE id = 'predecessor'"
                ).fetchone()[0]
                == 0
            )
            assert (
                other.execute(
                    "SELECT COUNT(*) FROM tasks WHERE id = 'successor'"
                ).fetchone()[0]
                == 1
            )
            assert (
                other.execute(
                    "SELECT COUNT(*) FROM task_purge_cleanup_items"
                ).fetchone()[0]
                == 1
            )
        observations.append("committed")
        return real_archive(*args, **kwargs)

    monkeypatch.setattr(cleanup, "archive_path", observed_archive)
    boundary = _boundary(plugin_modules, database_path, provider)
    result = _submit(boundary, payload)
    assert result["result"] == "ACCEPTED"
    assert result["post_commit"]["state"] == "verified", [
        item.get("error") for item in result["post_commit"].get("items", [])
    ]
    assert observations == ["committed"]
    assert not source.exists()
    item = result["post_commit"]["items"][0]
    assert (Path(item["archive"]) / "notes.txt").read_text() == "preserve me"
    replay = _submit(boundary, payload)
    assert replay["result"] == "ACCEPTED"
    assert replay["value"] == result["value"]
    assert replay["post_commit"]["state"] == "verified", replay["post_commit"]
    assert observations == ["committed"]
    with sqlite3.connect(database_path) as conn:
        assert [
            r[0]
            for r in conn.execute(
                "SELECT status FROM task_purge_cleanup_events ORDER BY event_id"
            )
        ] == ["prepared", "verified"]
        conn.row_factory = sqlite3.Row
        shown = plugin_modules["projections"].show_projection(
            conn, "successor", "orchestrator"
        )
        assert shown["purge_replacement"]["cleanup"]["state"] == "verified"
        assert (
            shown["purge_replacement"]["cleanup"]["items"][0]["archive"]
            == item["archive"]
        )


def test_approved_cleanup_archives_real_linked_worktree_and_replays(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    repo = tmp_path / "repository"
    repo.mkdir()

    def git(path, *args):
        return subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()

    git(repo, "init")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "base",
    )
    source = tmp_path / "predecessor-worktree"
    git(repo, "worktree", "add", "-b", "unfinished-task", str(source))
    (source / "unfinished.txt").write_text("retain uncommitted work", encoding="utf-8")
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name=? "
            "WHERE id='predecessor'",
            (str(source), "unfinished-task"),
        )
    update = _update()
    update["repository_worktree_disposition"]["action"] = "cleanup_after_commit"
    payload = _payload(update)
    _approve(database_path, payload)
    boundary = _boundary(plugin_modules, database_path, provider)
    result = _submit(boundary, payload)
    assert result["result"] == "ACCEPTED"
    assert result["post_commit"]["state"] == "verified", result
    archive = Path(result["post_commit"]["items"][0]["archive"])
    assert not source.exists()
    assert (archive / "unfinished.txt").read_text() == "retain uncommitted work"
    assert Path(git(archive, "rev-parse", "--show-toplevel")) == archive
    assert git(archive, "branch", "--show-current") == "unfinished-task"
    replay = _submit(boundary, payload)
    assert replay["value"] == result["value"]
    assert replay["post_commit"]["state"] == "verified"
    assert git(repo, "show-ref", "--verify", "refs/heads/unfinished-task")


def test_postcommit_failure_is_not_reported_as_database_rollback_and_can_retry(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    source, payload = _cleanup_payload(database_path, tmp_path, monkeypatch)
    _approve(database_path, payload)
    cleanup = plugin_modules["purge_cleanup"]
    real_archive = cleanup.archive_path

    def fail_move(*args, **kwargs):
        raise OSError("injected move failure")

    monkeypatch.setattr(cleanup, "archive_path", fail_move)
    boundary = _boundary(plugin_modules, database_path, provider)
    result = _submit(boundary, payload)
    assert result["result"] == "ACCEPTED"
    assert result["state_changed"] is True
    assert result["post_commit"]["state"] == "failed"
    assert source.exists()
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        shown = plugin_modules["projections"].show_projection(
            conn, "successor", "orchestrator"
        )
        assert shown["purge_replacement"]["cleanup"]["state"] == "failed"
        assert shown["purge_replacement"]["cleanup"]["remediation"]
    monkeypatch.setattr(cleanup, "archive_path", real_archive)
    replay = _submit(boundary, payload)
    assert replay["result"] == "ACCEPTED"
    assert replay["post_commit"]["state"] == "verified"
    assert not source.exists()
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM task_purge_replacements").fetchone()[0]
            == 1
        )
        assert [
            r[0]
            for r in conn.execute(
                "SELECT status FROM task_purge_cleanup_events ORDER BY event_id"
            )
        ] == ["prepared", "failed", "verified"]


def test_crash_after_move_before_journal_commit_recovers_from_archive(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    source, payload = _cleanup_payload(database_path, tmp_path, monkeypatch)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_outcome BEFORE INSERT ON task_purge_cleanup_events "
            "WHEN NEW.status != 'prepared' BEGIN SELECT RAISE(ABORT, 'journal unavailable'); END"
        )
    _approve(database_path, payload)
    boundary = _boundary(plugin_modules, database_path, provider)
    result = _submit(boundary, payload)
    assert result["result"] == "ACCEPTED"
    assert result["post_commit"]["state"] == "failed"
    assert not source.exists()
    with sqlite3.connect(database_path) as conn:
        destination = Path(
            conn.execute(
                "SELECT destination_path FROM task_purge_cleanup_items"
            ).fetchone()[0]
        )
        assert (destination / "notes.txt").read_text() == "preserve me"
        assert conn.execute(
            "SELECT status FROM task_purge_cleanup_events"
        ).fetchall() == [("prepared",)]
        conn.execute("DROP TRIGGER fail_outcome")
    replay = _submit(boundary, payload)
    assert replay["post_commit"]["state"] == "verified"
    assert (destination / "notes.txt").read_text() == "preserve me"


def test_multiple_cleanup_items_resume_only_unverified_items(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    source, payload = _cleanup_payload(database_path, tmp_path, monkeypatch)
    attachments_root = tmp_path / "attachments"
    attachment = attachments_root / "predecessor" / "proof.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("old evidence", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(attachments_root))
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO task_attachments(task_id,filename,stored_path,created_at) "
            "VALUES ('predecessor','proof.txt',?,1)",
            (str(attachment),),
        )
    _approve(database_path, payload)
    cleanup = plugin_modules["purge_cleanup"]
    real_archive = cleanup.archive_path
    calls = []

    def partial_move(src, dest, expected, **kwargs):
        calls.append(src)
        if src == source:
            raise OSError("one item temporarily unavailable")
        return real_archive(src, dest, expected, **kwargs)

    monkeypatch.setattr(cleanup, "archive_path", partial_move)
    boundary = _boundary(plugin_modules, database_path, provider)
    result = _submit(boundary, payload)
    assert result["result"] == "ACCEPTED"
    assert result["post_commit"]["state"] == "failed"
    assert {i["state"] for i in result["post_commit"]["items"]} == {
        "verified",
        "failed",
    }
    assert source.exists()
    assert not attachment.exists()
    calls.clear()

    def recovered_move(src, dest, expected, **kwargs):
        calls.append(src)
        return real_archive(src, dest, expected, **kwargs)

    monkeypatch.setattr(cleanup, "archive_path", recovered_move)
    replay = _submit(boundary, payload)
    assert replay["post_commit"]["state"] == "verified"
    assert calls == [source]
    for item in replay["post_commit"]["items"]:
        assert Path(item["archive"]).exists()


def test_retained_lineage_protects_files_after_predecessor_card_is_gone(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    source, first = _cleanup_payload(database_path, tmp_path, monkeypatch)
    first["update"]["repository_worktree_disposition"]["action"] = "retain"
    _approve(database_path, first)
    boundary = _boundary(plugin_modules, database_path, provider)
    assert _submit(boundary, first)["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE tasks SET workspace_path=? WHERE id='successor'", (str(source),)
        )
        conn.execute(
            "UPDATE task_handoff_requirements SET canonical_payload='{}' WHERE task_id='successor'"
        )
    update = _update()
    update["replacement_id"] = "purge:protected-retained"
    update["predecessor_task_id"] = "successor"
    update["successor_payload"]["task_id"] = "successor-2"
    update["repository_worktree_disposition"]["action"] = "cleanup_after_commit"
    second = _payload(update)
    second["approval_id"] = "approval:protected-retained"
    _approve(database_path, second, version=1)
    result = _submit(boundary, second, version=1)
    assert result["result"] == "REJECTED"
    assert (source / "notes.txt").read_text() == "preserve me"


def test_cleanup_serializes_new_workspace_references_during_move(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    source, payload = _cleanup_payload(database_path, tmp_path, monkeypatch)
    _approve(database_path, payload)
    cleanup = plugin_modules["purge_cleanup"]
    real_archive = cleanup.archive_path
    attempts = []

    def attempt_concurrent_reference(src, dest, expected, **kwargs):
        with sqlite3.connect(database_path, timeout=0.05) as other:
            # Reads still see committed replacement, but another write must
            # not claim the source between the shared-path check and move.
            assert (
                other.execute(
                    "SELECT COUNT(*) FROM tasks WHERE id='successor'"
                ).fetchone()[0]
                == 1
            )
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute(
                    "UPDATE tasks SET workspace_path=? WHERE id='child-a'",
                    (str(source),),
                )
        attempts.append("writer excluded")
        return real_archive(src, dest, expected, **kwargs)

    monkeypatch.setattr(cleanup, "archive_path", attempt_concurrent_reference)
    result = _submit(_boundary(plugin_modules, database_path, provider), payload)
    assert result["post_commit"]["state"] == "verified"
    assert attempts == ["writer excluded"]


def test_replacement_rollback_leaves_files_and_no_cleanup_plan(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    source, payload = _cleanup_payload(database_path, tmp_path, monkeypatch)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_cleanup_plan BEFORE INSERT ON task_purge_cleanup_items "
            "BEGIN SELECT RAISE(ABORT, 'injected plan failure'); END"
        )
    _approve(database_path, payload)
    result = _submit(_boundary(plugin_modules, database_path, provider), payload)
    assert result["result"] == "REJECTED"
    assert (source / "notes.txt").read_text() == "preserve me"
    assert not (source.parent / ".adrian-kanban-purge-archive").exists()
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'predecessor'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM task_purge_replacements").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM task_purge_cleanup_items").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM task_purge_cleanup_events").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("shared", [True, False])
def test_shared_or_unmanaged_workspace_cannot_be_cleaned(
    plugin_modules, tmp_path, monkeypatch, shared
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    source, payload = _cleanup_payload(database_path, tmp_path, monkeypatch)
    if shared:
        with sqlite3.connect(database_path) as conn:
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = 'child-a'",
                (str(source),),
            )
    else:
        monkeypatch.setenv(
            "HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "different-root")
        )
    _approve(database_path, payload)
    result = _submit(_boundary(plugin_modules, database_path, provider), payload)
    assert result["result"] == "REJECTED"
    assert source.exists()


def test_relation_with_missing_canonical_initiative_rolls_back(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO adrian_kanban_initiatives VALUES ('orphan-initiative')"
        )
        conn.execute(
            "UPDATE adrian_kanban_cards SET initiative_id = 'orphan-initiative' "
            "WHERE task_id = 'child-a'"
        )
    payload = _payload()
    _approve(database_path, payload)
    result = _submit(_boundary(plugin_modules, database_path, provider), payload)
    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM task_purge_replacements").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'predecessor'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "approved"
        )


def test_notification_routing_details_survive_replacement(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE kanban_notify_subs SET user_id = 'user-1', "
            "user_id_alt = 'alt-1', chat_type = 'private', "
            "notifier_profile = 'default', delivery_mode = 'notify', "
            'delivery_metadata = \'{"surface":"desktop"}\', last_event_id = 99 '
            "WHERE task_id = 'predecessor'"
        )
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)

    assert result["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT user_id, user_id_alt, chat_type, notifier_profile, "
            "delivery_mode, delivery_metadata, last_event_id "
            "FROM kanban_notify_subs WHERE task_id = 'successor'"
        ).fetchone() == (
            "user-1",
            "alt-1",
            "private",
            "default",
            "notify",
            '{"surface":"desktop"}',
            0,
        )


def test_purged_task_id_cannot_be_reused_by_ordinary_creation(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    payload = _payload()
    _approve(database_path, payload)
    boundary = _boundary(plugin_modules, database_path, provider)
    assert _submit(boundary, payload)["result"] == "ACCEPTED"

    successor = _successor_payload()
    successor["task_id"] = "predecessor"
    result = boundary.submit(
        "kanban_create",
        attempt_id="attempt:reuse",
        idempotency_key="key:reuse",
        target="predecessor",
        expected_version=0,
        session_id="session-purge",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile="default",
        payload=successor,
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'predecessor'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute(
            "SELECT predecessor_task_id FROM task_purge_replacements"
        ).fetchall() == [("predecessor",)]


@pytest.mark.parametrize("malformed_contract", [False, True])
def test_lifecycle_replacement_preserves_phase_and_prepares_real_inputs(
    plugin_modules, tmp_path, monkeypatch, malformed_contract
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    initiative_card_id = _seed_creation_noncompliant_graph(database_path)
    snapshot = plugin_modules["contracts"].expand_contract(
        step="D2",
        initiative_id="initiative-1",
        baseline_refs=("Canon/design.md",),
        governing_source_refs=("2-design/review.md",),
        prior_record_refs=(),
    )
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO initiative_transitions "
            "(initiative_card_id, initiative_id, transition_id, to_phase, "
            "created_at) VALUES (?, 'initiative-1', 1, 'D2', 1)",
            (initiative_card_id,),
        )
        plugin_modules["lifecycle"].LifecycleContractRepository(conn).attach(
            task_id="predecessor",
            snapshot=snapshot,
            skill=plugin_modules["skill_bundle"].resolve_skill_binding("D2"),
            created_at=1,
        )
        if malformed_contract:
            conn.execute(
                "UPDATE task_lifecycle_contracts "
                "SET canonical_contract_payload = '{broken' "
                "WHERE task_id = 'predecessor'"
            )

    update = _update()
    successor = update["successor_payload"]
    successor["body"] = "initiative_id: initiative-1\nstep: D2"
    successor["lifecycle_contract_v1"] = {
        "version": 1,
        "step": "D2",
        "baseline_refs": ["Canon/design.md"],
        "governing_source_refs": ["2-design/review.md"],
        "prior_record_refs": [],
    }
    contents = {"Canon/design.md": b"design", "2-design/review.md": b"review"}
    successor["task_input_manifest_v1"] = {
        "version": 1,
        "entries": [
            {
                "workspace_path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "source_kind": "snapshot_attachment",
            }
            for path, data in contents.items()
        ],
        "context_ref": {path: "Governing source" for path in contents},
        "snapshots": {
            path: {
                "filename": Path(path).name,
                "content_type": "text/markdown",
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
            for path, data in contents.items()
        },
    }
    preparation_calls = []

    def prepare(payload, context):
        preparation_calls.append(context.session_id)
        return plugin_modules["task_inputs"].prepare_task_input_manifest(
            payload["task_input_manifest_v1"],
            lambda *_args: pytest.fail("snapshot inputs must not fetch Git"),
        )

    payload = _payload(update)
    _approve(database_path, payload)
    boundary = _boundary(plugin_modules, database_path, provider, prepare)
    result = _submit(boundary, payload)

    assert result["result"] == "ACCEPTED"
    assert _submit(boundary, payload) == result
    assert preparation_calls == ["session-purge"]
    with sqlite3.connect(database_path) as conn:
        record = (
            plugin_modules["lifecycle"]
            .LifecycleContractRepository(conn)
            .load("successor")
        )
        assert record.snapshot.step == "D2"
        assert record.snapshot.initiative_id == "initiative-1"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_attachments WHERE task_id = 'successor'"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT declared_inputs_accessible FROM task_input_manifests "
                "WHERE task_id = 'successor'"
            ).fetchone()[0]
            == 1
        )

    # A missing handoff row is itself a creation defect, even when the
    # lifecycle snapshot and accessible manifest are otherwise valid.
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "DELETE FROM task_handoff_requirements WHERE task_id = 'successor'"
        )
    second_update = _update()
    second_update["replacement_id"] = "purge:missing-handoff"
    second_update["predecessor_task_id"] = "successor"
    second_update["successor_payload"] = json.loads(json.dumps(successor))
    second_update["successor_payload"]["task_id"] = "successor-2"
    second_payload = _payload(second_update)
    second_payload["approval_id"] = "approval:missing-handoff"
    _approve(database_path, second_payload, version=1)
    second_result = _submit(boundary, second_payload, version=1)
    assert second_result["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        second_record = (
            plugin_modules["lifecycle"]
            .LifecycleContractRepository(conn)
            .load("successor-2")
        )
        assert second_record.snapshot.step == "D2"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_handoff_requirements WHERE task_id = 'successor-2'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "reference, expected",
    [
        ("candidate:predecessor", "REJECTED"),
        ("predecessor", "REJECTED"),
        ("candidate:predecessor-unrelated", "ACCEPTED"),
    ],
)
def test_accepted_evidence_matches_exact_task_or_candidate_identity(
    plugin_modules, tmp_path, monkeypatch, reference, expected
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    initiative_card_id = _seed_creation_noncompliant_graph(database_path)
    with sqlite3.connect(database_path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE task_id = 'predecessor'"
        ).fetchone()[0]
        run_id = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = 'predecessor'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO task_candidate_handoffs "
            "(candidate_id, task_card_id, task_id, execution_run_id, reviewer, "
            "summary, metadata_json, submitted_by, created_at) VALUES "
            "('candidate:predecessor', ?, 'predecessor', ?, 'default', '', "
            "'{}', 'independent-reviewer', 4)",
            (card_id, run_id),
        )
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, "
            "iteration, result_kind, canonical_payload, accepted_task_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "('result:record', ?, 'initiative-1', 'D1', 1, 'closure', "
            "'{}', ?, '{}', 'seed', 1, 7)",
            (initiative_card_id, json.dumps([reference])),
        )
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)

    assert result["result"] == expected


def test_completed_prerequisite_does_not_prevent_replacement(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_creation_noncompliant_graph(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'parent-a'")
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(_boundary(plugin_modules, database_path, provider), payload)

    assert result["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT status FROM tasks WHERE id = 'successor'").fetchone()[
                0
            ]
            == "ready"
        )
        assert (
            conn.execute("SELECT status FROM tasks WHERE id = 'child-a'").fetchone()[0]
            == "todo"
        )
