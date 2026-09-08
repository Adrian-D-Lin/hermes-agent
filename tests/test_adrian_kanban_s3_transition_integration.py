"""Real D4 close-to-transition boundary, approval atomicity and stale evidence."""

import json
import sqlite3

import pytest

from tests.test_adrian_kanban_s3_d4_close import close_case, commands_module, _proof  # noqa: F401
from tests.test_adrian_kanban_s3_orchestration_checkpoints import (
    _approve as approve_close,
    _boundary,
    _submit as submit_close,
)
from tests.test_adrian_kanban_s3_initiatives import (
    _approve,
    _submit,
    _seed_reconciliation,
)


@pytest.mark.parametrize(
    "gap",
    [
        None,
        "missing_ref",
        "unaccepted_close",
        "stale_visit",
        "unaccepted_review",
        "missing_checkpoint",
    ],
)
def test_transition_requires_successful_current_d4_close(
    commands_module, close_case, gap
):
    c = close_case
    close_payload = {
        "initiative_id": "initiative-1",
        "update_kind": "phase_result",
        "update": c.update,
        "approval_id": "approval-close",
        "board": "orchestrator",
    }
    approve_close(c.path, close_payload, version=2)
    close_boundary = _boundary(
        commands_module, c.path, c.provider, phase_result_preparer=lambda *_: _proof(c)
    )
    closed = submit_close(close_boundary, close_payload, version=2)
    assert closed["result"] == "ACCEPTED", closed
    with sqlite3.connect(c.path) as conn:
        actor = json.loads(
            conn.execute(
                "SELECT actor_evidence FROM initiative_phase_results WHERE result_id='d4-close'"
            ).fetchone()[0]
        )
        predecessor = conn.execute(
            "SELECT MAX(transition_id) FROM initiative_transitions"
        ).fetchone()[0]
        assert actor["source_transition_id"] == predecessor
        assert actor["actor_profile"] == "default"
    _seed_reconciliation(
        c.path, result_id="reconcile-close", from_phase="D4", to_phase="DEV1"
    )
    transition = {
        "initiative_id": "initiative-1",
        "to_phase": "DEV1",
        "reconciliation_ref": "reconcile-close",
        "phase_close_ref": "d4-close",
        "approval_id": "approval-transition",
        "board": "orchestrator",
    }
    if gap == "missing_ref":
        del transition["phase_close_ref"]
    with sqlite3.connect(c.path) as conn:
        if gap == "unaccepted_close":
            conn.execute(
                "UPDATE initiative_phase_results SET accepted=0 WHERE result_id='d4-close'"
            )
        elif gap == "stale_visit":
            conn.execute(
                "UPDATE initiative_phase_results SET actor_evidence=? WHERE result_id='d4-close'",
                (
                    json.dumps({
                        "source_transition_id": predecessor + 1,
                        "actor_profile": "default",
                    }),
                ),
            )
        elif gap == "unaccepted_review":
            conn.execute(
                "DELETE FROM task_reviewer_verdicts WHERE candidate_id='post-review'"
            )
        elif gap == "missing_checkpoint":
            conn.execute(
                "UPDATE initiative_phase_results SET accepted=0 WHERE result_id='checkpoint:D4.4'"
            )
    _approve(
        c.path,
        approval_id="approval-transition",
        attempt_id="attempt-transition",
        operation="kanban_transition_initiative",
        target="initiative-1",
        expected_version=3,
        payload=transition,
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(c.path),
        provider=c.provider,
        handlers={
            "kanban_transition_initiative": commands_module._handle_transition_initiative
        },
    )

    def submit():
        return _submit(
            boundary,
            "kanban_transition_initiative",
            attempt_id="attempt-transition",
            key="transition-key",
            target="initiative-1",
            version=3,
            payload=transition,
        )

    response = submit()
    assert response["result"] == ("ACCEPTED" if gap is None else "REJECTED"), response
    with sqlite3.connect(c.path) as conn:
        state = conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id='approval-transition'"
        ).fetchone()[0]
        phase, trigger, saved = conn.execute(
            "SELECT to_phase, trigger, canonical_payload FROM initiative_transitions ORDER BY transition_id DESC LIMIT 1"
        ).fetchone()
        version = conn.execute(
            "SELECT record_version FROM adrian_kanban_cards WHERE card_type='initiative'"
        ).fetchone()[0]
        assert (state, phase, version) == (
            ("consumed", "DEV1", 4) if gap is None else ("approved", "D4", 3)
        )
        if gap is None:
            assert trigger == "model_assessment"
            assert json.loads(saved)["phase_close_ref"] == "d4-close"
        else:
            assert "phase_close_ref" in str(response), response
    if gap is None:
        assert submit() == response


def test_transition_tool_exposes_required_close_reference(commands_module):
    parameters = commands_module.TOOL_SCHEMAS["kanban_transition_initiative"][
        "parameters"
    ]
    assert "phase_close_ref" in parameters["required"]
    assert parameters["properties"]["phase_close_ref"]["type"] == "string"
