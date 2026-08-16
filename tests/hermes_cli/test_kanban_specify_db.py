"""Tests for kb.specify_triage_task — the DB-layer atomic promotion
from the triage column to todo. LLM-free by design."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _orchestrator_authority():
    return kb._scoped_mutation_authority(
        kb._MUTATION_AUTHORITY_DISPATCHER_ORCHESTRATOR
    )


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        triage=True,
    )


def test_specify_promotes_triage_to_todo(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="rough idea")
        assert kb.get_task(conn, tid).status == "triage"
    with kb.connect() as conn:
        with _orchestrator_authority():
            ok = kb.specify_triage_task(
                conn,
                tid,
                title="Refined: rough idea",
                body="**Goal**\nDo the thing.",
                author="specifier-bot",
            )
    assert ok is True
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    # No parents → recompute_ready should have flipped it past todo to ready.
    assert task.status == "ready"
    assert task.title == "Refined: rough idea"
    assert "**Goal**" in (task.body or "")


def test_specify_rejects_blank_title(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="rough")
    with kb.connect() as conn, _orchestrator_authority(), pytest.raises(ValueError):
        kb.specify_triage_task(conn, tid, title="   ", body="ok")


def test_specify_records_audit_comment_only_when_author_given(kanban_home):
    # With author → comment added.
    with kb.connect() as conn:
        tid1 = _create_triage(conn, title="a")
        with _orchestrator_authority():
            kb.specify_triage_task(
                conn, tid1, title="A-spec", body="b", author="ace"
            )
        comments1 = kb.list_comments(conn, tid1)
    assert len(comments1) == 1
    assert "Specified" in comments1[0].body
    assert comments1[0].author == "ace"

    # Without author → no comment (silent).
    with kb.connect() as conn:
        tid2 = _create_triage(conn, title="b")
        with _orchestrator_authority():
            kb.specify_triage_task(conn, tid2, title="B-spec", body="b")
        comments2 = kb.list_comments(conn, tid2)
    assert comments2 == []


def test_specify_refuses_untrusted_caller(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="proposal")
        assert kb.specify_triage_task(conn, tid, body="not accepted") is False
        assert kb.get_task(conn, tid).status == "triage"


def test_untrusted_caller_cannot_archive_or_delete_triage_proposal(kanban_home):
    with kb.connect() as conn:
        archive_id = _create_triage(conn, title="archive proposal")
        delete_id = _create_triage(conn, title="delete proposal")
        assert kb.archive_task(conn, archive_id) is False
        assert kb.delete_task(conn, delete_id) is False
        assert kb.get_task(conn, archive_id).status == "triage"
        assert kb.get_task(conn, delete_id).status == "triage"

        with _orchestrator_authority():
            assert kb.archive_task(conn, archive_id) is True
            assert kb.delete_task(conn, delete_id) is True
        assert kb.get_task(conn, archive_id).status == "archived"
        assert kb.get_task(conn, delete_id) is None


def test_untrusted_idempotency_collision_cannot_reuse_runnable_task(kanban_home):
    with kb.connect() as conn:
        with _orchestrator_authority():
            runnable = kb.create_task(
                conn,
                title="trusted work",
                assignee="worker",
                idempotency_key="shared-key",
            )
        assert kb.get_task(conn, runnable).status == "ready"
        for requested_triage in (False, True):
            with pytest.raises(ValueError, match="non-triage task"):
                kb.create_task(
                    conn,
                    title="untrusted duplicate",
                    assignee="worker",
                    triage=requested_triage,
                    idempotency_key="shared-key",
                )


def test_trusted_idempotency_retry_can_reuse_accepted_proposal(kanban_home):
    with kb.connect() as conn:
        with _orchestrator_authority():
            proposal = kb.create_task(
                conn,
                title="trusted proposal",
                assignee="worker",
                triage=True,
                idempotency_key="accepted-key",
            )
            assert kb.specify_triage_task(conn, proposal, body="accepted") is True
            assert kb.get_task(conn, proposal).status == "ready"
            assert kb.create_task(
                conn,
                title="retry",
                assignee="worker",
                triage=True,
                idempotency_key="accepted-key",
            ) == proposal


