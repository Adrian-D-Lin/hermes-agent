"""S3 public model-tool tests derived from ratified Kanban design v0.28.

This first public-tool sub-slice fixes the replacement surface and its thin
registration adapter.  It deliberately does not activate the unfinished
plugin; later sub-slices supply the business handlers and pre-tool policy.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def commands_module():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_tools"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    module = importlib.import_module(f"{package_name}.commands")
    yield module
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


class _RecordingContext:
    def __init__(self) -> None:
        self.registrations: list[dict] = []

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.registrations.append(
            {
                "name": name,
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                **kwargs,
            }
        )


class _RecordingBoundary:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _finish_response(self, response):
        return response

    def submit(self, operation: str, **fields):
        self.calls.append((operation, fields))
        return {
            "result": "ACCEPTED",
            "state_changed": operation not in {
                "kanban_show",
                "kanban_list",
                "kanban_attachments",
            },
            "attempt_id": fields.get("attempt_id") or "generated",
            "operation": operation,
            "value": {"delegated": True},
        }

    def _rejection_internal(self, attempt_id, operation, code):
        return {
            "result": "REJECTED",
            "state_changed": False,
            "attempt_id": attempt_id,
            "operation": operation,
            "failed_checks": [{"code": code}],
        }


def test_public_tool_schema_set_exactly_matches_ratified_surface(commands_module):
    expected = commands_module.RECOGNIZED_OPERATIONS

    assert frozenset(commands_module.TOOL_SCHEMAS) == expected
    assert len(commands_module.TOOL_SCHEMAS) == 18
    assert "kanban_specify" not in commands_module.TOOL_SCHEMAS
    assert "kanban_decompose" not in commands_module.TOOL_SCHEMAS

    with pytest.raises(TypeError):
        commands_module.TOOL_SCHEMAS["unexpected"] = {}


def test_each_public_schema_is_closed_and_self_describing(commands_module):
    for operation, schema in commands_module.TOOL_SCHEMAS.items():
        assert schema["name"] == operation
        assert isinstance(schema["description"], str) and schema["description"].strip()

        parameters = schema["parameters"]
        assert parameters["type"] == "object"
        assert isinstance(parameters["properties"], dict)
        assert set(parameters.get("required", ())) <= set(parameters["properties"])
        assert parameters["additionalProperties"] is False


def test_mutation_schemas_expose_replay_identity_but_reads_do_not(commands_module):
    for operation in commands_module.READ_ONLY_OPERATIONS:
        parameters = commands_module.TOOL_SCHEMAS[operation]["parameters"]
        assert "idempotency_key" not in parameters["properties"]
        assert "idempotency_key" not in parameters.get("required", ())

    mutations = (
        commands_module.ORDINARY_TASK_OPERATIONS
        | commands_module.INITIATIVE_OPERATIONS
    )
    for operation in mutations:
        parameters = commands_module.TOOL_SCHEMAS[operation]["parameters"]
        assert parameters["properties"]["idempotency_key"]["type"] == "string"
        assert "idempotency_key" in parameters["required"]
        assert parameters["properties"]["attempt_id"]["type"] == "string"


def test_initiative_tools_are_explicit_and_not_task_mutation_aliases(commands_module):
    expected_fields = {
        "kanban_create_initiative": {"initiative_id", "title", "body", "approval_id"},
        "kanban_update_initiative": {
            "initiative_id",
            "update_kind",
            "update",
            "approval_id",
        },
        "kanban_transition_initiative": {
            "initiative_id",
            "to_phase",
            "reconciliation_ref",
            "approval_id",
        },
        "kanban_close_initiative": {
            "initiative_id",
            "closure_result_ref",
            "approval_id",
        },
    }

    for operation, fields in expected_fields.items():
        properties = commands_module.TOOL_SCHEMAS[operation]["parameters"][
            "properties"
        ]
        assert fields <= set(properties)
        assert "task_id" not in properties
        assert "assignee" not in properties
        assert "parents" not in properties


def test_task_create_requires_both_identity_levels(commands_module):
    parameters = commands_module.TOOL_SCHEMAS["kanban_create"]["parameters"]

    assert {"task_id", "initiative_id", "title", "assignee"} <= set(
        parameters["required"]
    )


def test_ordinary_tools_preserve_their_operation_specific_arguments(commands_module):
    expected_fields = {
        "kanban_show": {"task_id", "board"},
        "kanban_list": {
            "initiative_id",
            "assignee",
            "status",
            "tenant",
            "include_archived",
            "limit",
            "board",
        },
        "kanban_attachments": {"task_id", "board"},
        "kanban_create": {
            "task_id",
            "initiative_id",
            "title",
            "assignee",
            "body",
            "parents",
            "tenant",
            "priority",
            "workspace_kind",
            "workspace_path",
            "project",
            "goal_mode",
            "goal_max_turns",
            "model",
            "provider",
            "board",
        },
        "kanban_complete": {
            "task_id",
            "summary",
            "metadata",
            "result",
            "created_cards",
            "artifacts",
            "board",
        },
        "kanban_block": {"task_id", "reason", "kind", "board"},
        "kanban_unblock": {"task_id", "board"},
        "kanban_comment": {"task_id", "body", "board"},
        "kanban_link": {"parent_id", "child_id", "board"},
        "kanban_heartbeat": {"task_id", "note", "board"},
        "kanban_attach": {
            "task_id",
            "filename",
            "content_base64",
            "content_type",
            "board",
        },
        "kanban_attach_url": {
            "task_id",
            "url",
            "filename",
            "content_type",
            "board",
        },
        "kanban_request_changes": {"task_id", "reason", "board"},
        "kanban_request_review": {
            "task_id",
            "summary",
            "reviewer",
            "metadata",
            "board",
        },
    }

    for operation, expected in expected_fields.items():
        properties = commands_module.TOOL_SCHEMAS[operation]["parameters"][
            "properties"
        ]
        assert expected <= set(properties), operation


def test_preserved_arguments_keep_their_native_compatible_json_types(commands_module):
    expected_types = {
        ("kanban_list", "include_archived"): "boolean",
        ("kanban_list", "limit"): "integer",
        ("kanban_create", "parents"): "array",
        ("kanban_create", "priority"): "integer",
        ("kanban_create", "goal_mode"): "boolean",
        ("kanban_create", "goal_max_turns"): "integer",
        ("kanban_complete", "metadata"): "object",
        ("kanban_complete", "created_cards"): "array",
        ("kanban_complete", "artifacts"): "array",
        ("kanban_request_review", "metadata"): "object",
        ("kanban_update_initiative", "update"): "object",
    }

    for (operation, field), expected_type in expected_types.items():
        observed = commands_module.TOOL_SCHEMAS[operation]["parameters"][
            "properties"
        ][field]
        assert observed["type"] == expected_type, (operation, field)

    array_fields = (
        ("kanban_create", "parents"),
        ("kanban_complete", "created_cards"),
        ("kanban_complete", "artifacts"),
    )
    for operation, field in array_fields:
        observed = commands_module.TOOL_SCHEMAS[operation]["parameters"][
            "properties"
        ][field]
        assert observed["items"] == {"type": "string"}

    assert commands_module.TOOL_SCHEMAS["kanban_create"]["parameters"][
        "properties"
    ]["workspace_kind"]["enum"] == ["scratch", "dir", "worktree"]
    assert commands_module.TOOL_SCHEMAS["kanban_block"]["parameters"][
        "properties"
    ]["kind"]["enum"] == [
        "dependency",
        "needs_input",
        "capability",
        "transient",
    ]


def test_operation_required_fields_preserve_existing_safety_contract(commands_module):
    expected_required = {
        "kanban_block": {"task_id", "reason", "idempotency_key"},
        "kanban_comment": {"task_id", "body", "idempotency_key"},
        "kanban_link": {"parent_id", "child_id", "idempotency_key"},
        "kanban_attach": {
            "task_id",
            "filename",
            "content_base64",
            "idempotency_key",
        },
        "kanban_attach_url": {"task_id", "url", "idempotency_key"},
        "kanban_request_changes": {"task_id", "reason", "idempotency_key"},
        "kanban_request_review": {"task_id", "summary", "idempotency_key"},
    }

    for operation, expected in expected_required.items():
        required = commands_module.TOOL_SCHEMAS[operation]["parameters"][
            "required"
        ]
        assert expected <= set(required), operation


def test_registration_is_complete_scoped_and_override_explicit(commands_module):
    context = _RecordingContext()
    boundary = _RecordingBoundary()

    result = commands_module.register_public_tools(context, boundary)

    assert result is None
    assert len(context.registrations) == 18
    assert {item["name"] for item in context.registrations} == set(
        commands_module.RECOGNIZED_OPERATIONS
    )
    assert len({id(item["handler"]) for item in context.registrations}) == 18
    for item in context.registrations:
        assert item["toolset"] == "kanban"
        assert item["schema"] is commands_module.TOOL_SCHEMAS[item["name"]]
        assert item["description"] == item["schema"]["description"]
        assert item["override"] is True
        assert item["is_async"] is False


def test_each_tool_delegates_to_its_own_operation_without_late_binding(
    commands_module,
):
    context = _RecordingContext()
    boundary = _RecordingBoundary()
    board_calls = []

    def board_resolver(operation, args, runtime_fields):
        board_calls.append((operation, args, runtime_fields))
        return ("orchestrator", "workspace-1", "builder")

    commands_module.register_public_tools(
        context,
        boundary,
        board_resolver=board_resolver,
    )

    for item in context.registrations:
        operation = item["name"]
        supplied = {"board": "orchestrator"}
        if operation in commands_module.INITIATIVE_OPERATIONS:
            supplied["initiative_id"] = "initiative-1"
        elif operation == "kanban_link":
            supplied.update(parent_id="task-parent", child_id="task-child")
        elif operation in commands_module.ORDINARY_TASK_OPERATIONS:
            supplied["task_id"] = "task-1"
        elif operation in {"kanban_show", "kanban_attachments"}:
            supplied["task_id"] = "task-1"
        if operation not in commands_module.READ_ONLY_OPERATIONS:
            supplied["attempt_id"] = f"attempt-{operation}"
            supplied["idempotency_key"] = f"idempotency-{operation}"
        before = dict(supplied)

        rendered = item["handler"](
            supplied,
            session_id="host-session-1",
            turn_id="host-turn-1",
            api_request_id="host-api-1",
            task_id="host-top-level-task",
            user_task="trusted host context",
        )

        assert supplied == before
        delegated_operation, fields = boundary.calls[-1]
        assert delegated_operation == operation
        assert fields["attempt_id"] == (
            None
            if operation in commands_module.READ_ONLY_OPERATIONS
            else f"attempt-{operation}"
        )
        assert fields["payload"] == {
            key: value
            for key, value in before.items()
            if key not in {"attempt_id", "idempotency_key"}
        }
        assert fields["payload"]["board"] == "orchestrator"
        if operation in commands_module.READ_ONLY_OPERATIONS:
            assert set(fields) == {"attempt_id", "payload"}
        else:
            expected_target = (
                "initiative-1"
                if operation in commands_module.INITIATIVE_OPERATIONS
                else "task-child"
                if operation == "kanban_link"
                else "task-1"
            )
            assert fields == {
                "attempt_id": f"attempt-{operation}",
                "idempotency_key": f"idempotency-{operation}",
                "target": expected_target,
                "derive_expected_version": True,
                "session_id": "host-session-1",
                "workspace_id": "workspace-1",
                "actor_profile": "builder",
                "execution_context": "model-tool",
                "turn_id": "host-turn-1",
                "api_request_id": "host-api-1",
                "user_task": "trusted host context",
                "payload": fields["payload"],
            }
        assert json.loads(rendered) == {
            "result": "ACCEPTED",
            "state_changed": operation not in commands_module.READ_ONLY_OPERATIONS,
            "attempt_id": (
                "generated"
                if operation in commands_module.READ_ONLY_OPERATIONS
                else f"attempt-{operation}"
            ),
            "operation": operation,
            "value": {"delegated": True},
        }

    assert len(board_calls) == len(commands_module.RECOGNIZED_OPERATIONS)


def test_model_tool_mutation_fails_closed_without_trusted_runtime_or_resolvers(
    commands_module,
):
    context = _RecordingContext()
    boundary = _RecordingBoundary()
    commands_module.register_public_tools(context, boundary)
    handler = next(
        item["handler"]
        for item in context.registrations
        if item["name"] == "kanban_comment"
    )

    rendered = handler(
        {
            "task_id": "task-1",
            "body": "evidence",
            "idempotency_key": "key-1",
            "attempt_id": "attempt-1",
        }
    )

    result = json.loads(rendered)
    assert result["result"] == "REJECTED"
    assert result["attempt_id"] == "attempt-1"
    assert result["failed_checks"] == [
        {"code": "PUBLIC_REQUEST_NORMALIZATION_FAILED"}
    ]
    assert boundary.calls == []


def test_model_tool_rejects_undeclared_authority_fields_and_board_conflicts(
    commands_module,
):
    boundary = _RecordingBoundary()
    normalizer = commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda *_: ("orchestrator", "workspace-1", "builder"),
    )

    undeclared = normalizer.submit(
        "kanban_comment",
        {
            "task_id": "task-1",
            "body": "evidence",
            "idempotency_key": "key-1",
            "session_id": "model-forged",
        },
        {"session_id": "host-session"},
    )
    conflict = normalizer.submit(
        "kanban_comment",
        {
            "task_id": "task-1",
            "body": "evidence",
            "idempotency_key": "key-2",
            "board": "wrong-board",
        },
        {"session_id": "host-session"},
    )

    assert undeclared["failed_checks"] == [
        {"code": "PUBLIC_REQUEST_NORMALIZATION_FAILED"}
    ]
    assert conflict["failed_checks"] == [
        {"code": "PUBLIC_REQUEST_NORMALIZATION_FAILED"}
    ]
    assert boundary.calls == []


def test_registration_requires_an_injected_boundary(commands_module):
    context = _RecordingContext()

    with pytest.raises(TypeError):
        commands_module.register_public_tools(context)

    assert not any(
        name in vars(commands_module)
        for name in ("_ACTIVE_BOUNDARY", "_BOUNDARY", "ACTIVE_BOUNDARY")
    )
