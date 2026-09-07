"""S3 command-boundary tests derived from ratified Kanban design v0.28.

This first slice fixes the public operation taxonomy and the boundary's
fail-closed behavior before any S3 runtime is registered.  Later S3 slices add
transaction, adapter, surface-parity, migration, and release tests without
weakening these foundation expectations.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def commands_module():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_commands"
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


def _assert_canonical_rejection(result: object, *, operation: str, code: str) -> None:
    assert type(result) is dict
    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    assert isinstance(result["attempt_id"], str) and result["attempt_id"]
    assert result["operation"] == operation
    assert set(result["boundary"]) == {"from", "to"}
    assert result["boundary"]["from"]
    assert result["boundary"]["to"]
    assert [finding["code"] for finding in result["failed_checks"]] == [code]
    finding = result["failed_checks"][0]
    for key in (
        "target",
        "expected",
        "observed",
        "accepted_format",
        "remediation",
        "responsible_actor",
        "retry",
    ):
        assert isinstance(finding[key], str) and finding[key]
    assert result["not_evaluated_checks"] == []


def test_operation_taxonomy_exactly_matches_ratified_canon(commands_module):
    assert commands_module.READ_ONLY_OPERATIONS == frozenset(
        {
            "kanban_show",
            "kanban_list",
            "kanban_attachments",
        }
    )
    assert commands_module.ORDINARY_TASK_OPERATIONS == frozenset(
        {
            "kanban_create",
            "kanban_complete",
            "kanban_block",
            "kanban_unblock",
            "kanban_comment",
            "kanban_link",
            "kanban_heartbeat",
            "kanban_attach",
            "kanban_attach_url",
            "kanban_request_changes",
            "kanban_request_review",
        }
    )
    assert commands_module.INITIATIVE_OPERATIONS == frozenset(
        {
            "kanban_create_initiative",
            "kanban_update_initiative",
            "kanban_transition_initiative",
            "kanban_close_initiative",
        }
    )
    assert commands_module.RECOGNIZED_OPERATIONS == (
        commands_module.READ_ONLY_OPERATIONS
        | commands_module.ORDINARY_TASK_OPERATIONS
        | commands_module.INITIATIVE_OPERATIONS
    )
    assert len(commands_module.RECOGNIZED_OPERATIONS) == 18


def test_unknown_operation_rejects_without_runtime_or_state_change(commands_module):
    result = commands_module.command_boundary(
        "kanban_specify",
        attempt_id="attempt-unknown",
        caller_supplied_identity="adrian",
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_specify",
        code="UNRECOGNIZED_OPERATION",
    )
    assert result["attempt_id"] == "attempt-unknown"
    assert "status" not in result


@pytest.mark.parametrize(
    "operation",
    sorted(
        {
            "kanban_show",
            "kanban_comment",
            "kanban_create_initiative",
        }
    ),
)
def test_recognized_operation_rejects_when_boundary_runtime_is_unavailable(
    commands_module,
    operation,
):
    result = commands_module.command_boundary(
        operation,
        attempt_id=f"attempt-{operation}",
    )

    _assert_canonical_rejection(
        result,
        operation=operation,
        code="COMMAND_BOUNDARY_UNAVAILABLE",
    )
    assert result["attempt_id"] == f"attempt-{operation}"


def test_missing_attempt_id_is_generated_but_never_blank(commands_module):
    result = commands_module.command_boundary("not-a-kanban-operation")

    _assert_canonical_rejection(
        result,
        operation="not-a-kanban-operation",
        code="UNRECOGNIZED_OPERATION",
    )
