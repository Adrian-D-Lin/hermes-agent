"""Failed D4.5 verification cannot become successful governed completion."""

import importlib
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_commands import (
    commands_module,
    _runtime_modules,
    _plugin_database,  # noqa: F401
    _seed_governed_running_task,
    _submit_governed,
    _claim_governed_review,
)


@pytest.mark.parametrize(
    "outcome",
    [
        "MATCH",
        "MISMATCH",
        "FAILED",
        "NOT_RUN",
        "probably okay",
        "empty",
        "count_mismatch",
    ],
)
def test_d4_5_unsuccessful_verification_cannot_complete(
    commands_module, tmp_path, monkeypatch, outcome
):
    modules = _runtime_modules(commands_module)
    path, provider = _plugin_database(tmp_path, monkeypatch, modules["provider"])
    task_id = "post-write-review"
    _seed_governed_running_task(
        path, task_id=task_id, execution_profile="test-authority-reviewer"
    )
    prefix = commands_module.__package__
    contracts = importlib.import_module(f"{prefix}.contracts")
    lifecycle = importlib.import_module(f"{prefix}.lifecycle")
    skills = importlib.import_module(f"{prefix}.skill_bundle")
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        lifecycle.LifecycleContractRepository(conn).attach(
            task_id=task_id,
            snapshot=contracts.expand_contract(
                step="D4.5",
                initiative_id=f"initiative-{task_id}",
                baseline_refs=("Canon/policy.md@" + "a" * 40,),
                prior_record_refs=("checkpoint:D4.3", "checkpoint:D4.4"),
                predecessor_ref="checkpoint:D4.4",
            ),
            skill=skills.resolve_skill_binding("D4"),
            created_at=1,
        )
    boundary = commands_module._CommandBoundary(
        database_path=str(path),
        provider=provider,
        handlers={
            "kanban_request_review": commands_module._handle_request_review,
            "kanban_complete": commands_module._handle_complete,
        },
        known_profiles={"test-authority-reviewer", "independent-reviewer"},
    )
    report = {
        "artifact_ref": "review.md",
        "tests_passed": outcome == "MATCH",
        "document_verifications": [
            {"path": "Canon/policy.md", "sha": "a" * 40, "result": outcome}
        ],
        "source_item_count": 0,
        "determination_count": 0,
        "development_baseline_ref": "Canon/policy.md@" + "a" * 40,
    }
    if outcome == "empty":
        report["document_verifications"] = []
    elif outcome == "count_mismatch":
        report["document_verifications"][0]["result"] = "MATCH"
        report["source_item_count"] = 1
    submitted = _submit_governed(
        boundary,
        operation="kanban_request_review",
        task_id=task_id,
        actor_profile="test-authority-reviewer",
        expected_version=0,
        payload={"summary": "Post-write result", "metadata": report},
        suffix="d4-submit",
    )
    assert submitted["result"] == "ACCEPTED", submitted
    _claim_governed_review(path, task_id)
    result = _submit_governed(
        boundary,
        operation="kanban_complete",
        task_id=task_id,
        actor_profile="independent-reviewer",
        expected_version=1,
        payload={"result": "accepted", "summary": "Reviewed"},
        suffix="d4-complete",
    )
    assert result["result"] == ("ACCEPTED" if outcome == "MATCH" else "REJECTED"), (
        result
    )
    if outcome != "MATCH":
        diagnostic = str(result)
        assert "document_verifications" in diagnostic, result
        assert "MATCH" in diagnostic, result
        assert "request-changes" in diagnostic, result
    with sqlite3.connect(path) as conn:
        status = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        count = conn.execute(
            "SELECT COUNT(*) FROM task_reviewer_verdicts WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        assert (status, count) == (
            ("done", 1) if outcome == "MATCH" else ("running", 0)
        )
