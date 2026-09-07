"""Read and attachment tests derived from Adrian Kanban design v0.28.

The suite treats plugin unified cards and lifecycle records as the identity and
coordination authority.  Native task/attachment rows may enrich that projection
but may not create a competing public identity.
"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture(scope="module")
def commands_module():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_read_attachments"
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


def _database(tmp_path, monkeypatch, commands_module):
    provider_module = importlib.import_module(
        f"{commands_module.__package__}.provider"
    )
    schema_module = importlib.import_module(f"{commands_module.__package__}.schema")
    database_path = (tmp_path / "kanban.sqlite3").resolve()
    with kb.connect_closing(database_path) as conn:
        schema_module.create_schema(conn)
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


def _seed_cards(database_path: Path, *, board: str = "orchestrator") -> tuple[int, int]:
    with sqlite3.connect(database_path, isolation_level=None) as conn:
        conn.execute(
            "INSERT INTO adrian_kanban_initiatives (initiative_id) VALUES (?)",
            ("initiative-read",),
        )
        initiative_card_id = int(
            conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, created_at, board_slug) "
                "VALUES ('initiative', ?, NULL, ?, 1000, ?)",
                ("initiative-read", "Read initiative", board),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, body, assignee, status, priority, tenant, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-read",
                "Read task",
                "Native execution detail",
                "builder-tester",
                "ready",
                7,
                "tenant-a",
                1001,
            ),
        )
        task_card_id = int(
            conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, created_at, board_slug) "
                "VALUES ('task', ?, ?, ?, 1001, ?)",
                ("initiative-read", "task-read", "Read task", board),
            ).lastrowid
        )
    return initiative_card_id, task_card_id


def _read_boundary(commands_module, database_path, provider, handlers):
    return commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers=handlers,
    )


def test_show_schema_accepts_exactly_one_unified_identity(commands_module):
    parameters = commands_module.TOOL_SCHEMAS["kanban_show"]["parameters"]
    assert "task_id" in parameters["properties"]
    assert "initiative_id" in parameters["properties"]
    alternatives = parameters.get("oneOf")
    assert alternatives == [
        {"required": ["task_id"]},
        {"required": ["initiative_id"]},
    ]


def test_task_read_is_plugin_rooted_and_redacts_attachment_path(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _initiative_card_id, task_card_id = _seed_cards(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        attachment_dir = kb.task_attachments_dir(
            "task-read", board="orchestrator"
        )
        attachment_dir.mkdir(parents=True, exist_ok=True)
        attachment_path = attachment_dir / "evidence.txt"
        attachment_path.write_bytes(b"design evidence")
        conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "task-read",
                "evidence.txt",
                str(attachment_path),
                "text/plain",
                len(b"design evidence"),
                "test-authority-reviewer",
                1002,
            ),
        )
        conn.execute(
            "INSERT INTO task_input_manifests "
            "(task_card_id, task_id, canonical_payload, "
            "declared_inputs_accessible, created_at) VALUES (?, ?, ?, 1, 1002)",
            (task_card_id, "task-read", '{"version":1}'),
        )
        conn.execute(
            "INSERT INTO task_input_entries "
            "(task_card_id, task_id, workspace_path, sha256, source_kind, "
            "source_locator, context_guidance) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_card_id,
                "task-read",
                "Canon/design.md",
                "a" * 64,
                "git_commit",
                "b" * 40,
                "Use as the oracle",
            ),
        )
        conn.commit()

    boundary = _read_boundary(
        commands_module,
        database_path,
        provider,
        {"kanban_show": commands_module._handle_show},
    )
    result = boundary.submit(
        "kanban_show",
        attempt_id="show-task",
        payload={"task_id": "task-read", "board": "orchestrator"},
    )

    assert result["result"] == "ACCEPTED"
    assert result["state_changed"] is False
    value = result["value"]
    assert value["card"] == {
        "card_type": "task",
        "initiative_id": "initiative-read",
        "task_id": "task-read",
        "title": "Read task",
        "board": "orchestrator",
        "record_version": 0,
    }
    assert value["task"]["status"] == "ready"
    assert value["task"]["assignee"] == "builder-tester"
    assert value["legacy"] is True
    assert value["lifecycle_contract"] is None
    assert value["input_manifest"]["declared_inputs_accessible"] is True
    assert value["input_manifest"]["entries"][0]["workspace_path"] == "Canon/design.md"
    assert value["attachments"][0]["filename"] == "evidence.txt"
    assert "stored_path" not in value["attachments"][0]


def test_cold_initiative_read_hydrates_structured_lifecycle_state(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    initiative_card_id, task_card_id = _seed_cards(database_path)
    with sqlite3.connect(database_path, isolation_level=None) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO initiative_transitions "
            "(initiative_card_id, initiative_id, previous_transition_id, "
            "transition_id, from_phase, from_segment_id, to_phase, to_segment_id, "
            "canon_route, actor_evidence, canonical_payload, created_at) "
            "VALUES (?, ?, NULL, 1, 'DEV1', NULL, 'DEV2', 'S1', ?, ?, ?, 1100)",
            (
                initiative_card_id,
                "initiative-read",
                "DEV2",
                '{"actor":"progenitor"}',
                json.dumps({"next_permitted_routes": ["DEV3"]}),
            ),
        )
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, canonical_payload, actor_evidence, "
            "idempotency_key, accepted, created_at) "
            "VALUES ('result-dev1', ?, ?, 'DEV1', NULL, 1, 'closure', ?, ?, "
            "'phase-result-key', 1, 1090)",
            (
                initiative_card_id,
                "initiative-read",
                json.dumps(
                    {
                        "findings": [
                            {"id": "F-OPEN", "status": "open", "route": "DEV2"}
                        ]
                    }
                ),
                '{"actor":"progenitor"}',
            ),
        )
        conn.execute(
            "INSERT INTO initiative_segment_projections "
            "(projection_id, projection_version, initiative_card_id, initiative_id, "
            "manifest_path, manifest_sha, content_digest, parsed_segment_definitions, "
            "readiness_refs, validation_result, projected_at) "
            "VALUES ('projection-1', 1, ?, ?, '2-design/segments.json', ?, ?, ?, ?, "
            "'accepted', 1080)",
            (
                initiative_card_id,
                "initiative-read",
                "c" * 40,
                "d" * 64,
                json.dumps([{"segment_id": "S1"}]),
                json.dumps(["ready:S1"]),
            ),
        )
        conn.execute(
            "INSERT INTO segment_workspaces "
            "(workspace_id, initiative_card_id, initiative_id, segment_id, "
            "projection_id, lifecycle_state, controller_binding_ref, active, "
            "created_at, updated_at) VALUES ('workspace-s1', ?, ?, 'S1', "
            "'projection-1', 'materialized', 'binding-1', 1, 1081, 1082)",
            (initiative_card_id, "initiative-read"),
        )
        conn.execute(
            "INSERT INTO segment_workspace_members "
            "(workspace_id, repository_identity, relative_path, branch, "
            "required_base_sha, observed_head, member_state, observed_at) "
            "VALUES ('workspace-s1', 'hermes', 'members/hermes', 'segment/S1', ?, ?, "
            "'ready', 1083)",
            ("e" * 40, "e" * 40),
        )
        conn.execute(
            "INSERT INTO task_lifecycle_contracts "
            "(contract_id, contract_version, step, task_card_id, task_id, "
            "initiative_card_id, initiative_id, segment_id, workspace_id, "
            "execution_profile, canonical_contract_payload, registry_hash, "
            "skill_id, skill_version, skill_hash, created_at) "
            "VALUES ('contract-dev2', '1', 'DEV2', ?, 'task-read', ?, ?, 'S1', "
            "'workspace-s1', 'builder-tester', '{}', ?, 'dev2-skill', '1', ?, 1101)",
            (task_card_id, initiative_card_id, "initiative-read", "f" * 64, "1" * 64),
        )

    boundary = _read_boundary(
        commands_module,
        database_path,
        provider,
        {"kanban_show": commands_module._handle_show},
    )
    result = boundary.submit(
        "kanban_show",
        attempt_id="show-initiative",
        payload={"initiative_id": "initiative-read", "board": "orchestrator"},
    )

    assert result["result"] == "ACCEPTED"
    value = result["value"]
    assert value["card"]["card_type"] == "initiative"
    assert value["current_transition"]["to_phase"] == "DEV2"
    assert value["current_transition"]["to_segment_id"] == "S1"
    assert [row["result_id"] for row in value["phase_results"]] == ["result-dev1"]
    assert value["segment_projection"]["projection_id"] == "projection-1"
    assert value["workspaces"][0]["members"][0]["repository_identity"] == "hermes"
    assert value["tasks"][0]["lifecycle_contract"]["execution_profile"] == "builder-tester"
    assert value["open_findings"] == [
        {"id": "F-OPEN", "status": "open", "route": "DEV2"}
    ]
    assert value["next_permitted_routes"] == ["DEV3"]


def test_attachment_commit_is_versioned_and_idempotent(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_cards(database_path)
    payload = {
        "task_id": "task-read",
        "filename": "result.txt",
        "content_base64": base64.b64encode(b"final result").decode("ascii"),
        "content_type": "text/plain",
        "board": "orchestrator",
    }
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_attach": commands_module._handle_attach},
    )
    fields = dict(
        attempt_id="attach-1",
        idempotency_key="attach-key",
        target="task-read",
        expected_version=0,
        session_id="session-attach",
        workspace_id=None,
        execution_context="model-tool",
        payload=payload,
    )
    first = boundary.submit("kanban_attach", **fields)
    replay = boundary.submit("kanban_attach", **fields)

    assert first["result"] == "ACCEPTED"
    assert replay == first
    assert first["value"]["size"] == len(b"final result")
    assert "stored_path" not in first["value"]
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT stored_path, uploaded_by FROM task_attachments WHERE task_id = ?",
            ("task-read",),
        ).fetchone()
        assert row is not None
        assert Path(row[0]).read_bytes() == b"final result"
        assert row[1] == "adrian-kanban"
        assert conn.execute(
            "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
            ("task-read",),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_attachments WHERE task_id = ?",
            ("task-read",),
        ).fetchone()[0] == 1


def test_late_attachment_failure_rolls_back_row_version_receipt_and_blob(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_cards(database_path)

    def fail_after_attachment(context):
        commands_module._handle_attach(context)
        return {"not_json": object()}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_attach": fail_after_attachment},
    )
    result = boundary.submit(
        "kanban_attach",
        attempt_id="attach-failure",
        idempotency_key="attach-failure-key",
        target="task-read",
        expected_version=0,
        session_id="session-attach",
        workspace_id=None,
        execution_context="model-tool",
        payload={
            "task_id": "task-read",
            "filename": "must-disappear.txt",
            "content_base64": base64.b64encode(b"rollback").decode("ascii"),
            "board": "orchestrator",
        },
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_attachments").fetchone()[0] == 0
        assert conn.execute(
            "SELECT record_version FROM adrian_kanban_cards WHERE task_id = ?",
            ("task-read",),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
        ).fetchone()[0] == 0
    attachment_dir = kb.task_attachments_dir("task-read", board="orchestrator")
    assert not attachment_dir.exists() or not any(attachment_dir.iterdir())


def test_attachments_read_rejects_native_only_identity(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    with sqlite3.connect(database_path, isolation_level=None) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) "
            "VALUES ('native-only', 'Native only', 'ready', 1)"
        )
    boundary = _read_boundary(
        commands_module,
        database_path,
        provider,
        {"kanban_attachments": commands_module._handle_attachments},
    )
    result = boundary.submit(
        "kanban_attachments",
        attempt_id="native-only",
        payload={"task_id": "native-only", "board": "orchestrator"},
    )
    assert result["result"] == "REJECTED"


def test_list_is_plugin_authoritative_and_limits_the_combined_order(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_cards(database_path)
    with sqlite3.connect(database_path, isolation_level=None) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) "
            "VALUES ('native-only', 'Native only', 'ready', 900)"
        )

    boundary = _read_boundary(
        commands_module,
        database_path,
        provider,
        {"kanban_list": commands_module._handle_list},
    )
    filtered = boundary.submit(
        "kanban_list",
        attempt_id="list-filtered",
        payload={
            "board": "orchestrator",
            "status": "ready",
            "include_archived": False,
        },
    )
    assert filtered["result"] == "ACCEPTED"
    assert [row["initiative_id"] for row in filtered["value"]["initiatives"]] == [
        "initiative-read"
    ]
    assert [row["task_id"] for row in filtered["value"]["tasks"]] == [
        "task-read"
    ]
    assert filtered["value"]["count"] == 2

    limited = boundary.submit(
        "kanban_list",
        attempt_id="list-limited",
        payload={"board": "orchestrator", "limit": 1},
    )
    assert limited["result"] == "ACCEPTED"
    assert len(limited["value"]["initiatives"]) == 1
    assert limited["value"]["tasks"] == []
    assert limited["value"]["count"] == 1


def test_url_preparation_rejects_http_and_private_hosts_without_network(
    commands_module, monkeypatch
):
    attachments = importlib.import_module(f"{commands_module.__package__}.attachments")
    monkeypatch.setattr(
        attachments.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(ValueError, match="HTTPS"):
        attachments.prepare_url_attachment("http://example.com/a.txt")
    with pytest.raises(ValueError, match="public|destination|address"):
        attachments.prepare_url_attachment("https://example.com/a.txt")


def test_url_attachment_prepares_before_write_lock_and_replay_does_not_refetch(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    _seed_cards(database_path)
    attachments = importlib.import_module(f"{commands_module.__package__}.attachments")
    calls = []

    def prepare(url, *, filename=None, content_type=None):
        with sqlite3.connect(database_path) as probe:
            assert probe.in_transaction is False
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        calls.append((url, filename, content_type))
        return attachments.PreparedAttachment(
            data=b"downloaded",
            filename=filename or "download.txt",
            content_type=content_type or "text/plain",
        )

    monkeypatch.setattr(commands_module, "prepare_url_attachment", prepare)
    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_attach_url": commands_module._handle_attach_url},
    )
    fields = dict(
        attempt_id="attach-url",
        idempotency_key="attach-url-key",
        target="task-read",
        expected_version=0,
        session_id="session-attach-url",
        workspace_id=None,
        execution_context="model-tool",
        payload={
            "task_id": "task-read",
            "url": "https://example.com/result.txt",
            "filename": "result.txt",
            "board": "orchestrator",
        },
    )
    first = boundary.submit("kanban_attach_url", **fields)
    replay = boundary.submit("kanban_attach_url", **fields)

    assert first["result"] == "ACCEPTED"
    assert replay == first
    assert calls == [("https://example.com/result.txt", "result.txt", None)]
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT filename, stored_path FROM task_attachments WHERE task_id = ?",
            ("task-read",),
        ).fetchone()
        assert row is not None
        assert row[0] == "result.txt"
        assert Path(row[1]).read_bytes() == b"downloaded"
