"""Tests for task_input_manifest_v1 in the public kanban_create schema.

Verifies that the public schema exposes the manifest as a native object,
the tool prose describes the authoritative shape, and a native manifest
dict passes through normalization/delegation without an undeclared-parameter
failure.
"""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
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


def _make_boundary(tmp_path):
    """A boundary stub with a real database_path containing the
    adrian_kanban_cards table so the preflight can open a connection
    without an OperationalError."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE adrian_kanban_cards (
            task_id TEXT,
            initiative_id TEXT,
            board_slug TEXT,
            card_type TEXT
        )"""
    )
    conn.commit()
    conn.close()

    class _RecordingBoundary:
        def __init__(self, database_path: str) -> None:
            self.database_path = database_path
            self.calls: list[tuple[str, dict]] = []

        def _finish_response(self, response):
            return response

        def submit(self, operation: str, **fields):
            self.calls.append((operation, fields))
            return {
                "result": "ACCEPTED",
                "state_changed": True,
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

    return _RecordingBoundary(str(db))


def test_public_schema_declares_manifest_object_and_required(commands_module):
    """Public schema declares task_input_manifest_v1 as type object and required."""
    params = commands_module.TOOL_SCHEMAS["kanban_create"]["parameters"]
    assert "task_input_manifest_v1" in params["properties"]
    assert "task_input_manifest_v1" in params["required"]
    manifest_schema = params["properties"]["task_input_manifest_v1"]
    assert manifest_schema["type"] == "object"
    assert manifest_schema["additionalProperties"] is False
    expected_keys = {"version", "entries", "context_ref", "snapshots"}
    assert set(manifest_schema["properties"].keys()) == expected_keys
    assert set(manifest_schema["required"]) == expected_keys
    assert manifest_schema["properties"]["version"]["enum"] == [1]
    assert manifest_schema["properties"]["entries"]["type"] == "array"
    assert manifest_schema["properties"]["context_ref"]["type"] == "object"
    assert manifest_schema["properties"]["snapshots"]["type"] == "object"


def test_tool_prose_explains_entry_forms(commands_module):
    """Tool prose contains the four top-level keys and explains
    git_commit / snapshot_attachment entry forms."""
    desc = (
        commands_module.TOOL_SCHEMAS["kanban_create"]["parameters"]
        ["properties"]["task_input_manifest_v1"]["description"]
    )
    for token in (
        "version",
        "entries",
        "context_ref",
        "snapshots",
        "git_commit",
        "snapshot_attachment",
        "workspace_path",
        "sha256",
        "source_kind",
        "source_locator",
        "filename",
        "content_type",
        "content_base64",
    ):
        assert token in desc, f"missing {token!r} in description"


def test_native_manifest_dict_passes_normalization(commands_module, tmp_path):
    """A registered/public kanban_create invocation can carry a native
    manifest dict through normalization/delegation without an
    undeclared-parameter failure."""
    boundary = _make_boundary(tmp_path)
    normalizer = commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda *_: ("orchestrator", "workspace-1", "builder"),
    )

    manifest = {
        "version": 1,
        "entries": [
            {
                "workspace_path": "2-design/review.md",
                "sha256": "a" * 64,
                "source_kind": "snapshot_attachment",
            }
        ],
        "context_ref": {"2-design/review.md": "review the design"},
        "snapshots": {
            "2-design/review.md": {
                "filename": "review.md",
                "content_type": "text/markdown",
                "content_base64": "aGVsbG8=",
            }
        },
    }

    args = {
        "task_id": "task-test",
        "initiative_id": "init-1",
        "title": "Test task",
        "assignee": "builder",
        "idempotency_key": "key-test",
        "lifecycle_contract_v1": {
            "version": 1,
            "step": "D2",
            "baseline_refs": ["Canon/design-lifecycle.md"],
            "governing_source_refs": ["2-design/review.md"],
            "prior_record_refs": [],
        },
        "task_input_manifest_v1": manifest,
        "board": "orchestrator",
    }

    result = normalizer.submit("kanban_create", args, {"session_id": "host"})

    failed = result.get("failed_checks") or []
    assert all(c.get("code") != "PUBLIC_REQUEST_NORMALIZATION_FAILED" for c in failed), (
        f"got normalization failure: {failed}"
    )
    assert boundary.calls, "boundary.submit was not called"
    _, fields = boundary.calls[0]
    assert "payload" in fields
    assert fields["payload"]["task_input_manifest_v1"] is manifest


def test_manifest_absent_from_other_create_schemas(commands_module):
    """task_input_manifest_v1 is only on kanban_create, not on other ops."""
    for op in ("kanban_complete", "kanban_block", "kanban_comment"):
        params = commands_module.TOOL_SCHEMAS[op]["parameters"]
        assert "task_input_manifest_v1" not in params.get("properties", {}), op
