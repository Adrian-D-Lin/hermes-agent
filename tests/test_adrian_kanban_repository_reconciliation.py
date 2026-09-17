"""Repository reconciliation is server-derived and freshness checked."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tests.test_adrian_kanban_s3_initiatives import (
    _database,
    _seed_initiative,
    commands_module,  # noqa: F401
)
from tests.test_adrian_kanban_s3_override_approval import evidence
from writegate.kanban_approvals import KanbanInitiativeApprovalHost


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _repository_case(commands_module, tmp_path, monkeypatch, database_path):
    remote = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    controlled_root = tmp_path / "AI-worktrees"
    evidence_body = b'{"route":"D1-to-PC1","result":"ready"}\n'
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    _git(repository, "config", "user.name", "Reconciliation Test")
    _git(repository, "config", "user.email", "reconciliation@example.invalid")
    (repository / "reconciliation.json").write_bytes(evidence_body)
    _git(repository, "add", "reconciliation.json")
    _git(repository, "commit", "-m", "publish reconciliation evidence")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "main")
    head = _git(repository, "rev-parse", "HEAD")

    branch = "initiative/initiative-1/coordination/repo-1"
    target = controlled_root / "initiative-1" / "coordination" / "repo-1"
    target.parent.mkdir(parents=True)
    _git(repository, "worktree", "add", "-b", branch, str(target), head)
    _git(target, "push", "-u", "origin", branch)

    workspace = importlib.import_module(f"{commands_module.__package__}.workspace")
    coordination = importlib.import_module(
        f"{commands_module.__package__}.coordination_workspace"
    )
    repository_binding = importlib.import_module(
        f"{commands_module.__package__}.repository_binding"
    )
    registry = workspace._TrustedRepositoryRegistry((
        workspace._RepositoryRegistration(
            repository_identity="repo-1",
            repository_root=str(repository.resolve()),
            controlled_worktree_root=str(controlled_root.resolve()),
            github_repository="Adrian-D-Lin/hermes-agent",
            integration_branch="main",
        ),
    ))
    monkeypatch.setattr(
        repository_binding.RepositoryBindingResolver,
        "resolve_integration_head",
        lambda self, allow_offline=True: repository_binding.RepositoryBindingResult(
            head_sha=head,
            state="online",
            remote_ref="refs/remotes/origin/main",
        ),
    )

    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        store = coordination.CoordinationWorkspaceStore(conn)
        planned = store.plan_or_read(
            initiative_id="initiative-1",
            project_id="project-1",
            repository_identity="repo-1",
            controller_binding_ref="tracker:orchestrator:initiative-1",
            planned_at=10,
        )
        conn.execute(
            "UPDATE initiative_coordination_workspaces SET "
            "lifecycle_state='materialized', updated_at=20 WHERE workspace_id = ?",
            (planned["workspace_id"],),
        )
        conn.execute(
            "UPDATE initiative_coordination_workspace_members SET "
            "required_base_sha=?, observed_head=?, member_state='materialized', "
            "observed_at=20 WHERE workspace_id=? AND repository_identity='repo-1'",
            (head, head, planned["workspace_id"]),
        )
        conn.commit()

    return {
        "registry": registry,
        "target": target,
        "head": head,
        "artifact_sha256": hashlib.sha256(evidence_body).hexdigest(),
    }


def _payload(case):
    return {
        "initiative_id": "initiative-1",
        "update_kind": "phase_result",
        "update": {
            "result_id": "reconciliation-d1-pc1",
            "phase": "D1",
            "segment_id": None,
            "iteration": 1,
            "result_kind": "repository_reconciliation",
            "contract_id": "adrian-kanban.lifecycle.d1",
            "contract_version": "1",
            "result": {
                "initiative_id": "initiative-1",
                "board": "orchestrator",
                "previous_transition_id": 1,
                "from_phase": "D1",
                "from_segment_id": None,
                "to_phase": "PC1",
                "to_segment_id": None,
                "canon_route": "adrian-kanban.lifecycle.d1->pc1",
                "exit_gate_ref": {
                    "repository_identity": "repo-1",
                    "path": "reconciliation.json",
                    "commit": case["head"],
                    "sha256": case["artifact_sha256"],
                },
                "verification_result": "accepted",
            },
            "accepted_task_refs": [],
            "accepted_checkpoint_refs": [],
        },
        "idempotency_key": "reconciliation-d1-pc1-key",
    }


def _normalizer(commands_module, path, provider, registry):
    boundary = commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={
            "kanban_update_initiative": commands_module._handle_update_initiative,
        },
        state_resolver=lambda conn, operation, target, payload: conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id=? AND board_slug=?",
            (target, payload["board"]),
        ).fetchone()[0],
        known_profiles={"default"},
        workspace_registry=registry,
    )
    return commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda operation, args, runtime: (
            "orchestrator",
            None,
            "default",
        ),
    )


def _runtime():
    return {
        "session_id": "session-initiative",
        "turn_id": "turn-reconcile-1",
        "api_request_id": "api-reconcile-1",
        "user_task": "Prepare the exact repository reconciliation for approval.",
    }


@pytest.fixture
def reconciliation_case(commands_module, tmp_path, monkeypatch):
    path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    case = _repository_case(commands_module, tmp_path, monkeypatch, path)
    approval = importlib.import_module(
        f"{commands_module.__package__}.initiative_mutation_approval"
    )
    monkeypatch.setattr(
        approval,
        "mint_current_tailscale_authorizer",
        lambda *, request_id, issued_at, ttl_seconds: evidence(request_id, issued_at),
    )
    case.update({
        "path": path,
        "provider": provider,
        "approval": approval,
        "normalizer": _normalizer(commands_module, path, provider, case["registry"]),
    })
    return case


def test_server_proof_is_shown_approved_rechecked_and_persisted(
    reconciliation_case, monkeypatch
):
    case = reconciliation_case
    displayed = []

    def approve(**request):
        displayed.append(json.loads(request["command"]))
        return {
            "approved": True,
            "decision": "once",
            "decision_at": "2026-09-14T01:00:00Z",
        }

    monkeypatch.setattr(case["approval"], "request_write_gate_approval", approve)
    result = case["normalizer"].submit(
        "kanban_update_initiative",
        _payload(case),
        _runtime(),
    )

    assert result["result"] == "ACCEPTED", result
    shown_proof = displayed[0]["update"]["result"]["repository_proof"]
    assert shown_proof["required_condition"] == "exact_integration_head"
    assert shown_proof["members"][0]["clean_including_untracked"] is True
    assert shown_proof["members"][0]["remote_containment"] is True
    with sqlite3.connect(case["path"]) as conn:
        stored = json.loads(
            conn.execute(
                "SELECT canonical_payload FROM initiative_phase_results "
                "WHERE result_id='reconciliation-d1-pc1'"
            ).fetchone()[0]
        )
    assert stored["repository_proof"] == shown_proof


def test_unverifiable_exit_gate_artifact_is_rejected_before_approval(
    reconciliation_case, monkeypatch
):
    case = reconciliation_case
    payload = _payload(case)
    payload["update"]["result"]["exit_gate_ref"]["sha256"] = "0" * 64
    monkeypatch.setattr(
        case["approval"],
        "request_write_gate_approval",
        lambda **_request: pytest.fail(
            "an unverifiable artifact must not be presented for approval"
        ),
    )

    result = case["normalizer"].submit(
        "kanban_update_initiative",
        payload,
        _runtime(),
    )

    assert result["result"] == "REJECTED"
    assert result["failed_checks"][0]["code"] == ("RECONCILIATION_PREPARATION_REJECTED")


def test_repository_change_after_approval_rejects_without_spending_approval(
    reconciliation_case, monkeypatch
):
    case = reconciliation_case

    def approve_then_dirty(**request):
        (case["target"] / "untracked-after-approval.txt").write_text(
            "not reconciled\n", encoding="utf-8"
        )
        return {
            "approved": True,
            "decision": "once",
            "decision_at": "2026-09-14T01:00:00Z",
        }

    monkeypatch.setattr(
        case["approval"], "request_write_gate_approval", approve_then_dirty
    )
    result = case["normalizer"].submit(
        "kanban_update_initiative",
        _payload(case),
        _runtime(),
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(case["path"]) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "approved"
        )


def test_self_declared_acceptance_without_server_proof_is_rejected(
    reconciliation_case, commands_module
):
    case = reconciliation_case
    boundary = case["normalizer"]._boundary
    raw = _payload(case)
    result = boundary.submit(
        "kanban_update_initiative",
        attempt_id="unsupported-manual-attempt",
        idempotency_key="unsupported-manual-key",
        target="initiative-1",
        derive_expected_version=True,
        session_id="session-initiative",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile="default",
        payload=raw | {"board": "orchestrator", "approval_id": "invented"},
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(case["path"]) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0]
            == 0
        )


def test_transition_revalidates_stored_proof_before_presenting_override(
    reconciliation_case, commands_module, monkeypatch
):
    case = reconciliation_case
    monkeypatch.setattr(
        case["approval"],
        "request_write_gate_approval",
        lambda **request: {
            "approved": True,
            "decision": "once",
            "decision_at": "2026-09-14T01:00:00Z",
        },
    )
    created = case["normalizer"].submit(
        "kanban_update_initiative",
        _payload(case),
        _runtime(),
    )
    assert created["result"] == "ACCEPTED", created
    (case["target"] / "late-change.txt").write_text(
        "invalidates reconciliation\n", encoding="utf-8"
    )

    boundary = commands_module._CommandBoundary(
        database_path=str(case["path"]),
        provider=case["provider"],
        handlers={
            "kanban_transition_initiative": commands_module._handle_transition_initiative,
        },
        state_resolver=lambda conn, operation, target, payload: conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id=? AND board_slug=?",
            (target, payload["board"]),
        ).fetchone()[0],
        known_profiles={"default"},
        workspace_registry=case["registry"],
    )
    normalizer = commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda operation, args, runtime: (
            "orchestrator",
            None,
            "default",
        ),
    )
    monkeypatch.setattr(
        commands_module,
        "present_and_record_override_approval",
        lambda *args, **kwargs: pytest.fail(
            "stale repository evidence must reject before approval presentation"
        ),
    )
    result = normalizer.submit(
        "kanban_transition_initiative",
        {
            "initiative_id": "initiative-1",
            "to_phase": "PC1",
            "reconciliation_ref": "reconciliation-d1-pc1",
            "gate_override": {
                "override_reason": "Adrian directed the reviewed initiative to PC1."
            },
            "idempotency_key": "override-d1-pc1-key",
        },
        {
            "session_id": "session-initiative",
            "turn_id": "turn-override-1",
            "api_request_id": "api-override-1",
            "user_task": "Move the reconciled initiative to PC1 after approval.",
        },
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(case["path"]) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM initiative_transitions").fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards "
                "WHERE initiative_id='initiative-1'"
            ).fetchone()[0]
            == 1
        )


def test_fresh_server_proof_supports_the_separately_approved_override(
    reconciliation_case, commands_module, monkeypatch
):
    case = reconciliation_case
    monkeypatch.setattr(
        case["approval"],
        "request_write_gate_approval",
        lambda **request: {
            "approved": True,
            "decision": "once",
            "decision_at": "2026-09-14T01:00:00Z",
        },
    )
    created = case["normalizer"].submit(
        "kanban_update_initiative",
        _payload(case),
        _runtime(),
    )
    assert created["result"] == "ACCEPTED", created

    monkeypatch.setattr(
        commands_module,
        "mint_current_tailscale_authorizer",
        lambda *, request_id, issued_at, ttl_seconds: evidence(request_id, issued_at),
    )

    def approve_override(
        database_path, *, initial_authorizer, prepared, session_key, now
    ):
        with sqlite3.connect(case["path"]) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            KanbanInitiativeApprovalHost(initial_authorizer).approve_distinct(
                conn,
                prepared["approval_id"],
                evidence("approval-click-pc1", now),
                expected_request_id=prepared["request_id"],
                expected_canonical_digest=prepared["canonical_digest"],
                approval_quote="Approve this exact D1 to PC1 override.",
                now=now,
            )
            conn.commit()
        return {"approved": True}

    monkeypatch.setattr(
        commands_module,
        "present_and_record_override_approval",
        approve_override,
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(case["path"]),
        provider=case["provider"],
        handlers={
            "kanban_transition_initiative": commands_module._handle_transition_initiative,
        },
        state_resolver=lambda conn, operation, target, payload: conn.execute(
            "SELECT record_version FROM adrian_kanban_cards "
            "WHERE initiative_id=? AND board_slug=?",
            (target, payload["board"]),
        ).fetchone()[0],
        known_profiles={"default"},
        workspace_registry=case["registry"],
    )
    normalizer = commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda operation, args, runtime: (
            "orchestrator",
            None,
            "default",
        ),
    )
    result = normalizer.submit(
        "kanban_transition_initiative",
        {
            "initiative_id": "initiative-1",
            "to_phase": "PC1",
            "reconciliation_ref": "reconciliation-d1-pc1",
            "gate_override": {
                "override_reason": "Adrian directed the reviewed initiative to PC1."
            },
            "idempotency_key": "override-d1-pc1-key",
        },
        {
            "session_id": "session-initiative",
            "turn_id": "turn-override-1",
            "api_request_id": "api-override-1",
            "user_task": "Move the reconciled initiative to PC1 after approval.",
        },
    )

    assert result["result"] == "ACCEPTED", result
    assert result["value"]["to_phase"] == "PC1"
    with sqlite3.connect(case["path"]) as conn:
        assert (
            conn.execute(
                "SELECT to_phase FROM initiative_transitions "
                "ORDER BY transition_id DESC LIMIT 1"
            ).fetchone()[0]
            == "PC1"
        )
