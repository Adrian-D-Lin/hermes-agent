"""Durable records required by atomic Kanban gate-override execution."""

from __future__ import annotations

import sqlite3

import pytest

from tests.test_adrian_kanban_s3_initiatives import (
    _database,
    _seed_initiative,
    commands_module,  # noqa: F401
)


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_override_execution_tables_have_the_required_durable_fields(
    commands_module, tmp_path, monkeypatch
):
    path, _ = _database(tmp_path, monkeypatch, commands_module)
    with sqlite3.connect(path) as conn:
        assert _columns(conn, "gate_override_records") >= {
            "request_id",
            "approval_id",
            "mutation_id",
            "initiative_card_id",
            "initiative_id",
            "from_phase",
            "from_segment_id",
            "to_phase",
            "to_segment_id",
            "override_authority_ref",
            "override_reason",
            "unsatisfied_gates",
            "movement_basis",
            "result",
            "actor_evidence",
            "canonical_payload",
            "created_at",
        }
        assert _columns(conn, "initiative_active_decisions") >= {
            "decision_id",
            "initiative_card_id",
            "initiative_id",
            "decision_kind",
            "authority_ref",
            "canonical_payload",
            "active",
            "created_at",
        }
        assert _columns(conn, "initiative_generated_comments") >= {
            "comment_id",
            "initiative_card_id",
            "initiative_id",
            "source_kind",
            "source_ref",
            "body",
            "created_at",
        }


def test_override_execution_schema_rejects_wrong_constant_meaning(
    commands_module, tmp_path, monkeypatch
):
    path, _ = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(path)
    with sqlite3.connect(path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE initiative_id='initiative-1'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO gate_override_records "
                "(request_id,approval_id,mutation_id,initiative_card_id,initiative_id,"
                "from_phase,to_phase,override_authority_ref,override_reason,"
                "unsatisfied_gates,movement_basis,result,actor_evidence,canonical_payload,created_at) "
                "VALUES ('r','a','m',?,'initiative-1','D1','D2','r','why','[]',"
                "'model_assessment','accepted_with_active_decision','{}','{}',1)",
                (card_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO initiative_active_decisions "
                "(decision_id,initiative_card_id,initiative_id,decision_kind,"
                "authority_ref,canonical_payload,active,created_at) "
                "VALUES ('d',?,'initiative-1','reviewer_verdict','r','{}',1,1)",
                (card_id,),
            )
