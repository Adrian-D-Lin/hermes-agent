"""Production Adrian Kanban mutation-authority plugin.

The plugin is inert while the native authority is selected. Selecting
``adrian-kanban`` wires one complete provider, command boundary, model-tool
surface, and verified-input pre-tool hook.
"""

from __future__ import annotations

import os
import sqlite3

from hermes_cli import kanban_db as _kb
from hermes_cli import config as _config

from .versioning import PLUGIN_NAME, PLUGIN_VERSION, release_identity

_provider_cache: dict[str, "AdrianKanbanAuthorityProvider"] = {}


def _trusted_repository_registry_getter():
    from .workspace import _RepositoryRegistration, _TrustedRepositoryRegistry

    config = _config.load_config_readonly()
    kanban_config = config.get("kanban")
    if not isinstance(kanban_config, dict):
        raise ValueError("kanban config must be a mapping")
    worktree_root = kanban_config.get("controlled_worktree_root")
    if not isinstance(worktree_root, str) or not worktree_root.strip():
        raise ValueError("kanban.controlled_worktree_root must be nonblank")
    registry = kanban_config.get("repository_registry")
    if not isinstance(registry, dict):
        raise ValueError("kanban.repository_registry must be a mapping")
    shared_root = os.path.normpath(os.path.expanduser(worktree_root))
    if not os.path.isabs(shared_root):
        raise ValueError(
            "kanban.controlled_worktree_root must be an absolute path"
        )
    registrations = []
    for repository_id, entry in registry.items():
        if not isinstance(repository_id, str) or not repository_id.strip():
            raise ValueError("repository registry ID must be nonblank")
        if not isinstance(entry, dict):
            raise ValueError(
                f"kanban.repository_registry.{repository_id} must be a mapping"
            )
        repository_root = entry.get("repository_root")
        if not isinstance(repository_root, str) or not repository_root.strip():
            raise ValueError(
                f"kanban.repository_registry.{repository_id}.repository_root "
                "must be nonblank"
            )
        resolved_root = os.path.normpath(os.path.expanduser(repository_root))
        if not os.path.isabs(resolved_root):
            raise ValueError(
                f"kanban.repository_registry.{repository_id}.repository_root "
                "must be an absolute path"
            )
        registrations.append(
            _RepositoryRegistration(
                repository_identity=repository_id,
                repository_root=resolved_root,
                controlled_worktree_root=shared_root,
            )
        )
    return _TrustedRepositoryRegistry(tuple(registrations))


def _trusted_repository_ids_getter() -> frozenset[str]:
    return frozenset(
        registration.repository_identity
        for registration in _trusted_repository_registry_getter()._registrations
    )


def register(ctx) -> None:
    """Register the one production authority runtime when it is selected."""
    authority = _kb.resolve_selected_authority()
    if authority == "native":
        return None
    if authority != PLUGIN_NAME:
        raise RuntimeError(f"unknown mutation authority: {authority!r}")

    database_path = _kb.resolve_authority_path()
    from .schema import create_schema

    conn = sqlite3.connect(database_path)
    try:
        create_schema(conn)
        conn.commit()
    finally:
        conn.close()

    from .commands import (
        _CommandBoundary,
        _handle_attach,
        _handle_attach_url,
        _handle_attachments,
        _handle_block,
        _handle_close_initiative,
        _handle_comment,
        _handle_complete,
        _handle_create,
        _handle_create_initiative,
        _handle_heartbeat,
        _handle_link,
        _handle_list,
        _handle_request_changes,
        _handle_request_review,
        _handle_show,
        _handle_transition_initiative,
        _handle_unblock,
        _handle_update_initiative,
        register_public_tools,
    )
    from .pre_tool_hook import (
        GitSegmentManifestPreparer,
        GitTaskInputPreparer,
        build_pre_tool_hook,
    )
    from .provider import AdrianKanbanAuthorityProvider, register_provider
    from .routing import ModelToolBoardResolver, resolve_expected_version
    from .phase_preparer import GitPhaseResultPreparer

    cached_provider = _provider_cache.get(database_path)
    if cached_provider is not None and cached_provider.is_healthy():
        provider = cached_provider
    else:
        provider = AdrianKanbanAuthorityProvider(database_path)
        _provider_cache[database_path] = provider
    trusted_registry = _trusted_repository_registry_getter()
    provider.bind_workspace_registry(trusted_registry)
    preparer = GitTaskInputPreparer()
    segment_preparer = GitSegmentManifestPreparer(_trusted_repository_ids_getter)
    handlers = {
        "kanban_show": _handle_show,
        "kanban_list": _handle_list,
        "kanban_attachments": _handle_attachments,
        "kanban_create": _handle_create,
        "kanban_complete": _handle_complete,
        "kanban_block": _handle_block,
        "kanban_unblock": _handle_unblock,
        "kanban_comment": _handle_comment,
        "kanban_link": _handle_link,
        "kanban_heartbeat": _handle_heartbeat,
        "kanban_attach": _handle_attach,
        "kanban_attach_url": _handle_attach_url,
        "kanban_request_changes": _handle_request_changes,
        "kanban_request_review": _handle_request_review,
        "kanban_create_initiative": _handle_create_initiative,
        "kanban_update_initiative": _handle_update_initiative,
        "kanban_transition_initiative": _handle_transition_initiative,
        "kanban_close_initiative": _handle_close_initiative,
    }
    boundary = provider.command_boundary
    if boundary is None:
        boundary = _CommandBoundary(
            database_path=database_path,
            provider=provider,
            handlers=handlers,
            state_resolver=resolve_expected_version,
            known_profiles=frozenset(
                {
                    "default",
                    "independent-reviewer",
                    "test-authority-reviewer",
                    "builder-tester",
                }
            ),
            task_input_preparer=preparer,
            segment_manifest_preparer=segment_preparer,
            phase_result_preparer=GitPhaseResultPreparer(),
            workspace_registry=trusted_registry,
        )
        provider.bind_command_boundary(boundary)
        register_provider(provider)
    register_public_tools(
        ctx,
        boundary,
        board_resolver=ModelToolBoardResolver(database_path),
    )
    ctx.register_hook(
        "pre_tool_call", build_pre_tool_hook(preparer, segment_preparer)
    )
    return None


def runtime_health() -> dict:
    """Return the selected authority's read-only compatibility health."""
    from .skill_bundle import validate_skill_bundle

    authority = _kb.resolve_selected_authority()
    info = {
        "selected_authority": authority,
        "database_path": None,
        "native_surface_ok": True,
        "healthy": True,
        "reason": None,
        "versions": release_identity(),
    }
    if authority == PLUGIN_NAME:
        database_path = _kb.resolve_authority_path()
        status = _kb.provider_status(database_path)
        provider_ok = status.present and status.healthy
        skill_ok = validate_skill_bundle()
        info["database_path"] = database_path
        info["native_surface_ok"] = False
        info["healthy"] = provider_ok and skill_ok
        if not provider_ok:
            info["reason"] = "unavailable provider"
        elif not skill_ok:
            info["reason"] = "invalid skill bundle"
    elif authority != "native":
        info["native_surface_ok"] = False
        info["healthy"] = False
        info["reason"] = "unknown authority"
    return info


__all__ = ["PLUGIN_NAME", "PLUGIN_VERSION", "register", "runtime_health"]
