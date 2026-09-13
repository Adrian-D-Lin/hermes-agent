"""Behavioral tests for v0.29 initiative coordination persistence."""

from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def coordination_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import yaml

    from hermes_cli.plugins import PluginManager

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["adrian-kanban"]}}),
        encoding="utf-8",
    )
    manager = PluginManager(scope_key=str(home.resolve()))
    manager.discover_and_load()
    loaded = manager._plugins["adrian-kanban"]
    assert loaded.enabled is True, loaded.error
    assert loaded.module is not None
    package = loaded.module
    modules: dict[str, ModuleType] = {
        "schema": importlib.import_module(f"{package.__name__}.schema"),
        "journal": importlib.import_module(f"{package.__name__}.journal"),
        "coordination": importlib.import_module(
            f"{package.__name__}.coordination_workspace"
        ),
        "materialization": importlib.import_module(
            f"{package.__name__}.coordination_materialization"
        ),
        "freshness": importlib.import_module(
            f"{package.__name__}.coordination_freshness"
        ),
        "phase_delivery": importlib.import_module(
            f"{package.__name__}.phase_delivery"
        ),
        "closure": importlib.import_module(
            f"{package.__name__}.coordination_closure"
        ),
        "closure_archive": importlib.import_module(
            f"{package.__name__}.closure_archive"
        ),
        "backfill": importlib.import_module(
            f"{package.__name__}.coordination_backfill"
        ),
        "workspace": importlib.import_module(f"{package.__name__}.workspace"),
    }
    try:
        yield modules
    finally:
        manager.unload("adrian-kanban")


def _connect(db: Path, schema: ModuleType) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    schema.create_schema(conn)
    conn.execute(
        "INSERT OR IGNORE INTO adrian_kanban_initiatives (initiative_id) "
        "VALUES ('init-1')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO adrian_kanban_cards "
        "(card_type, initiative_id, task_id, title, created_at, board_slug) "
        "VALUES ('initiative', 'init-1', NULL, 'Initiative 1', 1, 'board-1')"
    )
    conn.commit()
    return conn


def _plan(store, **overrides):
    values = {
        "initiative_id": "init-1",
        "project_id": "project-1",
        "repository_identity": "repo-1",
        "controller_binding_ref": "tracker:board-1:init-1",
        "planned_at": 10,
    }
    values.update(overrides)
    return store.plan_or_read(**values)


def _mark_workspace_materialized(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE initiative_coordination_workspaces SET "
        "lifecycle_state='materialized', updated_at=20 WHERE workspace_id=?",
        ("coord-init-1",),
    )
    conn.execute(
        "UPDATE initiative_coordination_workspace_members SET "
        "required_base_sha=?, observed_head=?, member_state='materialized', "
        "observed_at=20 WHERE workspace_id=? AND repository_identity=?",
        ("a" * 40, "b" * 40, "coord-init-1", "repo-1"),
    )
    conn.commit()


def test_plan_is_deterministic_and_idempotent(coordination_modules, tmp_path):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "coord.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        created = _plan(store)
        replayed = _plan(store, planned_at=99)
        assert created == replayed
        assert created["workspace_id"] == "coord-init-1"
        assert created["members"][0]["relative_path"] == (
            "init-1/coordination/repo-1"
        )
        assert created["members"][0]["branch"] == (
            "initiative/init-1/coordination/repo-1"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_coordination_workspaces"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_plan_replay_returns_current_lifecycle_and_evidence(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "current.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        _mark_workspace_materialized(conn)
        replayed = _plan(store, planned_at=30)
        assert replayed["lifecycle_state"] == "materialized"
        assert replayed["updated_at"] == 20
        assert replayed["members"][0]["member_state"] == "materialized"
        assert replayed["members"][0]["required_base_sha"] == "a" * 40
        assert replayed["members"][0]["observed_head"] == "b" * 40
    finally:
        conn.close()


def test_binding_version_advance_and_retry_are_guarded_and_idempotent(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "binding-version.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        _mark_workspace_materialized(conn)

        advanced = store.advance_binding_version("coord-init-1", 1, 30)
        retried = store.advance_binding_version("coord-init-1", 1, 99)

        assert advanced["binding_version"] == 2
        assert advanced["updated_at"] == 30
        assert advanced["members"][0]["member_state"] == "materialized"
        assert retried == advanced
        assert _plan(store, planned_at=100)["binding_version"] == 2
    finally:
        conn.close()


def test_add_planned_members_expands_planned_workspace_atomically(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "add-planned-members.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store, repository_identity="repo-primary")

        result = store.add_planned_members(
            "coord-init-1",
            ["repo-z", "repo-a"],
            expected_binding_version=1,
            at=30,
        )

        assert result["binding_version"] == 2
        assert result["lifecycle_state"] == "planned"
        assert result["updated_at"] == 30
        assert [
            member["repository_identity"] for member in result["members"]
        ] == ["repo-a", "repo-primary", "repo-z"]
        added = {
            member["repository_identity"]: member
            for member in result["members"]
            if member["repository_identity"] != "repo-primary"
        }
        assert added["repo-a"] == {
            "workspace_id": "coord-init-1",
            "repository_identity": "repo-a",
            "relative_path": "init-1/coordination/repo-a",
            "branch": "initiative/init-1/coordination/repo-a",
            "required_base_sha": None,
            "observed_head": None,
            "member_state": "planned",
            "failure_detail": None,
            "observed_at": 30,
        }
        assert added["repo-z"]["relative_path"] == (
            "init-1/coordination/repo-z"
        )
        replayed = _plan(
            store,
            repository_identity="repo-primary",
            planned_at=99,
        )
        assert replayed == result
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_add_planned_members_invalidates_materialized_binding_and_preserves_member(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "expand-materialized.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        _mark_workspace_materialized(conn)

        result = store.add_planned_members(
            "coord-init-1",
            ("repo-2",),
            expected_binding_version=1,
            at=30,
        )

        assert result["binding_version"] == 2
        assert result["lifecycle_state"] == "materializing"
        assert result["members"][0]["repository_identity"] == "repo-1"
        assert result["members"][0]["member_state"] == "materialized"
        assert result["members"][0]["required_base_sha"] == "a" * 40
        assert result["members"][0]["observed_head"] == "b" * 40
        assert result["members"][1]["repository_identity"] == "repo-2"
        assert result["members"][1]["member_state"] == "planned"
    finally:
        conn.close()


def test_add_planned_members_active_transaction_composes_with_outer_rollback(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "add-members-active-transaction.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        original = _plan(store)

        with pytest.raises(
            module.CoordinationWorkspaceError, match="requires an active transaction"
        ):
            store.add_planned_members_in_active_transaction(
                "coord-init-1",
                ["repo-2"],
                expected_binding_version=1,
                at=30,
            )

        conn.execute("BEGIN IMMEDIATE")
        changed = store.add_planned_members_in_active_transaction(
            "coord-init-1",
            ["repo-2"],
            expected_binding_version=1,
            at=30,
        )
        assert changed["binding_version"] == 2
        assert conn.in_transaction is True
        conn.rollback()

        assert store.read_active("init-1") == original
        assert conn.in_transaction is False
    finally:
        conn.close()


@pytest.mark.parametrize(
    "repository_identities",
    [
        [],
        (),
        "repo-2",
        {"repo-2"},
        {"repo": "repo-2"},
        ["repo-2", "repo-2"],
        [True],
        ["repo/escape"],
    ],
)
def test_add_planned_members_rejects_invalid_identity_sets(
    coordination_modules, tmp_path, repository_identities
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "invalid-member-set.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        original = _plan(store)
        with pytest.raises(module.CoordinationWorkspaceError):
            store.add_planned_members(
                "coord-init-1",
                repository_identities,
                expected_binding_version=1,
                at=30,
            )
        assert store.read_active("init-1") == original
        assert conn.in_transaction is False
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("setup", "expected_version", "match"),
    [
        ("existing", 1, "already exists"),
        ("stale", 2, "version mismatch"),
        ("wrong-state", 1, "invalid workspace state"),
    ],
)
def test_add_planned_members_rejects_existing_stale_and_wrong_state_without_mutation(
    coordination_modules, tmp_path, setup, expected_version, match
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / f"reject-member-{setup}.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        original = _plan(store)
        requested = ["repo-1"] if setup == "existing" else ["repo-2"]
        if setup == "wrong-state":
            conn.execute(
                "UPDATE initiative_coordination_workspaces "
                "SET lifecycle_state='failed' WHERE workspace_id='coord-init-1'"
            )
            conn.commit()
            original = store.read_active("init-1")

        with pytest.raises(module.CoordinationWorkspaceError, match=match):
            store.add_planned_members(
                "coord-init-1",
                requested,
                expected_binding_version=expected_version,
                at=30,
            )

        assert store.read_active("init-1") == original
        assert conn.in_transaction is False
    finally:
        conn.close()


@pytest.mark.parametrize("expected", [True, 0, -1, "1"])
def test_binding_version_advance_rejects_invalid_expected_version(
    coordination_modules, tmp_path, expected
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / f"bad-version-{expected}.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        _mark_workspace_materialized(conn)
        with pytest.raises(module.CoordinationWorkspaceError):
            store.advance_binding_version("coord-init-1", expected, 30)
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_binding_version_advance_rejects_wrong_state_and_large_mismatch(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "binding-state.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        with pytest.raises(module.CoordinationWorkspaceError, match="materialized"):
            store.advance_binding_version("coord-init-1", 1, 30)
        _mark_workspace_materialized(conn)
        conn.execute(
            "UPDATE initiative_coordination_workspaces SET binding_version=4 "
            "WHERE workspace_id='coord-init-1'"
        )
        conn.commit()
        with pytest.raises(module.CoordinationWorkspaceError, match="mismatch"):
            store.advance_binding_version("coord-init-1", 1, 40)
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_replace_member_freshness_is_guarded_and_idempotent(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "freshness-cas.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        _mark_workspace_materialized(conn)

        updated = store.replace_member_freshness(
            "coord-init-1",
            "repo-1",
            expected_observed_head="b" * 40,
            expected_required_base_sha="a" * 40,
            new_observed_head="c" * 40,
            new_required_base_sha="d" * 40,
            at=30,
        )
        replayed = store.replace_member_freshness(
            "coord-init-1",
            "repo-1",
            expected_observed_head="b" * 40,
            expected_required_base_sha="a" * 40,
            new_observed_head="c" * 40,
            new_required_base_sha="d" * 40,
            at=99,
        )

        assert updated["observed_head"] == "c" * 40
        assert updated["required_base_sha"] == "d" * 40
        assert updated["observed_at"] == 30
        assert replayed == updated
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_replace_member_freshness_rejects_stale_evidence_without_mutation(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "freshness-stale.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        _mark_workspace_materialized(conn)

        with pytest.raises(
            module.CoordinationWorkspaceError, match="do not match expected"
        ):
            store.replace_member_freshness(
                "coord-init-1",
                "repo-1",
                expected_observed_head="e" * 40,
                expected_required_base_sha="a" * 40,
                new_observed_head="c" * 40,
                new_required_base_sha="d" * 40,
                at=30,
            )

        row = conn.execute(
            "SELECT required_base_sha, observed_head, observed_at "
            "FROM initiative_coordination_workspace_members"
        ).fetchone()
        assert tuple(row) == ("a" * 40, "b" * 40, 20)
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_replace_member_freshness_requires_active_materialized_workspace(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "freshness-workspace-state.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        _mark_workspace_materialized(conn)
        conn.execute(
            "UPDATE initiative_coordination_workspaces SET lifecycle_state='failed'"
        )
        conn.commit()

        with pytest.raises(
            module.CoordinationWorkspaceError, match="lifecycle_state"
        ):
            store.replace_member_freshness(
                "coord-init-1",
                "repo-1",
                expected_observed_head="b" * 40,
                expected_required_base_sha="a" * 40,
                new_observed_head="c" * 40,
                new_required_base_sha="d" * 40,
                at=30,
            )
        assert conn.in_transaction is False
    finally:
        conn.close()


@pytest.mark.parametrize("field", ["expected_observed_head", "new_required_base_sha"])
def test_replace_member_freshness_rejects_invalid_sha(
    coordination_modules, tmp_path, field
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / f"freshness-invalid-{field}.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        _mark_workspace_materialized(conn)
        values = {
            "expected_observed_head": "b" * 40,
            "expected_required_base_sha": "a" * 40,
            "new_observed_head": "c" * 40,
            "new_required_base_sha": "d" * 40,
            "at": 30,
        }
        values[field] = "not-a-sha"
        with pytest.raises(module.CoordinationWorkspaceError, match=field):
            store.replace_member_freshness("coord-init-1", "repo-1", **values)
        assert conn.in_transaction is False
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initiative_id", "../escape"),
        ("project_id", ""),
        ("repository_identity", "repo/name"),
        ("controller_binding_ref", "binding ref"),
        ("planned_at", True),
    ],
)
def test_plan_rejects_unsafe_identity_components(
    coordination_modules, tmp_path, field, value
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / f"unsafe-{field}.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        with pytest.raises(module.CoordinationWorkspaceError):
            _plan(store, **{field: value})
        assert conn.in_transaction is False
    finally:
        conn.close()


@pytest.mark.parametrize(
    "bad_ref",
    [
        "tracker:board-1:init-1:extra",
        "tracker:board-1",
        "board-1:init-1",
        "tracker:board-1:init-2",
        "tracker:board name:init-1",
        "tracker:board-1:init/1",
        " tracker:board-1:init-1",
    ],
)
def test_plan_rejects_invalid_controller_reference(
    coordination_modules, tmp_path, bad_ref
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "invalid-controller.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        with pytest.raises(module.CoordinationWorkspaceError):
            _plan(store, controller_binding_ref=bad_ref)
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_plan_rejects_immutable_mismatch_and_rolls_back(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "mismatch.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        with pytest.raises(module.CoordinationWorkspaceError, match="mismatch"):
            _plan(store, project_id="project-2")
        assert conn.in_transaction is False
        assert store.read_active("init-1")["project_id"] == "project-1"
    finally:
        conn.close()


def test_read_active_orders_all_members(coordination_modules, tmp_path):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "members.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store, repository_identity="repo-z")
        conn.execute(
            "INSERT INTO initiative_coordination_workspace_members "
            "(workspace_id, repository_identity, relative_path, branch, "
            "member_state, observed_at) VALUES (?, ?, ?, ?, 'planned', ?)",
            (
                "coord-init-1",
                "repo-a",
                "init-1/coordination/repo-a",
                "initiative/init-1/coordination/repo-a",
                11,
            ),
        )
        conn.commit()
        active = store.read_active("init-1")
        assert [m["repository_identity"] for m in active["members"]] == [
            "repo-a",
            "repo-z",
        ]
    finally:
        conn.close()


def test_guarded_transitions_and_retirement(coordination_modules, tmp_path):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "transitions.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        with pytest.raises(module.CoordinationWorkspaceError, match="not allowed"):
            store.transition_member(
                "coord-init-1", "repo-1", "planned", "merged", 20
            )
        first = store.transition_member(
            "coord-init-1", "repo-1", "planned", "materializing", 20
        )
        replay = store.transition_member(
            "coord-init-1", "repo-1", "materializing", "materializing", 99
        )
        assert replay == first
        store.transition_workspace(
            "coord-init-1", "planned", "materializing", 20
        )
        store.transition_workspace(
            "coord-init-1", "materializing", "materialized", 30
        )
        store.transition_workspace(
            "coord-init-1", "materialized", "merged", 40
        )
        retired = store.transition_workspace(
            "coord-init-1", "merged", "retired", 50
        )
        assert retired["active"] == 0
        with pytest.raises(module.CoordinationWorkspaceError, match="no active"):
            store.read_active("init-1")
    finally:
        conn.close()


def test_failed_transition_requires_detail(coordination_modules, tmp_path):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "failure.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        store.transition_workspace(
            "coord-init-1", "planned", "materializing", 20
        )
        with pytest.raises(module.CoordinationWorkspaceError, match="required"):
            store.transition_workspace(
                "coord-init-1", "materializing", "failed", 30
            )
        failed = store.transition_workspace(
            "coord-init-1",
            "materializing",
            "failed",
            30,
            failure_detail="git worktree failed",
        )
        assert failed["failure_detail"] == "git worktree failed"
    finally:
        conn.close()


def test_pin_base_and_consume_materialization_are_exactly_idempotent(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "effects.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        pinned = store.pin_member_base(
            "coord-init-1", "repo-1", "a" * 40, 20
        )
        assert store.pin_member_base(
            "coord-init-1", "repo-1", "a" * 40, 99
        ) == pinned
        with pytest.raises(module.CoordinationWorkspaceError, match="mismatch"):
            store.pin_member_base("coord-init-1", "repo-1", "b" * 40, 21)
        store.transition_member(
            "coord-init-1", "repo-1", "planned", "materializing", 22
        )
        consumed = store.consume_materialization(
            "coord-init-1", "repo-1", "c" * 40, 23
        )
        assert store.consume_materialization(
            "coord-init-1", "repo-1", "c" * 40, 100
        ) == consumed
        with pytest.raises(module.CoordinationWorkspaceError, match="expected"):
            store.consume_materialization(
                "coord-init-1", "repo-1", "d" * 40, 24
            )
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_pin_and_consume_wrap_sqlite_failures(coordination_modules, tmp_path):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "sql-errors.db", schema)
    store = module.CoordinationWorkspaceStore(conn)
    _plan(store)
    conn.execute(
        "ALTER TABLE initiative_coordination_workspace_members "
        "RENAME TO unavailable_coordination_members"
    )
    conn.commit()
    try:
        with pytest.raises(module.CoordinationWorkspaceError):
            store.pin_member_base("coord-init-1", "repo-1", "a" * 40, 20)
        assert conn.in_transaction is False
    finally:
        conn.close()


def _event(**overrides):
    values = {
        "operation_id": "op-1",
        "idempotency_id": "idem-1",
        "member_target": "repo-1",
        "ordinal": 1,
        "operation_kind": "workspace_materialize",
        "workspace_id": "coord-init-1",
        "repository_identity": "repo-1",
        "state": "prepared",
        "intended_git_evidence": "base=abc;branch=coord",
        "intended_filesystem_evidence": "target=/tmp/coord",
        "actor_evidence": "tracker",
        "created_at": 20,
    }
    values.update(overrides)
    return values


def test_journal_replay_is_idempotent_and_mismatch_fails(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    conn = _connect(tmp_path / "journal.db", schema)
    try:
        store = module.CoordinationWorkspaceStore(conn)
        _plan(store)
        first = store.append_journal(_event())
        replay = store.append_journal(_event())
        assert replay == first
        with pytest.raises(module.CoordinationWorkspaceError, match="mismatched"):
            store.append_journal(_event(actor_evidence="different"))
        assert conn.in_transaction is False
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_coordination_operation_journal"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_shared_journal_engine_preserves_coordination_recovery_sequence(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    journal_module = coordination_modules["journal"]
    coordination = coordination_modules["coordination"]
    conn = _connect(tmp_path / "shared-journal.db", schema)
    try:
        _plan(coordination.CoordinationWorkspaceStore(conn))
        journal = journal_module.ExternalOperationJournal(
            conn, "initiative_coordination_operation_journal"
        )
        intent = journal_module.JournalIntent(
            operation_id="op-shared",
            idempotency_id="idem-shared",
            member_target="repo-1",
            operation_kind="workspace_materialize",
            workspace_id="coord-init-1",
            repository_identity="repo-1",
            intended_git_evidence="base=abc;branch=coord",
            intended_filesystem_evidence="target=/tmp/coord",
            actor_evidence="tracker",
            created_at=20,
        )
        conn.execute("BEGIN IMMEDIATE")
        journal.append_prepared(intent)
        conn.commit()
        assert journal.recovery_action("op-shared", "repo-1") == "verify"
        conn.execute("BEGIN IMMEDIATE")
        journal.append_failed(
            operation_id="op-shared",
            member_target="repo-1",
            observed_git_evidence=None,
            observed_filesystem_evidence="exists=false",
            error_disposition="effect absent after verification",
            recovery_disposition="resume",
            actor_evidence="tracker",
            created_at=21,
        )
        conn.commit()
        assert journal.recovery_action("op-shared", "repo-1") == "resume"
        conn.execute("BEGIN IMMEDIATE")
        journal.append_resume_prepared(
            operation_id="op-shared",
            member_target="repo-1",
            actor_evidence="tracker",
            created_at=22,
        )
        conn.commit()
        assert journal.head("op-shared", "repo-1").ordinal == 3
    finally:
        conn.close()


@pytest.mark.parametrize(
    "table_name", ["not_a_journal", ["external_operation_journal"]]
)
def test_shared_journal_engine_rejects_untrusted_table_name(
    coordination_modules, tmp_path, table_name
):
    schema = coordination_modules["schema"]
    journal_module = coordination_modules["journal"]
    conn = _connect(tmp_path / "invalid-table.db", schema)
    try:
        with pytest.raises(journal_module.JournalRejected, match="allowed"):
            journal_module.ExternalOperationJournal(conn, table_name)
    finally:
        conn.close()


def test_concurrent_planners_create_one_workspace(coordination_modules, tmp_path):
    schema = coordination_modules["schema"]
    module = coordination_modules["coordination"]
    db = tmp_path / "concurrent.db"
    seed = _connect(db, schema)
    seed.close()
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker():
        conn = sqlite3.connect(str(db), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            barrier.wait()
            results.append(_plan(module.CoordinationWorkspaceStore(conn)))
        except BaseException as exc:  # retained for assertion in parent thread
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert results[0]["workspace_id"] == results[1]["workspace_id"]
    check = sqlite3.connect(str(db))
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM initiative_coordination_workspaces"
        ).fetchone()[0] == 1
    finally:
        check.close()


def _materializer_fixture(coordination_modules, tmp_path, monkeypatch):
    schema = coordination_modules["schema"]
    coordination = coordination_modules["coordination"]
    workspace = coordination_modules["workspace"]
    conn = _connect(tmp_path / "materialize.db", schema)
    store = coordination.CoordinationWorkspaceStore(conn)
    _plan(store)
    repository_root = tmp_path / "repository"
    controlled_root = tmp_path / "AI-worktrees"
    repository_root.mkdir()
    controlled_root.mkdir()
    registration = workspace._RepositoryRegistration(
        repository_identity="repo-1",
        repository_root=str(repository_root.resolve()),
        controlled_worktree_root=str(controlled_root.resolve()),
    )
    registry = workspace._TrustedRepositoryRegistry((registration,))
    monkeypatch.setattr(
        workspace._SegmentWorkspaceController,
        "_resolve_origin_main",
        staticmethod(lambda _root: "a" * 40),
    )
    return conn, store, registry, repository_root, controlled_root


def _freshness_result(
    workspace,
    member,
    *,
    local,
    remote,
    recorded_ancestor=True,
    local_ancestor_remote=False,
    remote_ancestor_local=False,
    ready=True,
    failures=(),
):
    return workspace._MemberFreshness(
        repository_identity=member.repository_identity,
        target_path=member.target_path,
        recorded_head=member.observed_head,
        local_head=local,
        remote_head=remote,
        branch_matches=True,
        clean_including_untracked=ready,
        recorded_is_ancestor_of_local=recorded_ancestor,
        local_is_ancestor_of_remote=local_ancestor_remote,
        remote_is_ancestor_of_local=remote_ancestor_local,
        ready=ready,
        failures=failures,
    )


def test_freshness_controller_journals_remote_only_fast_forward(
    coordination_modules, tmp_path, monkeypatch
):
    freshness_module = coordination_modules["freshness"]
    workspace = coordination_modules["workspace"]
    conn, _, registry, repository_root, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_workspace_materialized(conn)

    class Executor(workspace._GitWorkspaceExecutor):
        def __init__(self):
            super().__init__()
            self.current = "b" * 40
            self.ff_calls = 0

        def inspect_freshness(self, member):
            return _freshness_result(
                workspace,
                member,
                local=self.current,
                remote="c" * 40,
                local_ancestor_remote=True,
            )

        def fast_forward_to_remote(
            self, member, *, expected_local_head, expected_remote_head
        ):
            assert conn.in_transaction is False
            assert expected_local_head == "b" * 40
            assert expected_remote_head == "c" * 40
            assert conn.execute(
                "SELECT state FROM initiative_coordination_operation_journal "
                "ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0] == "prepared"
            self.ff_calls += 1
            self.current = expected_remote_head
            return self.verify(member)

        def verify(self, member):
            return workspace._MemberVerification(
                member.repository_identity,
                member.target_path,
                self.current,
                True,
                True,
                str(repository_root.resolve()),
                True,
                (),
            )

        def _git(self, cwd, *args, allowed_returncodes=(0,)):
            if args == ("rev-parse", "HEAD"):
                return self.current
            if args == ("symbolic-ref", "--short", "HEAD"):
                return "initiative/init-1/coordination/repo-1"
            if args == ("status", "--porcelain", "--untracked-files=all"):
                return ""
            raise AssertionError(args)

    executor = Executor()
    controller = freshness_module.CoordinationFreshnessController(
        conn, registry, executor
    )
    try:
        result = controller.reconcile(
            initiative_id="init-1", actor_evidence="session:sess-1", at=30
        )
        assert executor.ff_calls == 1
        assert result["members"][0]["observed_head"] == "c" * 40
        assert result["members"][0]["required_base_sha"] == "c" * 40
        assert result["freshness_actions"] == [
            {
                "repository_identity": "repo-1",
                "action": "fast_forward",
                "from_head": "b" * 40,
                "to_head": "c" * 40,
            }
        ]
        assert [
            row[0]
            for row in conn.execute(
                "SELECT state FROM initiative_coordination_operation_journal "
                "ORDER BY ordinal"
            )
        ] == ["prepared", "verified"]
    finally:
        conn.close()


def test_freshness_controller_confirms_unexplained_local_advancement_once(
    coordination_modules, tmp_path, monkeypatch
):
    freshness_module = coordination_modules["freshness"]
    workspace = coordination_modules["workspace"]
    conn, _, registry, _, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_workspace_materialized(conn)
    confirmations = []

    class Executor(workspace._GitWorkspaceExecutor):
        def inspect_freshness(self, member):
            return _freshness_result(
                workspace,
                member,
                local="c" * 40,
                remote="a" * 40,
                recorded_ancestor=True,
                remote_ancestor_local=True,
            )

    controller = freshness_module.CoordinationFreshnessController(
        conn,
        registry,
        Executor(),
        advancement_confirmer=lambda items: confirmations.append(items) or True,
    )
    try:
        result = controller.reconcile(
            initiative_id="init-1", actor_evidence="session:sess-1", at=30
        )
        assert confirmations == [
            (
                {
                    "repository_identity": "repo-1",
                    "recorded_head": "b" * 40,
                    "local_head": "c" * 40,
                    "remote_head": "a" * 40,
                },
            )
        ]
        assert result["members"][0]["observed_head"] == "c" * 40
        assert result["members"][0]["required_base_sha"] == "a" * 40
    finally:
        conn.close()


def test_freshness_controller_surfaces_dirty_state_without_blocking_active_work(
    coordination_modules, tmp_path, monkeypatch
):
    freshness_module = coordination_modules["freshness"]
    workspace = coordination_modules["workspace"]
    conn, _, registry, _, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_workspace_materialized(conn)
    confirmations = []

    class DirtyExecutor(workspace._GitWorkspaceExecutor):
        def inspect_freshness(self, member):
            return _freshness_result(
                workspace,
                member,
                local="c" * 40,
                remote="a" * 40,
                remote_ancestor_local=True,
                ready=False,
                failures=("worktree_dirty",),
            )

    try:
        result = freshness_module.CoordinationFreshnessController(
            conn,
            registry,
            DirtyExecutor(),
            advancement_confirmer=lambda items: confirmations.append(items) or True,
        ).reconcile(
            initiative_id="init-1", actor_evidence="session:sess-1", at=30
        )
        assert result["members"][0]["observed_head"] == "b" * 40
        assert confirmations == []
        assert result["freshness_actions"] == [
            {
                "repository_identity": "repo-1",
                "action": "dirty_observed",
                "recorded_head": "b" * 40,
                "local_head": "c" * 40,
                "remote_head": "a" * 40,
            }
        ]
    finally:
        conn.close()


def test_phase_delivery_requires_clean_published_coordination_head(
    coordination_modules, tmp_path, monkeypatch
):
    delivery = coordination_modules["phase_delivery"]
    workspace = coordination_modules["workspace"]
    conn, _, registry, repository_root, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_workspace_materialized(conn)

    class Executor(workspace._GitWorkspaceExecutor):
        def __init__(self, status="", remote_head="b" * 40):
            super().__init__()
            self.status = status
            self.remote_head = remote_head

        def verify(self, member):
            return workspace._MemberVerification(
                member.repository_identity,
                member.target_path,
                "b" * 40,
                True,
                True,
                str(repository_root.resolve()),
                True,
                (),
            )

        def _git(self, cwd, *args, allowed_returncodes=(0,)):
            if args == ("status", "--porcelain", "--untracked-files=all"):
                return self.status
            if args == ("fetch", "--no-tags", "origin"):
                return ""
            if args == (
                "rev-parse",
                "refs/remotes/origin/initiative/init-1/coordination/repo-1",
            ):
                return self.remote_head
            raise AssertionError(args)

        def _is_ancestor(self, cwd, ancestor, descendant):
            return True

    try:
        proof = delivery.verify_transition_delivery(
            conn,
            registry,
            initiative_id="init-1",
            from_phase="D1",
            from_segment_id=None,
            phase_close_ref="phase-close-1",
            executor=Executor(),
        )
        assert proof["clean_including_untracked"] is True
        assert proof["members"][0]["head"] == "b" * 40
        assert proof["members"][0]["remote_head"] == "b" * 40

        with pytest.raises(delivery.PhaseDeliveryError, match="worktree is dirty"):
            delivery.verify_transition_delivery(
                conn,
                registry,
                initiative_id="init-1",
                from_phase="D1",
                from_segment_id=None,
                phase_close_ref="phase-close-1",
                executor=Executor(status="?? uncommitted.txt"),
            )

        with pytest.raises(delivery.PhaseDeliveryError, match="not the published"):
            delivery.verify_transition_delivery(
                conn,
                registry,
                initiative_id="init-1",
                from_phase="D1",
                from_segment_id=None,
                phase_close_ref="phase-close-1",
                executor=Executor(remote_head="c" * 40),
            )
    finally:
        conn.close()


def test_freshness_controller_decline_and_divergence_leave_tracker_unchanged(
    coordination_modules, tmp_path, monkeypatch
):
    freshness_module = coordination_modules["freshness"]
    workspace = coordination_modules["workspace"]
    conn, _, registry, _, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_workspace_materialized(conn)

    class LocalExecutor(workspace._GitWorkspaceExecutor):
        def inspect_freshness(self, member):
            return _freshness_result(
                workspace,
                member,
                local="c" * 40,
                remote="a" * 40,
                remote_ancestor_local=True,
            )

    try:
        controller = freshness_module.CoordinationFreshnessController(
            conn, registry, LocalExecutor(), advancement_confirmer=lambda _: False
        )
        with pytest.raises(
            freshness_module.CoordinationFreshnessError, match="declined"
        ):
            controller.reconcile(
                initiative_id="init-1", actor_evidence="session:sess-1", at=30
            )
        row = conn.execute(
            "SELECT required_base_sha, observed_head "
            "FROM initiative_coordination_workspace_members"
        ).fetchone()
        assert tuple(row) == ("a" * 40, "b" * 40)

        class DivergedExecutor(workspace._GitWorkspaceExecutor):
            def inspect_freshness(self, member):
                return _freshness_result(
                    workspace,
                    member,
                    local="c" * 40,
                    remote="d" * 40,
                    recorded_ancestor=True,
                )

        controller = freshness_module.CoordinationFreshnessController(
            conn, registry, DivergedExecutor(), advancement_confirmer=lambda _: True
        )
        with pytest.raises(
            freshness_module.CoordinationFreshnessError, match="genuine divergence"
        ):
            controller.reconcile(
                initiative_id="init-1", actor_evidence="session:sess-1", at=31
            )
        assert tuple(
            conn.execute(
                "SELECT required_base_sha, observed_head "
                "FROM initiative_coordination_workspace_members"
            ).fetchone()
        ) == ("a" * 40, "b" * 40)
    finally:
        conn.close()


def test_freshness_controller_recovers_fast_forward_before_tracker_cas(
    coordination_modules, tmp_path, monkeypatch
):
    freshness_module = coordination_modules["freshness"]
    journal_module = coordination_modules["journal"]
    workspace = coordination_modules["workspace"]
    conn, _, registry, _, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_workspace_materialized(conn)

    class Executor(workspace._GitWorkspaceExecutor):
        def inspect_freshness(self, member):
            return _freshness_result(
                workspace,
                member,
                local="c" * 40,
                remote="c" * 40,
                local_ancestor_remote=True,
                remote_ancestor_local=True,
            )

        def _git(self, cwd, *args, allowed_returncodes=(0,)):
            if args == ("rev-parse", "HEAD"):
                return "c" * 40
            if args == ("symbolic-ref", "--short", "HEAD"):
                return "initiative/init-1/coordination/repo-1"
            if args == ("status", "--porcelain", "--untracked-files=all"):
                return ""
            raise AssertionError(args)

    executor = Executor()
    controller = freshness_module.CoordinationFreshnessController(
        conn, registry, executor
    )
    operation_id = controller._derive_operation_id(
        "coord-init-1", "repo-1", "b" * 40, "c" * 40
    )
    member_path = str(
        (tmp_path / "AI-worktrees" / "init-1" / "coordination" / "repo-1").resolve()
    )
    intent = journal_module.JournalIntent(
        operation_id=operation_id,
        idempotency_id=operation_id,
        member_target="repo-1",
        operation_kind="coordination_freshness_ff",
        workspace_id="coord-init-1",
        repository_identity="repo-1",
        intended_git_evidence=controller._canonical_git_evidence(
            branch="initiative/init-1/coordination/repo-1",
            local="b" * 40,
            recorded="b" * 40,
            remote="c" * 40,
        ),
        intended_filesystem_evidence=f"target={member_path}",
        actor_evidence="session:sess-1",
        created_at=29,
    )
    journal = journal_module.ExternalOperationJournal(
        conn, "initiative_coordination_operation_journal"
    )
    conn.execute("BEGIN IMMEDIATE")
    journal.append_prepared(intent)
    conn.commit()

    try:
        result = controller.reconcile(
            initiative_id="init-1", actor_evidence="session:sess-1", at=30
        )
        assert result["members"][0]["observed_head"] == "c" * 40
        assert [
            row[0]
            for row in conn.execute(
                "SELECT state FROM initiative_coordination_operation_journal "
                "WHERE operation_id=? ORDER BY ordinal",
                (operation_id,),
            )
        ] == ["prepared", "verified"]
    finally:
        conn.close()


def test_materializer_journals_before_effect_and_is_idempotent(
    coordination_modules, tmp_path, monkeypatch
):
    materialization = coordination_modules["materialization"]
    workspace = coordination_modules["workspace"]
    conn, store, registry, repository_root, controlled_root = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )

    class FakeExecutor(workspace._GitWorkspaceExecutor):
        def __init__(self):
            super().__init__()
            self.exists = False
            self.materialize_calls = 0

        def verify(self, member):
            assert conn.in_transaction is False
            if not self.exists:
                return workspace._MemberVerification(
                    member.repository_identity,
                    member.target_path,
                    None,
                    False,
                    False,
                    None,
                    False,
                    ("member_absent",),
                )
            return workspace._MemberVerification(
                member.repository_identity,
                member.target_path,
                "b" * 40,
                True,
                True,
                str(repository_root.resolve()),
                True,
                (),
            )

        def materialize(self, member):
            assert conn.in_transaction is False
            assert conn.execute(
                "SELECT state FROM initiative_coordination_operation_journal "
                "ORDER BY ordinal DESC LIMIT 1"
            ).fetchone()[0] == "prepared"
            self.materialize_calls += 1
            self.exists = True
            return self.verify(member)

    executor = FakeExecutor()
    controller = materialization.CoordinationMaterializer(conn, registry, executor)
    try:
        result = controller.materialize(
            initiative_id="init-1", actor_evidence="session:sess-1", at=20
        )
        assert result["lifecycle_state"] == "materialized"
        assert result["members"][0]["member_state"] == "materialized"
        assert result["members"][0]["observed_head"] == "b" * 40
        assert result["member_roots"] == [
            str((controlled_root / "init-1" / "coordination" / "repo-1").resolve())
        ]
        assert executor.materialize_calls == 1
        assert [
            row[0]
            for row in conn.execute(
                "SELECT state FROM initiative_coordination_operation_journal "
                "ORDER BY ordinal"
            )
        ] == ["prepared", "verified"]

        replay = controller.materialize(
            initiative_id="init-1", actor_evidence="session:sess-1", at=99
        )
        assert replay["members"][0]["observed_at"] == 20
        assert executor.materialize_calls == 1
    finally:
        conn.close()


def test_materializer_completes_membership_expansion_without_recreating_existing_member(
    coordination_modules, tmp_path, monkeypatch
):
    materialization = coordination_modules["materialization"]
    coordination = coordination_modules["coordination"]
    workspace = coordination_modules["workspace"]
    schema = coordination_modules["schema"]
    conn = _connect(tmp_path / "materialize-expansion.db", schema)
    store = coordination.CoordinationWorkspaceStore(conn)
    _plan(store)
    _mark_workspace_materialized(conn)

    controlled_root = tmp_path / "AI-worktrees"
    controlled_root.mkdir()
    repository_roots = {}
    registrations = []
    for repository_identity in ("repo-1", "repo-2"):
        repository_root = tmp_path / f"source-{repository_identity}"
        repository_root.mkdir()
        repository_roots[repository_identity] = repository_root
        registrations.append(
            workspace._RepositoryRegistration(
                repository_identity=repository_identity,
                repository_root=str(repository_root.resolve()),
                controlled_worktree_root=str(controlled_root.resolve()),
            )
        )
    registry = workspace._TrustedRepositoryRegistry(tuple(registrations))
    monkeypatch.setattr(
        workspace._SegmentWorkspaceController,
        "_resolve_origin_main",
        staticmethod(lambda _root: "a" * 40),
    )

    store.add_planned_members(
        "coord-init-1",
        ["repo-2"],
        expected_binding_version=1,
        at=30,
    )

    class FakeExecutor(workspace._GitWorkspaceExecutor):
        def __init__(self):
            super().__init__()
            self.materialized = {"repo-1"}
            self.materialize_calls = []

        def verify(self, member):
            ready = member.repository_identity in self.materialized
            return workspace._MemberVerification(
                member.repository_identity,
                member.target_path,
                "b" * 40 if ready else None,
                ready,
                ready,
                (
                    str(repository_roots[member.repository_identity].resolve())
                    if ready
                    else None
                ),
                ready,
                () if ready else ("member_absent",),
            )

        def materialize(self, member):
            self.materialize_calls.append(member.repository_identity)
            self.materialized.add(member.repository_identity)
            return self.verify(member)

    executor = FakeExecutor()
    controller = materialization.CoordinationMaterializer(conn, registry, executor)
    try:
        result = controller.materialize(
            initiative_id="init-1",
            actor_evidence="session:sess-1",
            at=31,
        )

        assert result["lifecycle_state"] == "materialized"
        assert result["binding_version"] == 2
        assert [member["member_state"] for member in result["members"]] == [
            "materialized",
            "materialized",
        ]
        assert executor.materialize_calls == ["repo-2"]
        assert result["member_roots"] == [
            str(
                (
                    controlled_root
                    / "init-1"
                    / "coordination"
                    / repository_identity
                ).resolve()
            )
            for repository_identity in ("repo-1", "repo-2")
        ]
    finally:
        conn.close()


def test_materializer_resumes_safe_absence_without_duplicate_effect(
    coordination_modules, tmp_path, monkeypatch
):
    materialization = coordination_modules["materialization"]
    workspace = coordination_modules["workspace"]
    conn, store, registry, repository_root, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )

    class RecoveringExecutor(workspace._GitWorkspaceExecutor):
        def __init__(self):
            super().__init__()
            self.allow_success = False

        def verify(self, member):
            if not self.allow_success:
                return workspace._MemberVerification(
                    member.repository_identity,
                    member.target_path,
                    None,
                    False,
                    False,
                    None,
                    False,
                    ("member_absent",),
                )
            return workspace._MemberVerification(
                member.repository_identity,
                member.target_path,
                "c" * 40,
                True,
                True,
                str(repository_root.resolve()),
                True,
                (),
            )

        def materialize(self, member):
            if not self.allow_success:
                raise workspace._WorkspaceRejected("simulated absent effect")
            return self.verify(member)

    executor = RecoveringExecutor()
    controller = materialization.CoordinationMaterializer(conn, registry, executor)
    try:
        with pytest.raises(
            materialization.CoordinationMaterializationError,
            match="effect absent",
        ):
            controller.materialize(
                initiative_id="init-1", actor_evidence="session:sess-1", at=20
            )
        assert store.read_active("init-1")["lifecycle_state"] == "failed"
        executor.allow_success = True
        result = controller.materialize(
            initiative_id="init-1", actor_evidence="session:sess-1", at=30
        )
        assert result["lifecycle_state"] == "materialized"
        assert [
            row[0]
            for row in conn.execute(
                "SELECT state FROM initiative_coordination_operation_journal "
                "ORDER BY ordinal"
            )
        ] == ["prepared", "failed", "prepared", "verified"]
    finally:
        conn.close()


def test_materialized_replay_rejects_persisted_head_drift(
    coordination_modules, tmp_path, monkeypatch
):
    materialization = coordination_modules["materialization"]
    workspace = coordination_modules["workspace"]
    conn, store, registry, repository_root, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    store.pin_member_base("coord-init-1", "repo-1", "a" * 40, 20)
    store.transition_member(
        "coord-init-1", "repo-1", "planned", "materializing", 20
    )
    store.consume_materialization("coord-init-1", "repo-1", "b" * 40, 20)
    store.transition_workspace("coord-init-1", "planned", "materializing", 20)
    store.transition_workspace(
        "coord-init-1", "materializing", "materialized", 20
    )

    class DriftExecutor(workspace._GitWorkspaceExecutor):
        def verify(self, member):
            return workspace._MemberVerification(
                member.repository_identity,
                member.target_path,
                "d" * 40,
                True,
                True,
                str(repository_root.resolve()),
                True,
                (),
            )

    controller = materialization.CoordinationMaterializer(
        conn, registry, DriftExecutor()
    )
    try:
        with pytest.raises(
            materialization.CoordinationMaterializationError,
            match="evidence mismatch",
        ):
            controller.materialize(
                initiative_id="init-1", actor_evidence="session:sess-1", at=30
            )
    finally:
        conn.close()


def _ready_member_verification(workspace, member):
    return workspace._MemberVerification(
        member.repository_identity,
        member.target_path,
        member.observed_head,
        True,
        True,
        str(Path(member.repository_root).resolve()),
        True,
        (),
    )


def _ready_merge_verification(workspace, member, merge_head="c" * 40):
    return workspace._MergeVerification(
        member.repository_identity,
        member.target_path,
        member.observed_head,
        merge_head,
        merge_head,
        True,
        True,
        True,
        True,
        (),
    )


def test_coordination_closure_merges_journals_and_consumes_member(
    coordination_modules, tmp_path, monkeypatch
):
    closure = coordination_modules["closure"]
    workspace = coordination_modules["workspace"]
    conn, store, registry, _, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_workspace_materialized(conn)

    class Executor(workspace._GitWorkspaceExecutor):
        def __init__(self):
            super().__init__()
            self.merge_calls = 0

        def _git(self, cwd, *args, allowed_returncodes=(0,)):
            assert args == ("status", "--porcelain")
            return ""

        def verify(self, member):
            return _ready_member_verification(workspace, member)

        def merge_to_origin_main(
            self, member, *, expected_main_sha, expected_source_head
        ):
            assert conn.in_transaction is False
            assert expected_main_sha == "a" * 40
            assert expected_source_head == "b" * 40
            assert conn.execute(
                "SELECT state FROM initiative_coordination_operation_journal "
                "ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0] == "prepared"
            self.merge_calls += 1
            return _ready_merge_verification(workspace, member)

    executor = Executor()
    controller = closure.CoordinationClosureController(conn, registry, executor)
    try:
        result = controller.merge_all(
            initiative_id="init-1", actor_evidence="session:sess-1", at=30
        )
        assert executor.merge_calls == 1
        assert result["lifecycle_state"] == "merged"
        assert result["members"][0]["member_state"] == "merged"
        assert result["merge_heads"] == {"repo-1": "c" * 40}
        assert [
            row[0]
            for row in conn.execute(
                "SELECT state FROM initiative_coordination_operation_journal "
                "ORDER BY ordinal"
            )
        ] == ["prepared", "verified"]
    finally:
        conn.close()


def test_coordination_closure_preflights_all_members_before_first_effect(
    coordination_modules, tmp_path, monkeypatch
):
    closure = coordination_modules["closure"]
    coordination = coordination_modules["coordination"]
    workspace = coordination_modules["workspace"]
    conn, store, _, repository_root, controlled_root = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    store.add_planned_members(
        "coord-init-1", ["repo-2"], expected_binding_version=1, at=11
    )
    conn.execute(
        "UPDATE initiative_coordination_workspaces SET "
        "lifecycle_state='materialized', updated_at=20 WHERE workspace_id=?",
        ("coord-init-1",),
    )
    conn.execute(
        "UPDATE initiative_coordination_workspace_members SET "
        "required_base_sha=?, observed_head=?, member_state='materialized', "
        "observed_at=20 WHERE workspace_id=?",
        ("a" * 40, "b" * 40, "coord-init-1"),
    )
    conn.commit()
    registration_type = workspace._RepositoryRegistration
    registry = workspace._TrustedRepositoryRegistry(
        (
            registration_type(
                "repo-1", str(repository_root.resolve()), str(controlled_root.resolve())
            ),
            registration_type(
                "repo-2", str(repository_root.resolve()), str(controlled_root.resolve())
            ),
        )
    )

    class Executor(workspace._GitWorkspaceExecutor):
        def __init__(self):
            super().__init__()
            self.merge_calls = 0

        def _git(self, cwd, *args, allowed_returncodes=(0,)):
            return "untracked.txt" if "repo-2" in cwd else ""

        def verify(self, member):
            return _ready_member_verification(workspace, member)

        def merge_to_origin_main(self, *args, **kwargs):
            self.merge_calls += 1
            raise AssertionError("effect must not begin before full preflight")

    executor = Executor()
    try:
        with pytest.raises(closure.CoordinationClosureError, match="dirty"):
            closure.CoordinationClosureController(
                conn, registry, executor
            ).merge_all(
                initiative_id="init-1", actor_evidence="session:sess-1", at=30
            )
        assert executor.merge_calls == 0
        assert store.read_active("init-1")["lifecycle_state"] == "materialized"
    finally:
        conn.close()


def test_coordination_closure_resumes_failed_effect_without_duplicate_state(
    coordination_modules, tmp_path, monkeypatch
):
    closure = coordination_modules["closure"]
    workspace = coordination_modules["workspace"]
    conn, store, registry, _, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_workspace_materialized(conn)

    class Executor(workspace._GitWorkspaceExecutor):
        def __init__(self):
            super().__init__()
            self.fail = True
            self.merge_calls = 0

        def _git(self, cwd, *args, allowed_returncodes=(0,)):
            return ""

        def verify(self, member):
            return _ready_member_verification(workspace, member)

        def merge_to_origin_main(self, member, **kwargs):
            self.merge_calls += 1
            if self.fail:
                raise workspace._WorkspaceRejected("simulated merge failure")
            return _ready_merge_verification(workspace, member)

    executor = Executor()
    controller = closure.CoordinationClosureController(conn, registry, executor)
    try:
        with pytest.raises(closure.CoordinationClosureError, match="simulated"):
            controller.merge_all(
                initiative_id="init-1", actor_evidence="session:sess-1", at=30
            )
        assert store.read_active("init-1")["members"][0]["member_state"] == (
            "materialized"
        )
        executor.fail = False
        result = controller.merge_all(
            initiative_id="init-1", actor_evidence="session:sess-1", at=31
        )
        assert executor.merge_calls == 2
        assert result["lifecycle_state"] == "merged"
        assert [
            row[0]
            for row in conn.execute(
                "SELECT state FROM initiative_coordination_operation_journal "
                "ORDER BY ordinal"
            )
        ] == ["prepared", "failed", "prepared", "verified"]
    finally:
        conn.close()


def test_coordination_closure_replay_verifies_without_journal_or_state_writes(
    coordination_modules, tmp_path, monkeypatch
):
    closure = coordination_modules["closure"]
    workspace = coordination_modules["workspace"]
    conn, _, registry, _, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_workspace_materialized(conn)

    class Executor(workspace._GitWorkspaceExecutor):
        def _git(self, cwd, *args, allowed_returncodes=(0,)):
            return ""

        def verify(self, member):
            return _ready_member_verification(workspace, member)

        def verify_merge(self, member, **kwargs):
            return _ready_merge_verification(workspace, member)

        def merge_to_origin_main(self, member, **kwargs):
            return _ready_merge_verification(workspace, member)

    controller = closure.CoordinationClosureController(conn, registry, Executor())
    try:
        first = controller.merge_all(
            initiative_id="init-1", actor_evidence="session:sess-1", at=30
        )
        journal_count = conn.execute(
            "SELECT COUNT(*) FROM initiative_coordination_operation_journal"
        ).fetchone()[0]
        second = controller.merge_all(
            initiative_id="init-1", actor_evidence="session:sess-1", at=31
        )
        assert first["merge_heads"] == second["merge_heads"]
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_coordination_operation_journal"
        ).fetchone()[0] == journal_count
        assert second["lifecycle_state"] == "merged"
    finally:
        conn.close()


def _mark_initiative_closed_with_merged_coordination(conn):
    conn.execute(
        "UPDATE adrian_kanban_cards SET closed_at=25 "
        "WHERE initiative_id='init-1' AND task_id IS NULL"
    )
    conn.execute(
        "UPDATE initiative_coordination_workspaces SET "
        "lifecycle_state='merged', updated_at=25 WHERE workspace_id='coord-init-1'"
    )
    conn.execute(
        "UPDATE initiative_coordination_workspace_members SET "
        "required_base_sha=?, observed_head=?, member_state='merged', "
        "observed_at=25 WHERE workspace_id='coord-init-1'",
        ("a" * 40, "b" * 40),
    )
    conn.commit()


def test_closure_operation_journal_enforces_order_and_failed_resume(
    coordination_modules, tmp_path
):
    schema = coordination_modules["schema"]
    archive = coordination_modules["closure_archive"]
    conn = _connect(tmp_path / "closure-journal.db", schema)
    journal = archive.ClosureOperationJournal(conn)
    common = {
        "operation_id": "initiative-close-init-1",
        "initiative_id": "init-1",
        "workspace_id": None,
        "actor_evidence": "session:sess-1",
        "at": 30,
    }
    try:
        conn.execute("BEGIN IMMEDIATE")
        journal.append_state(state="prepared", evidence={"ready": True}, **common)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        journal.append_failed(
            evidence={"failed_stage": "merged"},
            error_disposition="merge unavailable",
            recovery_stage="merged",
            **common,
        )
        conn.commit()
        assert journal.next_stage(common["operation_id"]) == "merged"
        conn.execute("BEGIN IMMEDIATE")
        journal.append_state(state="merged", evidence={"members": []}, **common)
        conn.commit()
        assert [event["state"] for event in journal.history(common["operation_id"])] == [
            "prepared",
            "failed",
            "merged",
        ]
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(archive.ClosureArchiveError, match="expected 'closed'"):
            journal.append_state(state="retired", evidence={}, **common)
        conn.rollback()
    finally:
        conn.close()


def test_archive_controller_runs_verified_archive_before_retirement(
    coordination_modules, tmp_path, monkeypatch
):
    archive = coordination_modules["closure_archive"]
    workspace = coordination_modules["workspace"]
    conn, _, registry, repository_root, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_initiative_closed_with_merged_coordination(conn)
    calls = []

    class Executor(archive._GitArchiveExecutor):
        def archive(self, plan):
            calls.append("archive")
            assert plan.tracker_export["initiative_id"] == "init-1"
            assert plan.archive_relative_path == "5-archive/init-1"
            assert plan.members[0].member_state == "merged"
            return {
                "archive_commit_sha": "c" * 40,
                "archive_remote_sha": "c" * 40,
                "manifest_sha256": "d" * 64,
                "archive_relative_path": plan.archive_relative_path,
                "file_count": 1,
            }

        def verify_archive(self, plan, evidence):
            calls.append("verify_archive")
            assert evidence["manifest_sha256"] == "d" * 64
            return evidence

        def retire_archive_worktree(self, plan):
            calls.append("retire_archive_worktree")

        def retire_member(self, member):
            calls.append(f"retire:{member.repository_identity}")

    projects = lambda: [
        {
            "id": "project-1",
            "name": "Project 1",
            "board_slug": "board-1",
            "primary_path": str(repository_root.resolve()),
            "archived": False,
        }
    ]
    controller = archive.InitiativeArchiveController(
        conn, registry, projects, Executor()
    )
    try:
        result = controller.finalize(
            initiative_id="init-1", actor_evidence="session:sess-1", at=30
        )
        assert result["state"] == "consumed"
        assert calls == [
            "archive",
            "verify_archive",
            "retire_archive_worktree",
            "retire:repo-1",
            "verify_archive",
        ]
        assert [event["state"] for event in result["history"]] == [
            "prepared",
            "merged",
            "closed",
            "archive_verified",
            "retired",
            "consumed",
        ]
        workspace_row = conn.execute(
            "SELECT lifecycle_state,active FROM initiative_coordination_workspaces"
        ).fetchone()
        assert tuple(workspace_row) == ("retired", 0)
        assert conn.execute(
            "SELECT member_state FROM initiative_coordination_workspace_members"
        ).fetchone()[0] == "retired"
    finally:
        conn.close()


def test_archive_controller_records_failure_and_resumes_archive_stage(
    coordination_modules, tmp_path, monkeypatch
):
    archive = coordination_modules["closure_archive"]
    conn, _, registry, repository_root, _ = _materializer_fixture(
        coordination_modules, tmp_path, monkeypatch
    )
    _mark_initiative_closed_with_merged_coordination(conn)

    class Executor(archive._GitArchiveExecutor):
        def __init__(self):
            super().__init__()
            self.fail = True
            self.archive_calls = 0

        def archive(self, plan):
            self.archive_calls += 1
            if self.fail:
                raise archive.ClosureArchiveError("simulated archive failure")
            return {
                "archive_commit_sha": "c" * 40,
                "archive_remote_sha": "c" * 40,
                "manifest_sha256": "d" * 64,
                "archive_relative_path": plan.archive_relative_path,
                "file_count": 1,
            }

        def verify_archive(self, plan, evidence):
            return evidence

        def retire_archive_worktree(self, plan):
            return None

        def retire_member(self, member):
            return None

    executor = Executor()
    projects = lambda: [
        {
            "id": "project-1",
            "board_slug": "board-1",
            "primary_path": str(repository_root.resolve()),
        }
    ]
    controller = archive.InitiativeArchiveController(
        conn, registry, projects, executor
    )
    try:
        with pytest.raises(archive.ClosureArchiveError, match="simulated"):
            controller.finalize(
                initiative_id="init-1", actor_evidence="session:sess-1", at=30
            )
        assert [
            row[0]
            for row in conn.execute(
                "SELECT state FROM initiative_closure_operation_journal "
                "ORDER BY ordinal"
            )
        ] == ["prepared", "merged", "closed", "failed"]
        executor.fail = False
        result = controller.finalize(
            initiative_id="init-1", actor_evidence="session:sess-1", at=31
        )
        assert executor.archive_calls == 2
        assert result["state"] == "consumed"
        assert [event["state"] for event in result["history"]] == [
            "prepared",
            "merged",
            "closed",
            "failed",
            "archive_verified",
            "retired",
            "consumed",
        ]
    finally:
        conn.close()


def _git(cwd: Path, *args: str, text: bool = True):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=text,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_real_git_phase_delivery_proves_remote_head_and_rejects_untracked(
    coordination_modules, tmp_path
):
    delivery = coordination_modules["phase_delivery"]
    coordination = coordination_modules["coordination"]
    schema = coordination_modules["schema"]
    workspace = coordination_modules["workspace"]

    remote = tmp_path / "delivery-remote.git"
    repository_root = tmp_path / "delivery-repository"
    controlled_root = tmp_path / "delivery-worktrees"
    remote.mkdir()
    repository_root.mkdir()
    controlled_root.mkdir()
    _git(remote, "init", "--bare")
    _git(repository_root, "init", "-b", "main")
    _git(repository_root, "config", "user.name", "Delivery Test")
    _git(repository_root, "config", "user.email", "delivery@example.invalid")
    (repository_root / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository_root, "add", "README.md")
    _git(repository_root, "commit", "-m", "base")
    _git(repository_root, "remote", "add", "origin", str(remote))
    _git(repository_root, "push", "-u", "origin", "main")
    base_head = _git(repository_root, "rev-parse", "HEAD").strip()

    branch = "initiative/init-1/coordination/repo-1"
    target = controlled_root / "init-1" / "coordination" / "repo-1"
    target.parent.mkdir(parents=True)
    _git(repository_root, "worktree", "add", "-b", branch, str(target), base_head)
    (target / "delivered.txt").write_text("delivered\n", encoding="utf-8")
    _git(target, "add", "delivered.txt")
    _git(target, "commit", "-m", "deliver phase")
    _git(target, "push", "-u", "origin", branch)
    delivered_head = _git(target, "rev-parse", "HEAD").strip()

    conn = _connect(tmp_path / "delivery.sqlite3", schema)
    registry = workspace._TrustedRepositoryRegistry(
        (
            workspace._RepositoryRegistration(
                "repo-1",
                str(repository_root.resolve()),
                str(controlled_root.resolve()),
            ),
        )
    )
    store = coordination.CoordinationWorkspaceStore(conn)
    planned = store.plan_or_read(
        initiative_id="init-1",
        project_id="project-1",
        repository_identity="repo-1",
        controller_binding_ref="tracker:board-1:init-1",
        planned_at=10,
    )
    conn.execute(
        "UPDATE initiative_coordination_workspaces SET lifecycle_state='materialized' "
        "WHERE workspace_id=?",
        (planned["workspace_id"],),
    )
    conn.execute(
        "UPDATE initiative_coordination_workspace_members SET "
        "required_base_sha=?, observed_head=?, member_state='materialized' "
        "WHERE workspace_id=? AND repository_identity='repo-1'",
        (base_head, delivered_head, planned["workspace_id"]),
    )
    conn.commit()

    try:
        proof = delivery.verify_transition_delivery(
            conn,
            registry,
            initiative_id="init-1",
            from_phase="D1",
            from_segment_id=None,
            phase_close_ref="phase-close-1",
        )
        assert proof["members"][0]["head"] == delivered_head
        assert proof["members"][0]["remote_head"] == delivered_head

        (target / "untracked.txt").write_text("not delivered\n", encoding="utf-8")
        with pytest.raises(delivery.PhaseDeliveryError, match="worktree is dirty"):
            delivery.verify_transition_delivery(
                conn,
                registry,
                initiative_id="init-1",
                from_phase="D1",
                from_segment_id=None,
                phase_close_ref="phase-close-1",
            )
    finally:
        conn.close()


def test_git_archive_executor_exports_changed_files_pushes_and_then_retires(
    coordination_modules, tmp_path
):
    archive = coordination_modules["closure_archive"]
    coordination = coordination_modules["coordination"]
    schema = coordination_modules["schema"]
    workspace = coordination_modules["workspace"]

    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repository_root = tmp_path / "repository"
    controlled_root = tmp_path / "AI-worktrees"
    remote.mkdir()
    seed.mkdir()
    controlled_root.mkdir()
    _git(remote, "init", "--bare")
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.name", "Archive Test")
    _git(seed, "config", "user.email", "archive@example.invalid")
    (seed / "old.txt").write_text("remove me\n", encoding="utf-8")
    (seed / "keep.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", "-b", "main", str(remote), str(repository_root))
    _git(repository_root, "config", "user.name", "Archive Test")
    _git(repository_root, "config", "user.email", "archive@example.invalid")
    base = _git(repository_root, "rev-parse", "HEAD").strip()

    coordination_path = controlled_root / "init-1" / "coordination" / "repo-1"
    coordination_path.parent.mkdir(parents=True)
    branch = "initiative/init-1/coordination/repo-1"
    _git(
        repository_root,
        "worktree",
        "add",
        "-b",
        branch,
        str(coordination_path),
        base,
    )
    (coordination_path / "old.txt").unlink()
    (coordination_path / "keep.txt").write_text("updated\n", encoding="utf-8")
    (coordination_path / "decision.md").write_text(
        "# Decision\n\nRationale retained.\n", encoding="utf-8"
    )
    _git(coordination_path, "add", "-A")
    _git(coordination_path, "commit", "-m", "initiative source")
    source = _git(coordination_path, "rev-parse", "HEAD").strip()
    _git(repository_root, "merge", "--no-ff", "--no-edit", source)
    _git(repository_root, "push", "origin", "main")

    conn = _connect(tmp_path / "archive-real.db", schema)
    store = coordination.CoordinationWorkspaceStore(conn)
    store.plan_or_read(
        initiative_id="init-1",
        project_id="project-1",
        repository_identity="repo-1",
        controller_binding_ref="tracker:board-1:init-1",
        planned_at=10,
    )
    conn.execute(
        "UPDATE adrian_kanban_cards SET closed_at=25 "
        "WHERE initiative_id='init-1' AND task_id IS NULL"
    )
    conn.execute(
        "UPDATE initiative_coordination_workspaces SET "
        "lifecycle_state='merged', updated_at=25 WHERE workspace_id='coord-init-1'"
    )
    conn.execute(
        "UPDATE initiative_coordination_workspace_members SET "
        "required_base_sha=?, observed_head=?, member_state='merged', "
        "observed_at=25 WHERE workspace_id='coord-init-1'",
        (base, source),
    )
    conn.commit()
    registry = workspace._TrustedRepositoryRegistry(
        (
            workspace._RepositoryRegistration(
                "repo-1",
                str(repository_root.resolve()),
                str(controlled_root.resolve()),
            ),
        )
    )
    projects = lambda: [
        {
            "id": "project-1",
            "board_slug": "board-1",
            "primary_path": str(repository_root.resolve()),
        }
    ]
    try:
        result = archive.InitiativeArchiveController(
            conn, registry, projects
        ).finalize(
            initiative_id="init-1", actor_evidence="session:sess-1", at=30
        )
        assert result["state"] == "consumed"
        assert not coordination_path.exists()
        assert not (
            controlled_root / "init-1" / "archive" / "repo-1"
        ).exists()
        _git(repository_root, "fetch", "origin")
        tracker = json.loads(
            _git(
                repository_root,
                "show",
                "origin/main:5-archive/init-1/tracker.json",
            )
        )
        assert tracker["initiative_id"] == "init-1"
        assert _git(
            repository_root,
            "show",
            "origin/main:5-archive/init-1/repositories/repo-1/decision.md",
        ).startswith("# Decision")
        manifest = json.loads(
            _git(
                repository_root,
                "show",
                "origin/main:5-archive/init-1/manifest.json",
            )
        )
        assert manifest["dispositions"] == [
            {
                "disposition": "deleted",
                "original_path": "old.txt",
                "repository_identity": "repo-1",
                "source_commit": source,
            }
        ]
        assert any(
            item["original_path"] == "decision.md"
            for item in manifest["files"]
        )
    finally:
        conn.close()


def test_coordination_backfill_preflights_then_creates_only_primary_members(
    coordination_modules, tmp_path
):
    backfill = coordination_modules["backfill"]
    schema = coordination_modules["schema"]
    workspace = coordination_modules["workspace"]
    conn = _connect(tmp_path / "backfill.db", schema)
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    controlled = tmp_path / "AI-worktrees"
    primary.mkdir()
    secondary.mkdir()
    controlled.mkdir()
    registry = workspace._TrustedRepositoryRegistry(
        (
            workspace._RepositoryRegistration(
                "repo-primary", str(primary.resolve()), str(controlled.resolve())
            ),
            workspace._RepositoryRegistration(
                "repo-secondary", str(secondary.resolve()), str(controlled.resolve())
            ),
        )
    )
    projects = [
        {
            "id": "project-1",
            "board_slug": "board-1",
            "primary_path": str(primary.resolve()),
        },
        {
            "id": "unbound-project",
            "board_slug": None,
            "primary_path": None,
        },
    ]
    try:
        planned = backfill.plan_coordination_backfill(conn, registry, projects)
        assert [item["initiative_id"] for item in planned["planned"]] == [
            "init-1"
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_coordination_workspaces"
        ).fetchone()[0] == 0

        result = backfill.apply_coordination_backfill(
            conn, registry, projects, at=30
        )
        assert result["created"][0]["workspace_id"] == "coord-init-1"
        assert [
            row["repository_identity"]
            for row in conn.execute(
                "SELECT repository_identity FROM "
                "initiative_coordination_workspace_members"
            ).fetchall()
        ] == ["repo-primary"]

        replay = backfill.apply_coordination_backfill(
            conn, registry, projects, at=99
        )
        assert replay["created"] == []
        assert replay["existing"][0]["workspace_id"] == "coord-init-1"
    finally:
        conn.close()


def test_coordination_backfill_mapping_failure_writes_nothing(
    coordination_modules, tmp_path
):
    backfill = coordination_modules["backfill"]
    schema = coordination_modules["schema"]
    workspace = coordination_modules["workspace"]
    conn = _connect(tmp_path / "backfill-fail.db", schema)
    primary = tmp_path / "primary"
    controlled = tmp_path / "AI-worktrees"
    primary.mkdir()
    controlled.mkdir()
    registry = workspace._TrustedRepositoryRegistry(
        (
            workspace._RepositoryRegistration(
                "repo-primary", str(primary.resolve()), str(controlled.resolve())
            ),
        )
    )
    try:
        with pytest.raises(backfill.CoordinationBackfillError, match="no project"):
            backfill.apply_coordination_backfill(
                conn,
                registry,
                [
                    {
                        "id": "different-project",
                        "board_slug": "different-board",
                        "primary_path": str(primary.resolve()),
                    }
                ],
                at=30,
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM initiative_coordination_workspaces"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_coordination_backfill_path_runner_plans_without_writes_then_applies(
    coordination_modules, tmp_path
):
    backfill = coordination_modules["backfill"]
    schema = coordination_modules["schema"]
    workspace = coordination_modules["workspace"]
    database_path = tmp_path / "runner.db"
    primary = tmp_path / "primary"
    controlled = tmp_path / "AI-worktrees"
    primary.mkdir()
    controlled.mkdir()
    registry = workspace._TrustedRepositoryRegistry(
        (
            workspace._RepositoryRegistration(
                "repo-primary", str(primary.resolve()), str(controlled.resolve())
            ),
        )
    )
    projects = [
        {
            "id": "project-1",
            "board_slug": "board-1",
            "primary_path": str(primary.resolve()),
        }
    ]
    conn = _connect(database_path, schema)
    conn.close()

    planned = backfill.run_coordination_backfill(
        str(database_path.resolve()), registry, projects
    )
    assert planned["mode"] == "plan"
    assert [item["initiative_id"] for item in planned["planned"]] == ["init-1"]
    check = sqlite3.connect(database_path)
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM initiative_coordination_workspaces"
        ).fetchone()[0] == 0
    finally:
        check.close()

    applied = backfill.run_coordination_backfill(
        str(database_path.resolve()), registry, projects, apply=True, at=50
    )
    assert applied["mode"] == "apply"
    assert applied["created"][0]["workspace_id"] == "coord-init-1"
