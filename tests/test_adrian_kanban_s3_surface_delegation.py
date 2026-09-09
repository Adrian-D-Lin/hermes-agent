"""Generic cross-surface delegation into the selected Kanban command boundary."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.test_adrian_kanban_s1 import adrian_plugin_modules, kanban_home  # noqa: F401


def _select_plugin(home: Path, database_path: Path) -> None:
    home.joinpath("config.yaml").write_text(
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        f"  database_path: {database_path.as_posix()}\n",
        encoding="utf-8",
    )


def test_selected_authority_delegates_exact_request_to_bound_command_service(
    adrian_plugin_modules, kanban_home
):
    provider_module = importlib.import_module(
        f"{adrian_plugin_modules['package'].__name__}.provider"
    )
    database_path = (kanban_home.parent / "shared" / "kanban.sqlite3").resolve()
    database_path.parent.mkdir()
    _select_plugin(kanban_home, database_path)
    calls = []

    class Boundary:
        def submit(self, operation, **fields):
            calls.append((operation, fields))
            return {
                "result": "ACCEPTED",
                "state_changed": False,
                "attempt_id": fields["attempt_id"],
                "operation": operation,
                "value": {"cards": []},
            }

    provider = provider_module.AdrianKanbanAuthorityProvider(str(database_path))
    provider.bind_command_boundary(Boundary())
    provider_module.register_provider(provider)
    try:
        result = kb.delegate_authority_operation(
            "kanban_list",
            attempt_id="cli-attempt-1",
            payload={"board": "orchestrator"},
        )
    finally:
        kb.clear_authority_providers()

    assert result["result"] == "ACCEPTED"
    assert calls == [
        (
            "kanban_list",
            {
                "attempt_id": "cli-attempt-1",
                "payload": {"board": "orchestrator"},
            },
        )
    ]


def test_selected_authority_without_provider_fails_closed(kanban_home):
    database_path = (kanban_home.parent / "shared" / "kanban.sqlite3").resolve()
    _select_plugin(kanban_home, database_path)
    kb.clear_authority_providers()

    with pytest.raises(kb.AuthorityAdmissionRejected, match="no configured provider"):
        kb.delegate_authority_operation("kanban_list", payload={})


def test_provider_without_bound_command_service_fails_closed(
    adrian_plugin_modules, kanban_home
):
    provider_module = importlib.import_module(
        f"{adrian_plugin_modules['package'].__name__}.provider"
    )
    database_path = (kanban_home.parent / "shared" / "kanban.sqlite3").resolve()
    _select_plugin(kanban_home, database_path)
    provider = provider_module.AdrianKanbanAuthorityProvider(str(database_path))
    provider_module.register_provider(provider)
    try:
        with pytest.raises(
            kb.AuthorityAdmissionRejected, match="command boundary is unavailable"
        ):
            kb.delegate_authority_operation("kanban_list", payload={})
    finally:
        kb.clear_authority_providers()


def test_malformed_command_service_response_fails_closed(
    adrian_plugin_modules, kanban_home
):
    provider_module = importlib.import_module(
        f"{adrian_plugin_modules['package'].__name__}.provider"
    )
    database_path = (kanban_home.parent / "shared" / "kanban.sqlite3").resolve()
    _select_plugin(kanban_home, database_path)

    class Boundary:
        def submit(self, operation, **fields):
            return "not a canonical envelope"

    provider = provider_module.AdrianKanbanAuthorityProvider(str(database_path))
    provider.bind_command_boundary(Boundary())
    provider_module.register_provider(provider)
    try:
        with pytest.raises(
            kb.AuthorityAdmissionRejected, match="invalid command response"
        ):
            kb.delegate_authority_operation("kanban_list", payload={})
    finally:
        kb.clear_authority_providers()


def test_provider_boundary_binding_rejects_invalid_or_repeated_binding(
    adrian_plugin_modules, kanban_home
):
    provider_module = importlib.import_module(
        f"{adrian_plugin_modules['package'].__name__}.provider"
    )
    database_path = (kanban_home.parent / "shared" / "kanban.sqlite3").resolve()
    provider = provider_module.AdrianKanbanAuthorityProvider(str(database_path))

    with pytest.raises(Exception, match="command boundary"):
        provider.bind_command_boundary(object())

    class Boundary:
        def submit(self, operation, **fields):
            return {"result": "REJECTED"}

    first = Boundary()
    provider.bind_command_boundary(first)
    provider.bind_command_boundary(first)
    with pytest.raises(Exception, match="already bound"):
        provider.bind_command_boundary(Boundary())
