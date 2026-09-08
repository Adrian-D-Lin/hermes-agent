"""Historical D4 authority evidence from Kanban v0.28 / Write-Gate lifecycle."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from writegate import registry as writegate


@pytest.fixture(scope="module")
def module():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    name = "s3_execution_lease"
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    yield importlib.import_module(f"{name}.phase_d4_lease")
    for key in tuple(sys.modules):
        if key == name or key.startswith(name + "."):
            sys.modules.pop(key, None)


@pytest.fixture
def authority(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "Canon").mkdir(parents=True)
    path = tmp_path / "registry" / "writegate.sqlite3"
    monkeypatch.setattr(writegate, "utcnow_iso", lambda: "2026-09-01T00:00:00+00:00")
    registry = writegate.Registry(path)
    registry.create_request(
        request_id="approval-1",
        session_id="session-1",
        confirmed_worktree=str(root),
        narrowest_folder=str(root / "Canon"),
        recovery_location=str(root / "5-archive"),
        stated_outcome="approved exact edits",
    )
    registry.decide_request("approval-1", "once")
    registry.create_lease(
        lease_id="lease-1",
        approval_reference="approval-1",
        session_id="session-1",
        confirmed_worktree=str(root),
        approved_folder=str(root / "Canon"),
        approved_at="2026-09-01T00:00:00+00:00",
    )
    args = dict(
        approval_id="approval-1",
        lease_id="lease-1",
        session_id="session-1",
        worktree_path=str(root),
        document_paths=["Canon/policy.md"],
        execution_started_at="2026-09-01T00:01:00+00:00",
        execution_finished_at="2026-09-01T00:04:00+00:00",
    )
    yield registry, path, args
    registry.close()


@pytest.mark.parametrize("status", ["active", "expired"])
def test_historical_execution_does_not_require_live_lease(module, authority, status):
    registry, path, args = authority
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE write_gate_leases SET status=?", (status,))
        before = conn.execute("SELECT * FROM write_gate_leases").fetchall()
    proof = module.verify_execution_lease(registry, **args)
    assert proof.lease_id == "lease-1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        proof.session_id = "other"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM write_gate_leases").fetchall() == before


def _execution_update(args):
    return {
        "phase": "D4",
        "segment_id": None,
        "result": {
            "step": "D4.4",
            "write_gate_approval_ref": args["approval_id"],
            "approval_lease_ref": args["lease_id"],
            "approved_change_set_digest": "a" * 64,
            "execution_result": "applied approved changes",
            "post_write_documents": [{"path": "Canon/policy.md", "sha": "b" * 40}],
            "item_determinations": [
                {"item_id": "author#/edit_set/0", "determination": "applied"}
            ],
            "execution_started_at": args["execution_started_at"],
            "execution_finished_at": args["execution_finished_at"],
        },
    }


def test_execution_preparation_pins_full_update_and_original_executor(
    module, authority
):
    execution = importlib.import_module(f"{module.__package__}.phase_d4_execution")
    registry, path, args = authority
    update = _execution_update(args)
    reads = []

    def reader(commit, path):
        reads.append((commit, path))
        return b"post-write state"

    proof = execution.prepare_d4_execution(
        "initiative-1", update, registry, args["worktree_path"], reader
    )
    assert reads == [("b" * 40, "Canon/policy.md")]
    assert proof.lease.session_id == "session-1"
    assert proof.documents[0].sha256 == hashlib.sha256(b"post-write state").hexdigest()
    assert (
        proof.update_digest
        == hashlib.sha256(
            json.dumps(
                update,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        proof.initiative_id = "other"


@pytest.mark.parametrize("gap", [None, "initiative", "digest", "lease_type", "approval_id", "lease_id", "execution_started_at", "execution_finished_at", "documents_type", "document_type", "path", "commit", "proof_type"])
def test_prepared_execution_binding(module, authority, gap):
    execution = importlib.import_module(f"{module.__package__}.phase_d4_execution")
    registry, _, args = authority
    update = _execution_update(args)
    proof = execution.prepare_d4_execution(
        "initiative-1", update, registry, args["worktree_path"], lambda *_: b"content"
    )
    if gap == "initiative":
        proof = dataclasses.replace(proof, initiative_id="other")
    elif gap == "digest":
        update["result"]["execution_result"] = "different"
    elif gap == "lease_type":
        proof = dataclasses.replace(proof, lease=SimpleNamespace(**dataclasses.asdict(proof.lease)))
    elif gap in {"approval_id", "lease_id", "execution_started_at", "execution_finished_at"}:
        proof = dataclasses.replace(proof, lease=dataclasses.replace(proof.lease, **{gap: "other"}))
    elif gap == "documents_type":
        proof = dataclasses.replace(proof, documents=list(proof.documents))
    elif gap == "document_type":
        proof = dataclasses.replace(proof, documents=(SimpleNamespace(**dataclasses.asdict(proof.documents[0])),))
    elif gap in {"path", "commit"}:
        proof = dataclasses.replace(proof, documents=(dataclasses.replace(proof.documents[0], **{gap: "other"}),))
    elif gap == "proof_type":
        proof = SimpleNamespace(**dataclasses.asdict(proof))
    if gap is None:
        assert execution.validate_execution_proof("initiative-1", update, proof) is None
    else:
        with pytest.raises(ValueError) as error:
            execution.validate_execution_proof("initiative-1", update, proof)
        assert str(error.value).strip(), f"{gap}: rejection must explain the failed field"


@pytest.mark.parametrize(
    "gap",
    [
        "unknown_field",
        "missing_field",
        "wrong_phase",
        "wrong_step",
        "digest_type",
        "digest_newline",
        "duplicate_doc",
        "relative_escape",
        "short_sha",
        "duplicate_item",
        "invalid_determination",
        "expired_interval",
        "unknown_lease",
    ],
)
def test_execution_preparation_rejects_before_git_reads(module, authority, gap):
    execution = importlib.import_module(f"{module.__package__}.phase_d4_execution")
    registry, _, args = authority
    update = _execution_update(args)
    result = update["result"]
    if gap == "unknown_field":
        result["extra"] = "not in contract"
    elif gap == "missing_field":
        result.pop("execution_finished_at")
    elif gap == "wrong_phase":
        update["phase"] = "D3"
    elif gap == "wrong_step":
        result["step"] = "D4.3"
    elif gap == "digest_type":
        result["approved_change_set_digest"] = []
    elif gap == "digest_newline":
        result["approved_change_set_digest"] += "\n"
    elif gap == "duplicate_doc":
        result["post_write_documents"] *= 2
    elif gap == "relative_escape":
        result["post_write_documents"][0]["path"] = "../policy.md"
    elif gap == "short_sha":
        result["post_write_documents"][0]["sha"] = "b" * 7
    elif gap == "duplicate_item":
        result["item_determinations"] *= 2
    elif gap == "invalid_determination":
        result["item_determinations"][0]["determination"] = "maybe"
    elif gap == "expired_interval":
        result["execution_finished_at"] = "2026-09-01T00:06:00+00:00"
    else:
        result["approval_lease_ref"] = "unknown"

    def reader(*args):
        pytest.fail("invalid execution evidence must reject before reading Git")

    with pytest.raises(ValueError):
        execution.prepare_d4_execution(
            "initiative-1", update, registry, args["worktree_path"], reader
        )


def test_execution_preparation_rejects_nonbytes_reader_result(module, authority):
    execution = importlib.import_module(f"{module.__package__}.phase_d4_execution")
    registry, _, args = authority
    with pytest.raises(ValueError):
        execution.prepare_d4_execution(
            "initiative-1",
            _execution_update(args),
            registry,
            args["worktree_path"],
            lambda *_: "not bytes",
        )


@pytest.mark.parametrize(
    "gap",
    [
        "unknown_lease",
        "denied",
        "session",
        "worktree",
        "scope",
        "before_approval",
        "after_expiry",
        "reversed",
        "naive_time",
        "ended",
        "unknown_status",
        "duplicate_path",
        "escaping_path",
    ],
)
def test_execution_lease_rejects_unproven_scope_or_interval(module, authority, gap):
    registry, path, args = authority
    if gap == "unknown_lease":
        args["lease_id"] = "unknown"
    elif gap == "denied":
        registry.decide_request("approval-1", "deny")
    elif gap == "session":
        args["session_id"] = "another"
    elif gap == "worktree":
        args["worktree_path"] = str(Path(args["worktree_path"]).parent)
    elif gap == "scope":
        args["document_paths"] = ["other/policy.md"]
    elif gap == "before_approval":
        args["execution_started_at"] = "2026-08-31T23:59:00+00:00"
    elif gap == "after_expiry":
        args["execution_finished_at"] = "2026-09-01T00:06:00+00:00"
    elif gap == "reversed":
        args["execution_started_at"] = "2026-09-01T00:04:30+00:00"
    elif gap == "naive_time":
        args["execution_started_at"] = "2026-09-01T00:01:00"
    elif gap in {"ended", "unknown_status"}:
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE write_gate_leases SET status=?", (gap,))
    elif gap == "duplicate_path":
        args["document_paths"] *= 2
    else:
        args["document_paths"] = ["Canon/../../escape"]
    with pytest.raises(ValueError):
        module.verify_execution_lease(registry, **args)


def test_registry_errors_do_not_leak_details(module, authority):
    _, _, args = authority

    def fail(_):
        raise RuntimeError("private storage location")

    with pytest.raises(ValueError) as error:
        module.verify_execution_lease(
            SimpleNamespace(get_request=fail, get_lease=fail), **args
        )
    assert "private storage location" not in str(error.value)


@pytest.mark.parametrize("record", ["request", "lease"])
def test_lookup_must_return_requested_record_identity(module, authority, record):
    registry, _, args = authority
    request = registry.get_request("approval-1")
    lease = registry.get_lease("lease-1")
    if record == "request":
        request.request_id = "other"
    else:
        lease.lease_id = "other"
    with pytest.raises(ValueError):
        module.verify_execution_lease(
            SimpleNamespace(get_request=lambda _: request, get_lease=lambda _: lease),
            **args,
        )


def test_non_list_document_paths_rejected(module, authority):
    registry, _, args = authority
    args["document_paths"] = tuple(args["document_paths"])
    with pytest.raises(ValueError):
        module.verify_execution_lease(registry, **args)


@pytest.mark.parametrize("item,valid", [
    ({"item_id": "i", "determination": "applied"}, True),
    ({"item_id": "i", "determination": "deferred", "deferral_route": "task:follow-up"}, True),
    ({"item_id": "i", "determination": "rejected", "rejection_reason": "Contradicts the ratified scope."}, True),
    ({"item_id": "i", "determination": "deferred"}, False),
    ({"item_id": "i", "determination": "rejected"}, False),
    ({"item_id": "i", "determination": "deferred", "deferral_route": " "}, False),
    ({"item_id": "i", "determination": "rejected", "rejection_reason": None}, False),
    ({"item_id": "i", "determination": "rejected", "rejection_reason": []}, False),
    ({"item_id": "i", "determination": "applied", "unknown": "x"}, False),
])
def test_d4_determinations_retain_required_explanations(module, item, valid):
    execution = importlib.import_module(f"{module.__package__}.phase_d4_execution")
    before = json.dumps(item, sort_keys=True)
    if valid:
        assert execution._validate_determinations([item], "item_determinations") == [item]
    else:
        with pytest.raises(ValueError) as error:
            execution._validate_determinations([item], "item_determinations")
        assert str(error.value).strip()
    assert json.dumps(item, sort_keys=True) == before
