"""Phase-close evidence checks derived from Kanban v0.28 section 8.3.4."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def d1():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    name = "s3_phase_results"
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    yield importlib.import_module(f"{name}.phase_d1")
    for key in tuple(sys.modules):
        if key == name or key.startswith(name + "."):
            sys.modules.pop(key, None)


@pytest.fixture
def context():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE initiative_phase_results (result_id TEXT PRIMARY KEY,initiative_card_id INTEGER,initiative_id TEXT,phase TEXT,segment_id TEXT,result_kind TEXT,accepted INTEGER,contract_id TEXT,contract_version TEXT,canonical_payload TEXT,created_at INTEGER)"
    )
    yield SimpleNamespace(connection=conn)
    conn.close()


def _update():
    return {
        "result_id": "d1-result",
        "phase": "D1",
        "segment_id": None,
        "iteration": 1,
        "result_kind": "phase_close",
        "contract_id": "adrian-kanban.lifecycle.d1",
        "contract_version": "1",
        "accepted_task_refs": [],
        "accepted_checkpoint_refs": [],
        "result": {
            "draft_ref": {
                "path": "2-design/draft.md",
                "commit": "a" * 40,
                "sha256": hashlib.sha256(b"draft").hexdigest(),
            },
            "open_questions": [],
            "revision_findings": [],
            "prior_d2_result_ref": None,
            "next_route": "D2",
        },
    }


def _d2_update():
    update = _update()
    update.update(
        phase="D2",
        contract_id="adrian-kanban.lifecycle.d2",
        accepted_task_refs=["current-review"],
    )
    update["result"] = {
        "draft_ref": update["result"]["draft_ref"],
        "review_ref": {
            "path": "2-design/review.md",
            "commit": "c" * 40,
            "sha256": hashlib.sha256(b"review").hexdigest(),
        },
        "current_review_ref": "current-review",
        "prior_d2_result_ref": None,
        "finding_dispositions": [],
        "conclusion": "DRY",
        "next_route": "D3",
    }
    return update


def _d3_update(dispositions=()):
    update = _update()
    update.update(phase="D3", contract_id="adrian-kanban.lifecycle.d3")
    update["result"] = {
        "reviewed_ref": update["result"]["draft_ref"],
        "decision_record_ref": {
            "path": "2-design/decision.md",
            "commit": "c" * 40,
            "sha256": hashlib.sha256(b"decision").hexdigest(),
        },
        "d2_result_ref": "d2-dry",
        "finding_coverage": [],
        "decision_items": [],
        "next_route": "D2" if "design_amendment" in dispositions else "D4",
    }
    for index, disposition in enumerate(dispositions):
        ref = f"review#/findings/{index}"
        update["result"]["finding_coverage"].append({
            "finding_ref": ref,
            "status": "decision_required",
            "rationale": "human decision needed",
        })
        update["result"]["decision_items"].append({
            "finding_ref": ref,
            "disposition": disposition,
            "rationale": "recorded human decision",
            "amendment_text": "exact design edit"
            if disposition == "design_amendment"
            else None,
            "canon_decision": "exact Canon change decision"
            if disposition == "canon_amendment"
            else None,
        })
    return update


@pytest.mark.parametrize(
    "dispositions",
    [
        (),
        ("ratified_as_is",),
        ("design_amendment",),
        ("canon_amendment",),
        ("design_amendment", "canon_amendment", "ratified_as_is"),
    ],
)
def test_d3_preparation_preserves_all_decisions_and_exact_route(d1, dispositions):
    module = importlib.import_module(f"{d1.__package__}.phase_d3")
    update = _d3_update(dispositions)
    original = copy.deepcopy(update)
    calls = []
    proof = module.prepare_d3_result(
        "initiative-1",
        update,
        lambda *args: calls.append(("published", args)) or b"draft",
        lambda *args: calls.append(("immutable", args)) or b"decision",
    )
    assert update == original
    assert proof.reviewed.path == "2-design/draft.md"
    assert proof.decision_record.path == "2-design/decision.md"
    assert [kind for kind, _ in calls] == ["published", "immutable"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        proof.initiative_id = "another"


def test_d3_resolved_coverage_does_not_force_a_human_decision(d1):
    module = importlib.import_module(f"{d1.__package__}.phase_d3")
    update = _d3_update()
    update["result"]["finding_coverage"] = [
        {
            "finding_ref": "review#/findings/0",
            "status": "no_decision_required",
            "rationale": "already resolved, explicit coverage retained",
        }
    ]
    module.prepare_d3_result(
        "initiative-1", update, lambda *_: b"draft", lambda *_: b"decision"
    )


@pytest.mark.parametrize(
    "gap",
    [
        "omitted_decision",
        "extra_decision",
        "duplicate_coverage",
        "duplicate_decision",
        "wrong_route",
        "missing_amendment",
        "inapplicable_canon",
        "bad_status",
        "not_a_list",
        "extra_field",
    ],
)
def test_d3_invalid_package_rejects_before_artifact_reads(d1, gap):
    module = importlib.import_module(f"{d1.__package__}.phase_d3")
    update = _d3_update(("design_amendment",))
    result = update["result"]
    if gap == "omitted_decision":
        result["decision_items"] = []
    elif gap == "extra_decision":
        result["finding_coverage"][0]["status"] = "no_decision_required"
    elif gap == "duplicate_coverage":
        result["finding_coverage"] *= 2
    elif gap == "duplicate_decision":
        result["decision_items"] *= 2
    elif gap == "wrong_route":
        result["next_route"] = "D4"
    elif gap == "missing_amendment":
        result["decision_items"][0]["amendment_text"] = None
    elif gap == "inapplicable_canon":
        result["decision_items"][0]["canon_decision"] = "inapplicable"
    elif gap == "bad_status":
        result["finding_coverage"][0]["status"] = "invented"
    elif gap == "not_a_list":
        result["decision_items"] = {}
    else:
        result["extra"] = "unrecognized"
    calls = []
    with pytest.raises(ValueError):
        module.prepare_d3_result(
            "initiative-1",
            update,
            lambda *_: calls.append("draft") or b"draft",
            lambda *_: calls.append("decision") or b"decision",
        )
    assert calls == []


def test_d3_decision_document_digest_is_verified(d1):
    module = importlib.import_module(f"{d1.__package__}.phase_d3")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        module.prepare_d3_result(
            "initiative-1",
            _d3_update(),
            lambda *_: b"draft",
            lambda *_: b"wrong decision",
        )


def _git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def phase_repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    remote = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "2-design").mkdir()
    (root / "2-design/draft.md").write_bytes(b"draft")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "draft")
    draft_commit = _git(root, "rev-parse", "HEAD")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "origin", "main")
    _git(root, "checkout", "-b", "review")
    (root / "2-design/review.md").write_bytes(b"review")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "review")
    return root, draft_commit, _git(root, "rev-parse", "HEAD")


def _phase_preparer(d1, root, binding=None):
    module = importlib.import_module(f"{d1.__package__}.phase_preparer")
    inputs = importlib.import_module(f"{d1.__package__}.task_inputs")
    registry = SimpleNamespace(
        get_active_binding=lambda _: binding or SimpleNamespace(worktree_path=str(root))
    )
    return module.GitPhaseResultPreparer(
        lambda: registry
    ), inputs.TaskInputPreparationContext(
        session_id="session-1", execution_context="test"
    )


@pytest.mark.parametrize("phase", ["D1", "D2", "D3"])
def test_phase_preparer_reads_pinned_bytes_not_dirty_files(d1, phase_repository, phase):
    root, draft_commit, review_commit = phase_repository
    update = (
        _update() if phase == "D1" else _d2_update() if phase == "D2" else _d3_update()
    )
    draft_field = "reviewed_ref" if phase == "D3" else "draft_ref"
    update["result"][draft_field]["commit"] = draft_commit
    if phase == "D2":
        update["result"]["review_ref"]["commit"] = review_commit
    elif phase == "D3":
        update["result"]["decision_record_ref"] = {
            "path": "2-design/review.md",
            "commit": review_commit,
            "sha256": hashlib.sha256(b"review").hexdigest(),
        }
    (root / "2-design/draft.md").write_bytes(b"dirty draft")
    (root / "2-design/review.md").write_bytes(b"dirty review")
    before = _git(root, "status", "--porcelain")
    preparer, context = _phase_preparer(d1, root)
    proof = preparer(
        {
            "initiative_id": "initiative-1",
            "update_kind": "phase_result",
            "update": update,
            "cwd": "untrusted",
        },
        context,
    )
    assert (proof.reviewed if phase == "D3" else proof.draft).commit == draft_commit
    if phase == "D2":
        assert proof.review.commit == review_commit  # deliberately NOT on origin/main
    elif phase == "D3":
        assert proof.decision_record.commit == review_commit
    assert _git(root, "status", "--porcelain") == before


def test_phase_preparer_rejects_unpublished_draft(d1, phase_repository):
    root, _, review_commit = phase_repository
    update = _update()
    update["result"]["draft_ref"]["commit"] = review_commit
    preparer, context = _phase_preparer(d1, root)
    with pytest.raises(ValueError, match="not an ancestor"):
        preparer(
            {
                "initiative_id": "initiative-1",
                "update_kind": "phase_result",
                "update": update,
            },
            context,
        )


def test_phase_preparer_missing_binding_cannot_use_payload_cwd(d1, phase_repository):
    root, draft_commit, _ = phase_repository
    module = importlib.import_module(f"{d1.__package__}.phase_preparer")
    _, context = _phase_preparer(d1, root)
    update = _update()
    update["result"]["draft_ref"]["commit"] = draft_commit
    preparer = module.GitPhaseResultPreparer(
        lambda: SimpleNamespace(get_active_binding=lambda _: None)
    )
    with pytest.raises(ValueError, match="no active binding"):
        preparer(
            {
                "initiative_id": "initiative-1",
                "update_kind": "phase_result",
                "update": update,
                "cwd": str(root),
            },
            context,
        )


def test_d2_zero_finding_preparation_uses_distinct_artifact_readers(d1):
    module = importlib.import_module(f"{d1.__package__}.phase_d2")
    calls = []
    prepared = module.prepare_d2_result(
        "initiative-1",
        _d2_update(),
        lambda commit, path: calls.append(("published", commit, path)) or b"draft",
        lambda commit, path: calls.append(("immutable", commit, path)) or b"review",
    )
    assert calls == [
        ("published", "a" * 40, "2-design/draft.md"),
        ("immutable", "c" * 40, "2-design/review.md"),
    ]
    assert prepared.draft.sha256 == hashlib.sha256(b"draft").hexdigest()
    assert prepared.review.sha256 == hashlib.sha256(b"review").hexdigest()


@pytest.mark.parametrize(
    "classification,conclusion,route,valid",
    [
        (None, "DRY", "D3", True),
        ("coverage", "DRY", "D3", True),
        ("novel_material", "NOT_DRY", "D1", True),
        ("invented", "DRY", "D3", False),
        ("review_process_failure", "NOT_DRY", "D1", False),
        ("novel_material", "DRY", "D3", False),
        ("coverage", "NOT_DRY", "D1", False),
        (None, "NOT_DRY", "D1", False),
        ("novel_material", "NOT_DRY", "D3", False),
        (None, "DRY", "DEV1", False),
        (None, "UNKNOWN", "D3", False),
    ],
)
def test_d2_classification_route_consistency_without_substantive_judgment(
    d1, classification, conclusion, route, valid
):
    module = importlib.import_module(f"{d1.__package__}.phase_d2")
    update = _d2_update()
    update["result"].update(conclusion=conclusion, next_route=route)
    if classification is not None:
        update["result"]["finding_dispositions"] = [
            {
                "finding_ref": "current-review#/findings/0",
                "classification": classification,
                "disposition": "explicit disposition",
                "rationale": "orchestrator judgment recorded",
            }
        ]
    calls = []
    readers = (
        lambda *_: calls.append("draft") or b"draft",
        lambda *_: calls.append("review") or b"review",
    )
    if valid:
        assert (
            module.prepare_d2_result("initiative-1", update, *readers).initiative_id
            == "initiative-1"
        )
    else:
        with pytest.raises(ValueError):
            module.prepare_d2_result("initiative-1", update, *readers)
        assert calls == []


def _prior(
    context,
    *,
    result_id="d2-result",
    refs=("candidate#/findings/0",),
    created_at=1,
    card_id=1,
    initiative="initiative-1",
    accepted=1,
):
    payload = {
        "finding_dispositions": [
            {
                "finding_ref": ref,
                "disposition": "revise",
                "rationale": "review evidence",
            }
            for ref in refs
        ]
    }
    context.connection.execute(
        "INSERT INTO initiative_phase_results VALUES (?,?,?,'D2',NULL,'phase_close',?,'adrian-kanban.lifecycle.d2','1',?,?)",
        (result_id, card_id, initiative, accepted, json.dumps(payload), created_at),
    )


def _revision(update, refs):
    update["result"]["prior_d2_result_ref"] = "d2-result"
    update["result"]["revision_findings"] = [
        {
            "finding_ref": ref,
            "disposition": "addressed",
            "rationale": "draft change recorded",
        }
        for ref in refs
    ]


def test_first_d1_draft_is_verified_once_and_admission_does_not_mutate(d1, context):
    update = _update()
    calls = []
    prepared = d1.prepare_d1_result(
        "initiative-1", update, lambda *args: calls.append(args) or b"draft"
    )
    assert calls == [("a" * 40, "2-design/draft.md")]
    with pytest.raises(dataclasses.FrozenInstanceError):
        prepared.initiative_id = "other"
    before = context.connection.total_changes
    assert d1.admit_d1_result(context, 1, "initiative-1", update, prepared) is None
    assert context.connection.total_changes == before


def test_d1_revision_accounts_for_each_distinct_prior_finding(d1, context):
    refs = ("candidate#/findings/0", "candidate#/findings/1")
    _prior(context, refs=refs)
    update = _update()
    _revision(update, reversed(refs))
    prepared = d1.prepare_d1_result("initiative-1", update, lambda *_: b"draft")
    assert d1.admit_d1_result(context, 1, "initiative-1", update, prepared) is None


def test_prepared_d1_cannot_omit_a_required_key_after_verification(d1, context):
    update = _update()
    prepared = d1.prepare_d1_result("initiative-1", update, lambda *_: b"draft")
    del update["result"]["revision_findings"]
    with pytest.raises(ValueError, match="digest"):
        d1.admit_d1_result(context, 1, "initiative-1", update, prepared)


@pytest.mark.parametrize(
    "refs",
    [
        [],
        ["candidate#/findings/0"],
        ["invented#/findings/0"],
        ["candidate#/findings/0", "candidate#/findings/1", "extra#/findings/0"],
    ],
)
def test_d1_revision_cannot_drop_or_invent_prior_findings(d1, context, refs):
    _prior(context, refs=("candidate#/findings/0", "candidate#/findings/1"))
    update = _update()
    _revision(update, refs)
    prepared = d1.prepare_d1_result("initiative-1", update, lambda *_: b"draft")
    before = context.connection.total_changes
    with pytest.raises(ValueError):
        d1.admit_d1_result(context, 1, "initiative-1", update, prepared)
    assert context.connection.total_changes == before


def test_d1_cannot_hide_prior_review_or_reuse_stale_review(d1, context):
    _prior(context)
    update = _update()
    prepared = d1.prepare_d1_result("initiative-1", update, lambda *_: b"draft")
    with pytest.raises(ValueError):
        d1.admit_d1_result(context, 1, "initiative-1", update, prepared)
    _revision(update, ["candidate#/findings/0"])
    prepared = d1.prepare_d1_result("initiative-1", update, lambda *_: b"draft")
    _prior(context, result_id="d2-newer", created_at=2)
    with pytest.raises(ValueError):
        d1.admit_d1_result(context, 1, "initiative-1", update, prepared)


@pytest.mark.parametrize(
    "changed",
    [
        "result_id",
        "iteration",
        "accepted_task_refs",
        "accepted_checkpoint_refs",
        "result",
    ],
)
def test_prepared_d1_evidence_binds_entire_update(d1, context, changed):
    update = _update()
    prepared = d1.prepare_d1_result("initiative-1", update, lambda *_: b"draft")
    if changed == "iteration":
        update[changed] = 2
    elif changed.endswith("refs"):
        update[changed] = ["substituted"]
    elif changed == "result":
        update[changed]["open_questions"].append({
            "item_id": "q1",
            "question": "New question",
            "context": "Changed after approval preparation",
        })
    else:
        update[changed] = "different"
    with pytest.raises(ValueError):
        d1.admit_d1_result(context, 1, "initiative-1", update, prepared)


@pytest.mark.parametrize(
    "changes",
    [
        {"next_route": "D3"},
        {"open_questions": "not a list"},
        {"open_questions": [{"item_id": "q1", "question": "Why?"}]},
        {
            "open_questions": [{"item_id": "q", "question": "Why?", "context": "prior"}]
            * 2
        },
        {
            "revision_findings": [
                {"finding_ref": "f", "disposition": "fixed", "rationale": "why"}
            ]
        },
        {"prior_d2_result_ref": " "},
        {"unknown": "field"},
        {
            "revision_findings": [
                {"finding_ref": "f", "disposition": "fixed", "rationale": "why"}
            ]
            * 2,
            "prior_d2_result_ref": "d2-result",
        },
    ],
)
def test_invalid_d1_structure_is_rejected_before_artifact_read(d1, changes):
    update = _update()
    update["result"].update(copy.deepcopy(changes))
    calls = []
    with pytest.raises(ValueError):
        d1.prepare_d1_result(
            "initiative-1", update, lambda *args: calls.append(args) or b"draft"
        )
    assert calls == []


@pytest.mark.parametrize(
    "corrupt",
    [
        "not json",
        "[]",
        '{"finding_dispositions":null}',
        '{"finding_dispositions":[{}]}',
        '{"finding_dispositions":[{"finding_ref":"f"},{"finding_ref":"f"}]}',
    ],
)
def test_malformed_prior_d2_evidence_is_not_silently_ignored(d1, context, corrupt):
    _prior(context)
    context.connection.execute(
        "UPDATE initiative_phase_results SET canonical_payload=?", (corrupt,)
    )
    update = _update()
    _revision(update, ["candidate#/findings/0"])
    prepared = d1.prepare_d1_result("initiative-1", update, lambda *_: b"draft")
    with pytest.raises(ValueError):
        d1.admit_d1_result(context, 1, "initiative-1", update, prepared)


def test_other_initiative_and_unaccepted_d2_records_do_not_change_first_entry(
    d1, context
):
    _prior(context, initiative="other", card_id=2)
    _prior(context, result_id="unaccepted", accepted=0)
    update = _update()
    prepared = d1.prepare_d1_result("initiative-1", update, lambda *_: b"draft")
    assert d1.admit_d1_result(context, 1, "initiative-1", update, prepared) is None
    with pytest.raises(ValueError):
        d1.admit_d1_result(context, 1, "other", update, prepared)
