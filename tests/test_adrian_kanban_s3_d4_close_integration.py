"""D4 close public-boundary persistence and rejection/retry tests."""

import importlib
import json
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_d4_close import close_case, commands_module, _proof  # noqa: F401
from tests.test_adrian_kanban_s3_orchestration_checkpoints import (
    _approve,
    _boundary,
    _submit,
)


@pytest.mark.parametrize(
    "gap",
    [None, "failed_review", "missing_preparer", "wrong_proof", "missing_determination"],
)
def test_close_command_retains_approval_until_evidence_passes(
    commands_module, close_case, gap
):
    c = close_case
    proof = _proof(c)
    payload = {
        "initiative_id": "initiative-1",
        "update_kind": "phase_result",
        "update": c.update,
        "approval_id": "approve-close",
        "board": "orchestrator",
    }
    _approve(c.path, payload, version=2)
    if gap == "failed_review":
        c.metadata["document_verifications"][0]["result"] = "MISMATCH"
        with sqlite3.connect(c.path) as conn:
            conn.execute(
                "UPDATE task_candidate_handoffs SET metadata_json=? WHERE candidate_id='post-review'",
                (json.dumps(c.metadata),),
            )
    elif gap == "missing_determination":
        with sqlite3.connect(c.path) as conn:
            saved = json.loads(
                conn.execute(
                    "SELECT canonical_payload FROM initiative_phase_results WHERE result_id='checkpoint:D4.4'"
                ).fetchone()[0]
            )
            saved["item_determinations"].pop()
            conn.execute(
                "UPDATE initiative_phase_results SET canonical_payload=? WHERE result_id='checkpoint:D4.4'",
                (json.dumps(saved),),
            )
    prepared = (
        None
        if gap == "missing_preparer"
        else lambda *_: object() if gap == "wrong_proof" else proof
    )
    boundary = _boundary(
        commands_module, c.path, c.provider, phase_result_preparer=prepared
    )
    response = _submit(boundary, payload, version=2)
    assert response["result"] == ("ACCEPTED" if gap is None else "REJECTED"), response
    with sqlite3.connect(c.path) as conn:
        state = conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id='approve-close'"
        ).fetchone()[0]
        count = conn.execute(
            "SELECT COUNT(*) FROM initiative_phase_results WHERE result_id='d4-close'"
        ).fetchone()[0]
        version = conn.execute(
            "SELECT record_version FROM adrian_kanban_cards WHERE card_type='initiative'"
        ).fetchone()[0]
        assert (state, count, version) == (
            ("consumed", 1, 3) if gap is None else ("approved", 0, 2)
        )
        if gap is None:
            conn.row_factory = sqlite3.Row
            module = importlib.import_module(
                f"{commands_module.__package__}.projections"
            )
            shown = module.show_projection(conn, None, "orchestrator", "initiative-1")
            result = next(
                row for row in shown["phase_results"] if row["result_id"] == "d4-close"
            )
            assert result["canonical_payload"] == c.update["result"]
    if gap is None:
        assert _submit(boundary, payload, version=2) == response
    if gap == "failed_review":
        # A corrected report is a fresh immutable candidate, not an edit to the
        # old report. The changed close payload requires its own exact approval.
        c.metadata["document_verifications"][0]["result"] = "MATCH"
        with sqlite3.connect(c.path) as conn:
            conn.execute(
                "INSERT INTO task_candidate_handoffs SELECT 'post-review-fixed', task_card_id, task_id, execution_run_id+100, reviewer, summary, ?, submitted_by, created_at+1 FROM task_candidate_handoffs WHERE candidate_id='post-review'",
                (json.dumps(c.metadata),),
            )
            conn.execute(
                "INSERT INTO task_reviewer_verdicts SELECT 'verdict-fixed', task_card_id, task_id, 'post-review-fixed', review_run_id+100, reviewer, verdict, summary, created_at+1 FROM task_reviewer_verdicts WHERE candidate_id='post-review'"
            )
        c.update["result"]["verification_record_ref"] = "post-review-fixed"
        c.update["accepted_task_refs"] = ["post-review-fixed"]
        payload["approval_id"] = "approve-close-fixed"
        proof = _proof(c)
        _approve(c.path, payload, version=2)
        result = _submit(boundary, payload, version=2)
        assert result["result"] == "ACCEPTED", result
