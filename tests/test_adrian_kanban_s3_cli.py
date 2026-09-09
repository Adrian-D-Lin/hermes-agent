"""CLI and same-parser slash routing for the selected Adrian authority."""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban as cli


def _args(*tokens: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    subparsers = parser.add_subparsers(dest="command")
    cli.build_parser(subparsers)
    return parser.parse_args(["kanban", *tokens])


def _select_plugin(monkeypatch) -> None:
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        cli.kb, "resolve_selected_authority", lambda: "adrian-kanban"
    )
    monkeypatch.setattr(
        cli.kb,
        "init_db",
        lambda *_args, **_kwargs: pytest.fail("native DB initialization ran"),
    )


def _accepted(operation: str, fields: dict) -> dict:
    return {
        "result": "ACCEPTED",
        "state_changed": False,
        "attempt_id": fields.get("attempt_id", "generated"),
        "operation": operation,
        "value": {"cards": []},
    }


def _rejected(operation: str, fields: dict) -> dict:
    return {
        "result": "REJECTED",
        "state_changed": False,
        "attempt_id": fields.get("attempt_id", "generated"),
        "operation": operation,
        "boundary": {"from": "adrian-kanban", "to": "adrian-kanban"},
        "failed_checks": [{"code": "UNRECOGNIZED_OPERATION"}],
        "not_evaluated_checks": [],
    }


def _assert_actionable_rejection(rendered: dict, code: str) -> None:
    assert rendered["result"] == "REJECTED"
    assert rendered["state_changed"] is False
    assert isinstance(rendered["attempt_id"], str) and rendered["attempt_id"]
    assert set(rendered["boundary"]) == {"from", "to"}
    assert rendered["not_evaluated_checks"] == []
    assert len(rendered["failed_checks"]) == 1
    check = rendered["failed_checks"][0]
    assert check["code"] == code
    assert set(check) == {
        "code",
        "target",
        "expected",
        "observed",
        "accepted_format",
        "remediation",
        "responsible_actor",
        "retry",
    }
    assert all(isinstance(value, str) and value for value in check.values())


def test_plugin_cli_list_delegates_exact_supported_filters_without_native_init(
    monkeypatch, capsys
):
    _select_plugin(monkeypatch)
    calls = []

    def delegate(operation, **fields):
        calls.append((operation, fields))
        return _accepted(operation, fields)

    monkeypatch.setattr(cli.kb, "delegate_authority_operation", delegate)

    result = cli.kanban_command(
        _args(
            "--board",
            "orchestrator",
            "list",
            "--assignee",
            "builder-tester",
            "--status",
            "blocked",
            "--tenant",
            "grc",
            "--archived",
            "--json",
        )
    )

    assert result == 0
    assert len(calls) == 1
    operation, fields = calls[0]
    assert operation == "kanban_list"
    assert fields["payload"] == {
        "board": "orchestrator",
        "assignee": "builder-tester",
        "status": "blocked",
        "tenant": "grc",
        "include_archived": True,
    }
    assert isinstance(fields["attempt_id"], str) and fields["attempt_id"]
    assert json.loads(capsys.readouterr().out)["result"] == "ACCEPTED"


@pytest.mark.parametrize(
    ("tokens", "operation", "payload"),
    [
        (("show", "task-7", "--json"), "kanban_show", {"task_id": "task-7"}),
        (
            ("attachments", "task-8", "--json"),
            "kanban_attachments",
            {"task_id": "task-8"},
        ),
    ],
)
def test_plugin_cli_read_actions_delegate_to_the_shared_boundary(
    monkeypatch, capsys, tokens, operation, payload
):
    _select_plugin(monkeypatch)
    calls = []

    def delegate(actual_operation, **fields):
        calls.append((actual_operation, fields))
        return _accepted(actual_operation, fields)

    monkeypatch.setattr(cli.kb, "delegate_authority_operation", delegate)

    assert cli.kanban_command(_args(*tokens)) == 0

    assert calls[0][0] == operation
    assert calls[0][1]["payload"] == payload
    assert json.loads(capsys.readouterr().out)["operation"] == operation


@pytest.mark.parametrize(
    "tokens",
    [
        ("create", "must not use native creation"),
        ("complete", "task-7"),
        ("boards", "list"),
        ("dispatch",),
        ("notify-list",),
    ],
)
def test_plugin_cli_unavailable_actions_reject_through_boundary_without_fallback(
    monkeypatch, capsys, tokens
):
    _select_plugin(monkeypatch)
    calls = []

    def delegate(operation, **fields):
        calls.append((operation, fields))
        return _rejected(operation, fields)

    monkeypatch.setattr(cli.kb, "delegate_authority_operation", delegate)

    assert cli.kanban_command(_args(*tokens)) == 1

    assert len(calls) == 1
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["result"] == "REJECTED"
    assert rendered["state_changed"] is False
    assert rendered["failed_checks"][0]["code"] == "UNRECOGNIZED_OPERATION"


def test_plugin_slash_uses_the_same_authority_adapter(monkeypatch):
    _select_plugin(monkeypatch)
    calls = []

    def delegate(operation, **fields):
        calls.append((operation, fields))
        return _accepted(operation, fields)

    monkeypatch.setattr(cli.kb, "delegate_authority_operation", delegate)

    result = json.loads(cli.run_slash("list --json"))

    assert result["result"] == "ACCEPTED"
    assert calls[0][0] == "kanban_list"


def test_plugin_cli_does_not_silently_drop_unsupported_read_filters(
    monkeypatch, capsys
):
    _select_plugin(monkeypatch)
    calls = []

    def delegate(operation, **fields):
        calls.append((operation, fields))
        return _rejected(operation, fields)

    monkeypatch.setattr(cli.kb, "delegate_authority_operation", delegate)

    assert cli.kanban_command(_args("list", "--session", "session-9")) == 1

    assert len(calls) == 1
    assert calls[0][0] != "kanban_list"
    assert "session" in json.dumps(calls[0][1])
    assert json.loads(capsys.readouterr().out)["result"] == "REJECTED"


def test_plugin_cli_does_not_silently_drop_show_run_filters(monkeypatch, capsys):
    _select_plugin(monkeypatch)
    calls = []

    def delegate(operation, **fields):
        calls.append((operation, fields))
        return _rejected(operation, fields)

    monkeypatch.setattr(cli.kb, "delegate_authority_operation", delegate)

    assert (
        cli.kanban_command(
            _args(
                "show",
                "task-10",
                "--state-type",
                "status",
                "--state-name",
                "review",
            )
        )
        == 1
    )

    assert calls[0][0] != "kanban_show"
    assert calls[0][1]["payload"]["state_type"] == "status"
    assert calls[0][1]["payload"]["state_name"] == "review"
    assert json.loads(capsys.readouterr().out)["result"] == "REJECTED"


def test_plugin_authority_precedes_delegated_child_fast_fail(monkeypatch, capsys):
    _select_plugin(monkeypatch)
    calls = []
    monkeypatch.setattr(cli, "_is_delegated_child_cli_mutation", lambda _args: True)

    def delegate(operation, **fields):
        calls.append((operation, fields))
        return _rejected(operation, fields)

    monkeypatch.setattr(cli.kb, "delegate_authority_operation", delegate)

    assert cli.kanban_command(_args("complete", "task-11")) == 1
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["result"] == "REJECTED"


def test_authority_resolution_failure_never_falls_back_to_native(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.kb,
        "resolve_selected_authority",
        lambda: (_ for _ in ()).throw(RuntimeError("bad authority config")),
    )
    monkeypatch.setattr(
        cli.kb,
        "init_db",
        lambda *_args, **_kwargs: pytest.fail("native fallback initialized the DB"),
    )

    assert cli.kanban_command(_args("list", "--json")) == 1
    rendered = json.loads(capsys.readouterr().out)
    _assert_actionable_rejection(rendered, "AUTHORITY_RESOLUTION_FAILED")


def test_plugin_discovery_failure_is_rendered_fail_closed(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.kb, "resolve_selected_authority", lambda: "adrian-kanban"
    )
    monkeypatch.setattr(
        cli.kb,
        "init_db",
        lambda *_args, **_kwargs: pytest.fail("native fallback initialized the DB"),
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.discover_plugins",
        lambda: (_ for _ in ()).throw(RuntimeError("plugin unavailable")),
    )

    assert cli.kanban_command(_args("list", "--json")) == 1
    rendered = json.loads(capsys.readouterr().out)
    _assert_actionable_rejection(rendered, "AUTHORITY_BOUNDARY_UNAVAILABLE")
