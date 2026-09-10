"""The existing public initiative transition tool hosts the two-step override."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_initiatives import (
    _database,
    _seed_initiative,
    _seed_reconciliation,
    commands_module,  # noqa: F401
)
from tests.test_adrian_kanban_s3_override_approval import evidence
from tests.test_adrian_kanban_s3_transition_integration import (
    _git,
    _repository_with_origin_main,
)
from writegate.kanban_approvals import KanbanInitiativeApprovalHost


def _normalizer(commands_module, path, provider, *, workspace_registry=None):
    boundary = commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={
            "kanban_transition_initiative": commands_module._handle_transition_initiative
        },
        workspace_registry=workspace_registry,
        state_resolver=lambda conn, operation, target, payload: conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id=? AND board_slug=?",
            (target, payload["board"]),
        ).fetchone()[0],
        known_profiles={"default", "builder-tester"},
    )
    return commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda operation, args, runtime: (
            "orchestrator",
            None,
            runtime.get("profile", "default"),
        ),
    )


def _args(**changes):
    args = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV1",
        "reconciliation_ref": "reconciliation-1",
        "gate_override": {
            "override_reason": "Adrian explicitly directed this nonstandard route."
        },
        "idempotency_key": "public-override-1",
        "attempt_id": "public-attempt-1",
    }
    args.update(changes)
    return args


def _runtime(**changes):
    runtime = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "api_request_id": "api-request-1",
        "user_task": (
            "Prepare an exact override moving initiative-1 to DEV1 and wait for "
            "my approval before moving it."
        ),
        "profile": "default",
    }
    runtime.update(changes)
    return runtime


@pytest.fixture
def public_case(commands_module, tmp_path, monkeypatch):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    _seed_reconciliation(
        path,
        result_id="reconciliation-1",
        from_phase="D1",
        to_phase="DEV1",
    )
    monkeypatch.setattr(
        commands_module,
        "mint_current_tailscale_authorizer",
        lambda *, request_id, issued_at, ttl_seconds: evidence(request_id, issued_at),
    )
    return path, _normalizer(commands_module, path, provider), commands_module, monkeypatch


def test_public_surface_remains_18_operations_with_one_transition_variant(commands_module):
    assert len(commands_module.TOOL_SCHEMAS) == 18
    schema = commands_module.TOOL_SCHEMAS["kanban_transition_initiative"]["parameters"]
    assert "gate_override" in schema["properties"]
    assert schema["properties"]["gate_override"]["additionalProperties"] is False
    assert schema["properties"]["gate_override"]["required"] == ["override_reason"]
    assert "oneOf" in schema


def test_authenticated_prepare_approve_rederive_execute_is_one_public_call(public_case):
    path, normalizer, commands_module, monkeypatch = public_case
    presentations = []

    def approve(database_path, *, initial_authorizer, prepared, session_key, now):
        assert database_path == str(path)
        assert session_key == "session-1"
        presentations.append(prepared["canonical_digest"])
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                conn,
                prepared["approval_id"],
                evidence("approval-click-1", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact gate override.",
                now=now,
            )
            conn.commit()
        return {
            "approved": True,
            "approval_reference": "approval-click-1",
            "request_id": prepared["request_id"],
            "canonical_digest": prepared["canonical_digest"],
        }

    monkeypatch.setattr(commands_module, "present_and_record_override_approval", approve)

    result = normalizer.submit(
        "kanban_transition_initiative", _args(), _runtime()
    )

    assert result["result"] == "ACCEPTED", result
    assert result["value"]["result"] == "accepted_with_active_decision"
    assert result["value"]["from_phase"] == "D1"
    assert result["value"]["to_phase"] == "DEV1"
    assert len(presentations) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id='initiative-1'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 1
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "consumed"


def test_denial_cancels_preparation_without_moving_initiative(public_case):
    path, normalizer, commands_module, monkeypatch = public_case

    def deny(database_path, *, initial_authorizer, prepared, session_key, now):
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).cancel(
                conn, prepared["approval_id"], "user denied", now=now
            )
            conn.commit()
        return {
            "approved": False,
            "request_id": prepared["request_id"],
            "canonical_digest": prepared["canonical_digest"],
        }

    monkeypatch.setattr(commands_module, "present_and_record_override_approval", deny)

    result = normalizer.submit(
        "kanban_transition_initiative", _args(), _runtime()
    )

    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id='initiative-1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals"
        ).fetchone()[0] == "cancelled"
        assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 0


def test_exact_replay_returns_saved_execution_without_presenting_again(public_case):
    path, normalizer, commands_module, monkeypatch = public_case
    calls = []

    def approve(database_path, *, initial_authorizer, prepared, session_key, now):
        calls.append(prepared["request_id"])
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                conn,
                prepared["approval_id"],
                evidence("approval-click-1", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact gate override.",
                now=now,
            )
            conn.commit()
        return {"approved": True}

    monkeypatch.setattr(commands_module, "present_and_record_override_approval", approve)
    first = normalizer.submit("kanban_transition_initiative", _args(), _runtime())
    second = normalizer.submit("kanban_transition_initiative", _args(), _runtime())

    assert second == first
    assert calls == ["kanban-gate-override:turn-1"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0] == 2


def test_approved_interruption_resumes_execution_without_second_prompt(public_case):
    path, normalizer, commands_module, monkeypatch = public_case
    calls = []

    def approve_then_interrupt(
        database_path, *, initial_authorizer, prepared, session_key, now
    ):
        calls.append(prepared["request_id"])
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                conn,
                prepared["approval_id"],
                evidence("approval-click-1", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact gate override.",
                now=now,
            )
            conn.commit()
        raise RuntimeError("simulated interruption after durable approval")

    monkeypatch.setattr(
        commands_module,
        "present_and_record_override_approval",
        approve_then_interrupt,
    )
    interrupted = normalizer.submit(
        "kanban_transition_initiative", _args(), _runtime()
    )
    assert interrupted["result"] == "REJECTED"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "approved"
        assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 0

    monkeypatch.setattr(
        commands_module,
        "present_and_record_override_approval",
        lambda *args, **kwargs: pytest.fail("approved replay must not prompt again"),
    )
    resumed = normalizer.submit("kanban_transition_initiative", _args(), _runtime())

    assert resumed["result"] == "ACCEPTED", resumed
    assert calls == ["kanban-gate-override:turn-1"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0] == "consumed"
        assert conn.execute("SELECT COUNT(*) FROM gate_override_records").fetchone()[0] == 1


@pytest.mark.parametrize(
    "args,runtime",
    [
        (_args(phase_close_ref="close-1"), _runtime()),
        (_args(approval_id="approval-1"), _runtime()),
        (_args(), _runtime(turn_id="")),
        (_args(), _runtime(user_task="")),
        (_args(), _runtime(profile="builder-tester")),
    ],
)
def test_untrusted_or_mixed_override_shape_creates_no_proposal(
    public_case, args, runtime
):
    path, normalizer, _, _ = public_case

    result = normalizer.submit("kanban_transition_initiative", args, runtime)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gate_override_proposals").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM write_gate_kanban_approvals"
        ).fetchone()[0] == 0


def test_approved_override_into_dev2_materializes_planned_segment_before_movement(
    commands_module, tmp_path, monkeypatch
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    repository, base_sha = _repository_with_origin_main(tmp_path / "git")
    workspace = __import__(
        f"{commands_module.__package__}.workspace", fromlist=["workspace"]
    )
    controlled_root = (tmp_path / "segment-workspaces").resolve()
    registry = workspace._TrustedRepositoryRegistry(
        (
            workspace._RepositoryRegistration(
                repository_identity="repo-1",
                repository_root=str(repository),
                controlled_worktree_root=str(controlled_root),
            ),
        )
    )
    with sqlite3.connect(path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE initiative_id='initiative-1'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO initiative_segment_projections "
            "(projection_id, projection_version, initiative_card_id, initiative_id, "
            "manifest_path, manifest_sha, content_digest, parsed_segment_definitions, "
            "readiness_refs, validation_result, projected_at) VALUES "
            "('projection-1', 1, ?, 'initiative-1', 'segments.json', ?, ?, ?, ?, "
            "'accepted', 2)",
            (
                card_id,
                "a" * 40,
                "b" * 64,
                json.dumps(
                    [{"segment_id": "S1", "ordinal": 1, "dependency_ids": []}]
                ),
                json.dumps({"S1": "ready:S1"}),
            ),
        )
        conn.execute(
            "INSERT INTO segment_workspaces "
            "(workspace_id, initiative_card_id, initiative_id, segment_id, projection_id, "
            "lifecycle_state, controller_binding_ref, active, created_at, updated_at) "
            "VALUES ('initiative-1:S1', ?, 'initiative-1', 'S1', 'projection-1', "
            "'planned', 'adrian-kanban:workspace-controller:v1', 1, 2, 2)",
            (card_id,),
        )
        conn.execute(
            "INSERT INTO segment_workspace_members "
            "(workspace_id, repository_identity, relative_path, branch, required_base_sha, "
            "observed_head, member_state, observed_at) VALUES "
            "('initiative-1:S1', 'repo-1', 'initiative-1/S1/repo-1', "
            "'initiative-1/S1', NULL, NULL, 'planned', 2)"
        )
        conn.commit()
    _seed_reconciliation(
        path,
        result_id="reconciliation-dev2",
        from_phase="D1",
        to_phase="DEV2",
        to_segment_id="S1",
    )
    monkeypatch.setattr(
        commands_module,
        "mint_current_tailscale_authorizer",
        lambda *, request_id, issued_at, ttl_seconds: evidence(request_id, issued_at),
    )

    def approve(database_path, *, initial_authorizer, prepared, session_key, now):
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                conn,
                prepared["approval_id"],
                evidence("approval-click-dev2", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact DEV2 gate override.",
                now=now,
            )
            conn.commit()
        return {"approved": True}

    monkeypatch.setattr(commands_module, "present_and_record_override_approval", approve)
    result = _normalizer(
        commands_module, path, provider, workspace_registry=registry
    ).submit(
        "kanban_transition_initiative",
        _args(
            to_phase="DEV2",
            to_segment_id="S1",
            reconciliation_ref="reconciliation-dev2",
            idempotency_key="public-override-dev2",
            attempt_id="public-attempt-dev2",
        ),
        _runtime(
            user_task=(
                "Prepare an exact override moving initiative-1 to DEV2/S1 and "
                "wait for my approval before moving it."
            )
        ),
    )

    assert result["result"] == "ACCEPTED", result
    member = controlled_root / "initiative-1" / "S1" / "repo-1"
    assert member.is_dir()
    assert _git(member, "rev-parse", "HEAD") == base_sha
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT to_phase, to_segment_id FROM initiative_transitions "
            "ORDER BY transition_id DESC LIMIT 1"
        ).fetchone() == ("DEV2", "S1")
        assert conn.execute(
            "SELECT required_base_sha, observed_head, member_state "
            "FROM segment_workspace_members WHERE workspace_id='initiative-1:S1'"
        ).fetchone() == (base_sha, base_sha, "materialized")


def test_prepared_but_unapproved_internal_dev2_override_has_no_workspace_effect(
    commands_module, tmp_path, monkeypatch
):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    with sqlite3.connect(path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE initiative_id='initiative-1'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO initiative_segment_projections "
            "(projection_id, projection_version, initiative_card_id, initiative_id, "
            "manifest_path, manifest_sha, content_digest, parsed_segment_definitions, "
            "readiness_refs, validation_result, projected_at) VALUES "
            "('projection-1', 1, ?, 'initiative-1', 'segments.json', ?, ?, ?, ?, "
            "'accepted', 2)",
            (
                card_id,
                "a" * 40,
                "b" * 64,
                json.dumps(
                    [{"segment_id": "S1", "ordinal": 1, "dependency_ids": []}]
                ),
                json.dumps({"S1": "ready:S1"}),
            ),
        )
        conn.commit()
    _seed_reconciliation(
        path,
        result_id="reconciliation-dev2",
        from_phase="D1",
        to_phase="DEV2",
        to_segment_id="S1",
    )
    workspace = __import__(
        f"{commands_module.__package__}.workspace", fromlist=["workspace"]
    )
    registry = workspace._TrustedRepositoryRegistry(())
    normalizer = _normalizer(
        commands_module, path, provider, workspace_registry=registry
    )
    monkeypatch.setattr(
        commands_module,
        "mint_current_tailscale_authorizer",
        lambda *, request_id, issued_at, ttl_seconds: evidence(request_id, issued_at),
    )
    monkeypatch.setattr(
        commands_module,
        "present_and_record_override_approval",
        lambda *args, **kwargs: {"approved": False},
    )
    effects = []
    monkeypatch.setattr(
        commands_module,
        "_materialize_segment_workspace",
        lambda *args, **kwargs: effects.append(kwargs),
    )
    args = _args(
        to_phase="DEV2",
        to_segment_id="S1",
        reconciliation_ref="reconciliation-dev2",
        idempotency_key="prepared-dev2",
        attempt_id="prepared-dev2",
    )
    assert normalizer.submit("kanban_transition_initiative", args, _runtime())[
        "result"
    ] == "REJECTED"

    forged = normalizer._boundary.submit(
        "kanban_transition_initiative",
        attempt_id="forged-execute",
        idempotency_key="forged-execute",
        target="initiative-1",
        derive_expected_version=True,
        session_id="session-1",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile="default",
        turn_id="turn-1",
        api_request_id="api-request-1",
        user_task=_runtime()["user_task"],
        payload={
            "initiative_id": "initiative-1",
            "board": "orchestrator",
            "_approved_gate_override_request_id": "kanban-gate-override:turn-1",
        },
        override_now=1,
    )

    assert forged["result"] == "REJECTED"
    assert effects == []
