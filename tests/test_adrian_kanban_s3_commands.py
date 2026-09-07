"""S3 command-boundary tests derived from ratified Kanban design v0.28.

This first slice fixes the public operation taxonomy and the boundary's
fail-closed behavior before any S3 runtime is registered.  Later S3 slices add
transaction, adapter, surface-parity, migration, and release tests without
weakening these foundation expectations.
"""

from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


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


def _runtime_modules(commands_module):
    package = commands_module.__package__
    return {
        "capability": importlib.import_module(f"{package}.capability"),
        "provider": importlib.import_module(f"{package}.provider"),
        "private_adapter": importlib.import_module(f"{package}.private_adapter"),
    }


def _plugin_database(tmp_path, monkeypatch, provider_module):
    database_path = (tmp_path / "kanban.sqlite3").resolve()
    schema_module = importlib.import_module(f"{provider_module.__package__}.schema")

    # Initialize native schema and the test-only probe while native remains the
    # selected authority.  The S3 runtime is installed only after this setup.
    with kb.connect_closing(database_path) as conn:
        schema_module.create_schema(conn)
        conn.execute(
            "CREATE TABLE boundary_probe ("
            "value TEXT PRIMARY KEY, audit TEXT NOT NULL)"
        )
        conn.commit()

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        f"  database_path: {database_path.as_posix()}\n",
        encoding="utf-8",
    )
    provider = provider_module.AdrianKanbanAuthorityProvider(str(database_path))
    provider_module.register_provider(provider)
    return database_path, provider


def test_read_handler_never_mints_or_consumes_a_mutation_capability(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )

    def forbidden_mint(_binding):
        raise AssertionError("a read operation must not mint a capability")

    monkeypatch.setattr(provider, "_mint_after_admission", forbidden_mint)

    def read_handler(context):
        assert context.capability is None
        assert context.binding is None
        assert context.connection.in_transaction is False
        return {"projection": "visible"}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_show": read_handler},
    )

    result = boundary.submit(
        "kanban_show",
        attempt_id="attempt-read",
        payload={"task_id": "task-1"},
    )

    assert result == {
        "result": "ACCEPTED",
        "state_changed": False,
        "attempt_id": "attempt-read",
        "operation": "kanban_show",
        "value": {"projection": "visible"},
    }
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
        ).fetchone()[0] == 0


def test_mutation_uses_one_adapter_owned_transaction_and_consumes_exact_binding(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    observed = {}

    def mutation_handler(context):
        assert context.connection.in_transaction is True
        assert type(context.mutation_executor) is modules["private_adapter"]._PrivateNativeAdapter
        observed["binding"] = context.binding
        observed["capability"] = context.capability
        context.connection.execute(
            "INSERT INTO boundary_probe (value, audit) VALUES (?, ?)",
            ("committed", "same-transaction"),
        )
        return {"record_id": "committed"}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": mutation_handler},
    )
    payload = {"author": "worker", "body": "review evidence"}

    result = boundary.submit(
        "kanban_comment",
        attempt_id="attempt-write",
        idempotency_key="idempotency-write",
        target="task-1",
        expected_version=7,
        session_id="session-1",
        workspace_id="workspace-1",
        execution_context="run-1",
        payload=payload,
    )

    assert result == {
        "result": "ACCEPTED",
        "state_changed": True,
        "attempt_id": "attempt-write",
        "operation": "kanban_comment",
        "value": {"record_id": "committed"},
    }
    binding = observed["binding"]
    assert binding.operation == "kanban_comment"
    assert binding.target == "task-1"
    assert binding.expected_version == 7
    assert binding.session_id == "session-1"
    assert binding.workspace_id == "workspace-1"
    assert binding.execution_context == "run-1"
    assert binding.canonical_digest == hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert provider.is_consumed(observed["capability"]) is True
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT value, audit FROM boundary_probe"
        ).fetchall() == [("committed", "same-transaction")]


def test_mutation_failure_rolls_back_and_returns_canonical_rejection(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )

    def failing_handler(context):
        context.connection.execute(
            "INSERT INTO boundary_probe (value, audit) VALUES (?, ?)",
            ("must-rollback", "partial"),
        )
        raise RuntimeError("deliberate handler failure")

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": failing_handler},
    )

    result = boundary.submit(
        "kanban_comment",
        attempt_id="attempt-rollback",
        idempotency_key="idempotency-rollback",
        target="task-2",
        expected_version=0,
        session_id="session-2",
        execution_context="run-2",
        payload={"body": "partial"},
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_comment",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM boundary_probe").fetchone()[0] == 0


def test_recognized_but_unimplemented_operation_rejects_before_capability_mint(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )

    def forbidden_mint(_binding):
        raise AssertionError("unimplemented operation must not mint")

    monkeypatch.setattr(provider, "_mint_after_admission", forbidden_mint)
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={},
    )

    result = boundary.submit(
        "kanban_update_initiative",
        attempt_id="attempt-unimplemented",
        payload={},
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_update_initiative",
        code="OPERATION_NOT_IMPLEMENTED",
    )


def test_mutation_requires_idempotency_key_before_capability_mint_or_handler(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )

    def forbidden_mint(_binding):
        raise AssertionError("missing idempotency key must reject before mint")

    def forbidden_handler(_context):
        raise AssertionError("missing idempotency key must reject before handler")

    monkeypatch.setattr(provider, "_mint_after_admission", forbidden_mint)
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": forbidden_handler},
    )

    result = boundary.submit(
        "kanban_comment",
        attempt_id="attempt-missing-key",
        target="task-missing-key",
        expected_version=0,
        session_id="session-missing-key",
        execution_context="run-missing-key",
        payload={"body": "must reject"},
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_comment",
        code="IDEMPOTENCY_KEY_REQUIRED",
    )


def test_exact_idempotent_replay_returns_original_response_without_handler(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    calls = []

    def handler(context):
        calls.append(context.attempt_id)
        context.connection.execute(
            "INSERT INTO boundary_probe (value, audit) VALUES (?, ?)",
            ("replay-once", "first-attempt"),
        )
        return {"record_id": "replay-once"}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": handler},
    )
    fields = {
        "idempotency_key": "idempotency-replay",
        "target": "task-replay",
        "expected_version": 3,
        "session_id": "session-replay",
        "workspace_id": "workspace-replay",
        "execution_context": "run-replay",
        "payload": {"author": "worker", "body": "only once"},
    }

    first = boundary.submit("kanban_comment", attempt_id="attempt-first", **fields)
    second = boundary.submit("kanban_comment", attempt_id="attempt-retry", **fields)

    assert first["result"] == "ACCEPTED"
    assert second == first
    assert second["attempt_id"] == "attempt-first"
    assert calls == ["attempt-first"]

    request_identity = {
        "operation": "kanban_comment",
        "target": "task-replay",
        "expected_version": 3,
        "payload": fields["payload"],
        "session_id": "session-replay",
        "workspace_id": "workspace-replay",
        "plugin_version": modules["provider"].PLUGIN_VERSION,
        "protocol_version": modules["provider"].PROTOCOL_VERSION,
        "execution_context": "run-replay",
    }
    expected_digest = hashlib.sha256(
        json.dumps(
            request_identity,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(database_path) as conn:
        rows = conn.execute(
            "SELECT idempotency_key, operation, target, request_digest, "
            "response_json, created_at FROM adrian_kanban_command_receipts"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][:4] == (
            "idempotency-replay",
            "kanban_comment",
            "task-replay",
            expected_digest,
        )
        assert json.loads(rows[0][4]) == first
        assert type(rows[0][5]) is int and rows[0][5] > 0


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("operation", "kanban_heartbeat"),
        ("target", "task-other"),
        ("expected_version", 8),
        ("payload", {"body": "different"}),
        ("session_id", "session-other"),
        ("workspace_id", "workspace-other"),
        ("execution_context", "run-other"),
    ],
)
def test_same_idempotency_key_with_different_request_rejects_without_handler(
    commands_module,
    tmp_path,
    monkeypatch,
    changed_field,
    changed_value,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    calls = []

    def handler(context):
        calls.append(context.operation)
        return {"accepted": len(calls)}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            "kanban_comment": handler,
            "kanban_heartbeat": handler,
        },
    )
    base = {
        "operation": "kanban_comment",
        "attempt_id": "attempt-original",
        "idempotency_key": "idempotency-conflict",
        "target": "task-conflict",
        "expected_version": 7,
        "session_id": "session-conflict",
        "workspace_id": "workspace-conflict",
        "execution_context": "run-conflict",
        "payload": {"body": "original"},
    }
    first_operation = base.pop("operation")
    first = boundary.submit(first_operation, **base)
    assert first["result"] == "ACCEPTED"

    retry = dict(base)
    retry["attempt_id"] = "attempt-conflict"
    retry_operation = first_operation
    if changed_field == "operation":
        retry_operation = changed_value
    else:
        retry[changed_field] = changed_value
    conflict = boundary.submit(retry_operation, **retry)

    _assert_canonical_rejection(
        conflict,
        operation=retry_operation,
        code="IDEMPOTENCY_CONFLICT",
    )
    assert len(calls) == 1
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
        ).fetchone()[0] == 1


def test_failed_attempt_leaves_no_receipt_and_same_key_can_retry(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    attempts = []

    def handler(context):
        attempts.append(context.attempt_id)
        context.connection.execute(
            "INSERT INTO boundary_probe (value, audit) VALUES (?, ?)",
            ("retry-result", context.attempt_id),
        )
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails")
        return {"record_id": "retry-result"}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": handler},
    )
    fields = {
        "idempotency_key": "idempotency-after-failure",
        "target": "task-after-failure",
        "expected_version": 0,
        "session_id": "session-after-failure",
        "execution_context": "run-after-failure",
        "payload": {"body": "retry safely"},
    }

    failed = boundary.submit("kanban_comment", attempt_id="attempt-failed", **fields)
    _assert_canonical_rejection(
        failed,
        operation="kanban_comment",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM boundary_probe").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
        ).fetchone()[0] == 0

    accepted = boundary.submit("kanban_comment", attempt_id="attempt-success", **fields)
    assert accepted["result"] == "ACCEPTED"
    assert attempts == ["attempt-failed", "attempt-success"]
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM boundary_probe").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
        ).fetchone()[0] == 1


def test_nonserializable_result_rolls_back_mutation_and_receipt(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )

    def handler(context):
        context.connection.execute(
            "INSERT INTO boundary_probe (value, audit) VALUES (?, ?)",
            ("nonserializable", "must-rollback"),
        )
        return {"invalid": {"a-set"}}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": handler},
    )
    result = boundary.submit(
        "kanban_comment",
        attempt_id="attempt-nonserializable",
        idempotency_key="idempotency-nonserializable",
        target="task-nonserializable",
        expected_version=0,
        session_id="session-nonserializable",
        execution_context="run-nonserializable",
        payload={"body": "invalid result"},
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_comment",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM boundary_probe").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
        ).fetchone()[0] == 0


def _insert_native_task(database_path, task_id: str) -> None:
    with sqlite3.connect(database_path, isolation_level=None) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "S3 adapter transaction probe", "ready", 1_000),
        )


def test_private_adapter_can_dispatch_inside_the_boundary_transaction(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    _insert_native_task(database_path, "task-active-success")

    def handler(context):
        comment_id = context.mutation_executor._execute_in_active_transaction(
            context.capability,
            context.binding,
            modules["private_adapter"]._CommentArgs(
                task_id="task-active-success",
                author="orchestrator",
                body="one admitted transaction",
            ),
        )
        assert context.connection.in_transaction is True
        return {"comment_id": comment_id}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": handler},
    )

    result = boundary.submit(
        "kanban_comment",
        attempt_id="attempt-active-success",
        idempotency_key="key-active-success",
        target="task-active-success",
        expected_version=0,
        session_id="session-active-success",
        workspace_id=None,
        execution_context="run-active-success",
        payload={"author": "orchestrator", "body": "one admitted transaction"},
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"]["comment_id"] > 0
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
            ("task-active-success",),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts "
            "WHERE idempotency_key = ?",
            ("key-active-success",),
        ).fetchone()[0] == 1


def test_boundary_failure_rolls_back_native_adapter_write_and_receipt_together(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    _insert_native_task(database_path, "task-active-rollback")

    def handler(context):
        context.mutation_executor._execute_in_active_transaction(
            context.capability,
            context.binding,
            modules["private_adapter"]._CommentArgs(
                task_id="task-active-rollback",
                author="orchestrator",
                body="must roll back",
            ),
        )
        raise RuntimeError("fail after private adapter mutation")

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": handler},
    )

    result = boundary.submit(
        "kanban_comment",
        attempt_id="attempt-active-rollback",
        idempotency_key="key-active-rollback",
        target="task-active-rollback",
        expected_version=0,
        session_id="session-active-rollback",
        workspace_id=None,
        execution_context="run-active-rollback",
        payload={"author": "orchestrator", "body": "must roll back"},
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_comment",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
            ("task-active-rollback",),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts "
            "WHERE idempotency_key = ?",
            ("key-active-rollback",),
        ).fetchone()[0] == 0


def test_active_transaction_entry_rejects_outside_its_exact_boundary_scope(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    _insert_native_task(database_path, "task-active-outside")
    payload = {"author": "orchestrator", "body": "outside"}
    binding = modules["capability"].CapabilityBinding(
        operation="kanban_comment",
        target="task-active-outside",
        expected_version=0,
        canonical_digest=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest(),
        session_id="session-active-outside",
        workspace_id=None,
        plugin_version=modules["provider"].PLUGIN_VERSION,
        protocol_version=modules["provider"].PROTOCOL_VERSION,
        execution_context="run-active-outside",
    )
    capability = provider._mint_after_admission(binding)

    conn = sqlite3.connect(database_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        adapter = provider._create_mutation_executor(conn)
        with pytest.raises(
            modules["private_adapter"]._PrivateAdapterRejected,
            match="active boundary transaction",
        ):
            adapter._execute_in_active_transaction(
                capability,
                binding,
                modules["private_adapter"]._CommentArgs(
                    task_id="task-active-outside",
                    author="orchestrator",
                    body="outside",
                ),
            )
        assert provider.is_consumed(capability) is False
    finally:
        conn.close()
