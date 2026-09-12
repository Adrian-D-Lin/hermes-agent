"""Design-oracle tests for the trusted Session Startup anchor resolver."""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    package = loaded.module
    assert package is not None
    result: dict[str, ModuleType] = {
        "anchor": importlib.import_module(
            f"{package.__name__}.session_startup_anchor"
        ),
        "schema": importlib.import_module(f"{package.__name__}.schema"),
        "workspace": importlib.import_module(f"{package.__name__}.workspace"),
    }
    try:
        yield result
    finally:
        manager.unload("adrian-kanban")


def _seed_database(path: Path, schema: ModuleType) -> None:
    conn = sqlite3.connect(str(path))
    try:
        schema.create_schema(conn)
        conn.execute(
            "INSERT INTO adrian_kanban_initiatives (initiative_id) VALUES (?)",
            ("initiative-1",),
        )
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, created_at, board_slug) "
            "VALUES ('initiative', ?, NULL, 'Initiative One', 1, ?)",
            ("initiative-1", "board-1"),
        )
        conn.commit()
    finally:
        conn.close()


def _registry(workspace: ModuleType, root: Path, *, duplicate: bool = False):
    registrations = [
        workspace._RepositoryRegistration(
            repository_identity="repo-1",
            repository_root=str(root),
            controlled_worktree_root=str(root.parent / "worktrees"),
        )
    ]
    if duplicate:
        registrations.append(
            workspace._RepositoryRegistration(
                repository_identity="repo-2",
                repository_root=str(root),
                controlled_worktree_root=str(root.parent / "worktrees"),
            )
        )
    return workspace._TrustedRepositoryRegistry(tuple(registrations))


class _FakeMaterializer:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def materialize(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _FakeFreshness:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def reconcile(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _freshness_factory_for(materializer):
    return lambda _conn, _confirmer: _FakeFreshness(materializer.result)


def _inputs(root: Path):
    project = {
        "id": "project-1",
        "name": "Project One",
        "board_slug": "board-1",
        "primary_path": str(root),
    }
    initiative = {
        "initiative_id": "initiative-1",
        "title": "Initiative One",
        "current_phase": "DEV2",
        "current_segment_id": "S1",
    }
    record = {"session_id": "session-1"}
    return project, initiative, record


def _resolver(modules, database: Path, root: Path, materializer, confirm, **kwargs):
    kwargs.setdefault("freshness_factory", _freshness_factory_for(materializer))
    return modules["anchor"].SessionStartupAnchorResolver(
        tracker_database_path=str(database),
        registry=_registry(modules["workspace"], root),
        materializer_factory=lambda _conn: materializer,
        confirm_trusted_logical_binding=confirm,
        epoch_provider=lambda: 100,
        **kwargs,
    )


def _seed_materialized_workspace(
    database: Path,
    schema: ModuleType,
    *,
    binding_version: int = 1,
    controller_binding_ref: str = "tracker:board-1:initiative-1",
) -> None:
    _seed_database(database, schema)
    conn = sqlite3.connect(str(database))
    try:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards "
            "WHERE initiative_id = ? AND task_id IS NULL",
            ("initiative-1",),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO initiative_coordination_workspaces "
            "(workspace_id, initiative_card_id, initiative_id, project_id, "
            "lifecycle_state, controller_binding_ref, binding_version, active, "
            "failure_detail, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'materialized', ?, ?, 1, NULL, 1, 1)",
            (
                "coord-initiative-1",
                card_id,
                "initiative-1",
                "project-1",
                controller_binding_ref,
                binding_version,
            ),
        )
        conn.execute(
            "INSERT INTO initiative_coordination_workspace_members "
            "(workspace_id, repository_identity, relative_path, branch, "
            "required_base_sha, observed_head, member_state, failure_detail, "
            "observed_at) VALUES (?, ?, ?, ?, ?, ?, 'materialized', NULL, 1)",
            (
                "coord-initiative-1",
                "repo-1",
                "initiative-1/coordination/repo-1",
                "initiative/initiative-1/coordination/repo-1",
                "a" * 40,
                "b" * 40,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _anchored_inputs(root: Path):
    project, initiative, record = _inputs(root)
    record.update(
        {
            "selected_project_id": "project-1",
            "selected_initiative_id": "initiative-1",
            "accepted_phase": "DEV2",
            "accepted_segment_id": "S1",
            "logical_workspace_id": "coord-initiative-1",
            "writegate_binding_version": "1",
            "writegate_binding_ref": "42",
        }
    )
    return project, initiative, record


def _binding(member_roots, **overrides):
    values = {
        "id": 42,
        "project": "project-1",
        "initiative": "initiative-1",
        "board": "board-1",
        "logical_workspace_id": "coord-initiative-1",
        "member_roots": tuple(member_roots),
        "binding_version": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _revalidator(modules, database, root, materializer, binding_loader):
    return modules["anchor"].SessionStartupAnchorResolver(
        tracker_database_path=str(database),
        registry=_registry(modules["workspace"], root),
        materializer_factory=lambda _conn: materializer,
        freshness_factory=_freshness_factory_for(materializer),
        active_binding_loader=binding_loader,
        epoch_provider=lambda: 100,
    )


def test_happy_composition_uses_only_authoritative_fields(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_database(database, modules["schema"])
    member_root = tmp_path / "worktrees" / "initiative-1" / "coordination" / "repo-1"
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(member_root)],
        }
    )
    confirmations = []

    def confirm(**kwargs):
        confirmations.append(kwargs)
        return SimpleNamespace(id=42)

    resolver = _resolver(modules, database, root, materializer, confirm)
    project, initiative, record = _inputs(root)
    project["chat_claimed_path"] = str(tmp_path / "untrusted")
    record["cwd"] = str(tmp_path / "untrusted")

    result = resolver.resolve(project, initiative, record)

    assert result == {
        "accepted_phase": "DEV2",
        "accepted_segment_id": "S1",
        "logical_workspace_id": "coord-initiative-1",
        "writegate_binding_version": "1",
        "writegate_binding_ref": "42",
    }
    assert materializer.calls == [
        {
            "initiative_id": "initiative-1",
            "actor_evidence": "session-startup:session-1",
            "at": 100,
        }
    ]
    assert confirmations == [
        {
            "session_id": "session-1",
            "project": "project-1",
            "initiative": "initiative-1",
            "board": "board-1",
            "logical_workspace_id": "coord-initiative-1",
            "member_roots": (str(member_root),),
            "binding_version": 1,
        }
    ]


def test_repository_match_is_canonical_equality_not_prefix(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_database(database, modules["schema"])
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(tmp_path / "member")],
        }
    )
    resolver = _resolver(
        modules,
        database,
        root,
        materializer,
        lambda **_kwargs: SimpleNamespace(id=42),
    )
    project, initiative, record = _inputs(root / ".." / "primary")
    assert resolver.resolve(project, initiative, record)["accepted_phase"] == "DEV2"

    project["primary_path"] = str(root / "child")
    with pytest.raises(
        modules["anchor"].SessionStartupAnchorError,
        match="no trusted registration",
    ):
        resolver.resolve(project, initiative, record)


def test_unknown_and_ambiguous_repository_fail_closed(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_database(database, modules["schema"])
    anchor = modules["anchor"]
    materializer = _FakeMaterializer({})
    project, initiative, record = _inputs(tmp_path / "unknown")
    resolver = _resolver(
        modules, database, root, materializer, lambda **_kwargs: None
    )
    with pytest.raises(anchor.SessionStartupAnchorError, match="no trusted"):
        resolver.resolve(project, initiative, record)

    ambiguous = anchor.SessionStartupAnchorResolver(
        tracker_database_path=str(database),
        registry=_registry(modules["workspace"], root, duplicate=True),
        materializer_factory=lambda _conn: materializer,
        confirm_trusted_logical_binding=lambda **_kwargs: None,
        epoch_provider=lambda: 100,
    )
    project["primary_path"] = str(root)
    with pytest.raises(anchor.SessionStartupAnchorError, match="ambiguous"):
        ambiguous.resolve(project, initiative, record)


def test_declined_approval_fails_and_connection_is_closed(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_database(database, modules["schema"])
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(tmp_path / "member")],
        }
    )
    opened = []

    def connection_factory(path):
        conn = sqlite3.connect(path)
        opened.append(conn)
        return conn

    resolver = _resolver(
        modules,
        database,
        root,
        materializer,
        lambda **_kwargs: None,
        connection_factory=connection_factory,
    )
    with pytest.raises(
        modules["anchor"].SessionStartupAnchorError, match="declined"
    ):
        resolver.resolve(*_inputs(root))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize("binding_id", [None, True, 0, -1, "42"])
def test_malformed_binding_id_fails_closed(modules, tmp_path, binding_id):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_database(database, modules["schema"])
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(tmp_path / "member")],
        }
    )
    resolver = _resolver(
        modules,
        database,
        root,
        materializer,
        lambda **_kwargs: SimpleNamespace(id=binding_id),
    )
    with pytest.raises(
        modules["anchor"].SessionStartupAnchorError,
        match="binding_record.id must be a positive integer",
    ):
        resolver.resolve(*_inputs(root))


@pytest.mark.parametrize(
    ("target", "value", "message"),
    [
        ("project", [], "project must be a dict"),
        ("initiative", [], "initiative must be a dict"),
        ("record", [], "record must be a dict"),
        ("segment", "", "current_segment_id must be a nonblank"),
    ],
)
def test_malformed_authoritative_input_is_rejected(
    modules, tmp_path, target, value, message
):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_database(database, modules["schema"])
    materializer = _FakeMaterializer({})
    resolver = _resolver(
        modules, database, root, materializer, lambda **_kwargs: None
    )
    project, initiative, record = _inputs(root)
    if target == "project":
        project = value
    elif target == "initiative":
        initiative = value
    elif target == "record":
        record = value
    else:
        initiative["current_segment_id"] = value
    with pytest.raises(modules["anchor"].SessionStartupAnchorError, match=message):
        resolver.resolve(project, initiative, record)


def test_revalidate_unchanged_anchor_is_read_only(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    member_root = tmp_path / "worktrees" / "initiative-1" / "coordination" / "repo-1"
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(member_root)],
        }
    )
    resolver = _revalidator(
        modules,
        database,
        root,
        materializer,
        lambda _session_id: _binding([str(member_root)]),
    )

    result = resolver.revalidate(*_anchored_inputs(root))

    assert result == {
        "classification": "unchanged",
        "reasons": [],
        "accepted_phase": "DEV2",
        "accepted_segment_id": "S1",
        "logical_workspace_id": "coord-initiative-1",
        "writegate_binding_version": "1",
        "writegate_binding_ref": "42",
    }
    assert materializer.calls == [
        {
            "initiative_id": "initiative-1",
            "actor_evidence": "session-startup-revalidate:session-1",
            "at": 100,
        }
    ]


@pytest.mark.parametrize(
    ("record_field", "value", "message"),
    [
        ("selected_project_id", "other-project", "selected_project_id"),
        ("selected_initiative_id", "other-initiative", "selected_initiative_id"),
    ],
)
def test_revalidate_rejects_stored_selection_identity_mismatch(
    modules, tmp_path, record_field, value, message
):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    member_root = tmp_path / "member"
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(member_root)],
        }
    )
    resolver = _revalidator(
        modules,
        database,
        root,
        materializer,
        lambda _session_id: _binding([str(member_root)]),
    )
    project, initiative, record = _anchored_inputs(root)
    record[record_field] = value

    with pytest.raises(
        modules["anchor"].SessionStartupAnchorError,
        match=message,
    ):
        resolver.revalidate(project, initiative, record)


def test_revalidate_lifecycle_change_requires_focused_revalidation(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    member_root = tmp_path / "member"
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(member_root)],
        }
    )
    resolver = _revalidator(
        modules,
        database,
        root,
        materializer,
        lambda _session_id: _binding([str(member_root)]),
    )
    project, initiative, record = _anchored_inputs(root)
    initiative["current_phase"] = "DEV3"
    initiative["current_segment_id"] = "S2"

    result = resolver.revalidate(project, initiative, record)

    assert result["classification"] == "focused_revalidation_required"
    assert result["reasons"][-2:] == ["phase_changed", "segment_changed"]
    assert result["accepted_phase"] == "DEV3"
    assert result["accepted_segment_id"] == "S2"


def test_revalidate_missing_binding_is_repairable(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    member_root = tmp_path / "member"
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(member_root)],
        }
    )
    resolver = _revalidator(
        modules, database, root, materializer, lambda _session_id: None
    )

    result = resolver.revalidate(*_anchored_inputs(root))

    assert result["classification"] == "focused_revalidation_required"
    assert result["reasons"] == ["active_binding_missing"]
    assert result["writegate_binding_ref"] is None


def test_revalidate_stale_valid_binding_is_repairable(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    member_root = tmp_path / "member"
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(member_root)],
        }
    )
    resolver = _revalidator(
        modules,
        database,
        root,
        materializer,
        lambda _session_id: _binding([str(member_root)], id=43, board="old-board"),
    )

    result = resolver.revalidate(*_anchored_inputs(root))

    assert result["classification"] == "focused_revalidation_required"
    assert result["reasons"] == ["binding_board_changed", "binding_ref_changed"]
    assert result["writegate_binding_ref"] == "43"


def test_revalidate_root_order_preserves_primary_member_semantics(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    first = str(tmp_path / "first")
    second = str(tmp_path / "second")
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [first, second],
        }
    )
    resolver = _revalidator(
        modules,
        database,
        root,
        materializer,
        lambda _session_id: _binding([second, first]),
    )

    result = resolver.revalidate(*_anchored_inputs(root))

    assert result["classification"] == "focused_revalidation_required"
    assert "binding_membership_changed" in result["reasons"]


@pytest.mark.parametrize(
    ("workspace_field", "value", "message"),
    [
        ("workspace_id", "coord-other", "materialized workspace_id"),
        ("controller_binding_ref", "tracker:other-board:initiative-1", "controller_binding_ref"),
    ],
)
def test_revalidate_rejects_corrupt_workspace_identity(
    modules, tmp_path, workspace_field, value, message
):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(
        database,
        modules["schema"],
        controller_binding_ref=(
            value if workspace_field == "controller_binding_ref"
            else "tracker:board-1:initiative-1"
        ),
    )
    member_root = tmp_path / "member"
    materializer = _FakeMaterializer(
        {
            "workspace_id": value if workspace_field == "workspace_id" else "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(member_root)],
        }
    )
    resolver = _revalidator(
        modules,
        database,
        root,
        materializer,
        lambda _session_id: _binding([str(member_root)]),
    )

    with pytest.raises(
        modules["anchor"].SessionStartupAnchorError,
        match=message,
    ):
        resolver.revalidate(*_anchored_inputs(root))


def _replacement_resolver(
    modules, database, root, materializer, confirm, *, binding_loader=None
):
    return modules["anchor"].SessionStartupAnchorResolver(
        tracker_database_path=str(database),
        registry=_registry(modules["workspace"], root),
        materializer_factory=lambda _conn: materializer,
        freshness_factory=_freshness_factory_for(materializer),
        confirm_trusted_logical_binding=confirm,
        active_binding_loader=binding_loader,
        epoch_provider=lambda: 100,
    )


def test_replace_lifecycle_change_bumps_version_exactly_once_across_retry(
    modules, tmp_path
):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    member_root = tmp_path / "member"
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 2,
            "member_roots": [str(member_root)],
        }
    )
    confirmations = []

    def confirm(**kwargs):
        confirmations.append(kwargs)
        return SimpleNamespace(id=43)

    resolver = _replacement_resolver(
        modules, database, root, materializer, confirm
    )
    project, initiative, record = _anchored_inputs(root)
    initiative["current_phase"] = "DEV3"

    first = resolver.replace(project, initiative, record)
    second = resolver.replace(project, initiative, record)

    assert first["writegate_binding_version"] == "2"
    assert second == first
    assert [item["binding_version"] for item in confirmations] == [2, 2]
    conn = sqlite3.connect(str(database))
    try:
        assert conn.execute(
            "SELECT binding_version FROM initiative_coordination_workspaces"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_resolve_existing_workspace_reconciles_before_exact_verification(
    modules, tmp_path
):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    member_root = tmp_path / "member"
    result = {
        "workspace_id": "coord-initiative-1",
        "binding_version": 1,
        "member_roots": [str(member_root)],
    }
    events = []

    class Materializer(_FakeMaterializer):
        def materialize(self, **kwargs):
            events.append("materializer")
            return super().materialize(**kwargs)

    class Freshness(_FakeFreshness):
        def reconcile(self, **kwargs):
            events.append("freshness")
            return super().reconcile(**kwargs)

    materializer = Materializer(result)
    resolver = modules["anchor"].SessionStartupAnchorResolver(
        tracker_database_path=str(database),
        registry=_registry(modules["workspace"], root),
        materializer_factory=lambda _conn: materializer,
        freshness_factory=lambda _conn, _confirm: Freshness(result),
        confirm_trusted_logical_binding=lambda **_kwargs: events.append("confirm")
        or SimpleNamespace(id=42),
        epoch_provider=lambda: 100,
    )

    resolver.resolve(*_inputs(root))
    assert events == ["freshness", "materializer", "confirm"]


def test_replace_freshness_failure_leaves_binding_version_unchanged(
    modules, tmp_path
):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    materializer = _FakeMaterializer({})

    class FailedFreshness:
        def reconcile(self, **_kwargs):
            raise RuntimeError("dirty member repo-1")

    resolver = modules["anchor"].SessionStartupAnchorResolver(
        tracker_database_path=str(database),
        registry=_registry(modules["workspace"], root),
        materializer_factory=lambda _conn: materializer,
        freshness_factory=lambda _conn, _confirm: FailedFreshness(),
        confirm_trusted_logical_binding=lambda **_kwargs: SimpleNamespace(id=42),
        epoch_provider=lambda: 100,
    )
    project, initiative, record = _anchored_inputs(root)
    initiative["current_phase"] = "DEV3"

    with pytest.raises(
        modules["anchor"].SessionStartupAnchorError, match="dirty member"
    ):
        resolver.replace(project, initiative, record)
    conn = sqlite3.connect(str(database))
    try:
        assert conn.execute(
            "SELECT binding_version FROM initiative_coordination_workspaces"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_default_advancement_confirmer_uses_deterministic_once_only_request(
    modules, monkeypatch
):
    calls = []
    import tools.approval as approval

    monkeypatch.setattr(
        approval,
        "request_write_gate_approval",
        lambda **kwargs: calls.append(kwargs)
        or {"approved": True, "decision": "once"},
    )
    advancements = (
        {
            "repository_identity": "repo-1",
            "recorded_head": "a" * 40,
            "local_head": "b" * 40,
            "remote_head": "a" * 40,
        },
    )

    assert modules["anchor"]._default_workspace_advancement_confirmer(
        "session-1", advancements
    ) is True
    assert modules["anchor"]._default_workspace_advancement_confirmer(
        "session-1", advancements
    ) is True
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0]["request_id"].startswith("coordination-advancement-")
    assert calls[0]["session_key"] == "session-1"
    assert calls[0]["timeout_seconds"] == 300
    assert '"repository_identity":"repo-1"' in calls[0]["command"]


def test_replace_unchanged_lifecycle_does_not_bump_version(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    member_root = tmp_path / "member"
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 1,
            "member_roots": [str(member_root)],
        }
    )
    resolver = _replacement_resolver(
        modules,
        database,
        root,
        materializer,
        lambda **_kwargs: SimpleNamespace(id=43),
    )

    result = resolver.replace(*_anchored_inputs(root))

    assert result["writegate_binding_version"] == "1"
    conn = sqlite3.connect(str(database))
    try:
        assert conn.execute(
            "SELECT binding_version FROM initiative_coordination_workspaces"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_replace_rejects_workspace_mismatch_before_confirmation(modules, tmp_path):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    confirmations = []
    materializer = _FakeMaterializer({})
    resolver = _replacement_resolver(
        modules,
        database,
        root,
        materializer,
        lambda **kwargs: confirmations.append(kwargs),
    )
    project, initiative, record = _anchored_inputs(root)
    initiative["current_phase"] = "DEV3"
    record["logical_workspace_id"] = "coord-other"

    with pytest.raises(
        modules["anchor"].SessionStartupAnchorError,
        match="logical_workspace_id",
    ):
        resolver.replace(project, initiative, record)
    assert confirmations == []
    assert materializer.calls == []


def test_replace_decline_after_lifecycle_change_leaves_version_advanced(
    modules, tmp_path
):
    database = tmp_path / "tracker.db"
    root = tmp_path / "primary"
    _seed_materialized_workspace(database, modules["schema"])
    member_root = tmp_path / "member"
    materializer = _FakeMaterializer(
        {
            "workspace_id": "coord-initiative-1",
            "binding_version": 2,
            "member_roots": [str(member_root)],
        }
    )
    resolver = _replacement_resolver(
        modules, database, root, materializer, lambda **_kwargs: None
    )
    project, initiative, record = _anchored_inputs(root)
    initiative["current_phase"] = "DEV3"

    with pytest.raises(
        modules["anchor"].SessionStartupAnchorError,
        match="declined",
    ):
        resolver.replace(project, initiative, record)
    conn = sqlite3.connect(str(database))
    try:
        assert conn.execute(
            "SELECT binding_version FROM initiative_coordination_workspaces"
        ).fetchone()[0] == 2
    finally:
        conn.close()
