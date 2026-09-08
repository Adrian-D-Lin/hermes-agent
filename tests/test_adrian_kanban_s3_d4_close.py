"""D4 closure: ratified v0.28 section 8.3.4 and the D4 skill exit gate."""

import hashlib
import importlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.test_adrian_kanban_s3_orchestration_checkpoints import (
    commands_module,  # noqa: F401 - shared isolated plugin fixture
    _approve,
    _boundary,
    _d4_execution_fixture,
    _seed_accepted_handoff,
    _submit,
)


@pytest.fixture
def close_case(commands_module, tmp_path, monkeypatch):
    path, provider, card_id, execution, proof, _ = _d4_execution_fixture(
        commands_module, tmp_path, monkeypatch
    )
    _approve(path, execution, version=1)
    assert (
        _submit(
            _boundary(
                commands_module, path, provider, phase_result_preparer=lambda *_: proof
            ),
            execution,
            version=1,
        )["result"]
        == "ACCEPTED"
    )
    _seed_accepted_handoff(
        path, card_id, step="D4.5", candidate_id="post-review", sequence=5
    )
    prefix = commands_module.__package__
    lifecycle = importlib.import_module(f"{prefix}.lifecycle")
    contracts = importlib.import_module(f"{prefix}.contracts")
    skills = importlib.import_module(f"{prefix}.skill_bundle")
    module = importlib.import_module(f"{prefix}.phase_d4_evidence")
    baseline = {
        "path": "Canon/design.md",
        "commit": "c" * 40,
        "sha256": hashlib.sha256(b"baseline").hexdigest(),
    }
    count = len(execution["update"]["result"]["item_determinations"])
    metadata = {
        "document_verifications": [
            {"path": baseline["path"], "sha": baseline["commit"], "result": "MATCH"}
        ],
        "source_item_count": count,
        "determination_count": count,
        "development_baseline_ref": baseline["path"] + "@" + baseline["commit"],
    }
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DELETE FROM task_lifecycle_contracts WHERE task_id='task:D4.5'")
        lifecycle.LifecycleContractRepository(conn).attach(
            task_id="task:D4.5",
            snapshot=contracts.expand_contract(
                step="D4.5",
                initiative_id="initiative-1",
                baseline_refs=(metadata["development_baseline_ref"],),
                prior_record_refs=("checkpoint:D4.3", "checkpoint:D4.4"),
                predecessor_ref="checkpoint:D4.4",
            ),
            skill=skills.resolve_skill_binding("D4"),
            created_at=3,
        )
        conn.execute(
            "UPDATE task_candidate_handoffs SET metadata_json=? WHERE candidate_id='post-review'",
            (json.dumps(metadata),),
        )
    update = {
        "result_id": "d4-close",
        "phase": "D4",
        "segment_id": None,
        "iteration": 3,
        "result_kind": "phase_close",
        "contract_id": "adrian-kanban.lifecycle.d4",
        "contract_version": "1",
        "accepted_task_refs": ["post-review"],
        "accepted_checkpoint_refs": ["checkpoint:D4.4"],
        "result": {
            "verified_baseline_ref": baseline,
            "verification_record_ref": "post-review",
            "next_route": "DEV1",
        },
    }
    return SimpleNamespace(
        path=path,
        provider=provider,
        card_id=card_id,
        module=module,
        update=update,
        metadata=metadata,
    )


def _proof(case):
    def published_reader(commit, path):
        assert (commit, path) == ("c" * 40, "Canon/design.md")
        return b"baseline"

    return case.module.prepare_d4_result("initiative-1", case.update, published_reader)


@pytest.mark.parametrize(
    "gap",
    [
        None,
        "failed",
        "unknown",
        "source_count",
        "determination_count",
        "missing_document",
        "duplicate_document",
        "wrong_commit",
        "baseline",
        "unaccepted_review",
        "wrong_checkpoint",
        "unaccepted_execution",
        "unaccepted_approval",
        "wrong_approval_digest",
        "wrong_approval_id",
        "missing_approval_identity",
        "approval_contract",
        "duplicate_post_document",
        "missing_original_item",
        "foreign_original_item",
        "missing_reason",
        "stale_proof",
        "wrong_proof",
    ],
)
def test_d4_close_reconciles_actual_evidence_without_mutation(close_case, gap):
    c = close_case
    proof = _proof(c)
    if gap in {"failed", "unknown"}:
        c.metadata["document_verifications"][0]["result"] = (
            "MISMATCH" if gap == "failed" else "probably okay"
        )
    elif gap in {"source_count", "determination_count"}:
        c.metadata[
            "source_item_count" if gap == "source_count" else "determination_count"
        ] += 1
    elif gap == "missing_document":
        c.metadata["document_verifications"] = []
    elif gap == "duplicate_document":
        c.metadata["document_verifications"] *= 2
    elif gap == "wrong_commit":
        c.metadata["document_verifications"][0]["sha"] = "f" * 40
    elif gap == "baseline":
        c.metadata["development_baseline_ref"] = "other.md@" + "c" * 40
    elif gap == "stale_proof":
        c.update["iteration"] += 1
    elif gap == "wrong_proof":
        proof = object()
    with sqlite3.connect(c.path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE task_candidate_handoffs SET metadata_json=? WHERE candidate_id='post-review'",
            (json.dumps(c.metadata),),
        )
        if gap == "unaccepted_review":
            conn.execute(
                "DELETE FROM task_reviewer_verdicts WHERE candidate_id='post-review'"
            )
        elif gap == "wrong_checkpoint":
            conn.execute(
                "UPDATE initiative_phase_results SET canonical_payload='{}' WHERE result_id='checkpoint:D4.4'"
            )
        elif gap == "unaccepted_execution":
            conn.execute(
                "UPDATE initiative_phase_results SET accepted=0 WHERE result_id='checkpoint:D4.4'"
            )
        elif gap == "unaccepted_approval":
            conn.execute(
                "UPDATE initiative_phase_results SET accepted=0 WHERE result_id='checkpoint:D4.3'"
            )
        elif gap == "approval_contract":
            conn.execute(
                "UPDATE initiative_phase_results SET contract_version='other' WHERE result_id='checkpoint:D4.3'"
            )
        elif gap in {"wrong_approval_digest", "wrong_approval_id"}:
            approval = json.loads(
                conn.execute(
                    "SELECT canonical_payload FROM initiative_phase_results WHERE result_id='checkpoint:D4.3'"
                ).fetchone()[0]
            )
            approval[
                "approved_change_set_digest"
                if gap == "wrong_approval_digest"
                else "write_gate_approval_ref"
            ] = "f" * 64
            conn.execute(
                "UPDATE initiative_phase_results SET canonical_payload=? WHERE result_id='checkpoint:D4.3'",
                (json.dumps(approval),),
            )
        elif gap == "missing_approval_identity":
            for checkpoint_id in ("checkpoint:D4.3", "checkpoint:D4.4"):
                record = json.loads(
                    conn.execute(
                        "SELECT canonical_payload FROM initiative_phase_results WHERE result_id=?",
                        (checkpoint_id,),
                    ).fetchone()[0]
                )
                record.pop("write_gate_approval_ref")
                conn.execute(
                    "UPDATE initiative_phase_results SET canonical_payload=? WHERE result_id=?",
                    (json.dumps(record), checkpoint_id),
                )
        elif gap == "duplicate_post_document":
            execution = json.loads(
                conn.execute(
                    "SELECT canonical_payload FROM initiative_phase_results WHERE result_id='checkpoint:D4.4'"
                ).fetchone()[0]
            )
            execution["post_write_documents"] *= 2
            conn.execute(
                "UPDATE initiative_phase_results SET canonical_payload=? WHERE result_id='checkpoint:D4.4'",
                (json.dumps(execution),),
            )
        elif gap in {
            "missing_original_item",
            "foreign_original_item",
            "missing_reason",
        }:
            row = conn.execute(
                "SELECT canonical_payload FROM initiative_phase_results WHERE result_id='checkpoint:D4.4'"
            ).fetchone()
            execution = json.loads(row[0])
            items = execution["item_determinations"]
            if gap == "missing_original_item":
                items.pop()
            elif gap == "foreign_original_item":
                items[0]["item_id"] = "invented"
            else:
                items[0]["determination"] = "rejected"
            conn.execute(
                "UPDATE initiative_phase_results SET canonical_payload=? WHERE result_id='checkpoint:D4.4'",
                (json.dumps(execution),),
            )
        before = conn.total_changes
        if gap is None:
            c.module.admit_d4_result(
                SimpleNamespace(connection=conn),
                c.card_id,
                "initiative-1",
                c.update,
                proof,
            )
        else:
            with pytest.raises(ValueError):
                c.module.admit_d4_result(
                    SimpleNamespace(connection=conn),
                    c.card_id,
                    "initiative-1",
                    c.update,
                    proof,
                )
        assert conn.total_changes == before


@pytest.mark.parametrize(
    "gap", ["hash", "route", "extra_status", "review_ref", "phase"]
)
def test_d4_close_preparation_rejects_invalid_contract(close_case, gap):
    c = close_case
    if gap == "hash":
        c.update["result"]["verified_baseline_ref"]["sha256"] = "0" * 64
    elif gap == "route":
        c.update["result"]["next_route"] = "DEV2"
    elif gap == "extra_status":
        c.update["result"]["verification_status"] = "verified"
    elif gap == "review_ref":
        c.update["result"]["verification_record_ref"] = ""
    else:
        c.update["phase"] = "D3"
    with pytest.raises(ValueError):
        _proof(c)
