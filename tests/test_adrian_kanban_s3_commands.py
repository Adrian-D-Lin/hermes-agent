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
    assert commands_module.READ_ONLY_OPERATIONS == frozenset({
        "kanban_show",
        "kanban_list",
        "kanban_attachments",
    })
    assert commands_module.ORDINARY_TASK_OPERATIONS == frozenset({
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
    })
    assert commands_module.INITIATIVE_OPERATIONS == frozenset({
        "kanban_create_initiative",
        "kanban_update_initiative",
        "kanban_transition_initiative",
        "kanban_close_initiative",
    })
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
    sorted({
        "kanban_show",
        "kanban_comment",
        "kanban_create_initiative",
    }),
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
            "CREATE TABLE boundary_probe (value TEXT PRIMARY KEY, audit TEXT NOT NULL)"
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


def test_schema_additively_upgrades_unified_cards_with_route_and_version(
    commands_module,
):
    schema_module = importlib.import_module(f"{commands_module.__package__}.schema")
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            "CREATE TABLE adrian_kanban_initiatives ("
            "initiative_id TEXT PRIMARY KEY);"
            "CREATE TABLE adrian_kanban_cards ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "card_type TEXT NOT NULL,"
            "initiative_id TEXT NOT NULL,"
            "task_id TEXT,"
            "title TEXT NOT NULL,"
            "created_at INTEGER NOT NULL);"
            "INSERT INTO adrian_kanban_initiatives VALUES ('legacy-init');"
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, created_at) "
            "VALUES ('initiative', 'legacy-init', NULL, 'Legacy', 1);"
        )

        schema_module.create_schema(conn)

        columns = {
            row[1]: row
            for row in conn.execute("PRAGMA table_info(adrian_kanban_cards)")
        }
        assert columns["board_slug"][3] == 1
        assert columns["board_slug"][4] == "'default'"
        assert columns["record_version"][3] == 1
        assert columns["record_version"][4] == "0"
        assert conn.execute(
            "SELECT board_slug, record_version FROM adrian_kanban_cards "
            "WHERE initiative_id = 'legacy-init'"
        ).fetchone() == ("default", 0)
    finally:
        conn.close()


def test_schema_adds_immutable_manifest_and_handoff_records(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, _provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    expected_tables = {
        "task_input_manifests",
        "task_input_entries",
        "task_handoff_requirements",
        "task_candidate_handoffs",
        "task_handoff_rejections",
        "task_reviewer_verdicts",
    }
    with sqlite3.connect(database_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert expected_tables <= tables
        requirement_columns = {
            row[1]: row
            for row in conn.execute("PRAGMA table_info(task_handoff_requirements)")
        }
        assert requirement_columns["task_card_id"][5] == 1
        assert requirement_columns["version"][3] == 1
        assert requirement_columns["reviewer"][3] == 1
        rejection_columns = {
            row[1]: row
            for row in conn.execute("PRAGMA table_info(task_handoff_rejections)")
        }
        assert rejection_columns["task_card_id"][3] == 1
        assert rejection_columns["execution_run_id"][3] == 1
        assert rejection_columns["findings_json"][3] == 1
        verdict_foreign_keys = list(
            conn.execute("PRAGMA foreign_key_list(task_reviewer_verdicts)")
        )
        candidate_fk_columns = {
            (row[3], row[4])
            for row in verdict_foreign_keys
            if row[2] == "task_candidate_handoffs"
        }
        assert candidate_fk_columns == {
            ("task_card_id", "task_card_id"),
            ("candidate_id", "candidate_id"),
        }


def test_manifest_entries_require_their_exact_parent_manifest(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, _provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    task_id = "task-manifest-parent"
    _insert_native_task(database_path, task_id)
    _insert_unified_card(
        database_path,
        initiative_id="initiative-manifest-parent",
        task_id=task_id,
        title="Manifest parent",
    )
    with sqlite3.connect(database_path, isolation_level=None) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        entry = (
            card_id,
            task_id,
            "Canon/design.md",
            "a" * 64,
            "git_commit",
            "b" * 40,
            "Use as the design oracle",
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_input_entries "
                "(task_card_id, task_id, workspace_path, sha256, source_kind, "
                "source_locator, context_guidance) VALUES (?, ?, ?, ?, ?, ?, ?)",
                entry,
            )
        conn.execute(
            "INSERT INTO task_input_manifests "
            "(task_card_id, task_id, canonical_payload, "
            "declared_inputs_accessible, created_at) VALUES (?, ?, '{}', 1, 1000)",
            (card_id, task_id),
        )
        conn.execute(
            "INSERT INTO task_input_entries "
            "(task_card_id, task_id, workspace_path, sha256, source_kind, "
            "source_locator, context_guidance) VALUES (?, ?, ?, ?, ?, ?, ?)",
            entry,
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_input_entries WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 1
        )


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
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


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
        assert (
            type(context.mutation_executor)
            is modules["private_adapter"]._PrivateNativeAdapter
        )
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
    assert (
        binding.canonical_digest
        == hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    assert provider.is_consumed(observed["capability"]) is True
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT value, audit FROM boundary_probe").fetchall() == [
            ("committed", "same-transaction")
        ]


def _prescriptive_checks(commands_module):
    diagnostics = importlib.import_module(f"{commands_module.__package__}.diagnostics")
    failed = tuple(
        diagnostics.FailedCheck(
            code=f"FIELD_REQUIRED:{field}",
            target=field,
            expected="a committed artifact reference",
            observed="missing",
            accepted_format="repository-relative path at an immutable commit",
            remediation=f"Supply {field} from the approved lifecycle artifact and retry.",
            responsible_actor="orchestrator",
            retry="same_operation",
        )
        for field in ("brief_ref", "review_ref")
    )
    pending = (
        diagnostics.NotEvaluatedCheck(
            code="ARTIFACT_DIGEST:brief_ref", requires=("FIELD_REQUIRED:brief_ref",)
        ),
    )
    return diagnostics, failed, pending


@pytest.mark.parametrize("read_only", [False, True])
def test_prescriptive_rejection_preserves_all_findings_and_rolls_back(
    commands_module, tmp_path, monkeypatch, read_only
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(tmp_path, monkeypatch, modules["provider"])
    diagnostics, failed, pending = _prescriptive_checks(commands_module)

    def handler(context):
        if not read_only:
            context.connection.execute(
                "INSERT INTO boundary_probe (value, audit) VALUES ('discard', 'discard')"
            )
        raise diagnostics.CommandRejected(
            failed_checks=failed, not_evaluated_checks=pending
        )

    operation = "kanban_show" if read_only else "kanban_comment"
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path), provider=provider, handlers={operation: handler}
    )
    result = boundary.submit(
        operation, attempt_id="diagnostic-attempt", idempotency_key="diagnostic-retry",
        target="task-1", expected_version=0, session_id="session-1",
        execution_context="run-1", payload={"task_id": "task-1"},
    )
    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    assert result["attempt_id"] == "diagnostic-attempt"
    assert result["failed_checks"] == [check.as_dict() for check in failed]
    assert result["not_evaluated_checks"] == [check.as_dict() for check in pending]
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM boundary_probe").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("invalid", ["empty", "list", "unknown", "self", "cycle", "duplicate"])
def test_prescriptive_rejection_rejects_invalid_diagnostic_graph(commands_module, invalid):
    diagnostics, failed, pending = _prescriptive_checks(commands_module)
    if invalid == "empty":
        failed, pending = (), ()
    elif invalid == "list":
        failed = list(failed)
    elif invalid == "unknown":
        pending = (diagnostics.NotEvaluatedCheck(code="A", requires=("absent",)),)
    elif invalid == "self":
        pending = (diagnostics.NotEvaluatedCheck(code="A", requires=("A",)),)
    elif invalid == "cycle":
        pending = (
            diagnostics.NotEvaluatedCheck(code="A", requires=("B",)),
            diagnostics.NotEvaluatedCheck(code="B", requires=("A",)),
        )
    elif invalid == "duplicate":
        failed = (failed[0], failed[0])
    with pytest.raises((TypeError, ValueError)):
        diagnostics.CommandRejected(failed_checks=failed, not_evaluated_checks=pending)


def test_prescriptive_exception_text_does_not_dump_diagnostic_content(commands_module):
    diagnostics, failed, pending = _prescriptive_checks(commands_module)
    exc = diagnostics.CommandRejected(failed_checks=failed, not_evaluated_checks=pending)
    assert str(exc) == "command validation rejected"
    assert exc.failed_checks == failed
    assert exc.not_evaluated_checks == pending


def test_prescriptive_dependency_chain_is_order_independent(commands_module):
    diagnostics, failed, pending = _prescriptive_checks(commands_module)
    chain = (
        diagnostics.NotEvaluatedCheck(code="REVIEW_CONTENT", requires=(pending[0].code,)),
        pending[0],
    )
    exc = diagnostics.CommandRejected(failed_checks=failed, not_evaluated_checks=chain)
    assert exc.not_evaluated_checks == chain


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


def test_idempotent_replay_is_stable_when_host_derived_version_changes(
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
    state = {"version": 4}
    resolver_calls = []
    handler_calls = []

    def state_resolver(conn, operation, target, payload):
        assert conn.row_factory is sqlite3.Row
        resolver_calls.append((operation, target, dict(payload)))
        return state["version"]

    def handler(_context):
        handler_calls.append("called")
        return {"accepted_version": state["version"]}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": handler},
        state_resolver=state_resolver,
    )
    fields = {
        "attempt_id": "attempt-derived",
        "idempotency_key": "idempotency-derived",
        "target": "task-derived",
        "derive_expected_version": True,
        "session_id": "session-derived",
        "workspace_id": None,
        "execution_context": "model-tool",
        "payload": {"task_id": "task-derived", "body": "evidence"},
    }

    first = boundary.submit("kanban_comment", **fields)
    state["version"] = 5
    replay = boundary.submit("kanban_comment", **fields)

    assert first["result"] == "ACCEPTED"
    assert replay == first
    assert handler_calls == ["called"]
    assert len(resolver_calls) == 2


def test_derived_state_is_revalidated_after_serialized_transaction_begins(
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
    versions = iter((4, 5))
    handler_calls = []

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": lambda _context: handler_calls.append("called")},
        state_resolver=lambda *_: next(versions),
    )

    result = boundary.submit(
        "kanban_comment",
        attempt_id="attempt-stale",
        idempotency_key="idempotency-stale",
        target="task-stale",
        derive_expected_version=True,
        session_id="session-stale",
        workspace_id=None,
        execution_context="model-tool",
        payload={"task_id": "task-stale", "body": "evidence"},
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_comment",
        code="STALE_DERIVED_STATE",
    )
    assert handler_calls == []


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
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 1
        )


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
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )

    accepted = boundary.submit("kanban_comment", attempt_id="attempt-success", **fields)
    assert accepted["result"] == "ACCEPTED"
    assert attempts == ["attempt-failed", "attempt-success"]
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM boundary_probe").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 1
        )


def test_audited_rejection_commits_only_safe_audit_without_acceptance_receipt(
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
    checks = (
        commands_module.FailedCheck(
            code="HANDOFF_FIELD_INVALID:artifact_ref",
            target="artifact_ref",
            expected="a declared nonblank text value",
            observed="invalid",
            accepted_format="nonblank string",
            remediation="supply the required artifact reference and retry",
            responsible_actor="session_agent",
            retry="same_operation",
        ),
        commands_module.FailedCheck(
            code="HANDOFF_FIELD_INVALID:tests_passed",
            target="tests_passed",
            expected="a declared boolean value",
            observed="invalid",
            accepted_format="true or false",
            remediation="supply the required test result and retry",
            responsible_actor="session_agent",
            retry="same_operation",
        ),
    )

    def handler(context):
        context.connection.execute(
            "INSERT INTO boundary_probe (value, audit) VALUES (?, ?)",
            ("rejected-attempt", "field names and reasons only"),
        )
        raise commands_module._AuditedMutationRejection(checks)

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_request_review": handler},
    )
    result = boundary.submit(
        "kanban_request_review",
        attempt_id="attempt-audited-rejection",
        idempotency_key="idempotency-audited-rejection",
        target="task-audited-rejection",
        expected_version=0,
        session_id="session-audited-rejection",
        workspace_id=None,
        execution_context="run-audited-rejection",
        payload={"task_id": "task-audited-rejection"},
    )

    assert result["result"] == "REJECTED"
    assert result["state_changed"] is False
    assert [finding["code"] for finding in result["failed_checks"]] == [
        "HANDOFF_FIELD_INVALID:artifact_ref",
        "HANDOFF_FIELD_INVALID:tests_passed",
    ]
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT audit FROM boundary_probe WHERE value = ?",
                ("rejected-attempt",),
            ).fetchone()[0]
            == "field names and reasons only"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("invalid", "expected_error"),
    (
        ([], TypeError),
        ((), ValueError),
        (("not-a-failed-check",), TypeError),
        (["not-a-failed-check"], TypeError),
    ),
)
def test_audited_rejection_requires_nonempty_exact_failed_check_tuple(
    commands_module,
    invalid,
    expected_error,
):
    with pytest.raises(expected_error):
        commands_module._AuditedMutationRejection(invalid)


def test_audited_rejection_rejects_failed_check_subclasses(commands_module):
    class DerivedFailedCheck(commands_module.FailedCheck):
        pass

    derived = DerivedFailedCheck(
        code="DERIVED",
        target="task",
        expected="base FailedCheck",
        observed="subclass",
        accepted_format="base FailedCheck",
        remediation="use the exact diagnostic type",
        responsible_actor="system_operator",
        retry="same_operation",
    )
    with pytest.raises(TypeError):
        commands_module._AuditedMutationRejection((derived,))


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
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


def _insert_native_task(database_path, task_id: str) -> None:
    with sqlite3.connect(database_path, isolation_level=None) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "S3 adapter transaction probe", "ready", 1_000),
        )


def _seed_running_native_task(database_path, task_id: str, claim_lock: str) -> int:
    with sqlite3.connect(database_path, isolation_level=None) as conn:
        run = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, status, claim_lock, claim_expires, started_at) "
            "VALUES (?, 'running', ?, 1000, 900)",
            (task_id, claim_lock),
        )
        run_id = int(run.lastrowid)
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = ?, "
            "claim_expires = 1000, started_at = 900, current_run_id = ? "
            "WHERE id = ?",
            (claim_lock, run_id, task_id),
        )
    return run_id


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
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                ("task-active-success",),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts "
                "WHERE idempotency_key = ?",
                ("key-active-success",),
            ).fetchone()[0]
            == 1
        )


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
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                ("task-active-rollback",),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts "
                "WHERE idempotency_key = ?",
                ("key-active-rollback",),
            ).fetchone()[0]
            == 0
        )


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
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
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


@pytest.mark.parametrize(
    ("operation", "native_name"),
    (
        ("kanban_complete", "complete_task"),
        ("kanban_block", "block_task"),
        ("kanban_unblock", "unblock_task"),
        ("kanban_comment", "add_comment"),
        ("kanban_heartbeat", "heartbeat_worker"),
        ("kanban_request_changes", "request_changes"),
        ("kanban_request_review", "request_review"),
    ),
)
def test_all_retained_s2_mutations_use_the_active_boundary_transaction(
    commands_module,
    tmp_path,
    monkeypatch,
    operation,
    native_name,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    adapter_module = modules["private_adapter"]
    observed = {}

    def native_spy(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        observed["in_transaction"] = args[0].in_transaction
        return f"native:{operation}"

    monkeypatch.setattr(kb, native_name, native_spy)
    if operation == "kanban_heartbeat":

        def claim_spy(*args, **kwargs):
            observed["claim_args"] = args
            observed["claim_kwargs"] = kwargs
            observed["claim_in_transaction"] = args[0].in_transaction
            return True

        monkeypatch.setattr(kb, "heartbeat_claim", claim_spy)

    arguments = {
        "kanban_complete": adapter_module._CompleteTaskArgs("task-retained"),
        "kanban_block": adapter_module._BlockTaskArgs(
            "task-retained", reason="waiting", kind="needs_input"
        ),
        "kanban_unblock": adapter_module._UnblockTaskArgs("task-retained"),
        "kanban_comment": adapter_module._CommentArgs(
            "task-retained", "orchestrator", "evidence"
        ),
        "kanban_heartbeat": adapter_module._HeartbeatArgs(
            "task-retained", claim_lock="host:claim", note="alive"
        ),
        "kanban_request_changes": adapter_module._RequestChangesArgs(
            "task-retained", "revise"
        ),
        "kanban_request_review": adapter_module._RequestReviewArgs(
            "task-retained", summary="ready"
        ),
    }[operation]

    def handler(context):
        result = context.mutation_executor._execute_in_active_transaction(
            context.capability,
            context.binding,
            arguments,
        )
        return {"native_result": result}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={operation: handler},
    )
    result = boundary.submit(
        operation,
        attempt_id=f"attempt-{operation}",
        idempotency_key=f"key-{operation}",
        target="task-retained",
        expected_version=0,
        session_id="session-retained",
        workspace_id=None,
        execution_context="run-retained",
        payload={"operation": operation},
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {"native_result": f"native:{operation}"}
    assert observed["in_transaction"] is True
    if operation == "kanban_heartbeat":
        assert observed["claim_in_transaction"] is True
        assert observed["claim_kwargs"] == {
            "claimer": "host:claim",
            "_allow_nested": True,
        }
    if operation == "kanban_comment":
        assert "_allow_nested" not in observed["kwargs"]
    else:
        assert observed["kwargs"]["_allow_nested"] is True


def test_active_transaction_operation_allowlist_matches_implemented_mutations(
    commands_module,
):
    adapter_module = _runtime_modules(commands_module)["private_adapter"]

    assert adapter_module._ACTIVE_TRANSACTION_OPERATIONS == frozenset({
        "kanban_create",
        "kanban_complete",
        "kanban_block",
        "kanban_unblock",
        "kanban_comment",
        "kanban_heartbeat",
        "kanban_request_changes",
        "kanban_request_review",
        "kanban_link",
        "kanban_attach",
        "kanban_attach_url",
    })


@pytest.mark.parametrize(
    ("method_name", "expected_type_name", "kwargs"),
    (
        (
            "_complete_in_active_transaction",
            "_CompleteTaskArgs",
            {
                "task_id": "task-wrapper",
                "result": "done",
                "summary": "summary",
                "metadata": {"quality": "accepted"},
                "created_cards": ("task-child",),
                "expected_run_id": 4,
            },
        ),
        (
            "_block_in_active_transaction",
            "_BlockTaskArgs",
            {
                "task_id": "task-wrapper",
                "reason": "waiting",
                "kind": "dependency",
                "expected_run_id": 4,
            },
        ),
        (
            "_unblock_in_active_transaction",
            "_UnblockTaskArgs",
            {"task_id": "task-wrapper"},
        ),
        (
            "_comment_in_active_transaction",
            "_CommentArgs",
            {
                "task_id": "task-wrapper",
                "author": "session-wrapper",
                "body": "evidence",
            },
        ),
        (
            "_heartbeat_in_active_transaction",
            "_HeartbeatArgs",
            {
                "task_id": "task-wrapper",
                "claim_lock": "host:claim",
                "note": "alive",
                "expected_run_id": 4,
            },
        ),
        (
            "_request_changes_in_active_transaction",
            "_RequestChangesArgs",
            {
                "task_id": "task-wrapper",
                "reason": "revise",
                "expected_run_id": 4,
            },
        ),
        (
            "_request_review_in_active_transaction",
            "_RequestReviewArgs",
            {
                "task_id": "task-wrapper",
                "summary": "ready",
                "metadata": {"handoff": "valid"},
                "reviewer": "reviewer",
                "expected_run_id": 4,
                "force": False,
                "with_reason": False,
            },
        ),
    ),
)
def test_private_adapter_exposes_narrow_typed_boundary_methods(
    commands_module,
    monkeypatch,
    method_name,
    expected_type_name,
    kwargs,
):
    adapter_module = _runtime_modules(commands_module)["private_adapter"]
    observed = {}

    def execute_spy(self, capability, binding, arguments):
        observed["self"] = self
        observed["capability"] = capability
        observed["binding"] = binding
        observed["arguments"] = arguments
        return "transport-result"

    monkeypatch.setattr(
        adapter_module._PrivateNativeAdapter,
        "_execute_in_active_transaction",
        execute_spy,
    )
    adapter = object.__new__(adapter_module._PrivateNativeAdapter)
    capability = object()
    binding = object()

    result = getattr(adapter, method_name)(capability, binding, **kwargs)

    assert result == "transport-result"
    assert observed["self"] is adapter
    assert observed["capability"] is capability
    assert observed["binding"] is binding
    assert type(observed["arguments"]).__name__ == expected_type_name
    for field, value in kwargs.items():
        assert getattr(observed["arguments"], field) == value


@pytest.mark.parametrize(
    "operation",
    (
        "kanban_complete",
        "kanban_block",
        "kanban_unblock",
        "kanban_heartbeat",
        "kanban_request_changes",
        "kanban_request_review",
    ),
)
def test_native_nested_transaction_switch_requires_an_exact_bool(operation):
    calls = {
        "kanban_complete": lambda conn: kb.complete_task(
            conn, "task-exact-bool", _allow_nested=1
        ),
        "kanban_block": lambda conn: kb.block_task(
            conn, "task-exact-bool", _allow_nested=1
        ),
        "kanban_unblock": lambda conn: kb.unblock_task(
            conn, "task-exact-bool", _allow_nested=1
        ),
        "kanban_heartbeat": lambda conn: kb.heartbeat_worker(
            conn, "task-exact-bool", _allow_nested=1
        ),
        "kanban_request_changes": lambda conn: kb.request_changes(
            conn, "task-exact-bool", reason="revise", _allow_nested=1
        ),
        "kanban_request_review": lambda conn: kb.request_review(
            conn, "task-exact-bool", _allow_nested=1
        ),
    }

    with sqlite3.connect(":memory:", isolation_level=None) as conn:
        with pytest.raises(TypeError, match="_allow_nested must be a bool"):
            calls[operation](conn)


def test_native_claim_heartbeat_nested_switch_requires_an_exact_bool():
    with sqlite3.connect(":memory:", isolation_level=None) as conn:
        with pytest.raises(TypeError, match="_allow_nested must be a bool"):
            kb.heartbeat_claim(
                conn,
                "task-exact-bool",
                claimer="host:claim",
                _allow_nested=1,
            )


def test_private_heartbeat_stops_when_exact_claim_cannot_be_extended(
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
    heartbeat_called = False

    def claim_spy(*_args, **_kwargs):
        return False

    def heartbeat_spy(*_args, **_kwargs):
        nonlocal heartbeat_called
        heartbeat_called = True
        return True

    monkeypatch.setattr(kb, "heartbeat_claim", claim_spy)
    monkeypatch.setattr(kb, "heartbeat_worker", heartbeat_spy)

    def handler(context):
        result = context.mutation_executor._heartbeat_in_active_transaction(
            context.capability,
            context.binding,
            task_id="task-heartbeat-claim",
            claim_lock="host:wrong-claim",
        )
        return {"heartbeat": result}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_heartbeat": handler},
    )
    result = boundary.submit(
        "kanban_heartbeat",
        attempt_id="attempt-heartbeat-claim",
        idempotency_key="key-heartbeat-claim",
        target="task-heartbeat-claim",
        expected_version=0,
        session_id="session-heartbeat-claim",
        workspace_id=None,
        execution_context="run-heartbeat-claim",
        payload={"task_id": "task-heartbeat-claim"},
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {"heartbeat": False}
    assert heartbeat_called is False


def test_private_complete_composes_inside_boundary_owned_transaction(
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
    _insert_native_task(database_path, "task-complete-active-transaction")
    cleanup_transaction_states = []
    monkeypatch.setattr(
        kb,
        "_cleanup_workspace",
        lambda conn, _task_id: cleanup_transaction_states.append(conn.in_transaction),
    )
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        binding = modules["capability"].CapabilityBinding(
            operation="kanban_complete",
            target="task-complete-active-transaction",
            expected_version=0,
            canonical_digest="digest-complete-active",
            session_id="session-complete-active",
            workspace_id=None,
            plugin_version=commands_module.PLUGIN_VERSION,
            protocol_version=commands_module.PROTOCOL_VERSION,
            execution_context="test",
            actor_profile="builder",
        )
        capability = provider._mint_after_admission(binding)
        adapter = provider._create_mutation_executor(conn)
        with adapter.mutation_transaction(capability, binding):
            assert (
                adapter._complete_in_active_transaction(
                    capability,
                    binding,
                    task_id="task-complete-active-transaction",
                    summary="Atomic completion.",
                )
                is True
            )
            assert cleanup_transaction_states == []
        assert cleanup_transaction_states == [False]
    finally:
        conn.close()


def test_private_complete_rollback_discards_deferred_workspace_cleanup(
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
    task_id = "task-complete-rollback-cleanup"
    _insert_native_task(database_path, task_id)
    cleanup_calls = []
    monkeypatch.setattr(
        kb,
        "_cleanup_workspace",
        lambda _conn, cleaned_task_id: cleanup_calls.append(cleaned_task_id),
    )

    def handler(context):
        assert (
            context.mutation_executor._complete_in_active_transaction(
                context.capability,
                context.binding,
                task_id=task_id,
                summary="Must roll back after native completion.",
            )
            is True
        )
        raise RuntimeError("fail after nested completion")

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_complete": handler},
    )
    result = boundary.submit(
        "kanban_complete",
        attempt_id="attempt-complete-rollback-cleanup",
        idempotency_key="key-complete-rollback-cleanup",
        target=task_id,
        expected_version=0,
        session_id="session-complete-rollback-cleanup",
        workspace_id=None,
        execution_context="test",
        actor_profile="builder",
        payload={"task_id": task_id},
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_complete",
        code="COMMAND_EXECUTION_FAILED",
    )
    assert cleanup_calls == []
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()[0]
            == "ready"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


def _insert_unified_card(
    database_path,
    *,
    initiative_id: str,
    task_id: str | None,
    title: str,
    board: str = "orchestrator",
) -> None:
    with sqlite3.connect(database_path, isolation_level=None) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO adrian_kanban_initiatives "
            "(initiative_id) VALUES (?)",
            (initiative_id,),
        )
        if task_id is not None:
            conn.execute(
                "INSERT OR IGNORE INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, created_at, "
                "board_slug) VALUES ('initiative', ?, NULL, ?, ?, ?)",
                (initiative_id, f"Initiative {initiative_id}", 999, board),
            )
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, created_at, board_slug) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "task" if task_id is not None else "initiative",
                initiative_id,
                task_id,
                title,
                1_000,
                board,
            ),
        )


def _submit_versioned_task_mutation(
    commands_module,
    database_path,
    provider,
    *,
    operation: str,
    handler,
    task_id: str,
    payload: dict,
    expected_version: int,
    key: str,
    session_id: str = "session-basic-handler",
):
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={operation: handler},
    )
    return boundary.submit(
        operation,
        attempt_id=f"attempt-{key}",
        idempotency_key=key,
        target=task_id,
        expected_version=expected_version,
        session_id=session_id,
        workspace_id=None,
        execution_context="model-tool",
        payload=payload,
    )


def test_block_and_unblock_handlers_mutate_native_and_unified_state(
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
    task_id = "task-basic-block"
    _insert_native_task(database_path, task_id)
    _insert_unified_card(
        database_path,
        initiative_id="initiative-basic-block",
        task_id=task_id,
        title="Block and unblock",
    )

    blocked = _submit_versioned_task_mutation(
        commands_module,
        database_path,
        provider,
        operation="kanban_block",
        handler=commands_module._handle_block,
        task_id=task_id,
        payload={
            "task_id": task_id,
            "reason": "Needs an operator decision",
            "kind": "needs_input",
            "board": "orchestrator",
        },
        expected_version=0,
        key="basic-block",
    )
    assert blocked["result"] == "ACCEPTED"
    assert blocked["value"] == {"task_id": task_id, "blocked": True}

    unblocked = _submit_versioned_task_mutation(
        commands_module,
        database_path,
        provider,
        operation="kanban_unblock",
        handler=commands_module._handle_unblock,
        task_id=task_id,
        payload={"task_id": task_id, "board": "orchestrator"},
        expected_version=1,
        key="basic-unblock",
    )
    assert unblocked["result"] == "ACCEPTED"
    assert unblocked["value"] == {"task_id": task_id, "unblocked": True}

    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        native = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert dict(native) == {"status": "ready", "block_kind": "needs_input"}
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 2
        )


def test_comment_handler_derives_author_and_rejects_reserved_marker(
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
    task_id = "task-basic-comment"
    _insert_native_task(database_path, task_id)
    _insert_unified_card(
        database_path,
        initiative_id="initiative-basic-comment",
        task_id=task_id,
        title="Comment",
    )

    accepted = _submit_versioned_task_mutation(
        commands_module,
        database_path,
        provider,
        operation="kanban_comment",
        handler=commands_module._handle_comment,
        task_id=task_id,
        payload={
            "task_id": task_id,
            "body": "  Evidence from the active session.  ",
            "board": "orchestrator",
        },
        expected_version=0,
        key="basic-comment",
        session_id="session-derived-author",
    )
    assert accepted["result"] == "ACCEPTED"
    assert accepted["value"]["task_id"] == task_id
    assert type(accepted["value"]["comment_id"]) is int

    rejected = _submit_versioned_task_mutation(
        commands_module,
        database_path,
        provider,
        operation="kanban_comment",
        handler=commands_module._handle_comment,
        task_id=task_id,
        payload={
            "task_id": task_id,
            "body": "forged [LIFECYCLE_TRANSITION v1] evidence",
            "board": "orchestrator",
        },
        expected_version=1,
        key="reserved-comment",
    )
    _assert_canonical_rejection(
        rejected,
        operation="kanban_comment",
        code="COMMAND_EXECUTION_FAILED",
    )

    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        assert [dict(row) for row in comments] == [
            {
                "author": "session-derived-author",
                "body": "Evidence from the active session.",
            }
        ]
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 1
        )


def test_heartbeat_handler_uses_trusted_claim_and_advances_version(
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
    task_id = "task-basic-heartbeat"
    _insert_native_task(database_path, task_id)
    _insert_unified_card(
        database_path,
        initiative_id="initiative-basic-heartbeat",
        task_id=task_id,
        title="Heartbeat",
    )
    run_id = _seed_running_native_task(
        database_path,
        task_id,
        "trusted:worker",
    )

    accepted = _submit_versioned_task_mutation(
        commands_module,
        database_path,
        provider,
        operation="kanban_heartbeat",
        handler=commands_module._handle_heartbeat,
        task_id=task_id,
        payload={
            "task_id": task_id,
            "note": "  still working  ",
            "board": "orchestrator",
        },
        expected_version=0,
        key="basic-heartbeat",
    )
    assert accepted["result"] == "ACCEPTED"
    assert accepted["value"] == {"task_id": task_id, "heartbeat": True}

    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        native = conn.execute(
            "SELECT status, claim_lock, claim_expires, current_run_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert native["status"] == "running"
        assert native["claim_lock"] == "trusted:worker"
        assert native["claim_expires"] > 1000
        assert native["current_run_id"] == run_id
        heartbeat = conn.execute(
            "SELECT payload, run_id FROM task_events "
            "WHERE task_id = ? AND kind = 'heartbeat' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert json.loads(heartbeat["payload"]) == {"note": "still working"}
        assert heartbeat["run_id"] == native["current_run_id"]
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 1
        )


def test_failed_heartbeat_claim_rolls_back_without_receipt_or_version(
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
    task_id = "task-heartbeat-rejected"
    _insert_native_task(database_path, task_id)
    _insert_unified_card(
        database_path,
        initiative_id="initiative-heartbeat-rejected",
        task_id=task_id,
        title="Rejected heartbeat",
    )
    _seed_running_native_task(database_path, task_id, "trusted:worker")

    monkeypatch.setattr(kb, "heartbeat_claim", lambda *_args, **_kwargs: False)
    rejected = _submit_versioned_task_mutation(
        commands_module,
        database_path,
        provider,
        operation="kanban_heartbeat",
        handler=commands_module._handle_heartbeat,
        task_id=task_id,
        payload={"task_id": task_id, "board": "orchestrator"},
        expected_version=0,
        key="heartbeat-rejected",
    )
    _assert_canonical_rejection(
        rejected,
        operation="kanban_heartbeat",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE task_id = ? AND kind = 'heartbeat'",
                (task_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


def test_handler_failure_after_native_mutation_rolls_back_every_layer(
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
    task_id = "task-basic-rollback"
    _insert_native_task(database_path, task_id)
    _insert_unified_card(
        database_path,
        initiative_id="initiative-basic-rollback",
        task_id=task_id,
        title="Rollback",
    )

    monkeypatch.setattr(
        commands_module,
        "_advance_task_version",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("CAS failed")),
    )
    rejected = _submit_versioned_task_mutation(
        commands_module,
        database_path,
        provider,
        operation="kanban_block",
        handler=commands_module._handle_block,
        task_id=task_id,
        payload={
            "task_id": task_id,
            "reason": "must roll back",
            "board": "orchestrator",
        },
        expected_version=0,
        key="basic-rollback",
    )
    _assert_canonical_rejection(
        rejected,
        operation="kanban_block",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()[0]
            == "ready"
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


def test_link_handler_atomically_links_two_unified_task_cards(
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
    for task_id, initiative_id in (
        ("task-parent", "initiative-link-parent"),
        ("task-child", "initiative-link-child"),
    ):
        _insert_native_task(database_path, task_id)
        _insert_unified_card(
            database_path,
            initiative_id=initiative_id,
            task_id=task_id,
            title=task_id,
        )

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_link": commands_module._handle_link},
    )
    result = boundary.submit(
        "kanban_link",
        attempt_id="attempt-link",
        idempotency_key="idempotency-link",
        target="task-child",
        expected_version=0,
        session_id="session-link",
        workspace_id=None,
        execution_context="run-link",
        payload={
            "parent_id": "task-parent",
            "child_id": "task-child",
            "board": "orchestrator",
        },
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {
        "parent_id": "task-parent",
        "child_id": "task-child",
    }
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_links WHERE parent_id = ? AND child_id = ?",
                ("task-parent", "task-child"),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards "
                "WHERE task_id = 'task-child'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts "
                "WHERE idempotency_key = ?",
                ("idempotency-link",),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("invalid_kind", ("initiative", "legacy"))
def test_link_handler_rejects_non_task_endpoint_without_state_or_receipt(
    commands_module,
    tmp_path,
    monkeypatch,
    invalid_kind,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    _insert_native_task(database_path, "task-child")
    _insert_unified_card(
        database_path,
        initiative_id="initiative-link-reject",
        task_id="task-child",
        title="Child",
    )
    if invalid_kind == "initiative":
        _insert_unified_card(
            database_path,
            initiative_id="initiative-endpoint",
            task_id=None,
            title="Not a task",
        )
        parent_id = "initiative-endpoint"
    else:
        _insert_native_task(database_path, "legacy-parent")
        parent_id = "legacy-parent"

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_link": commands_module._handle_link},
    )
    result = boundary.submit(
        "kanban_link",
        attempt_id=f"attempt-link-{invalid_kind}",
        idempotency_key=f"idempotency-link-{invalid_kind}",
        target="task-child",
        expected_version=0,
        session_id="session-link-reject",
        workspace_id=None,
        execution_context="run-link-reject",
        payload={
            "parent_id": parent_id,
            "child_id": "task-child",
            "board": "orchestrator",
        },
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_link",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


def test_link_native_nested_transaction_switch_requires_an_exact_bool():
    with sqlite3.connect(":memory:", isolation_level=None) as conn:
        with pytest.raises(TypeError, match="_allow_nested must be a bool"):
            kb.link_tasks(conn, "parent", "child", _allow_nested=1)


def test_link_binding_target_must_be_the_child(
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
    for task_id in ("task-parent", "task-child"):
        _insert_native_task(database_path, task_id)
        _insert_unified_card(
            database_path,
            initiative_id=f"initiative-{task_id}",
            task_id=task_id,
            title=task_id,
        )

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_link": commands_module._handle_link},
    )
    result = boundary.submit(
        "kanban_link",
        attempt_id="attempt-link-target-mismatch",
        idempotency_key="idempotency-link-target-mismatch",
        target="task-parent",
        expected_version=0,
        session_id="session-link-target-mismatch",
        workspace_id=None,
        execution_context="run-link-target-mismatch",
        payload={
            "parent_id": "task-parent",
            "child_id": "task-child",
            "board": "orchestrator",
        },
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_link",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


def test_create_adapter_uses_explicit_identity_and_ready_native_transport(
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
        task_id = context.mutation_executor._create_in_active_transaction(
            context.capability,
            context.binding,
            task_id="task-explicit",
            title="Explicit identity",
            assignee="builder",
            body="Bounded create transport",
            parents=(),
            tenant=None,
            priority=4,
            workspace_kind="scratch",
            workspace_path=None,
            project=None,
            goal_mode=False,
            goal_max_turns=None,
            model=None,
            provider=None,
            board=None,
        )
        assert context.connection.in_transaction is True
        return {"task_id": task_id}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": handler},
    )
    result = boundary.submit(
        "kanban_create",
        attempt_id="attempt-create-explicit",
        idempotency_key="idempotency-create-explicit",
        target="task-explicit",
        expected_version=0,
        session_id="session-create-explicit",
        workspace_id=None,
        execution_context="run-create-explicit",
        payload={"task_id": "task-explicit"},
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {"task_id": "task-explicit"}
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, title, body, assignee, status, priority, session_id "
            "FROM tasks "
            "WHERE id = ?",
            ("task-explicit",),
        ).fetchone()
        assert dict(row) == {
            "id": "task-explicit",
            "title": "Explicit identity",
            "body": "Bounded create transport",
            "assignee": "builder",
            "status": "ready",
            "priority": 4,
            "session_id": "session-create-explicit",
        }
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts "
                "WHERE idempotency_key = ?",
                ("idempotency-create-explicit",),
            ).fetchone()[0]
            == 1
        )


def test_create_adapter_rolls_back_native_task_with_boundary_failure(
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
        context.mutation_executor._create_in_active_transaction(
            context.capability,
            context.binding,
            task_id="task-create-rollback",
            title="Must roll back",
            assignee="builder",
        )
        raise RuntimeError("fail after native task creation")

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": handler},
    )
    result = boundary.submit(
        "kanban_create",
        attempt_id="attempt-create-rollback",
        idempotency_key="idempotency-create-rollback",
        target="task-create-rollback",
        expected_version=0,
        session_id="session-create-rollback",
        workspace_id=None,
        execution_context="run-create-rollback",
        payload={"task_id": "task-create-rollback"},
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_create",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = ?",
                ("task-create-rollback",),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


def test_create_adapter_binding_target_must_equal_explicit_task_id(
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
        context.mutation_executor._create_in_active_transaction(
            context.capability,
            context.binding,
            task_id="task-create-target",
            title="Target check",
            assignee="builder",
        )
        return {"unexpected": True}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": handler},
    )
    result = boundary.submit(
        "kanban_create",
        attempt_id="attempt-create-target",
        idempotency_key="idempotency-create-target",
        target="different-task",
        expected_version=0,
        session_id="session-create-target",
        workspace_id=None,
        execution_context="run-create-target",
        payload={"task_id": "task-create-target"},
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_create",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = ?",
                ("task-create-target",),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("invalid", (1, True, "", "   "))
def test_native_explicit_task_identity_requires_an_exact_nonblank_string(invalid):
    with sqlite3.connect(":memory:", isolation_level=None) as conn:
        with pytest.raises(TypeError, match="_task_id must be a nonblank string"):
            kb.create_task(conn, title="invalid explicit identity", _task_id=invalid)


@pytest.mark.parametrize(
    ("override", "message"),
    (
        ({"body": ""}, "body"),
        ({"parents": None}, "parents"),
        ({"tenant": " "}, "tenant"),
        ({"priority": -1}, "priority"),
        ({"priority": True}, "priority"),
        ({"workspace_kind": 1}, "workspace_kind"),
        ({"workspace_path": ""}, "workspace_path"),
        ({"project": " "}, "project"),
        ({"goal_mode": 1}, "goal_mode"),
        ({"goal_max_turns": 0}, "goal_max_turns"),
        ({"model": ""}, "model"),
        ({"provider": "provider-only"}, "provider requires a model"),
        ({"board": " "}, "board"),
    ),
)
def test_create_transport_rejects_noncanonical_typed_fields(
    commands_module,
    override,
    message,
):
    adapter_module = _runtime_modules(commands_module)["private_adapter"]
    fields = {
        "task_id": "task-create-types",
        "title": "Typed create",
        "assignee": "builder",
    }
    fields.update(override)

    with pytest.raises(adapter_module._PrivateAdapterRejected, match=message):
        adapter_module._CreateTaskArgs(**fields)


def _seed_initiative_card(database_path, initiative_id="initiative-create"):
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO adrian_kanban_initiatives (initiative_id) VALUES (?)",
            (initiative_id,),
        )
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, created_at, board_slug) "
            "VALUES ('initiative', ?, NULL, ?, 1, 'orchestrator')",
            (initiative_id, f"Initiative {initiative_id}"),
        )


def _submit_create(boundary, *, task_id="task-create-public", **payload_overrides):
    payload = {
        "task_id": task_id,
        "initiative_id": "initiative-create",
        "title": "Public task creation",
        "assignee": "builder",
        "board": "orchestrator",
    }
    payload.update(payload_overrides)
    return boundary.submit(
        "kanban_create",
        attempt_id=f"attempt-{task_id}",
        idempotency_key=f"idempotency-{task_id}",
        target=task_id,
        expected_version=0,
        session_id="session-create-public",
        workspace_id=None,
        execution_context="run-create-public",
        payload=payload,
    )


def test_create_handler_atomically_persists_native_and_unified_task_cards(
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
    _seed_initiative_card(database_path)
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands_module._handle_create},
    )

    result = _submit_create(
        boundary,
        body="A bounded ordinary task",
        priority=2,
        parents=[],
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {
        "initiative_id": "initiative-create",
        "task_id": "task-create-public",
        "handoff_governed": False,
    }
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        native = conn.execute(
            "SELECT id, title, status, session_id FROM tasks WHERE id = ?",
            ("task-create-public",),
        ).fetchone()
        assert dict(native) == {
            "id": "task-create-public",
            "title": "Public task creation",
            "status": "ready",
            "session_id": "session-create-public",
        }
        card = conn.execute(
            "SELECT card_type, initiative_id, task_id, title "
            "FROM adrian_kanban_cards WHERE task_id = ?",
            ("task-create-public",),
        ).fetchone()
        assert dict(card) == {
            "card_type": "task",
            "initiative_id": "initiative-create",
            "task_id": "task-create-public",
            "title": "Public task creation",
        }


def test_create_handler_atomically_fixes_handoff_requirements(
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
    _seed_initiative_card(database_path)
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands_module._handle_create},
        known_profiles={
            "default",
            "independent-reviewer",
            "test-authority-reviewer",
        },
    )
    requirements = {
        "version": 1,
        "reviewer": "default",
        "fields": {
            "sources_consulted": {"type": "list", "min_items": 1},
            "recommendation": {
                "type": "enum",
                "values": ["DRY", "NOT DRY"],
            },
        },
    }
    result = _submit_create(
        boundary,
        task_id="task-handoff-create",
        assignee="independent-reviewer",
        goal_mode=True,
        handoff_requirements_v1=requirements,
    )
    assert result["result"] == "ACCEPTED"
    assert result["value"]["handoff_governed"] is True
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT h.version, h.execution_profile, h.reviewer, "
            "h.canonical_payload, c.task_id "
            "FROM task_handoff_requirements h "
            "JOIN adrian_kanban_cards c ON c.id = h.task_card_id "
            "WHERE h.task_id = ?",
            ("task-handoff-create",),
        ).fetchone()
        assert row["version"] == 1
        assert row["execution_profile"] == "independent-reviewer"
        assert row["reviewer"] == "default"
        assert row["task_id"] == "task-handoff-create"
        assert json.loads(row["canonical_payload"]) == requirements


@pytest.mark.parametrize(
    "override",
    (
        {"goal_mode": False},
        {
            "goal_mode": True,
            "handoff_requirements_v1": {
                "version": 1,
                "reviewer": "missing-profile",
                "fields": {"evidence": {"type": "text"}},
            },
        },
        {
            "goal_mode": True,
            "handoff_requirements_v1": {
                "version": True,
                "reviewer": "default",
                "fields": {},
            },
        },
    ),
)
def test_create_handler_rejects_invalid_handoff_without_partial_state(
    commands_module,
    tmp_path,
    monkeypatch,
    override,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    _seed_initiative_card(database_path)
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands_module._handle_create},
        known_profiles={"default", "independent-reviewer"},
    )
    payload = {
        "assignee": "independent-reviewer",
        "goal_mode": True,
        "handoff_requirements_v1": {
            "version": 1,
            "reviewer": "default",
            "fields": {"evidence": {"type": "text"}},
        },
    }
    payload.update(override)
    result = _submit_create(
        boundary,
        task_id="task-invalid-handoff",
        **payload,
    )
    _assert_canonical_rejection(
        result,
        operation="kanban_create",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'task-invalid-handoff'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_cards "
                "WHERE task_id = 'task-invalid-handoff'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM task_handoff_requirements").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


def test_create_handler_rejects_missing_canonical_initiative_without_native_task(
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
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands_module._handle_create},
    )

    result = _submit_create(boundary)

    _assert_canonical_rejection(
        result,
        operation="kanban_create",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM adrian_kanban_cards").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


def test_create_handler_rejects_native_only_parent_dependency_endpoint(
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
    _seed_initiative_card(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, assignee, status, created_at, workspace_kind) "
            "VALUES ('native-only-parent', 'Legacy native parent', "
            "'builder', 'done', 1, 'scratch')"
        )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands_module._handle_create},
    )

    result = _submit_create(boundary, parents=["native-only-parent"])

    _assert_canonical_rejection(
        result,
        operation="kanban_create",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'task-create-public'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_links WHERE child_id = 'task-create-public'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_cards "
                "WHERE task_id = 'task-create-public'"
            ).fetchone()[0]
            == 0
        )


def test_create_handler_allows_cross_initiative_task_parent(
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
    _seed_initiative_card(database_path)
    _seed_initiative_card(database_path, "initiative-parent")
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, assignee, status, created_at, workspace_kind) "
            "VALUES ('cross-parent', 'Cross parent', 'builder', "
            "'done', 1, 'scratch')"
        )
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, created_at, "
            "board_slug) "
            "VALUES ('task', 'initiative-parent', 'cross-parent', "
            "'Cross parent', 1, 'orchestrator')"
        )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands_module._handle_create},
    )

    result = _submit_create(boundary, parents=["cross-parent"])

    assert result["result"] == "ACCEPTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_links "
                "WHERE parent_id = 'cross-parent' AND child_id = 'task-create-public'"
            ).fetchone()[0]
            == 1
        )


def test_create_handler_rolls_back_both_identity_levels_on_unified_card_failure(
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
    _seed_initiative_card(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, created_at) "
            "VALUES ('task', ?, 'task-duplicate-card', 'Existing', 1)",
            ("initiative-create",),
        )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands_module._handle_create},
    )

    result = _submit_create(boundary, task_id="task-duplicate-card")

    _assert_canonical_rejection(
        result,
        operation="kanban_create",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = 'task-duplicate-card'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_cards "
                "WHERE task_id = 'task-duplicate-card'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("override", "field"),
    (
        ({"initiative_id": None}, "initiative_id"),
        ({"title": " "}, "title"),
        ({"assignee": None}, "assignee"),
        ({"parents": None}, "parents"),
        ({"parents": ["parent", "parent"]}, "parents"),
        ({"unexpected": "field"}, "unexpected"),
    ),
)
def test_create_handler_rejects_invalid_or_unknown_payload_fields(
    commands_module,
    tmp_path,
    monkeypatch,
    override,
    field,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path,
        monkeypatch,
        modules["provider"],
    )
    _seed_initiative_card(database_path)
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands_module._handle_create},
    )

    result = _submit_create(boundary, **override)

    _assert_canonical_rejection(
        result,
        operation="kanban_create",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_cards WHERE card_type = 'task'"
            ).fetchone()[0]
            == 0
        )
    assert field


_GOVERNED_REQUIREMENTS = {
    "version": 1,
    "reviewer": "independent-reviewer",
    "fields": {
        "artifact_ref": {"type": "text"},
        "tests_passed": {"type": "boolean"},
    },
}


def _seed_governed_running_task(
    database_path,
    *,
    task_id: str,
    execution_profile: str = "builder",
    reviewer: str = "independent-reviewer",
) -> int:
    _insert_native_task(database_path, task_id)
    _insert_unified_card(
        database_path,
        initiative_id=f"initiative-{task_id}",
        task_id=task_id,
        title=f"Governed {task_id}",
    )
    run_id = _seed_running_native_task(
        database_path,
        task_id,
        f"{execution_profile}:claim",
    )
    requirements = {
        **_GOVERNED_REQUIREMENTS,
        "reviewer": reviewer,
    }
    with sqlite3.connect(database_path) as conn:
        card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE tasks SET assignee = ? WHERE id = ?",
            (execution_profile, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET profile = ? WHERE id = ?",
            (execution_profile, run_id),
        )
        conn.execute(
            "INSERT INTO task_handoff_requirements "
            "(task_card_id, task_id, version, execution_profile, reviewer, "
            "canonical_payload, created_at) VALUES (?, ?, 1, ?, ?, ?, 1000)",
            (
                card_id,
                task_id,
                execution_profile,
                reviewer,
                json.dumps(requirements, sort_keys=True, separators=(",", ":")),
            ),
        )
    return run_id


def _seed_d2_lifecycle_contract(commands_module, database_path, task_id: str) -> None:
    contracts = importlib.import_module(f"{commands_module.__package__}.contracts")
    snapshot = contracts.expand_contract(
        step="D2",
        initiative_id=f"initiative-{task_id}",
        baseline_refs=("2-design/design.md@commit",),
        governing_source_refs=("Canon/design-lifecycle.md@commit",),
    )
    with sqlite3.connect(database_path) as conn:
        task_card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        initiative_card_id = conn.execute(
            "SELECT id FROM adrian_kanban_cards WHERE initiative_id = ? "
            "AND card_type = 'initiative' AND task_id IS NULL",
            (f"initiative-{task_id}",),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO task_lifecycle_contracts ("
            "contract_id, contract_version, step, task_card_id, task_id, "
            "initiative_card_id, initiative_id, segment_id, workspace_id, "
            "execution_profile, canonical_contract_payload, registry_hash, "
            "skill_id, skill_version, skill_hash, created_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, 1000)",
            (
                snapshot.contract_id,
                str(snapshot.contract_version),
                snapshot.step,
                task_card_id,
                task_id,
                initiative_card_id,
                snapshot.initiative_id,
                snapshot.execution_profile,
                snapshot.canonical_payload(),
                snapshot.registry_hash,
                "d2-iterative-review",
                "0.1.0",
                "a" * 64,
            ),
        )


def _valid_d2_output() -> dict:
    return {
        "artifact_ref": "2-design/d2-review.md@commit",
        "tests_passed": True,
        "review_pass_log": ["pass-1"],
        "angle_coverage": [
            "principle_alignment",
            "design_integration",
            "documentation_silence",
            "contradiction",
            "completeness_internal_coherence",
            "dependencies_downstream_impact",
            "new_principle_candidate",
            "alternative_design",
        ],
        "findings": [],
        "conclusion": "DRY",
    }


def _governed_boundary(commands_module, database_path, provider):
    handler_names = {
        "kanban_request_review": "_handle_request_review",
        "kanban_request_changes": "_handle_request_changes",
        "kanban_complete": "_handle_complete",
    }
    return commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={
            operation: getattr(commands_module, name)
            for operation, name in handler_names.items()
            if hasattr(commands_module, name)
        },
        known_profiles={"builder", "independent-reviewer"},
    )


def _submit_governed(
    boundary,
    *,
    operation: str,
    task_id: str,
    actor_profile: str,
    expected_version: int,
    payload: dict,
    suffix: str,
):
    return boundary.submit(
        operation,
        attempt_id=f"attempt-{suffix}",
        idempotency_key=f"idempotency-{suffix}",
        target=task_id,
        expected_version=expected_version,
        session_id=f"session-{actor_profile}",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile=actor_profile,
        payload={"task_id": task_id, "board": "orchestrator", **payload},
    )


def _claim_governed_review(database_path, task_id: str) -> int:
    with sqlite3.connect(database_path) as conn:
        run = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, claim_lock, claim_expires, started_at) "
            "VALUES (?, 'independent-reviewer', 'running', "
            "'independent-reviewer:claim', 2000, 1100)",
            (task_id,),
        )
        run_id = int(run.lastrowid)
        conn.execute(
            "UPDATE tasks SET status = 'running', current_run_id = ?, "
            "claim_lock = 'independent-reviewer:claim', claim_expires = 2000, "
            "assignee = 'independent-reviewer' WHERE id = ?",
            (run_id, task_id),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'claimed', ?, 1100)",
            (task_id, run_id, '{"source_status":"review"}'),
        )
    return run_id


def test_governed_request_review_admits_candidate_and_routes_fixed_reviewer(
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
    task_id = "task-governed-review"
    execution_run_id = _seed_governed_running_task(
        database_path,
        task_id=task_id,
    )
    boundary = _governed_boundary(commands_module, database_path, provider)
    metadata = {
        "artifact_ref": "artifacts/change-set.md",
        "tests_passed": True,
        "extra_evidence": ["pytest"],
    }

    result = _submit_governed(
        boundary,
        operation="kanban_request_review",
        task_id=task_id,
        actor_profile="builder",
        expected_version=0,
        payload={
            "summary": "Implementation and tests are ready.",
            "reviewer": "independent-reviewer",
            "metadata": metadata,
        },
        suffix="governed-review",
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"]["task_id"] == task_id
    assert result["value"]["reviewer"] == "independent-reviewer"
    assert result["value"]["candidate_id"]
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert dict(task) == {
            "status": "review",
            "assignee": "independent-reviewer",
            "current_run_id": None,
        }
        candidate = conn.execute(
            "SELECT candidate_id, execution_run_id, reviewer, summary, "
            "metadata_json, submitted_by FROM task_candidate_handoffs "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert candidate["candidate_id"] == result["value"]["candidate_id"]
        assert candidate["execution_run_id"] == execution_run_id
        assert candidate["reviewer"] == "independent-reviewer"
        assert candidate["summary"] == "Implementation and tests are ready."
        assert json.loads(candidate["metadata_json"]) == metadata
        assert candidate["submitted_by"] == "builder"
        assert (
            conn.execute(
                "SELECT outcome FROM task_runs WHERE id = ?",
                (execution_run_id,),
            ).fetchone()[0]
            == "review_requested"
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 1
        )


def test_lifecycle_request_review_applies_the_snapshot_fixed_output_validator(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path, monkeypatch, modules["provider"]
    )
    task_id = "task-d2-valid-output"
    _seed_governed_running_task(
        database_path,
        task_id=task_id,
        execution_profile="independent-reviewer",
        reviewer="builder",
    )
    _seed_d2_lifecycle_contract(commands_module, database_path, task_id)

    result = _submit_governed(
        _governed_boundary(commands_module, database_path, provider),
        operation="kanban_request_review",
        task_id=task_id,
        actor_profile="independent-reviewer",
        expected_version=0,
        payload={
            "summary": "The complete eight-angle record is ready.",
            "reviewer": "builder",
            "metadata": _valid_d2_output(),
        },
        suffix="d2-valid-output",
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"]["candidate_id"]


def test_lifecycle_output_rejection_is_audited_and_rolls_back_candidate_state(
    commands_module,
    tmp_path,
    monkeypatch,
):
    modules = _runtime_modules(commands_module)
    database_path, provider = _plugin_database(
        tmp_path, monkeypatch, modules["provider"]
    )
    task_id = "task-d2-invalid-output"
    execution_run_id = _seed_governed_running_task(
        database_path,
        task_id=task_id,
        execution_profile="independent-reviewer",
        reviewer="builder",
    )
    _seed_d2_lifecycle_contract(commands_module, database_path, task_id)
    metadata = _valid_d2_output()
    metadata["findings"] = [
        {
            "materiality": "material",
            "impact": "secret-impact-value",
            "route": "D1",
        }
    ]

    result = _submit_governed(
        _governed_boundary(commands_module, database_path, provider),
        operation="kanban_request_review",
        task_id=task_id,
        actor_profile="independent-reviewer",
        expected_version=0,
        payload={"summary": "Incomplete output.", "metadata": metadata},
        suffix="d2-invalid-output",
    )

    assert result["result"] == "REJECTED"
    assert any(
        item["target"] == "findings[0].citation"
        for item in result["failed_checks"]
    )
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0] == "running"
        assert conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?", (execution_run_id,)
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_candidate_handoffs WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0] == 0
        audit = conn.execute(
            "SELECT findings_json FROM task_handoff_rejections WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        assert "secret-impact-value" not in audit


def test_invalid_governed_handoff_audits_fields_without_values_or_state_change(
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
    task_id = "task-invalid-handoff"
    execution_run_id = _seed_governed_running_task(
        database_path,
        task_id=task_id,
    )
    boundary = _governed_boundary(commands_module, database_path, provider)

    result = _submit_governed(
        boundary,
        operation="kanban_request_review",
        task_id=task_id,
        actor_profile="builder",
        expected_version=0,
        payload={
            "summary": "Candidate with malformed metadata.",
            "metadata": {
                "artifact_ref": "   ",
                "secret_extra": "DO_NOT_PERSIST_THIS_VALUE",
            },
        },
        suffix="invalid-handoff",
    )

    assert result["result"] == "REJECTED"
    assert [item["target"] for item in result["failed_checks"]] == [
        "artifact_ref",
        "tests_passed",
    ]
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute(
            "SELECT status, assignee, current_run_id, claim_lock "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert dict(task) == {
            "status": "running",
            "assignee": "builder",
            "current_run_id": execution_run_id,
            "claim_lock": "builder:claim",
        }
        run = conn.execute(
            "SELECT ended_at, outcome, summary, metadata FROM task_runs WHERE id = ?",
            (execution_run_id,),
        ).fetchone()
        assert tuple(run) == (None, None, None, None)
        audit = conn.execute(
            "SELECT findings_json, submitted_by FROM task_handoff_rejections "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert audit["submitted_by"] == "builder"
        assert "DO_NOT_PERSIST_THIS_VALUE" not in audit["findings_json"]
        assert json.loads(audit["findings_json"]) == [
            {"field": "artifact_ref", "reason": "must be a nonblank string"},
            {"field": "tests_passed", "reason": "is required"},
        ]
        assert (
            conn.execute("SELECT COUNT(*) FROM task_candidate_handoffs").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 0
        )


def test_governed_request_changes_returns_same_card_to_execution_profile(
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
    task_id = "task-governed-changes"
    _seed_governed_running_task(database_path, task_id=task_id)
    boundary = _governed_boundary(commands_module, database_path, provider)
    admitted = _submit_governed(
        boundary,
        operation="kanban_request_review",
        task_id=task_id,
        actor_profile="builder",
        expected_version=0,
        payload={
            "summary": "Ready for review.",
            "metadata": {
                "artifact_ref": "artifact.md",
                "tests_passed": True,
            },
        },
        suffix="changes-admit",
    )
    assert admitted["result"] == "ACCEPTED"
    review_run_id = _claim_governed_review(database_path, task_id)

    result = _submit_governed(
        boundary,
        operation="kanban_request_changes",
        task_id=task_id,
        actor_profile="independent-reviewer",
        expected_version=1,
        payload={"reason": "Add the missing rollback regression."},
        suffix="changes-return",
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {
        "task_id": task_id,
        "execution_profile": "builder",
    }
    with sqlite3.connect(database_path) as conn:
        task = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert task == ("ready", "builder", None)
        assert (
            conn.execute(
                "SELECT outcome FROM task_runs WHERE id = ?",
                (review_run_id,),
            ).fetchone()[0]
            == "changes_requested"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_candidate_handoffs WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM task_reviewer_verdicts WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 2
        )


def test_governed_reviewer_completion_accepts_latest_candidate_immutably(
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
    task_id = "task-governed-complete"
    _seed_governed_running_task(database_path, task_id=task_id)
    boundary = _governed_boundary(commands_module, database_path, provider)
    admitted = _submit_governed(
        boundary,
        operation="kanban_request_review",
        task_id=task_id,
        actor_profile="builder",
        expected_version=0,
        payload={
            "summary": "Ready for acceptance.",
            "metadata": {
                "artifact_ref": "artifact.md",
                "tests_passed": True,
            },
        },
        suffix="complete-admit",
    )
    candidate_id = admitted["value"]["candidate_id"]
    review_run_id = _claim_governed_review(database_path, task_id)

    result = _submit_governed(
        boundary,
        operation="kanban_complete",
        task_id=task_id,
        actor_profile="independent-reviewer",
        expected_version=1,
        payload={
            "summary": "Accepted after independent verification.",
            "result": "accepted",
            "metadata": {"review_evidence": "tests/review.txt"},
            "artifacts": ["artifacts/review-report.md"],
        },
        suffix="complete-accept",
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {
        "task_id": task_id,
        "accepted_candidate_id": candidate_id,
    }
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        assert (
            conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()[0]
            == "done"
        )
        verdict = conn.execute(
            "SELECT candidate_id, review_run_id, reviewer, verdict, summary "
            "FROM task_reviewer_verdicts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert dict(verdict) == {
            "candidate_id": candidate_id,
            "review_run_id": review_run_id,
            "reviewer": "independent-reviewer",
            "verdict": "accepted",
            "summary": "Accepted after independent verification.",
        }
        run_metadata = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?",
            (review_run_id,),
        ).fetchone()[0]
        assert json.loads(run_metadata) == {
            "artifacts": ["artifacts/review-report.md"],
            "review_evidence": "tests/review.txt",
        }
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 2
        )


def test_governed_direct_completion_outside_review_is_rejected_without_state_change(
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
    task_id = "task-governed-bypass"
    execution_run_id = _seed_governed_running_task(
        database_path,
        task_id=task_id,
    )
    boundary = _governed_boundary(commands_module, database_path, provider)

    result = _submit_governed(
        boundary,
        operation="kanban_complete",
        task_id=task_id,
        actor_profile="builder",
        expected_version=0,
        payload={"summary": "Attempted direct completion."},
        suffix="complete-bypass",
    )

    _assert_canonical_rejection(
        result,
        operation="kanban_complete",
        code="COMMAND_EXECUTION_FAILED",
    )
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone() == ("running", execution_run_id)
        assert (
            conn.execute("SELECT COUNT(*) FROM task_reviewer_verdicts").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            == 0
        )


def test_ordinary_task_retains_direct_native_completion_without_handoff_verdict(
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
    task_id = "task-ordinary-complete"
    _insert_native_task(database_path, task_id)
    _insert_unified_card(
        database_path,
        initiative_id="initiative-ordinary-complete",
        task_id=task_id,
        title="Ordinary completion",
    )
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_complete": commands_module._handle_complete},
        known_profiles={"builder", "independent-reviewer"},
    )

    result = _submit_governed(
        boundary,
        operation="kanban_complete",
        task_id=task_id,
        actor_profile="builder",
        expected_version=0,
        payload={"summary": "Ordinary task done."},
        suffix="ordinary-complete",
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {
        "task_id": task_id,
        "accepted_candidate_id": None,
    }
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()[0]
            == "done"
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM task_reviewer_verdicts").fetchone()[0]
            == 0
        )
