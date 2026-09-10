"""Dispatcher-service tests derived from design v0.28 section 9.

The service is only an owner/driver for the already-tested lifecycle dispatcher:
it discovers governed ready tasks, derives all launch evidence from authoritative
state, and never delegates eligibility selection to native Hermes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_cli import kanban_db as kb

from tests.test_adrian_kanban_s2 import (
    _insert_dispatch_task,
    _staged_dispatch_database,
    provider_modules,  # noqa: F401 - imported pytest fixture
)


def test_dispatch_tick_discovers_only_governed_ready_tasks_in_priority_order(
    provider_modules, tmp_path, monkeypatch
):
    dispatcher = provider_modules["dispatcher"]
    conn, provider = _staged_dispatch_database(
        provider_modules, tmp_path, monkeypatch
    )
    low = _insert_dispatch_task(
        provider_modules, conn, tmp_path, task_id="task:low", priority=1
    )
    high = _insert_dispatch_task(
        provider_modules, conn, tmp_path, task_id="task:high", priority=9
    )
    _insert_dispatch_task(
        provider_modules,
        conn,
        tmp_path,
        task_id="legacy-ready",
        priority=99,
        attach_contract=False,
    )
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = 'task:low'")
    spawned = []
    monkeypatch.setattr(
        dispatcher,
        "resolve_skill_binding",
        lambda phase: high.skill,
    )
    try:
        result = dispatcher._dispatch_ready_once(
            provider,
            conn,
            dispatcher_session_id="dispatcher:service-1",
            spawn_fn=lambda task, workspace, **kwargs: spawned.append(task.id) or 8101,
            board="default",
        )

        assert result.candidate_task_ids == ("task:high",)
        assert result.launched_task_ids == ("task:high",)
        assert result.rejections == ()
        assert spawned == ["task:high"]
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = 'legacy-ready'"
        ).fetchone()[0] == "ready"
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = 'task:low'"
        ).fetchone()[0] == "blocked"
        assert low.task_id == "task:low"
    finally:
        kb.clear_authority_providers()
        conn.close()


def test_dispatch_tick_derives_live_skill_compatibility_and_fails_closed_on_drift(
    provider_modules, tmp_path, monkeypatch
):
    dispatcher = provider_modules["dispatcher"]
    lifecycle = provider_modules["lifecycle"]
    conn, provider = _staged_dispatch_database(
        provider_modules, tmp_path, monkeypatch
    )
    record = _insert_dispatch_task(
        provider_modules, conn, tmp_path, task_id="task:drift"
    )
    monkeypatch.setattr(
        dispatcher,
        "resolve_skill_binding",
        lambda phase: lifecycle.SkillBinding(
            skill_id=record.skill.skill_id,
            skill_version=record.skill.skill_version,
            skill_hash="different-live-skill-hash",
        ),
    )
    spawned = []
    try:
        result = dispatcher._dispatch_ready_once(
            provider,
            conn,
            dispatcher_session_id="dispatcher:service-2",
            spawn_fn=lambda *args, **kwargs: spawned.append(args) or 8102,
        )

        assert result.candidate_task_ids == ("task:drift",)
        assert result.launched_task_ids == ()
        assert len(result.rejections) == 1
        assert result.rejections[0].task_id == "task:drift"
        assert "COMPATIBILITY_SKILL_HASH_MISMATCH" in result.rejections[0].diagnostic
        assert "remediation:" in result.rejections[0].diagnostic
        assert spawned == []
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = 'task:drift'"
        ).fetchone()[0] == "ready"
    finally:
        kb.clear_authority_providers()
        conn.close()


def test_dispatch_tick_derives_exact_accepted_checkpoint_predecessor(
    provider_modules, tmp_path, monkeypatch
):
    dispatcher = provider_modules["dispatcher"]
    contracts = provider_modules["contracts"]
    lifecycle = provider_modules["lifecycle"]
    conn, provider = _staged_dispatch_database(
        provider_modules, tmp_path, monkeypatch
    )
    initiative_card_id = conn.execute(
        "SELECT id FROM adrian_kanban_cards WHERE card_type='initiative'"
    ).fetchone()[0]
    conn.execute(
        "UPDATE initiative_transitions SET to_phase='D4', to_segment_id=NULL "
        "WHERE initiative_id='I1'"
    )
    task_id = "task:D4.5"
    task_root = tmp_path / "dev45"
    task_root.mkdir()
    conn.execute(
        "INSERT INTO tasks (id,title,assignee,status,priority,created_at,"
        "workspace_kind,workspace_path) VALUES (?,?,'test-authority-reviewer',"
        "'ready',1,30,'dir',?)",
        (task_id, task_id, str(task_root)),
    )
    conn.execute(
        "INSERT INTO adrian_kanban_cards "
        "(card_type,initiative_id,task_id,title,created_at) "
        "VALUES ('task','I1',?,?,30)",
        (task_id, task_id),
    )
    snapshot = contracts.expand_contract(
        step="D4.5",
        initiative_id="I1",
        baseline_refs=("baseline",),
        prior_record_refs=("record",),
        predecessor_ref="checkpoint:D4.4",
    )
    skill = lifecycle.SkillBinding("skill:D4.5", "1", "skill-hash")
    conn.execute("BEGIN IMMEDIATE")
    record = lifecycle.LifecycleContractRepository(conn).attach(
        task_id=task_id, snapshot=snapshot, skill=skill, created_at=31
    )
    conn.commit()
    conn.execute(
        "INSERT INTO initiative_phase_results "
        "(result_id,initiative_card_id,initiative_id,phase,segment_id,iteration,"
        "result_kind,canonical_payload,actor_evidence,idempotency_key,accepted,"
        "created_at) VALUES ('checkpoint:D4.4',?,'I1','D4',NULL,1,"
        "'phase_close',?,'actor','idem-dev44',1,32)",
        (initiative_card_id, '{"step":"D4.4"}'),
    )
    monkeypatch.setattr(dispatcher, "resolve_skill_binding", lambda phase: skill)
    attempt = dispatcher._derive_dispatch_attempt(
        conn,
        record,
        attempt_id="attempt:D4.5",
        dispatcher_session_id="dispatcher:service-3",
        board="default",
    )

    assert attempt.predecessor == dispatcher.PredecessorEvidence(
        reference="checkpoint:D4.4",
        evidence_kind="initiative_checkpoint",
        initiative_id="I1",
        accepted=True,
    )
    kb.clear_authority_providers()
    conn.close()


def test_dispatch_tick_derives_accepted_completion_from_predecessor_task(
    provider_modules, tmp_path, monkeypatch
):
    dispatcher = provider_modules["dispatcher"]
    conn, provider = _staged_dispatch_database(
        provider_modules, tmp_path, monkeypatch
    )
    conn.execute(
        "UPDATE initiative_transitions SET to_phase='D4', to_segment_id=NULL "
        "WHERE initiative_id='I1'"
    )
    predecessor = _insert_dispatch_task(
        provider_modules,
        conn,
        tmp_path,
        task_id="task:D4.1",
        step="D4.1",
    )
    target = _insert_dispatch_task(
        provider_modules,
        conn,
        tmp_path,
        task_id="task:D4.2",
        assignee="test-authority-reviewer",
        step="D4.2",
    )
    predecessor_run = conn.execute(
        "INSERT INTO task_runs "
        "(task_id,profile,status,started_at,ended_at,outcome) "
        "VALUES (?,'independent-reviewer','done',40,41,'completed')",
        (predecessor.task_id,),
    ).lastrowid
    review_run = conn.execute(
        "INSERT INTO task_runs "
        "(task_id,profile,status,started_at,ended_at,outcome) "
        "VALUES (?,'test-authority-reviewer','done',42,43,'completed')",
        (target.task_id,),
    ).lastrowid
    conn.execute(
        "INSERT INTO task_candidate_handoffs "
        "(candidate_id,task_card_id,task_id,execution_run_id,reviewer,summary,"
        "metadata_json,submitted_by,created_at) VALUES "
        "('handoff:D4.1',?,?,?,'test-authority-reviewer','complete','{}',"
        "'independent-reviewer',44)",
        (predecessor.task_card_id, predecessor.task_id, predecessor_run),
    )
    conn.execute(
        "INSERT INTO task_reviewer_verdicts "
        "(verdict_id,task_card_id,task_id,candidate_id,review_run_id,reviewer,"
        "verdict,summary,created_at) VALUES "
        "('verdict:D4.1',?,?,'handoff:D4.1',?,'test-authority-reviewer',"
        "'accepted','accepted',45)",
        (predecessor.task_card_id, predecessor.task_id, review_run),
    )
    monkeypatch.setattr(
        dispatcher, "resolve_skill_binding", lambda phase: target.skill
    )
    try:
        attempt = dispatcher._derive_dispatch_attempt(
            conn,
            target,
            attempt_id="attempt:D4.2",
            dispatcher_session_id="dispatcher:service-4",
            board="default",
        )

        assert attempt.predecessor == dispatcher.PredecessorEvidence(
            reference="handoff:D4.1",
            evidence_kind="accepted_handoff",
            initiative_id="I1",
            accepted=True,
        )
    finally:
        kb.clear_authority_providers()
        conn.close()


def test_segment_dispatch_accepts_legitimate_head_advance_after_materialization(
    provider_modules, tmp_path, monkeypatch
):
    """A shared segment worktree is expected to accumulate reviewed commits.

    The stored observed head is the materialization baseline until DEV4 seals the
    final delivery head.  Dispatch must therefore validate repository identity,
    branch, and base ancestry without treating an ordinary head advance as drift.
    """
    dispatcher = provider_modules["dispatcher"]
    workspace = provider_modules["workspace"]
    from tests.test_adrian_kanban_s2 import (
        _dispatch_attempt,
        _insert_ready_segment_dispatch_task,
    )

    conn, provider = _staged_dispatch_database(
        provider_modules, tmp_path, monkeypatch
    )
    record, registry, _segment_root, _member_root = (
        _insert_ready_segment_dispatch_task(provider_modules, conn, tmp_path)
    )
    monkeypatch.setattr(
        workspace._GitWorkspaceExecutor,
        "verify",
        lambda self, member: workspace._MemberVerification(
            repository_identity=member.repository_identity,
            target_path=member.target_path,
            observed_head="d" * 40,
            branch_matches=True,
            base_contained=True,
            common_repository=str(tmp_path / "git-common"),
            ready=True,
            failures=(),
        ),
    )
    spawned = []
    try:
        outcome = dispatcher._LifecycleDispatcher(
            provider,
            conn,
            spawn_fn=lambda task, resolved_workspace, **kwargs: (
                spawned.append(task.id) or 8201
            ),
            workspace_registry=registry,
        ).dispatch(_dispatch_attempt(provider_modules, record))

        assert outcome.decision.admitted is True
        assert spawned == [record.task_id]
    finally:
        kb.clear_authority_providers()
        conn.close()


def test_dispatch_service_unit_is_plugin_owned_and_restartable():
    unit = (
        Path(__file__).parents[1]
        / "plugins"
        / "adrian-kanban"
        / "systemd"
        / "adrian-kanban-dispatcher.service"
    ).read_text(encoding="utf-8")

    assert "WorkingDirectory=%h/.hermes" in unit
    assert "Environment=HERMES_HOME=%h/.hermes" in unit
    assert "EnvironmentFile=-%h/.hermes/.env" in unit
    assert (
        "ExecStart=%h/.hermes/hermes-agent/venv/bin/python "
        "-m plugins.adrian-kanban.dispatcher_service"
    ) in unit
    assert "hermes kanban daemon" not in unit
    assert "Restart=on-failure" in unit
