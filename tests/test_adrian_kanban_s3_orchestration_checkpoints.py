"""Orchestration-checkpoint tests derived from Kanban design v0.28 section 8.3."""

from __future__ import annotations

import hashlib
import dataclasses
import importlib
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from writegate.kanban_approvals import create_kanban_approval_schema


@pytest.fixture(scope="module")
def commands_module():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_checkpoints"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    module = importlib.import_module(f"{package_name}.commands")
    yield module
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


def _database(tmp_path, monkeypatch, commands_module):
    provider_module = importlib.import_module(f"{commands_module.__package__}.provider")
    schema_module = importlib.import_module(f"{commands_module.__package__}.schema")
    database_path = (tmp_path / "kanban.sqlite3").resolve()
    with kb.connect_closing(database_path) as conn:
        schema_module.create_schema(conn)
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
    provider = provider_module.AdrianKanbanAuthorityProvider(str(database_path))
    provider_module.register_provider(provider)
    return database_path, provider


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
            "'session-checkpoint', ?, ?, ?, ?)",
            (
                payload["approval_id"],
                f"attempt:{payload['approval_id']}",
                version,
                _approval_digest(payload),
                '{"peer_identity":"adrian@tailnet"}',
                now - 2,
                now - 1,
                now + 600,
                "approved-on-second-action",
            ),
        )
        conn.commit()


def _seed_initiative(database_path: Path, phase: str) -> int:
    with sqlite3.connect(database_path) as conn:
        conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')")
        card_id = conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, "
            "board_slug, record_version) VALUES "
            "('initiative', 'initiative-1', NULL, 'Initiative', 'body', 1, "
            "'orchestrator', 0)"
        ).lastrowid
        conn.execute(
            "INSERT INTO initiative_transitions "
            "(initiative_card_id, initiative_id, previous_transition_id, "
            "transition_id, from_phase, from_segment_id, to_phase, "
            "to_segment_id, trigger, actor_evidence, canonical_payload, "
            "created_at) VALUES (?, 'initiative-1', NULL, 1, NULL, NULL, ?, "
            "NULL, 'initialization', '{}', '{}', 1)",
            (card_id, phase),
        )
        conn.commit()
        return card_id


def _seed_accepted_handoff(
    database_path: Path,
    card_id: int,
    *,
    step: str,
    candidate_id: str,
    sequence: int,
) -> None:
    task_id = f"task:{step}"
    task_card_id: int
    with sqlite3.connect(database_path) as conn:
        task_card_id = conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, "
            "board_slug, record_version) VALUES "
            "('task', 'initiative-1', ?, ?, '', 1, 'orchestrator', 0)",
            (task_id, step),
        ).lastrowid
        conn.execute(
            "INSERT INTO task_lifecycle_contracts "
            "(contract_id, contract_version, step, task_card_id, task_id, "
            "initiative_card_id, initiative_id, segment_id, workspace_id, "
            "execution_profile, canonical_contract_payload, registry_hash, "
            "skill_id, skill_version, skill_hash, created_at) VALUES "
            "(?, '1', ?, ?, ?, ?, 'initiative-1', NULL, NULL, "
            "'independent-reviewer', '{}', ?, ?, '1', ?, 1)",
            (
                f"adrian-kanban.lifecycle.{step.split('.')[0].lower()}",
                step,
                task_card_id,
                task_id,
                card_id,
                "r" * 64,
                f"skill:{step}",
                "s" * 64,
            ),
        )
        conn.execute(
            "INSERT INTO task_candidate_handoffs "
            "(candidate_id, task_card_id, task_id, execution_run_id, reviewer, "
            "summary, metadata_json, submitted_by, created_at) VALUES "
            "(?, ?, ?, ?, 'test-authority-reviewer', '', '{}', "
            "'independent-reviewer', 1)",
            (candidate_id, task_card_id, task_id, sequence),
        )
        conn.execute(
            "INSERT INTO task_reviewer_verdicts "
            "(verdict_id, task_card_id, task_id, candidate_id, review_run_id, "
            "reviewer, verdict, summary, created_at) VALUES "
            "(?, ?, ?, ?, ?, 'test-authority-reviewer', 'accepted', '', 1)",
            (
                f"verdict:{candidate_id}",
                task_card_id,
                task_id,
                candidate_id,
                1000 + sequence,
            ),
        )
        conn.commit()
    if step in {"D4.1", "D4.2"}:
        prefix = "s3_adrian_kanban_checkpoints"
        lifecycle = importlib.import_module(f"{prefix}.lifecycle")
        contracts = importlib.import_module(f"{prefix}.contracts")
        skills = importlib.import_module(f"{prefix}.skill_bundle")
        with sqlite3.connect(database_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "DELETE FROM task_lifecycle_contracts WHERE task_id=?", (task_id,)
            )
            lifecycle.LifecycleContractRepository(conn).attach(
                task_id=task_id,
                snapshot=contracts.expand_contract(
                    step=step,
                    initiative_id="initiative-1",
                    baseline_refs=("Canon/design.md",),
                    prior_record_refs=("candidate:D4.1",),
                    predecessor_ref="candidate:D4.1" if step == "D4.2" else None,
                ),
                skill=skills.resolve_skill_binding("D4"),
                created_at=1,
            )
            metadata = (
                {"edit_set": _d4_checkpoint_edits()}
                if step == "D4.1"
                else {
                    "edit_findings": [
                        {
                            "edit_ref": "candidate:D4.1#/edit_set/0",
                            "consistency": "consistent",
                            "collateral_changes": [],
                        }
                    ],
                    "conclusion": "verified",
                    "item_count": 1,
                }
            )
            conn.execute(
                "UPDATE task_candidate_handoffs SET metadata_json=? WHERE candidate_id=?",
                (json.dumps(metadata), candidate_id),
            )


def _d4_checkpoint_edits():
    return [
        {
            "document_ref": "Canon/design.md",
            "current_state_ref": "Canon/design.md@" + "b" * 40,
            "edit": "exact approved change",
        }
    ]


def _d2_evidence_fixture(
    commands_module, tmp_path, monkeypatch, *, baseline="2-design/draft.md"
):
    database_path, _ = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D2")
    _seed_accepted_handoff(
        database_path, card_id, step="D2", candidate_id="review-candidate", sequence=1
    )
    prefix = commands_module.__package__
    lifecycle = importlib.import_module(f"{prefix}.lifecycle")
    contracts = importlib.import_module(f"{prefix}.contracts")
    skills = importlib.import_module(f"{prefix}.skill_bundle")
    artifacts = importlib.import_module(f"{prefix}.published_artifact")
    draft = artifacts.VerifiedArtifact("2-design/draft.md", "a" * 40, "b" * 64)
    metadata = {
        "review_pass_log": ["Complete eight-angle review"],
        "angle_coverage": [
            "principle_alignment",
            "design_integration",
            "documentation_silence",
            "contradiction",
            "completeness_internal_coherence",
            "dependencies_downstream_impact",
            "new_principle_candidate",
            "alternative_design",
        ],
        "findings": [
            {
                "citation": "draft section1",
                "materiality": "material",
                "impact": "delivery",
                "route": "D1",
            }
        ]
        * 2,
        "conclusion": "NOT_DRY",
    }
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DELETE FROM task_lifecycle_contracts WHERE task_id='task:D2'")
        lifecycle.LifecycleContractRepository(conn).attach(
            task_id="task:D2",
            snapshot=contracts.expand_contract(
                step="D2",
                initiative_id="initiative-1",
                baseline_refs=(baseline,),
                governing_source_refs=("Canon/policy.md",),
            ),
            skill=skills.resolve_skill_binding("D2"),
            created_at=1,
        )
        task_card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE task_id='task:D2'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO task_input_manifests VALUES (?,'task:D2','{}',1,1)",
            (task_card_id,),
        )
        conn.execute(
            "INSERT INTO task_input_entries VALUES (?,'task:D2',?,?,'git_commit',?,'Review this draft')",
            (task_card_id, draft.path, draft.sha256, draft.commit),
        )
        conn.execute(
            "UPDATE task_candidate_handoffs SET metadata_json=? WHERE candidate_id='review-candidate'",
            (json.dumps(metadata),),
        )
    return database_path, card_id, draft


def _d4_evidence_fixture(commands_module, tmp_path, monkeypatch, step):
    database_path, _ = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    _seed_accepted_handoff(
        database_path, card_id, step=step, candidate_id="d4-candidate", sequence=1
    )
    prefix = commands_module.__package__
    lifecycle = importlib.import_module(f"{prefix}.lifecycle")
    contracts = importlib.import_module(f"{prefix}.contracts")
    skills = importlib.import_module(f"{prefix}.skill_bundle")
    metadata = {
        "D4.1": {
            "edit_set": [
                {
                    "document_ref": "Canon/policy.md",
                    "current_state_ref": "Canon/policy.md@" + "a" * 40,
                    "edit": "exact change",
                }
            ]
            * 2
        },
        "D4.2": {
            "edit_findings": [
                {
                    "edit_ref": "author#/edit_set/0",
                    "consistency": "consistent",
                    "collateral_changes": [],
                }
            ]
            * 2,
            "conclusion": "verified",
            "item_count": 2,
        },
        "D4.5": {
            "document_verifications": [
                {"path": "Canon/policy.md", "sha": "a" * 40, "result": "matches"}
            ],
            "source_item_count": 4,
            "determination_count": 4,
            "development_baseline_ref": "Canon/policy.md",
        },
    }[step]
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "DELETE FROM task_lifecycle_contracts WHERE task_id=?", (f"task:{step}",)
        )
        lifecycle.LifecycleContractRepository(conn).attach(
            task_id=f"task:{step}",
            snapshot=contracts.expand_contract(
                step=step,
                initiative_id="initiative-1",
                baseline_refs=("Canon/policy.md",),
                prior_record_refs=("prior-accepted-record",),
                predecessor_ref=None if step == "D4.1" else "prior-accepted-record",
            ),
            skill=skills.resolve_skill_binding("D4"),
            created_at=1,
        )
        conn.execute(
            "UPDATE task_candidate_handoffs SET metadata_json=?",
            (json.dumps(metadata),),
        )
    module = importlib.import_module(f"{prefix}.phase_d4_evidence")
    return database_path, card_id, module, metadata


@pytest.mark.parametrize("step", ["D4.1", "D4.2", "D4.5"])
def test_d4_inventory_preserves_distinct_equal_rows_and_reads_only(
    commands_module, tmp_path, monkeypatch, step
):
    database_path, card_id, module, metadata = _d4_evidence_fixture(
        commands_module, tmp_path, monkeypatch, step
    )
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        before = conn.total_changes
        evidence = module.load_d4_evidence(
            conn, card_id, "initiative-1", "d4-candidate", step
        )
        assert evidence.metadata == metadata
        assert evidence.step == step
        field = "edit_set" if step == "D4.1" else "edit_findings"
        assert evidence.item_refs == (
            ()
            if step == "D4.5"
            else tuple(f"d4-candidate#/{field}/{i}" for i in range(2))
        )
        assert conn.total_changes == before


@pytest.mark.parametrize(
    "gap",
    [
        "wrong_step",
        "wrong_initiative",
        "unaccepted",
        "bad_metadata",
        "false_count",
        "wrong_profile",
    ],
)
def test_d4_inventory_rejects_unproven_candidate_or_false_count(
    commands_module, tmp_path, monkeypatch, gap
):
    database_path, card_id, module, metadata = _d4_evidence_fixture(
        commands_module, tmp_path, monkeypatch, "D4.2"
    )
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        if gap == "unaccepted":
            conn.execute("DELETE FROM task_reviewer_verdicts")
        elif gap == "bad_metadata":
            conn.execute("UPDATE task_candidate_handoffs SET metadata_json='not-json'")
        elif gap == "false_count":
            metadata["item_count"] = 3
            conn.execute(
                "UPDATE task_candidate_handoffs SET metadata_json=?",
                (json.dumps(metadata),),
            )
        elif gap == "wrong_profile":
            conn.execute(
                "UPDATE task_lifecycle_contracts SET execution_profile='builder-tester'"
            )
        with pytest.raises(ValueError):
            module.load_d4_evidence(
                conn,
                card_id,
                "other" if gap == "wrong_initiative" else "initiative-1",
                "d4-candidate",
                "D4.1" if gap == "wrong_step" else "D4.2",
            )


@pytest.mark.parametrize(
    "gap",
    [
        None,
        "omission",
        "foreign_edit",
        "predecessor",
        "prior_record",
        "digest",
        "newline",
        "none",
        "list",
        "integer",
        "duplicate_review",
    ],
)
def test_d4_source_pair_binds_complete_review_to_exact_approved_edits(
    commands_module, tmp_path, monkeypatch, gap
):
    database_path, card_id, module, author = _d4_evidence_fixture(
        commands_module, tmp_path, monkeypatch, "D4.1"
    )
    _seed_accepted_handoff(
        database_path, card_id, step="D4.2", candidate_id="verifier", sequence=2
    )
    prefix = commands_module.__package__
    lifecycle = importlib.import_module(f"{prefix}.lifecycle")
    contracts = importlib.import_module(f"{prefix}.contracts")
    skills = importlib.import_module(f"{prefix}.skill_bundle")
    findings = [
        {
            "edit_ref": f"d4-candidate#/edit_set/{i}",
            "consistency": "consistent",
            "collateral_changes": [],
        }
        for i in range(2)
    ]
    if gap == "omission":
        findings.pop()
    elif gap == "foreign_edit":
        findings[1]["edit_ref"] = "foreign#/edit_set/1"
    elif gap == "duplicate_review":
        findings.append(dict(findings[0]))
    metadata = {
        "edit_findings": findings,
        "conclusion": "verified",
        "item_count": len(findings),
    }
    digest = hashlib.sha256(
        json.dumps(
            author["edit_set"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    digest = {
        "digest": "0" * 64,
        "newline": digest + "\n",
        "none": None,
        "list": [],
        "integer": 123,
    }.get(gap, digest)
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DELETE FROM task_lifecycle_contracts WHERE task_id='task:D4.2'")
        lifecycle.LifecycleContractRepository(conn).attach(
            task_id="task:D4.2",
            snapshot=contracts.expand_contract(
                step="D4.2",
                initiative_id="initiative-1",
                baseline_refs=("Canon/policy.md",),
                prior_record_refs=(
                    "other" if gap == "prior_record" else "d4-candidate",
                ),
                predecessor_ref="other" if gap == "predecessor" else "d4-candidate",
            ),
            skill=skills.resolve_skill_binding("D4"),
            created_at=2,
        )
        conn.execute(
            "UPDATE task_candidate_handoffs SET metadata_json=? WHERE candidate_id='verifier'",
            (json.dumps(metadata),),
        )
        before = conn.total_changes
        if gap in {None, "duplicate_review"}:
            source, review = module.validate_d4_source_pair(
                conn, card_id, "initiative-1", "d4-candidate", "verifier", digest
            )
            assert source.metadata == author
            assert review.metadata == metadata
            assert len(source.item_refs) == 2
            assert len(review.item_refs) == len(findings)
        else:
            with pytest.raises(ValueError):
                module.validate_d4_source_pair(
                    conn, card_id, "initiative-1", "d4-candidate", "verifier", digest
                )
        assert conn.total_changes == before


def test_d2_inventory_keeps_equal_content_findings_distinct_and_is_read_only(
    commands_module, tmp_path, monkeypatch
):
    database_path, card_id, draft = _d2_evidence_fixture(
        commands_module, tmp_path, monkeypatch
    )
    module = importlib.import_module(f"{commands_module.__package__}.phase_d2_evidence")
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        before = conn.total_changes
        result = module.load_d2_review(
            conn, card_id, "initiative-1", "review-candidate", draft
        )
        assert result.finding_refs == (
            "review-candidate#/findings/0",
            "review-candidate#/findings/1",
        )
        assert result.conclusion == "NOT_DRY"
        assert result.candidate_ref == "review-candidate"
        assert conn.total_changes == before


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE task_input_entries SET workspace_path='different.md'",
        "UPDATE task_input_entries SET source_locator='different-commit'",
        "UPDATE task_input_entries SET sha256='different-hash'",
        "UPDATE task_input_entries SET source_kind='snapshot_attachment'",
        "UPDATE task_input_manifests SET declared_inputs_accessible=0",
        "DELETE FROM task_reviewer_verdicts",
        "UPDATE task_candidate_handoffs SET metadata_json='{}'",
        "UPDATE task_candidate_handoffs SET metadata_json='not json'",
        "UPDATE task_lifecycle_contracts SET execution_profile='default'",
    ],
)
def test_d2_inventory_rejects_wrong_or_unaccepted_evidence(
    commands_module, tmp_path, monkeypatch, mutation
):
    database_path, card_id, draft = _d2_evidence_fixture(
        commands_module, tmp_path, monkeypatch
    )
    module = importlib.import_module(f"{commands_module.__package__}.phase_d2_evidence")
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(mutation)
        before = conn.total_changes
        with pytest.raises(ValueError):
            module.load_d2_review(
                conn, card_id, "initiative-1", "review-candidate", draft
            )
        assert conn.total_changes == before


def test_d2_inventory_requires_explicit_baseline_not_merely_available_input(
    commands_module, tmp_path, monkeypatch
):
    database_path, card_id, draft = _d2_evidence_fixture(
        commands_module, tmp_path, monkeypatch, baseline="other-baseline.md"
    )
    module = importlib.import_module(f"{commands_module.__package__}.phase_d2_evidence")
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ValueError, match="baseline"):
            module.load_d2_review(
                conn, card_id, "initiative-1", "review-candidate", draft
            )


def test_d2_inventory_rejects_cross_initiative_evidence(
    commands_module, tmp_path, monkeypatch
):
    database_path, card_id, draft = _d2_evidence_fixture(
        commands_module, tmp_path, monkeypatch
    )
    module = importlib.import_module(f"{commands_module.__package__}.phase_d2_evidence")
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ValueError):
            module.load_d2_review(conn, card_id, "other", "review-candidate", draft)
        with pytest.raises(ValueError):
            module.load_d2_review(
                conn, card_id + 100, "initiative-1", "review-candidate", draft
            )


def test_d2_history_retains_reviews_not_on_current_baseline(
    commands_module, tmp_path, monkeypatch
):
    database_path, card_id, _ = _d2_evidence_fixture(
        commands_module, tmp_path, monkeypatch, baseline="older-draft.md"
    )
    module = importlib.import_module(f"{commands_module.__package__}.phase_d2_evidence")
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        before = conn.total_changes
        history = module.load_d2_history(conn, card_id, "initiative-1")
        assert [item.candidate_ref for item in history] == ["review-candidate"]
        assert history[0].finding_refs == (
            "review-candidate#/findings/0",
            "review-candidate#/findings/1",
        )
        assert module.load_d2_history(conn, card_id, "other") == ()
        assert conn.total_changes == before


def test_d2_empty_findings_remains_an_accepted_review(
    commands_module, tmp_path, monkeypatch
):
    database_path, card_id, draft = _d2_evidence_fixture(
        commands_module, tmp_path, monkeypatch
    )
    module = importlib.import_module(f"{commands_module.__package__}.phase_d2_evidence")
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        metadata = json.loads(
            conn.execute(
                "SELECT metadata_json FROM task_candidate_handoffs"
            ).fetchone()[0]
        )
        metadata.update(findings=[], conclusion="DRY")
        conn.execute(
            "UPDATE task_candidate_handoffs SET metadata_json=?",
            (json.dumps(metadata),),
        )
        review = module.load_d2_review(
            conn, card_id, "initiative-1", "review-candidate", draft
        )
        assert review.finding_refs == ()
        assert review.conclusion == "DRY"
        assert len(module.load_d2_history(conn, card_id, "initiative-1")) == 1


def _d2_close_fixture(commands_module, tmp_path, monkeypatch):
    database_path, card_id, draft = _d2_evidence_fixture(
        commands_module, tmp_path, monkeypatch
    )
    module = importlib.import_module(f"{commands_module.__package__}.phase_d2")
    digest = hashlib.sha256(b"draft").hexdigest()
    with sqlite3.connect(database_path) as conn:
        conn.execute("UPDATE task_input_entries SET sha256=?", (digest,))
    update = {
        "result_id": "d2-close",
        "phase": "D2",
        "segment_id": None,
        "iteration": 1,
        "result_kind": "phase_close",
        "contract_id": "adrian-kanban.lifecycle.d2",
        "contract_version": "1",
        "accepted_task_refs": ["review-candidate"],
        "accepted_checkpoint_refs": [],
        "result": {
            "draft_ref": {"path": draft.path, "commit": draft.commit, "sha256": digest},
            "review_ref": {
                "path": "2-design/review.md",
                "commit": "c" * 40,
                "sha256": hashlib.sha256(b"review").hexdigest(),
            },
            "current_review_ref": "review-candidate",
            "prior_d2_result_ref": None,
            "finding_dispositions": [
                {
                    "finding_ref": f"review-candidate#/findings/{i}",
                    "classification": "novel_material",
                    "disposition": "revise draft",
                    "rationale": "material gap recorded",
                }
                for i in range(2)
            ],
            "conclusion": "NOT_DRY",
            "next_route": "D1",
        },
    }
    return database_path, card_id, module, update


def _d3_close_fixture(commands_module, tmp_path, monkeypatch):
    database_path, card_id, _, prior = _d2_close_fixture(
        commands_module, tmp_path, monkeypatch
    )
    prior["result"].update(conclusion="DRY", next_route="D3")
    for finding in prior["result"]["finding_dispositions"]:
        finding["classification"] = "coverage"
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO initiative_phase_results (result_id,initiative_card_id,initiative_id,phase,segment_id,iteration,result_kind,contract_id,contract_version,canonical_payload,accepted_task_refs,accepted_checkpoint_refs,actor_evidence,idempotency_key,accepted,created_at) VALUES ('d2-dry',?,'initiative-1','D2',NULL,1,'phase_close','adrian-kanban.lifecycle.d2','1',?,'[\"review-candidate\"]','[]','{}','prior-key',1,2)",
            (card_id, json.dumps(prior["result"])),
        )
        conn.execute("UPDATE initiative_transitions SET to_phase='D3'")
    update = {
        "result_id": "d3-close",
        "phase": "D3",
        "segment_id": None,
        "iteration": 1,
        "result_kind": "phase_close",
        "contract_id": "adrian-kanban.lifecycle.d3",
        "contract_version": "1",
        "accepted_task_refs": ["review-candidate"],
        "accepted_checkpoint_refs": [],
        "result": {
            "reviewed_ref": prior["result"]["draft_ref"],
            "decision_record_ref": {
                "path": "2-design/decision.md",
                "commit": "c" * 40,
                "sha256": hashlib.sha256(b"decision").hexdigest(),
            },
            "d2_result_ref": "d2-dry",
            "finding_coverage": [
                {
                    "finding_ref": f"review-candidate#/findings/{i}",
                    "status": "no_decision_required",
                    "rationale": "resolved coverage",
                }
                for i in range(2)
            ],
            "decision_items": [],
            "next_route": "D4",
        },
    }
    module = importlib.import_module(f"{commands_module.__package__}.phase_d3")
    return database_path, card_id, module, update


@pytest.mark.parametrize("gap", [None, "missing_coverage", "unapproved", "wrong_proof"])
def test_d3_command_requires_approved_complete_ratification_package(
    commands_module, tmp_path, monkeypatch, gap
):
    database_path, _, module, update = _d3_close_fixture(
        commands_module, tmp_path, monkeypatch
    )
    provider_module = importlib.import_module(f"{commands_module.__package__}.provider")
    provider = provider_module.AdrianKanbanAuthorityProvider(str(database_path))
    provider_module.register_provider(provider)
    if gap == "missing_coverage":
        update["result"]["finding_coverage"].pop()
    payload = {
        "initiative_id": "initiative-1",
        "board": "orchestrator",
        "update_kind": "phase_result",
        "update": update,
        "approval_id": "approval-d3",
    }
    if gap != "unapproved":
        _approve(database_path, payload)

    def preparer(payload, _):
        return module.prepare_d3_result(
            payload["initiative_id"],
            payload["update"],
            lambda *_: b"draft",
            lambda *_: b"decision",
        )

    if gap == "wrong_proof":
        preparer = lambda *_: object()
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_update_initiative": commands_module._handle_update_initiative
        },
        phase_result_preparer=preparer,
    )
    response = _submit(boundary, payload)
    assert response["result"] == ("ACCEPTED" if gap is None else "REJECTED"), response
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_phase_results WHERE phase='D3'"
        ).fetchone()[0] == (1 if gap is None else 0)
        if gap != "unapproved":
            assert conn.execute(
                "SELECT state FROM write_gate_kanban_approvals WHERE approval_id='approval-d3'"
            ).fetchone()[0] == ("consumed" if gap is None else "approved")
        assert (
            conn.execute(
                "SELECT to_phase FROM initiative_transitions ORDER BY transition_id DESC LIMIT 1"
            ).fetchone()[0]
            == "D3"
        )  # result admission alone never routes or authorizes Canon writes
    if gap is None:
        assert _submit(boundary, payload) == response


@pytest.mark.parametrize(
    "gap",
    [
        None,
        "stale_prior",
        "not_dry",
        "baseline",
        "missing_coverage",
        "extra_coverage",
        "missing_candidate",
        "checkpoint",
        "corrupt_prior",
        "wrong_current",
        "post_prepare_change",
    ],
)
def test_d3_admission_binds_complete_dry_history_without_mutation(
    commands_module, tmp_path, monkeypatch, gap
):
    database_path, card_id, module, update = _d3_close_fixture(
        commands_module, tmp_path, monkeypatch
    )
    if gap == "stale_prior":
        update["result"]["d2_result_ref"] = "stale"
    elif gap == "baseline":
        update["result"]["reviewed_ref"]["commit"] = "d" * 40
    elif gap == "missing_coverage":
        update["result"]["finding_coverage"].pop()
    elif gap == "extra_coverage":
        update["result"]["finding_coverage"].append({
            "finding_ref": "invented",
            "status": "no_decision_required",
            "rationale": "not real",
        })
    elif gap == "missing_candidate":
        update["accepted_task_refs"] = []
    elif gap == "checkpoint":
        update["accepted_checkpoint_refs"] = ["notD3"]
    prepared = module.prepare_d3_result(
        "initiative-1", update, lambda *_: b"draft", lambda *_: b"decision"
    )
    if gap == "post_prepare_change":
        update["result"]["decision_items"].append({"finding_ref": "new"})
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        if gap in {"not_dry", "wrong_current", "corrupt_prior"}:
            prior = json.loads(
                conn.execute(
                    "SELECT canonical_payload FROM initiative_phase_results WHERE result_id='d2-dry'"
                ).fetchone()[0]
            )
            if gap == "not_dry":
                prior["conclusion"] = "NOT_DRY"
            elif gap == "wrong_current":
                prior["current_review_ref"] = "unaccepted"
            conn.execute(
                "UPDATE initiative_phase_results SET canonical_payload=? WHERE result_id='d2-dry'",
                ("notJSON" if gap == "corrupt_prior" else json.dumps(prior),),
            )
        before = conn.total_changes
        if gap is None:
            assert (
                module.admit_d3_result(
                    SimpleNamespace(connection=conn),
                    card_id,
                    "initiative-1",
                    update,
                    prepared,
                )
                is None
            )
        else:
            with pytest.raises(ValueError):
                module.admit_d3_result(
                    SimpleNamespace(connection=conn),
                    card_id,
                    "initiative-1",
                    update,
                    prepared,
                )
        assert conn.total_changes == before


@pytest.mark.parametrize(
    "gap",
    [None, "missing_finding", "wrong_baseline", "missing_preparer", "wrong_proof"],
)
def test_d2_command_admission_is_atomic_and_replayable(
    commands_module, tmp_path, monkeypatch, gap
):
    database_path, _, module, update = _d2_close_fixture(
        commands_module, tmp_path, monkeypatch
    )
    provider_module = importlib.import_module(f"{commands_module.__package__}.provider")
    provider = provider_module.AdrianKanbanAuthorityProvider(str(database_path))
    provider_module.register_provider(provider)
    if gap == "missing_finding":
        update["result"]["finding_dispositions"].pop()
    elif gap == "wrong_baseline":
        update["result"]["draft_ref"]["commit"] = "d" * 40
    calls = []

    def preparer(payload, preparation_context):
        calls.append(preparation_context.session_id)
        with sqlite3.connect(database_path, timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        return module.prepare_d2_result(
            payload["initiative_id"],
            payload["update"],
            lambda *_: b"draft",
            lambda *_: b"review",
        )

    if gap == "missing_preparer":
        preparer = None
    elif gap == "wrong_proof":
        preparer = lambda *_: object()
    payload = {
        "initiative_id": "initiative-1",
        "board": "orchestrator",
        "update_kind": "phase_result",
        "update": update,
        "approval_id": "approval-d2-close",
    }
    _approve(database_path, payload)
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_update_initiative": commands_module._handle_update_initiative
        },
        phase_result_preparer=preparer,
    )
    result = _submit(boundary, payload)
    assert result["result"] == ("ACCEPTED" if gap is None else "REJECTED"), result
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[
            0
        ] == (1 if gap is None else 0)
        assert conn.execute(
            "SELECT state FROM write_gate_kanban_approvals WHERE approval_id=?",
            (payload["approval_id"],),
        ).fetchone()[0] == ("consumed" if gap is None else "approved")
        assert conn.execute(
            "SELECT record_version FROM adrian_kanban_cards WHERE card_type='initiative'"
        ).fetchone()[0] == (1 if gap is None else 0)
    if gap is None:
        assert _submit(boundary, payload) == result
        assert calls == ["session-checkpoint"]
    else:
        assert result["failed_checks"][0]["code"] == (
            "PHASE_RESULT_PREPARER"
            if gap in {"missing_preparer", "wrong_proof"}
            else "PHASE_RESULT_EVIDENCE"
        )


@pytest.mark.parametrize("zero_findings", [False, True])
def test_d2_close_accepts_complete_not_dry_or_zero_finding_dry_evidence(
    commands_module, tmp_path, monkeypatch, zero_findings
):
    database_path, card_id, module, update = _d2_close_fixture(
        commands_module, tmp_path, monkeypatch
    )
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        if zero_findings:
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM task_candidate_handoffs"
                ).fetchone()[0]
            )
            metadata.update(findings=[], conclusion="DRY")
            conn.execute(
                "UPDATE task_candidate_handoffs SET metadata_json=?",
                (json.dumps(metadata),),
            )
            update["result"].update(
                finding_dispositions=[], conclusion="DRY", next_route="D3"
            )
        prepared = module.prepare_d2_result(
            "initiative-1", update, lambda *_: b"draft", lambda *_: b"review"
        )
        before = conn.total_changes
        assert (
            module.admit_d2_result(
                SimpleNamespace(connection=conn),
                card_id,
                "initiative-1",
                update,
                prepared,
            )
            is None
        )
        assert conn.total_changes == before


@pytest.mark.parametrize(
    "gap",
    [
        "missing_finding",
        "invented_finding",
        "missing_candidate",
        "extra_candidate",
        "wrong_current",
        "checkpoint",
        "wrong_baseline",
        "stale_prior",
    ],
)
def test_d2_close_rejects_incomplete_or_misaligned_evidence(
    commands_module, tmp_path, monkeypatch, gap
):
    database_path, card_id, module, update = _d2_close_fixture(
        commands_module, tmp_path, monkeypatch
    )
    if gap == "missing_finding":
        update["result"]["finding_dispositions"].pop()
    elif gap == "invented_finding":
        update["result"]["finding_dispositions"][0]["finding_ref"] = (
            "invented#/findings/0"
        )
    elif gap == "missing_candidate":
        update["accepted_task_refs"] = []
    elif gap == "extra_candidate":
        update["accepted_task_refs"].append("unaccepted")
    elif gap == "wrong_current":
        update["result"]["current_review_ref"] = "not-a-current-review"
    elif gap == "checkpoint":
        update["accepted_checkpoint_refs"] = ["not-a-D2-checkpoint"]
    elif gap == "wrong_baseline":
        update["result"]["draft_ref"]["commit"] = "d" * 40
    elif gap == "stale_prior":
        update["result"]["prior_d2_result_ref"] = "nonexistent-prior"
    prepared = module.prepare_d2_result(
        "initiative-1", update, lambda *_: b"draft", lambda *_: b"review"
    )
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        before = conn.total_changes
        with pytest.raises(ValueError):
            module.admit_d2_result(
                SimpleNamespace(connection=conn),
                card_id,
                "initiative-1",
                update,
                prepared,
            )
        assert conn.total_changes == before


def test_d2_close_cannot_omit_a_historical_review_without_prior_closing_record(
    commands_module, tmp_path, monkeypatch
):
    database_path, card_id, module, update = _d2_close_fixture(
        commands_module, tmp_path, monkeypatch
    )
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        # Second accepted pass on the same immutable task baseline, distinct candidate evidence.
        conn.execute(
            "INSERT INTO task_candidate_handoffs SELECT 'older-candidate',task_card_id,task_id,2,reviewer,summary,metadata_json,submitted_by,0 FROM task_candidate_handoffs WHERE candidate_id='review-candidate'"
        )
        conn.execute(
            "INSERT INTO task_reviewer_verdicts SELECT 'older-verdict',task_card_id,task_id,'older-candidate',1002,reviewer,verdict,summary,0 FROM task_reviewer_verdicts WHERE candidate_id='review-candidate'"
        )
        prepared = module.prepare_d2_result(
            "initiative-1", update, lambda *_: b"draft", lambda *_: b"review"
        )
        with pytest.raises(ValueError, match="historical"):
            module.admit_d2_result(
                SimpleNamespace(connection=conn),
                card_id,
                "initiative-1",
                update,
                prepared,
            )
        update["accepted_task_refs"].append("older-candidate")
        prepared = module.prepare_d2_result(
            "initiative-1", update, lambda *_: b"draft", lambda *_: b"review"
        )
        with pytest.raises(ValueError, match="historical findings"):
            module.admit_d2_result(
                SimpleNamespace(connection=conn),
                card_id,
                "initiative-1",
                update,
                prepared,
            )
        update["result"]["finding_dispositions"].extend([
            {
                "finding_ref": f"older-candidate#/findings/{i}",
                "classification": "coverage",
                "disposition": "retained in accumulated review",
                "rationale": "earlier accepted pass",
            }
            for i in range(2)
        ])
        prepared = module.prepare_d2_result(
            "initiative-1", update, lambda *_: b"draft", lambda *_: b"review"
        )
        assert (
            module.admit_d2_result(
                SimpleNamespace(connection=conn),
                card_id,
                "initiative-1",
                update,
                prepared,
            )
            is None
        )


def _boundary(commands_module, database_path, provider, *, phase_result_preparer=None):
    return commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        phase_result_preparer=phase_result_preparer,
        handlers={
            "kanban_update_initiative": commands_module._handle_update_initiative
        },
    )


def _submit(
    boundary, payload: dict, *, actor_profile: str = "default", version: int = 0
):
    return boundary.submit(
        "kanban_update_initiative",
        attempt_id=f"attempt:{payload['approval_id']}",
        idempotency_key=f"key:{payload['approval_id']}",
        target="initiative-1",
        expected_version=version,
        session_id="session-checkpoint",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile=actor_profile,
        payload=payload,
    )


def _checkpoint_payload(step: str, accepted_task_refs: list[str]) -> dict:
    phase = "D4" if step.startswith("D4.") else "DEV1"
    contract = f"adrian-kanban.lifecycle.{phase.lower()}"
    result: dict = {"step": step}
    if step == "D4.3":
        result.update({
            "write_gate_approval_ref": "write-gate:change-set:1",
            "approved_change_set_digest": hashlib.sha256(
                json.dumps(
                    _d4_checkpoint_edits(), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "current_document_refs": ["Canon/design.md@" + "b" * 40],
        })
    elif step == "D4.4":
        result.update({
            "write_gate_approval_ref": "write-gate:change-set:1",
            "approval_lease_ref": "write-gate:lease:1",
            "approved_change_set_digest": "a" * 64,
            "execution_result": "applied",
            "post_write_documents": [{"path": "Canon/design.md", "sha": "c" * 40}],
            "item_determinations": [
                {"item_id": "change-1", "determination": "applied"}
            ],
        })
    elif step == "DEV1.2":
        result.update({
            "cumulative_record_ref": "2-design/dev1.2.md@" + "d" * 40,
            "source_angles": [
                {
                    "step": f"DEV1.1{letter}",
                    "candidate_ref": f"candidate:DEV1.1{letter}",
                    "item_count": index,
                }
                for index, letter in enumerate("abcde", start=1)
            ],
            "total_item_count": 15,
        })
    return {
        "initiative_id": "initiative-1",
        "update_kind": "orchestration_checkpoint",
        "update": {
            "result_id": f"checkpoint:{step}",
            "phase": phase,
            "segment_id": None,
            "iteration": 1,
            "contract_id": contract,
            "contract_version": "1",
            "result": result,
            "accepted_task_refs": accepted_task_refs,
            "accepted_checkpoint_refs": [],
        },
        "approval_id": f"approval:{step}",
        "board": "orchestrator",
    }


def _d4_execution_fixture(commands_module, tmp_path, monkeypatch):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    for sequence, step in enumerate(("D4.1", "D4.2"), 1):
        _seed_accepted_handoff(database_path, card_id, step=step, candidate_id=f"candidate:{step}", sequence=sequence)
    approval = _checkpoint_payload("D4.3", ["candidate:D4.1", "candidate:D4.2"])
    _approve(database_path, approval)
    assert _submit(_boundary(commands_module, database_path, provider), approval)["result"] == "ACCEPTED"
    payload = _checkpoint_payload("D4.4", [])
    update = payload["update"]
    update["iteration"] = 2
    update["accepted_checkpoint_refs"] = ["checkpoint:D4.3"]
    result = update["result"]
    result["approved_change_set_digest"] = approval["update"]["result"]["approved_change_set_digest"]
    result["execution_started_at"] = "2026-09-01T00:01:00+00:00"
    result["execution_finished_at"] = "2026-09-01T00:02:00+00:00"
    prefix = commands_module.__package__
    execution = importlib.import_module(f"{prefix}.phase_d4_execution")
    evidence = importlib.import_module(f"{prefix}.phase_d4_evidence")
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        author, reviewer = evidence.validate_d4_source_pair(conn, card_id, "initiative-1", "candidate:D4.1", "candidate:D4.2", result["approved_change_set_digest"])
    result["item_determinations"] = [{"item_id": ref, "determination": "applied"} for ref in (*author.item_refs, *reviewer.item_refs)]
    lease = execution.VerifiedExecutionLease(result["write_gate_approval_ref"], result["approval_lease_ref"], "original-executor", str(tmp_path), str(tmp_path / "Canon"), result["execution_started_at"], result["execution_finished_at"])
    proof = execution.PreparedD4Execution("initiative-1", hashlib.sha256(json.dumps(update, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest(), lease, (execution.VerifiedArtifact("Canon/design.md", "c" * 40, "d" * 64),))
    return database_path, provider, card_id, payload, proof, execution


@pytest.mark.parametrize("gap", [None, "missing_item", "foreign_item", "duplicate_item", "wrong_documents", "foreign_checkpoint", "corrupt_refs", "unaccepted_checkpoint", "contract", "wrong_approval", "missing_proof"])
def test_d4_execution_admission_complete_original_evidence(commands_module, tmp_path, monkeypatch, gap):
    path, _, card_id, payload, proof, execution = _d4_execution_fixture(commands_module, tmp_path, monkeypatch)
    update = payload["update"]
    if gap == "missing_item":
        update["result"]["item_determinations"].pop()
    elif gap == "foreign_item":
        update["result"]["item_determinations"][0]["item_id"] = "invented"
    elif gap == "duplicate_item":
        update["result"]["item_determinations"] *= 2
    elif gap == "wrong_documents":
        update["result"]["post_write_documents"][0]["path"] = "Canon/other.md"
        proof = dataclasses.replace(proof, documents=(dataclasses.replace(proof.documents[0], path="Canon/other.md"),))
    elif gap == "foreign_checkpoint":
        update["accepted_checkpoint_refs"] = ["other"]
    elif gap == "wrong_approval":
        update["result"]["write_gate_approval_ref"] = "other"
        proof = dataclasses.replace(proof, lease=dataclasses.replace(proof.lease, approval_id="other"))
    proof = dataclasses.replace(proof, update_digest=hashlib.sha256(json.dumps(update, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest())
    if gap == "missing_proof":
        proof = None
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        if gap == "corrupt_refs":
            conn.execute("UPDATE initiative_phase_results SET accepted_task_refs='not-json' WHERE result_id='checkpoint:D4.3'")
        elif gap == "unaccepted_checkpoint":
            conn.execute("UPDATE initiative_phase_results SET accepted=0 WHERE result_id='checkpoint:D4.3'")
        elif gap == "contract":
            conn.execute("UPDATE initiative_phase_results SET contract_version='2' WHERE result_id='checkpoint:D4.3'")
        before = conn.total_changes
        if gap is None:
            assert execution.admit_d4_execution(conn, card_id, "initiative-1", update, proof) is None
        else:
            with pytest.raises(ValueError):
                execution.admit_d4_execution(conn, card_id, "initiative-1", update, proof)
        assert conn.total_changes == before


@pytest.mark.parametrize("gap", [None, "deferred", "rejected", "missing_route", "missing_reason", "blank_route", "blank_reason", "no_preparer", "wrong_proof", "missing_item", "wrong_digest"])
def test_d4_execution_public_command_is_atomic(commands_module, tmp_path, monkeypatch, gap):
    path, provider, _, payload, proof, _ = _d4_execution_fixture(commands_module, tmp_path, monkeypatch)
    accepted_case = gap in (None, "deferred", "rejected")
    if gap in ("deferred", "rejected", "missing_route", "missing_reason", "blank_route", "blank_reason"):
        item = payload["update"]["result"]["item_determinations"][0]
        deferred = gap in ("deferred", "missing_route", "blank_route")
        item["determination"] = "deferred" if deferred else "rejected"
        field = "deferral_route" if deferred else "rejection_reason"
        if gap not in ("missing_route", "missing_reason"):
            item[field] = " " if gap in ("blank_route", "blank_reason") else ("task:follow-up" if deferred else "Outside the approved change set.")
        proof = dataclasses.replace(proof, update_digest=hashlib.sha256(json.dumps(payload["update"], sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest())
    elif gap == "missing_item":
        payload["update"]["result"]["item_determinations"].pop()
        proof = dataclasses.replace(proof, update_digest=hashlib.sha256(json.dumps(payload["update"], sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest())
    elif gap == "wrong_digest":
        payload["update"]["result"]["approved_change_set_digest"] = "e" * 64
        proof = dataclasses.replace(proof, update_digest=hashlib.sha256(json.dumps(payload["update"], sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest())
    elif gap == "wrong_proof":
        proof = object()
    preparer = None if gap == "no_preparer" else lambda *_: proof
    boundary = _boundary(commands_module, path, provider, phase_result_preparer=preparer)
    _approve(path, payload, version=1)
    response = _submit(boundary, payload, version=1)
    assert response["result"] == ("ACCEPTED" if accepted_case else "REJECTED"), response
    if gap in ("missing_route", "missing_reason", "blank_route", "blank_reason"):
        assert field in json.dumps(response), response
    if accepted_case:
        assert _submit(boundary, payload, version=1) == response
    with sqlite3.connect(path) as conn:
        state = conn.execute("SELECT state FROM write_gate_kanban_approvals WHERE approval_id=?", (payload["approval_id"],)).fetchone()[0]
        version = conn.execute("SELECT record_version FROM adrian_kanban_cards WHERE card_type='initiative'").fetchone()[0]
        count = conn.execute("SELECT count(*) FROM initiative_phase_results WHERE result_id='checkpoint:D4.4'").fetchone()[0]
        if accepted_case:
            stored = json.loads(conn.execute("SELECT canonical_payload FROM initiative_phase_results WHERE result_id='checkpoint:D4.4'").fetchone()[0])
            assert stored["item_determinations"] == payload["update"]["result"]["item_determinations"]
            conn.row_factory = sqlite3.Row
            projections = importlib.import_module(f"{commands_module.__package__}.projections")
            card = projections.show_projection(conn, None, "orchestrator", "initiative-1")
            checkpoint = next(row for row in card["phase_results"] if row["result_id"] == "checkpoint:D4.4")
            assert checkpoint["canonical_payload"]["item_determinations"] == stored["item_determinations"]
    assert (state, version, count) == (("consumed", 2, 1) if accepted_case else ("approved", 1, 0))


@pytest.mark.parametrize(
    "gap",
    ["digest", "newline", "docs", "duplicate_docs", "foreign_review", "missing_review"],
)
def test_d4_3_rejects_unbound_evidence_without_spending_approval(
    commands_module, tmp_path, monkeypatch, gap
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    for sequence, step in enumerate(("D4.1", "D4.2"), start=1):
        _seed_accepted_handoff(
            database_path,
            card_id,
            step=step,
            candidate_id=f"candidate:{step}",
            sequence=sequence,
        )
    payload = _checkpoint_payload("D4.3", ["candidate:D4.1", "candidate:D4.2"])
    result = payload["update"]["result"]
    if gap == "digest":
        result["approved_change_set_digest"] = "0" * 64
    elif gap == "newline":
        result["approved_change_set_digest"] += "\n"
    elif gap == "docs":
        result["current_document_refs"] = ["Canon/other.md@" + "b" * 40]
    elif gap == "duplicate_docs":
        result["current_document_refs"] *= 2
    else:
        with sqlite3.connect(database_path) as conn:
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM task_candidate_handoffs WHERE candidate_id='candidate:D4.2'"
                ).fetchone()[0]
            )
            if gap == "foreign_review":
                metadata["edit_findings"][0]["edit_ref"] = "foreign#/edit_set/0"
            else:
                metadata["edit_findings"] = []
                metadata["item_count"] = 0
            conn.execute(
                "UPDATE task_candidate_handoffs SET metadata_json=? WHERE candidate_id='candidate:D4.2'",
                (json.dumps(metadata),),
            )
    _approve(database_path, payload)
    response = _submit(_boundary(commands_module, database_path, provider), payload)
    assert response["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM initiative_phase_results").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE id=?", (card_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT state FROM write_gate_kanban_approvals WHERE approval_id=?",
                (payload["approval_id"],),
            ).fetchone()[0]
            == "approved"
        )


def test_d4_3_checkpoint_pins_exact_accepted_predecessors_and_replays(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    _seed_accepted_handoff(
        database_path, card_id, step="D4.1", candidate_id="candidate:D4.1", sequence=1
    )
    _seed_accepted_handoff(
        database_path, card_id, step="D4.2", candidate_id="candidate:D4.2", sequence=2
    )
    payload = _checkpoint_payload("D4.3", ["candidate:D4.1", "candidate:D4.2"])
    _approve(database_path, payload)
    boundary = _boundary(commands_module, database_path, provider)

    first = _submit(boundary, payload)
    replay = _submit(boundary, payload)

    assert first["result"] == "ACCEPTED"
    assert replay == first
    assert first["value"] == {
        "initiative_id": "initiative-1",
        "update_kind": "orchestration_checkpoint",
        "record_version": 1,
        "result_id": "checkpoint:D4.3",
        "step": "D4.3",
    }
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM initiative_phase_results WHERE result_id = ?",
            ("checkpoint:D4.3",),
        ).fetchone()
        assert row["result_kind"] == "orchestration_checkpoint"
        assert json.loads(row["canonical_payload"])["step"] == "D4.3"
        assert json.loads(row["accepted_task_refs"]) == [
            "candidate:D4.1",
            "candidate:D4.2",
        ]
        assert (
            conn.execute(
                "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
                (payload["approval_id"],),
            ).fetchone()[0]
            == "consumed"
        )


@pytest.mark.parametrize(
    ("phase", "refs", "actor"),
    [
        ("D1", ["candidate:D4.1", "candidate:D4.2"], "default"),
        ("D4", ["candidate:D4.1"], "default"),
        ("D4", ["candidate:D4.1", "candidate:D4.2"], "builder-tester"),
    ],
)
def test_d4_3_rejects_wrong_phase_incomplete_predecessors_or_wrong_actor_atomically(
    commands_module, tmp_path, monkeypatch, phase, refs, actor
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, phase)
    _seed_accepted_handoff(
        database_path, card_id, step="D4.1", candidate_id="candidate:D4.1", sequence=1
    )
    _seed_accepted_handoff(
        database_path, card_id, step="D4.2", candidate_id="candidate:D4.2", sequence=2
    )
    payload = _checkpoint_payload("D4.3", refs)
    _approve(database_path, payload)

    result = _submit(
        _boundary(commands_module, database_path, provider),
        payload,
        actor_profile=actor,
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards "
                "WHERE initiative_id = 'initiative-1'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
                (payload["approval_id"],),
            ).fetchone()[0]
            == "approved"
        )


def test_d4_4_requires_matching_d4_3_checkpoint_and_change_set_digest(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider, _, payload, proof, _ = _d4_execution_fixture(commands_module, tmp_path, monkeypatch)
    _approve(database_path, payload, version=1)
    boundary = _boundary(commands_module, database_path, provider, phase_result_preparer=lambda *_: proof)
    accepted = _submit(boundary, payload, version=1)
    assert accepted["result"] == "ACCEPTED"
    second_payload = json.loads(json.dumps(payload))
    second_payload["approval_id"] = "approval:D4.4:mismatch"
    second_payload["update"]["result_id"] = "checkpoint:D4.4:mismatch"
    second_payload["update"]["iteration"] = 3
    second_payload["update"]["accepted_checkpoint_refs"] = ["checkpoint:D4.3"]
    second_payload["update"]["result"]["approved_change_set_digest"] = "e" * 64
    proof = dataclasses.replace(proof, update_digest=hashlib.sha256(json.dumps(second_payload["update"], sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest())
    _approve(database_path, second_payload, version=2)
    rejected = _submit(
        boundary,
        second_payload,
        version=2,
    )
    assert rejected["result"] == "REJECTED"


def test_dev1_2_checkpoint_requires_all_five_angle_handoffs(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "DEV1")
    refs = []
    for sequence, letter in enumerate("abcde", start=1):
        candidate = f"candidate:DEV1.1{letter}"
        refs.append(candidate)
        _seed_accepted_handoff(
            database_path,
            card_id,
            step=f"DEV1.1{letter}",
            candidate_id=candidate,
            sequence=sequence,
        )
    payload = _checkpoint_payload("DEV1.2", refs)
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT result_kind, canonical_payload FROM initiative_phase_results"
        ).fetchone()
        assert row[0] == "orchestration_checkpoint"
        assert json.loads(row[1])["total_item_count"] == 15


def test_unknown_checkpoint_step_rejects_without_spending_approval(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_initiative(database_path, "D4")
    payload = _checkpoint_payload("D4.3", [])
    payload["update"]["result_id"] = "checkpoint:D4.9"
    payload["update"]["result"]["step"] = "D4.9"
    payload["approval_id"] = "approval:D4.9"
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
                (payload["approval_id"],),
            ).fetchone()[0]
            == "approved"
        )


def test_d4_3_rejects_unexpected_checkpoint_predecessor(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    _seed_accepted_handoff(
        database_path, card_id, step="D4.1", candidate_id="candidate:D4.1", sequence=1
    )
    _seed_accepted_handoff(
        database_path, card_id, step="D4.2", candidate_id="candidate:D4.2", sequence=2
    )
    payload = _checkpoint_payload("D4.3", ["candidate:D4.1", "candidate:D4.2"])
    payload["update"]["accepted_checkpoint_refs"] = ["unexpected:checkpoint"]
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
                (payload["approval_id"],),
            ).fetchone()[0]
            == "approved"
        )


def test_checkpoint_rejects_candidate_whose_unified_card_identity_is_corrupt(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    _seed_accepted_handoff(
        database_path, card_id, step="D4.1", candidate_id="candidate:D4.1", sequence=1
    )
    _seed_accepted_handoff(
        database_path, card_id, step="D4.2", candidate_id="candidate:D4.2", sequence=2
    )
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE adrian_kanban_cards SET task_id = 'corrupt-task-id' "
            "WHERE task_id = 'task:D4.2'"
        )
        conn.commit()
    payload = _checkpoint_payload("D4.3", ["candidate:D4.1", "candidate:D4.2"])
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM initiative_phase_results").fetchone()[0]
            == 0
        )


def _seed_checkpoint(
    database_path: Path,
    card_id: int,
    *,
    result_id: str,
    step: str,
    iteration: int,
    created_at: int,
) -> None:
    canonical_payload = {"step": step}
    if step == "DEV1.2":
        canonical_payload["cumulative_record_ref"] = "2-design/dev1.2.md@" + "a" * 40
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "(?, ?, 'initiative-1', 'DEV1', NULL, ?, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.dev1', '1', "
            "?, '[]', '[]', '{}', ?, 1, ?)",
            (
                result_id,
                card_id,
                iteration,
                json.dumps(
                    canonical_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                f"seed:{result_id}",
                created_at,
            ),
        )
        conn.commit()


def _dev1_3_payload(*, dry: bool = True) -> dict:
    payload = _checkpoint_payload("DEV1.2", [])
    payload["approval_id"] = "approval:DEV1.3"
    payload["update"]["result_id"] = "checkpoint:DEV1.3"
    payload["update"]["iteration"] = 2
    payload["update"]["accepted_checkpoint_refs"] = ["checkpoint:DEV1.2"]
    payload["update"]["result"] = {
        "step": "DEV1.3",
        "pass_counter": 1,
        "current_cumulative_record_ref": "2-design/dev1.2.md@" + "a" * 40,
        "updated_cumulative_record_ref": "2-design/dev1.3.md@" + "b" * 40,
        "follow_up_task_refs": [],
        "no_new_material_declarations": [
            {"step": f"DEV1.1{letter}", "no_new_material": True} for letter in "abcde"
        ],
        "dry": dry,
    }
    return payload


def test_dev1_3_accepts_latest_cumulative_checkpoint_and_five_angle_dry_result(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "DEV1")
    _seed_checkpoint(
        database_path,
        card_id,
        result_id="checkpoint:DEV1.2",
        step="DEV1.2",
        iteration=1,
        created_at=1,
    )
    payload = _dev1_3_payload()
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "ACCEPTED"
    assert result["value"]["step"] == "DEV1.3"
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT canonical_payload, accepted_checkpoint_refs "
            "FROM initiative_phase_results WHERE result_id = 'checkpoint:DEV1.3'"
        ).fetchone()
        assert json.loads(row[0])["dry"] is True
        assert json.loads(row[1]) == ["checkpoint:DEV1.2"]


def test_dev1_3_dry_rejects_any_angle_with_new_material(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "DEV1")
    _seed_checkpoint(
        database_path,
        card_id,
        result_id="checkpoint:DEV1.2",
        step="DEV1.2",
        iteration=1,
        created_at=1,
    )
    payload = _dev1_3_payload()
    payload["update"]["result"]["no_new_material_declarations"][2][
        "no_new_material"
    ] = False
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM initiative_phase_results "
                "WHERE result_id = 'checkpoint:DEV1.3'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
                (payload["approval_id"],),
            ).fetchone()[0]
            == "approved"
        )


def test_d4_4_rejects_corrupt_d4_3_payload_even_when_missing_values_match(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "D4")
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "('checkpoint:D4.3', ?, 'initiative-1', 'D4', NULL, 1, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.d4', '1', "
            "?, '[]', '[]', '{}', 'seed:d4.3', 1, 1)",
            (card_id, json.dumps({"step": "D4.3"})),
        )
        conn.commit()
    payload = _checkpoint_payload("D4.4", [])
    payload["update"]["iteration"] = 2
    payload["update"]["accepted_checkpoint_refs"] = ["checkpoint:D4.3"]
    payload["update"]["result"].pop("write_gate_approval_ref")
    payload["update"]["result"].pop("approved_change_set_digest")
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT state FROM write_gate_kanban_approvals WHERE approval_id = ?",
                (payload["approval_id"],),
            ).fetchone()[0]
            == "approved"
        )


def test_dev1_2_rejects_boolean_total_even_when_equal_to_numeric_sum(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "DEV1")
    refs = []
    for sequence, letter in enumerate("abcde", start=1):
        candidate = f"candidate:DEV1.1{letter}"
        refs.append(candidate)
        _seed_accepted_handoff(
            database_path,
            card_id,
            step=f"DEV1.1{letter}",
            candidate_id=candidate,
            sequence=sequence,
        )
    payload = _checkpoint_payload("DEV1.2", refs)
    for index, angle in enumerate(payload["update"]["result"]["source_angles"]):
        angle["item_count"] = 1 if index == 0 else 0
    payload["update"]["result"]["total_item_count"] = True
    _approve(database_path, payload)

    result = _submit(_boundary(commands_module, database_path, provider), payload)

    assert result["result"] == "REJECTED"


def test_dev1_3_rejects_nonboolean_dry_and_stale_cumulative_reference(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    card_id = _seed_initiative(database_path, "DEV1")
    _seed_checkpoint(
        database_path,
        card_id,
        result_id="checkpoint:DEV1.2",
        step="DEV1.2",
        iteration=1,
        created_at=1,
    )
    boundary = _boundary(commands_module, database_path, provider)

    nonboolean = _dev1_3_payload()
    nonboolean["update"]["result"]["dry"] = "yes"
    _approve(database_path, nonboolean)
    assert _submit(boundary, nonboolean)["result"] == "REJECTED"

    stale = _dev1_3_payload()
    stale["approval_id"] = "approval:DEV1.3:stale"
    stale["update"]["result_id"] = "checkpoint:DEV1.3:stale"
    stale["update"]["result"]["current_cumulative_record_ref"] = (
        "2-design/stale.md@" + "c" * 40
    )
    _approve(database_path, stale)
    assert _submit(boundary, stale)["result"] == "REJECTED"
