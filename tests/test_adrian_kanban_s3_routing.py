from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def routing_modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_routing"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    yield {
        "routing": importlib.import_module(f"{package_name}.routing"),
        "schema": importlib.import_module(f"{package_name}.schema"),
    }
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


class _Registry:
    def __init__(self, binding):
        self.binding = binding

    def get_active_binding(self, session_id):
        assert session_id == "session-1"
        return self.binding


def _resolver(
    routing,
    database_path,
    binding,
    *,
    boards=(),
):
    return routing.ModelToolBoardResolver(
        str(database_path),
        registry_getter=lambda: _Registry(binding),
        projects_connector=lambda: contextlib.nullcontext(object()),
        board_lister=lambda: list(boards),
    )


def _database(tmp_path, schema):
    path = (tmp_path / "kanban.sqlite3").resolve()
    with sqlite3.connect(path) as conn:
        schema.create_schema(conn)
    return path


def test_project_binding_precedes_default_workdir_route(
    routing_modules,
    tmp_path,
    monkeypatch,
):
    routing = routing_modules["routing"]
    database_path = _database(tmp_path, routing_modules["schema"])
    project = SimpleNamespace(
        id="project-1",
        slug="orchestrator-project",
        board_slug="orchestrator",
    )
    monkeypatch.setattr(routing._projects, "get_project", lambda *_: project)
    binding = SimpleNamespace(
        project="project-1",
        board="orchestrator",
        worktree_path=str(tmp_path),
        producer="confirm_binding",
    )
    resolver = _resolver(
        routing,
        database_path,
        binding,
        boards=[{"slug": "wrong", "default_workdir": str(tmp_path)}],
    )

    assert resolver(
        "kanban_create",
        {"task_id": "task-1", "project": "orchestrator-project"},
        {"session_id": "session-1"},
    ) == ("orchestrator", None)


def test_unique_exact_default_workdir_is_fallback_and_ambiguity_rejects(
    routing_modules,
    tmp_path,
    monkeypatch,
):
    routing = routing_modules["routing"]
    database_path = _database(tmp_path, routing_modules["schema"])
    project = SimpleNamespace(
        id="project-1",
        slug="project",
        board_slug=None,
    )
    monkeypatch.setattr(routing._projects, "project_for_path", lambda *_: project)
    binding = SimpleNamespace(
        project=None,
        board=None,
        worktree_path=str(tmp_path),
        producer="confirm_binding",
    )
    one = _resolver(
        routing,
        database_path,
        binding,
        boards=[{"slug": "orchestrator", "default_workdir": str(tmp_path)}],
    )
    ambiguous = _resolver(
        routing,
        database_path,
        binding,
        boards=[
            {"slug": "orchestrator", "default_workdir": str(tmp_path)},
            {"slug": "duplicate", "default_workdir": str(tmp_path)},
        ],
    )

    assert one(
        "kanban_list", {}, {"session_id": "session-1"}
    ) == ("orchestrator", None)
    with pytest.raises(
        routing.RouteResolutionRejected,
        match="multiple boards",
    ):
        ambiguous("kanban_list", {}, {"session_id": "session-1"})


def test_dispatcher_binding_is_pinned_and_public_project_cannot_be_forged(
    routing_modules,
    tmp_path,
):
    routing = routing_modules["routing"]
    database_path = _database(tmp_path, routing_modules["schema"])
    binding = SimpleNamespace(
        project="project-1",
        board="orchestrator",
        worktree_path=str(tmp_path),
        producer="dispatcher",
    )
    resolver = _resolver(routing, database_path, binding)

    assert resolver(
        "kanban_heartbeat",
        {"task_id": "task-1"},
        {"session_id": "session-1"},
    ) == ("orchestrator", None)
    with pytest.raises(
        routing.RouteResolutionRejected,
        match="cannot verify public project",
    ):
        resolver(
            "kanban_create",
            {"task_id": "task-1", "project": "forged"},
            {"session_id": "session-1"},
        )


def test_task_contract_supplies_logical_workspace_without_path_inference(
    routing_modules,
    tmp_path,
):
    routing = routing_modules["routing"]
    schema = routing_modules["schema"]
    database_path = _database(tmp_path, schema)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO task_lifecycle_contracts ("
            "contract_id, contract_version, step, task_card_id, task_id, "
            "initiative_card_id, initiative_id, segment_id, workspace_id, "
            "execution_profile, canonical_contract_payload, registry_hash, "
            "skill_id, skill_version, skill_hash, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "contract-1",
                "1",
                "DEV2.1",
                1,
                "task-1",
                1,
                "initiative-1",
                "S1",
                "workspace-S1",
                "builder-tester",
                "{}",
                "registry-hash",
                "dev2-build",
                "1",
                "skill-hash",
                1,
            ),
        )
    binding = SimpleNamespace(
        project=None,
        board="orchestrator",
        worktree_path=str(tmp_path),
        producer="dispatcher",
    )
    resolver = _resolver(routing, database_path, binding)

    assert resolver(
        "kanban_heartbeat",
        {"task_id": "task-1"},
        {"session_id": "session-1"},
    ) == ("orchestrator", "workspace-S1")


def test_expected_version_uses_unified_card_and_canonical_board(
    routing_modules,
):
    routing = routing_modules["routing"]
    schema = routing_modules["schema"]
    conn = sqlite3.connect(":memory:")
    try:
        schema.create_schema(conn)
        conn.execute(
            "INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')"
        )
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, created_at, "
            "board_slug, record_version) "
            "VALUES ('initiative', 'initiative-1', NULL, 'Initiative', 1, "
            "'orchestrator', 3)"
        )
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, created_at, "
            "board_slug, record_version) "
            "VALUES ('task', 'initiative-1', 'task-1', 'Task', 1, "
            "'orchestrator', 7)"
        )

        assert routing.resolve_expected_version(
            conn,
            "kanban_comment",
            "task-1",
            {"board": "orchestrator"},
        ) == 7
        assert routing.resolve_expected_version(
            conn,
            "kanban_transition_initiative",
            "initiative-1",
            {"board": "orchestrator"},
        ) == 3
        assert routing.resolve_expected_version(
            conn,
            "kanban_create",
            "task-new",
            {"board": "orchestrator"},
        ) == 0
        with pytest.raises(
            routing.RouteResolutionRejected,
            match="does not match resolved board",
        ):
            routing.resolve_expected_version(
                conn,
                "kanban_comment",
                "task-1",
                {"board": "wrong"},
            )
    finally:
        conn.close()
