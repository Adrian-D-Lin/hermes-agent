"""Production registration tests derived from the S3 release contract."""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb


@pytest.fixture()
def package():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_registration"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    yield module
    kb.clear_authority_providers()
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            sys.modules.pop(name, None)


class _Registration:
    def dispose(self):
        return None


class _Context:
    def __init__(self):
        self.tools = []
        self.hooks = []

    def register_tool(self, **fields):
        self.tools.append(fields)
        return _Registration()

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))
        return _Registration()


def _configure_workspace_registry(package, monkeypatch, tmp_path):
    controlled_root = str((tmp_path / "controlled-worktrees").resolve())
    repository_root = str((tmp_path / "repository").resolve())
    monkeypatch.setattr(
        package._config,
        "load_config_readonly",
        lambda: {
            "kanban": {
                "controlled_worktree_root": controlled_root,
                "repository_registry": {
                    "orchestrator": {"repository_root": repository_root}
                },
            }
        },
    )
    return controlled_root, repository_root


def test_register_is_inert_while_native_authority_is_selected(
    package, monkeypatch
):
    monkeypatch.setattr(package._kb, "resolve_selected_authority", lambda: "native")
    context = _Context()

    assert package.register(context) is None

    assert context.tools == []
    assert context.hooks == []


def test_register_rejects_unknown_authority(package, monkeypatch):
    monkeypatch.setattr(package._kb, "resolve_selected_authority", lambda: "other")

    with pytest.raises(RuntimeError, match="authority"):
        package.register(_Context())


def test_register_wires_one_complete_plugin_authority_runtime(
    package, monkeypatch, tmp_path
):
    database_path = str((tmp_path / "kanban.db").resolve())
    monkeypatch.setattr(
        package._kb,
        "resolve_selected_authority",
        lambda: "adrian-kanban",
    )
    monkeypatch.setattr(
        package._kb,
        "resolve_authority_path",
        lambda **_kwargs: database_path,
    )
    controlled_root, repository_root = _configure_workspace_registry(
        package, monkeypatch, tmp_path
    )
    context = _Context()

    assert package.register(context) is None

    assert {item["name"] for item in context.tools} == set(
        importlib.import_module(f"{package.__name__}.commands").RECOGNIZED_OPERATIONS
    )
    assert all(item["override"] is True for item in context.tools)
    assert [name for name, _callback in context.hooks] == [
        "pre_tool_call",
        "pre_user_turn",
    ]
    status = package._kb.provider_status(database_path)
    assert status.present is True
    assert status.healthy is True
    with sqlite3.connect(database_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "adrian_kanban_cards" in tables
    assert "adrian_kanban_command_receipts" in tables
    provider = package._provider_cache[database_path]
    assert provider._workspace_registry.controlled_worktree_root == controlled_root
    registration = provider._workspace_registry.lookup("orchestrator")
    assert registration.repository_root == repository_root

    delegated = package._kb.delegate_authority_operation(
        "not-a-kanban-operation",
        attempt_id="registration-read-1",
    )
    assert delegated["result"] == "REJECTED", delegated
    assert delegated["operation"] == "not-a-kanban-operation"
    assert delegated["failed_checks"][0]["code"] == "UNRECOGNIZED_OPERATION"


def test_repeated_registration_reuses_one_provider_instance(
    package, monkeypatch, tmp_path
):
    database_path = str((tmp_path / "kanban.db").resolve())
    monkeypatch.setattr(
        package._kb,
        "resolve_selected_authority",
        lambda: "adrian-kanban",
    )
    monkeypatch.setattr(
        package._kb,
        "resolve_authority_path",
        lambda **_kwargs: database_path,
    )
    _configure_workspace_registry(package, monkeypatch, tmp_path)
    package.register(_Context())
    first = package._provider_cache[database_path]
    package.register(_Context())

    assert package._provider_cache[database_path] is first


def test_manifest_and_runtime_share_one_release_identity(package):
    versioning = importlib.import_module(f"{package.__name__}.versioning")
    contracts = importlib.import_module(f"{package.__name__}.contracts")
    manifest_path = (
        Path(__file__).parents[1] / "plugins" / "adrian-kanban" / "plugin.yaml"
    )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert package.PLUGIN_NAME == "adrian-kanban"
    assert package.PLUGIN_VERSION == versioning.PLUGIN_VERSION == "0.4.0"
    assert manifest["name"] == package.PLUGIN_NAME
    assert manifest["version"] == package.PLUGIN_VERSION
    assert manifest["kind"] == "standalone"
    assert versioning.REGISTRY_VERSION == contracts.REGISTRY_VERSION
    assert manifest["hermes"] == {
        "protocol_version": versioning.PROTOCOL_VERSION,
        "schema_version": versioning.SCHEMA_VERSION,
        "registry_version": versioning.REGISTRY_VERSION,
        "skill_bundle_version": versioning.SKILL_BUNDLE_VERSION,
        "desktop_extension_version": versioning.DESKTOP_EXTENSION_VERSION,
    }
    assert manifest["capabilities"] == ["tools.override"]
    assert set(manifest["provides_tools"]) == set(
        importlib.import_module(f"{package.__name__}.commands").RECOGNIZED_OPERATIONS
    )
    assert manifest["provides_hooks"] == ["pre_tool_call", "pre_user_turn"]


def test_runtime_health_reports_exact_versions_and_path(
    package, monkeypatch, tmp_path
):
    database_path = str((tmp_path / "kanban.db").resolve())
    monkeypatch.setattr(
        package._kb,
        "resolve_selected_authority",
        lambda: "adrian-kanban",
    )
    monkeypatch.setattr(
        package._kb,
        "resolve_authority_path",
        lambda **_kwargs: database_path,
    )
    _configure_workspace_registry(package, monkeypatch, tmp_path)
    package.register(_Context())

    health = package.runtime_health()

    assert health["healthy"] is True
    assert health["selected_authority"] == "adrian-kanban"
    assert health["database_path"] == database_path
    assert health["native_surface_ok"] is False
    assert health["versions"] == importlib.import_module(
        f"{package.__name__}.versioning"
    ).release_identity()
