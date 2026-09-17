"""Focused tests for the public kanban_create lifecycle-dispatch contract.

Covers the corrected TOOL_SCHEMAS contract for kanban_create (typed
handoff_requirements_v1, required body/goal_mode/handoff, goal_mode
constrained to true, exact body anchor-line prose) and the
public-normalizer preflight that surfaces specific structured rejections
before any boundary submission:

* the live erroneous completed-output handoff shape is rejected
  specifically (HANDOFF_REQUIREMENTS_INVALID), not as
  PUBLIC_REQUEST_NORMALIZATION_FAILED or COMMAND_EXECUTION_FAILED;
* a valid D2 handoff declaration passes the normalizer preflight;
* false goal_mode and missing body anchor lines get specific actionable
  rejections;
* a different mutation operation (kanban_complete) reaches its normal path
  without being rejected for kanban_create-only lifecycle fields;
* no mutation occurs on rejection.
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
    package_name = "s3_adrian_kanban_create_contract"
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
        if module_name == package_name or module_name.startswith(
            f"{package_name}."
        ):
            sys.modules.pop(module_name, None)


def _make_boundary(tmp_path):
    """Boundary stub over a real adrian_kanban_cards table so the
    object-type preflight opens a connection; records every submit."""
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
            self._known_profiles = frozenset(
                {
                    "default",
                    "independent-reviewer",
                    "test-authority-reviewer",
                    "builder-tester",
                }
            )
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


def _valid_handoff_declaration():
    """The likely-valid D2 declaration, confirmed against
    normalize_handoff_requirements."""
    return {
        "version": 1,
        "reviewer": "default",
        "fields": {
            "review_pass_log": {"type": "list", "min_items": 1},
            "angle_coverage": {"type": "list", "min_items": 8},
            "findings": {"type": "list"},
            "conclusion": {"type": "enum", "values": ["DRY", "NOT_DRY"]},
        },
    }


def _completed_output_handoff():
    """The exact live erroneous shape: completed review output, not a
    declaration."""
    return {
        "angle_coverage": [
            "scope",
            "architecture",
            "interfaces",
            "data flow",
            "error handling",
            "testing",
            "documentation",
            "performance",
        ],
        "conclusion": "",
        "findings": [],
        "review_pass_log": [],
    }


def _lifecycle_contract():
    return {
        "version": 1,
        "step": "D2",
        "baseline_refs": ["Canon/design-lifecycle.md"],
        "governing_source_refs": ["2-design/review.md"],
        "prior_record_refs": [],
    }


def _manifest():
    return {
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


def _args(**overrides):
    value = {
        "task_id": "task-d2",
        "initiative_id": "init-1",
        "title": "D2 review",
        "assignee": "independent-reviewer",
        "body": "initiative_id: init-1\nstep: D2",
        "goal_mode": True,
        "handoff_requirements_v1": _valid_handoff_declaration(),
        "lifecycle_contract_v1": _lifecycle_contract(),
        "task_input_manifest_v1": _manifest(),
        "idempotency_key": "key-contract-test",
        "board": "orchestrator",
    }
    value.update(overrides)
    return value


def _normalizer(commands_module, boundary):
    return commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda *_: ("orchestrator", None, "default"),
    )


def _failed_codes(result):
    return [check.get("code") for check in result.get("failed_checks") or []]


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------


def test_schema_advertises_and_requires_full_coherent_contract(commands_module):
    params = commands_module.TOOL_SCHEMAS["kanban_create"]["parameters"]
    required = set(params["required"])
    assert required == {
        "task_id",
        "initiative_id",
        "title",
        "assignee",
        "idempotency_key",
        "lifecycle_contract_v1",
        "task_input_manifest_v1",
        "body",
        "goal_mode",
        "handoff_requirements_v1",
    }
    props = params["properties"]

    # goal_mode is constrained to true.
    assert props["goal_mode"]["type"] == "boolean"
    assert props["goal_mode"]["enum"] == [True]
    assert "Required" in props["goal_mode"]["description"]

    # handoff declares the exact top-level keys and nested field
    # declarations; the prose makes clear this is a declaration, not the
    # completed review output.
    handoff = props["handoff_requirements_v1"]
    assert handoff["type"] == "object"
    assert handoff["additionalProperties"] is False
    assert set(handoff["properties"]) == {"version", "reviewer", "fields"}
    assert set(handoff["required"]) == {"version", "reviewer", "fields"}
    assert handoff["properties"]["version"]["enum"] == [1]
    fields_schema = handoff["properties"]["fields"]
    assert fields_schema["type"] == "object"
    assert fields_schema.get("minProperties") == 1
    declaration_schema = fields_schema["additionalProperties"]
    assert declaration_schema["type"] == "object"
    assert declaration_schema["additionalProperties"] is False
    assert set(declaration_schema["properties"]) == {
        "type",
        "min_items",
        "values",
    }
    assert declaration_schema["required"] == ["type"]
    assert declaration_schema["properties"]["type"]["enum"] == [
        "text",
        "boolean",
        "list",
        "enum",
    ]
    description = handoff["description"]
    assert "declaration" in description.lower()
    assert "not the completed" in description.lower()
    assert "review_pass_log" in description
    # The single generic declaration schema cannot express the
    # field-type-specific key combinations structurally, so the prose
    # states the canonical alternatives and that they are enforced at
    # runtime.
    assert "canonicalized at runtime" in description
    assert "type-only declarations" in description

    # body prose explains the exact mandatory lines and the segment
    # condition.
    body_description = props["body"]["description"]
    for token in (
        "initiative_id: <initiative_id>",
        "step: <step>",
        "segment_id: <segment_id>",
        "Not free-form",
    ):
        assert token in body_description, f"missing {token!r} in body prose"


# ---------------------------------------------------------------------------
# Preflight rejections
# ---------------------------------------------------------------------------


def test_live_completed_output_handoff_rejected_specifically_before_boundary(
    commands_module, tmp_path
):
    boundary = _make_boundary(tmp_path)
    normalizer = _normalizer(commands_module, boundary)

    result = normalizer.submit(
        "kanban_create",
        _args(handoff_requirements_v1=_completed_output_handoff()),
        {"session_id": "host"},
    )

    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    codes = _failed_codes(result)
    assert "HANDOFF_REQUIREMENTS_INVALID" in codes
    assert "PUBLIC_REQUEST_NORMALIZATION_FAILED" not in codes
    assert "COMMAND_EXECUTION_FAILED" not in codes
    check = next(
        c
        for c in result["failed_checks"]
        if c["code"] == "HANDOFF_REQUIREMENTS_INVALID"
    )
    assert check["target"] == "handoff_requirements_v1"
    # The actual canonical validation diagnostic, not a generic message.
    assert "version" in check["observed"]
    assert "reviewer" in check["observed"]
    assert check["responsible_actor"] == "session_agent"
    assert check["retry"] == "same_operation"
    assert "declaration" in check["remediation"].lower()
    # Rejected before any boundary submission.
    assert boundary.calls == []


def test_valid_d2_declaration_passes_normalizer_preflight(commands_module, tmp_path):
    boundary = _make_boundary(tmp_path)
    normalizer = _normalizer(commands_module, boundary)

    result = normalizer.submit(
        "kanban_create",
        _args(),
        {"session_id": "host"},
    )

    assert result["result"] == "ACCEPTED"
    assert _failed_codes(result) == []
    assert boundary.calls, "boundary.submit was not called"
    _, fields = boundary.calls[0]
    assert fields["payload"]["handoff_requirements_v1"] == (
        _valid_handoff_declaration()
    )
    assert fields["payload"]["goal_mode"] is True


def test_false_goal_mode_gets_specific_rejection(commands_module, tmp_path):
    boundary = _make_boundary(tmp_path)
    normalizer = _normalizer(commands_module, boundary)

    result = normalizer.submit(
        "kanban_create",
        _args(goal_mode=False),
        {"session_id": "host"},
    )

    assert result["result"] == "REJECTED"
    codes = _failed_codes(result)
    assert "GOAL_MODE_REQUIRED" in codes
    assert "PUBLIC_REQUEST_NORMALIZATION_FAILED" not in codes
    check = next(
        c for c in result["failed_checks"] if c["code"] == "GOAL_MODE_REQUIRED"
    )
    assert check["target"] == "goal_mode"
    assert "goal_mode was False" in check["observed"]
    assert check["accepted_format"] == "the literal boolean true"
    assert check["responsible_actor"] == "session_agent"
    assert check["retry"] == "same_operation"
    assert boundary.calls == []


def test_missing_goal_mode_gets_specific_rejection(commands_module, tmp_path):
    boundary = _make_boundary(tmp_path)
    normalizer = _normalizer(commands_module, boundary)

    args = _args()
    args.pop("goal_mode")
    result = normalizer.submit("kanban_create", args, {"session_id": "host"})

    assert result["result"] == "REJECTED"
    codes = _failed_codes(result)
    assert "GOAL_MODE_REQUIRED" in codes
    check = next(
        c for c in result["failed_checks"] if c["code"] == "GOAL_MODE_REQUIRED"
    )
    assert "goal_mode was omitted" in check["observed"]
    assert boundary.calls == []


def test_missing_body_anchor_lines_get_specific_rejection(
    commands_module, tmp_path
):
    boundary = _make_boundary(tmp_path)
    normalizer = _normalizer(commands_module, boundary)

    result = normalizer.submit(
        "kanban_create",
        _args(body="initiative_id: init-1"),  # step line missing
        {"session_id": "host"},
    )

    assert result["result"] == "REJECTED"
    codes = _failed_codes(result)
    assert "KANBAN_CREATE_BODY_ANCHOR_LINES_MISSING" in codes
    check = next(
        c
        for c in result["failed_checks"]
        if c["code"] == "KANBAN_CREATE_BODY_ANCHOR_LINES_MISSING"
    )
    assert check["target"] == "body"
    assert "body must contain the exact line: step: D2" in check["observed"]
    assert check["responsible_actor"] == "session_agent"
    assert check["retry"] == "same_operation"
    assert boundary.calls == []


def test_non_string_body_gets_specific_rejection(commands_module, tmp_path):
    boundary = _make_boundary(tmp_path)
    normalizer = _normalizer(commands_module, boundary)

    result = normalizer.submit(
        "kanban_create",
        _args(body=None),
        {"session_id": "host"},
    )

    assert result["result"] == "REJECTED"
    codes = _failed_codes(result)
    assert "KANBAN_CREATE_BODY_ANCHOR_LINES_MISSING" in codes
    check = next(
        c
        for c in result["failed_checks"]
        if c["code"] == "KANBAN_CREATE_BODY_ANCHOR_LINES_MISSING"
    )
    assert "body must be a string" in check["observed"]
    assert boundary.calls == []


def test_whitespace_padded_initiative_id_derives_stripped_anchor(
    commands_module, tmp_path
):
    """Regression: a valid nonblank initiative_id with surrounding whitespace
    is normalized with .strip() before the required body line is derived,
    matching _handle_create. The body carries the stripped anchor line, so
    the preflight passes and the payload reaches the boundary untouched."""
    boundary = _make_boundary(tmp_path)
    normalizer = _normalizer(commands_module, boundary)

    result = normalizer.submit(
        "kanban_create",
        _args(
            initiative_id="  init-1  ",
            body="initiative_id: init-1\nstep: D2",
        ),
        {"session_id": "host"},
    )

    assert result["result"] == "ACCEPTED"
    assert _failed_codes(result) == []
    assert boundary.calls, "boundary.submit was not called"
    _, fields = boundary.calls[0]
    # No preflight normalization of the payload: the raw value passes
    # through, the handler owns the strip.
    assert fields["payload"]["initiative_id"] == "  init-1  "


def test_whitespace_padded_segment_id_is_not_preflight_normalized(
    commands_module, tmp_path
):
    """Regression: a segment_id with surrounding whitespace is malformed
    (the prerequisite is valid nonblank *and already trimmed*, matching the
    handler). The preflight does not strip it and derive an anchor from it;
    it defers to the canonical handler via the boundary path."""
    boundary = _make_boundary(tmp_path)
    normalizer = _normalizer(commands_module, boundary)

    contract = _lifecycle_contract()
    contract["segment_id"] = "  seg-1  "
    result = normalizer.submit(
        "kanban_create",
        _args(
            lifecycle_contract_v1=contract,
            body="initiative_id: init-1\nstep: D2\nsegment_id: seg-1",
        ),
        {"session_id": "host"},
    )

    assert result["result"] == "ACCEPTED"
    assert _failed_codes(result) == []
    assert boundary.calls, (
        "whitespace-padded segment_id must defer to the boundary/handler "
        "path, not be rejected or normalized at preflight"
    )
    _, fields = boundary.calls[0]
    # The payload was not preflight-normalized: the padded segment_id and
    # the trimmed anchor line both reach the handler unchanged.
    assert fields["payload"]["lifecycle_contract_v1"]["segment_id"] == "  seg-1  "
    assert fields["payload"]["body"] == (
        "initiative_id: init-1\nstep: D2\nsegment_id: seg-1"
    )


def test_other_mutation_operation_reaches_normal_path_without_kanban_create_fields(
    commands_module, tmp_path
):
    """Regression: the lifecycle-anchor preflight is scoped to
    kanban_create. A different mutation operation must not be rejected
    for kanban_create-only fields (goal_mode, handoff_requirements_v1,
    body anchor lines) and must reach its normal boundary path."""
    boundary = _make_boundary(tmp_path)
    boundary.handlers = {"kanban_complete": lambda context: {"done": True}}
    normalizer = _normalizer(commands_module, boundary)

    result = normalizer.submit(
        "kanban_complete",
        {"task_id": "task-complete", "idempotency_key": "complete-1"},
        {"session_id": "host"},
    )

    assert result["result"] == "ACCEPTED"
    assert result["state_changed"] is True
    assert _failed_codes(result) == []
    assert boundary.calls, "boundary.submit was not called"
    operation, fields = boundary.calls[0]
    assert operation == "kanban_complete"
    # The payload went through untouched: no kanban_create lifecycle
    # fields were injected, checked, or rejected.
    assert fields["payload"] == {"task_id": "task-complete", "board": "orchestrator"}
    for code in (
        "GOAL_MODE_REQUIRED",
        "HANDOFF_REQUIREMENTS_REQUIRED",
        "HANDOFF_REQUIREMENTS_INVALID",
        "KANBAN_CREATE_BODY_ANCHOR_LINES_MISSING",
    ):
        assert code not in _failed_codes(result)


def test_rejection_performs_no_mutation(commands_module, tmp_path):
    db = tmp_path / "state.db"
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

    boundary = _make_boundary(tmp_path)
    boundary.database_path = str(db)
    normalizer = _normalizer(commands_module, boundary)

    for payload in (
        _args(handoff_requirements_v1=_completed_output_handoff()),
        _args(goal_mode=False),
        _args(body="no anchors here"),
    ):
        result = normalizer.submit("kanban_create", payload, {"session_id": "host"})
        assert result["result"] == "REJECTED"
        assert result["state_changed"] is False

    conn = sqlite3.connect(db)
    count = conn.execute(
        "SELECT COUNT(*) FROM adrian_kanban_cards"
    ).fetchone()[0]
    conn.close()
    assert count == 0
    assert boundary.calls == []
