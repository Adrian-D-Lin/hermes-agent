"""Historical D4 authority evidence from Kanban v0.28 / Write-Gate lifecycle."""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
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
