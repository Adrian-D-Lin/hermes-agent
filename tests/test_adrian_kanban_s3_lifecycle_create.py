"""Lifecycle task-creation tests derived from Kanban v0.28 §§8.1 and 9.1."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture(scope="module")
def modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_lifecycle_create"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    loaded = {
        "commands": importlib.import_module(f"{package_name}.commands"),
        "private_adapter": importlib.import_module(
            f"{package_name}.private_adapter"
        ),
        "provider": importlib.import_module(f"{package_name}.provider"),
        "schema": importlib.import_module(f"{package_name}.schema"),
        "task_inputs": importlib.import_module(f"{package_name}.task_inputs"),
    }
    yield loaded
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


def _database(tmp_path, monkeypatch, modules):
    database_path = (tmp_path / "kanban.sqlite3").resolve()
    with kb.connect_closing(database_path) as conn:
        modules["schema"].create_schema(conn)
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
    provider = modules["provider"].AdrianKanbanAuthorityProvider(str(database_path))
    modules["provider"].register_provider(provider)
    return database_path, provider


def _seed_initiative(database_path, phase="D2", segment_id=None):
    with sqlite3.connect(database_path) as conn:
        conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')")
        card_id = conn.execute(
            "INSERT INTO adrian_kanban_cards ("
            "card_type, initiative_id, task_id, title, body, created_at, "
            "board_slug, record_version) VALUES ('initiative', 'initiative-1', "
            "NULL, 'Initiative', '# [[INITIATIVE_LEDGER]]', 1, 'orchestrator', 0)"
        ).lastrowid
        conn.execute(
            "INSERT INTO initiative_transitions ("
            "initiative_card_id, initiative_id, previous_transition_id, "
            "transition_id, from_phase, from_segment_id, to_phase, to_segment_id, "
            "trigger, actor_evidence, canonical_payload, created_at) VALUES ("
            "?, 'initiative-1', NULL, 1, NULL, NULL, ?, ?, 'initialization', "
            "'session', '{}', 1)",
            (card_id, phase, segment_id),
        )
    return card_id


def _snapshot(path, content, guidance):
    return (
        {
            "workspace_path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "source_kind": "snapshot_attachment",
        },
        path,
        {
            "filename": Path(path).name,
            "content_type": "text/markdown",
            "content_base64": base64.b64encode(content).decode("ascii"),
        },
        guidance,
    )


def _manifest():
    first = _snapshot("Canon/design-lifecycle.md", b"design", "Use as design oracle")
    second = _snapshot("2-design/review.md", b"review", "Use as prior review")
    return {
        "version": 1,
        "entries": [first[0], second[0]],
        "context_ref": {first[1]: first[3], second[1]: second[3]},
        "snapshots": {first[1]: first[2], second[1]: second[2]},
    }


def _contract(**overrides):
    value = {
        "version": 1,
        "step": "D2",
        "baseline_refs": ["Canon/design-lifecycle.md"],
        "governing_source_refs": ["2-design/review.md"],
        "prior_record_refs": [],
    }
    value.update(overrides)
    return value


def _requirements():
    return {
        "version": 1,
        "reviewer": "default",
        "fields": {
            "review_record_ref": {"type": "text"},
            "finding_count": {"type": "text"},
        },
    }


def _preparer(task_inputs):
    def prepare(payload, preparation_context):
        assert type(preparation_context) is task_inputs.TaskInputPreparationContext
        assert preparation_context.session_id == "session-create"
        assert preparation_context.execution_context == "model-tool"
        return task_inputs.prepare_task_input_manifest(
            payload["task_input_manifest_v1"],
            lambda _commit, _path: pytest.fail("snapshot create must not read Git"),
        )

    return prepare


def _boundary(modules, database_path, provider):
    commands = modules["commands"]
    return commands._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands._handle_create},
        known_profiles={
            "default",
            "independent-reviewer",
            "test-authority-reviewer",
            "builder-tester",
        },
        task_input_preparer=_preparer(modules["task_inputs"]),
    )


def _payload(**overrides):
    value = {
        "task_id": "task-d2",
        "initiative_id": "initiative-1",
        "title": "D2 independent review",
        "assignee": "independent-reviewer",
        "body": "initiative_id: initiative-1\nstep: D2",
        "goal_mode": True,
        "handoff_requirements_v1": _requirements(),
        "lifecycle_contract_v1": _contract(),
        "task_input_manifest_v1": _manifest(),
        "board": "orchestrator",
    }
    value.update(overrides)
    return value


def _submit(boundary, payload, key="create-d2"):
    return boundary.submit(
        "kanban_create",
        attempt_id=f"attempt-{key}",
        idempotency_key=key,
        target=payload["task_id"],
        expected_version=0,
        session_id="session-create",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile="default",
        payload=payload,
    )


def test_lifecycle_create_atomically_persists_contract_manifest_and_snapshots(
    modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, modules)
    _seed_initiative(database_path)
    boundary = _boundary(modules, database_path, provider)

    result = _submit(boundary, _payload())
    replay = _submit(boundary, _payload())

    assert result["result"] == "ACCEPTED"
    assert replay == result
    assert result["value"] == {
        "initiative_id": "initiative-1",
        "task_id": "task-d2",
        "handoff_governed": True,
        "lifecycle_governed": True,
        "contract_id": "adrian-kanban.lifecycle.d2",
        "contract_version": 1,
        "step": "D2",
    }
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        native = conn.execute(
            "SELECT id, assignee, status, goal_mode FROM tasks WHERE id = 'task-d2'"
        ).fetchone()
        assert dict(native) == {
            "id": "task-d2",
            "assignee": "independent-reviewer",
            "status": "ready",
            "goal_mode": 1,
        }
        contract = conn.execute(
            "SELECT contract_id, contract_version, step, execution_profile, "
            "canonical_contract_payload, skill_id, skill_version, skill_hash "
            "FROM task_lifecycle_contracts WHERE task_id = 'task-d2'"
        ).fetchone()
        assert contract["contract_id"] == "adrian-kanban.lifecycle.d2"
        assert contract["contract_version"] == "1"
        assert contract["step"] == "D2"
        assert contract["execution_profile"] == "independent-reviewer"
        assert contract["skill_id"] == "d2-iterative-review"
        assert contract["skill_version"] == "0.1.0"
        assert len(contract["skill_hash"]) == 64
        snapshot = json.loads(contract["canonical_contract_payload"])
        assert snapshot["baseline_refs"] == ["Canon/design-lifecycle.md"]
        assert snapshot["governing_source_refs"] == ["2-design/review.md"]

        manifest = conn.execute(
            "SELECT canonical_payload, declared_inputs_accessible "
            "FROM task_input_manifests WHERE task_id = 'task-d2'"
        ).fetchone()
        assert manifest["declared_inputs_accessible"] == 1
        persisted = json.loads(manifest["canonical_payload"])
        assert "snapshots" not in persisted
        assert "content_base64" not in manifest["canonical_payload"]
        entries = conn.execute(
            "SELECT workspace_path, source_kind, source_locator, context_guidance "
            "FROM task_input_entries WHERE task_id = 'task-d2' "
            "ORDER BY workspace_path"
        ).fetchall()
        assert [row["workspace_path"] for row in entries] == [
            "2-design/review.md",
            "Canon/design-lifecycle.md",
        ]
        assert {row["source_kind"] for row in entries} == {
            "snapshot_attachment"
        }
        attachment_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT id FROM task_attachments WHERE task_id = 'task-d2'"
            )
        }
        assert {row["source_locator"] for row in entries} == attachment_ids
        assert len(attachment_ids) == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM task_handoff_requirements "
            "WHERE task_id = 'task-d2'"
        ).fetchone()[0] == 1


def test_verified_manifest_can_accompany_an_uncontracted_source_dependent_task(
    modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, modules)
    _seed_initiative(database_path)
    boundary = _boundary(modules, database_path, provider)
    payload = _payload(
        task_id="task-source-only",
        title="Source-dependent ordinary task",
        assignee="builder-tester",
    )
    payload.pop("lifecycle_contract_v1")
    payload.pop("handoff_requirements_v1")
    payload.pop("goal_mode")

    result = _submit(boundary, payload, "create-source-only")

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {
        "initiative_id": "initiative-1",
        "task_id": "task-source-only",
        "handoff_governed": False,
    }
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_input_manifests "
            "WHERE task_id = 'task-source-only' AND declared_inputs_accessible = 1"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_lifecycle_contracts "
            "WHERE task_id = 'task-source-only'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts "
            "WHERE idempotency_key = 'create-source-only'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "defect",
    (
        "missing_manifest",
        "missing_handoff",
        "goal_mode_false",
        "wrong_assignee",
        "wrong_phase",
        "wrong_contract_version_type",
        "unknown_contract_field",
        "missing_body_navigation",
        "unverified_preparation",
    ),
)
def test_lifecycle_create_rejects_invalid_admission_without_partial_rows(
    modules, tmp_path, monkeypatch, defect
):
    database_path, provider = _database(tmp_path, monkeypatch, modules)
    _seed_initiative(database_path)
    boundary = _boundary(modules, database_path, provider)
    payload = _payload()
    if defect == "missing_manifest":
        payload.pop("task_input_manifest_v1")
    elif defect == "missing_handoff":
        payload.pop("handoff_requirements_v1")
    elif defect == "goal_mode_false":
        payload["goal_mode"] = False
    elif defect == "wrong_assignee":
        payload["assignee"] = "builder-tester"
    elif defect == "wrong_phase":
        payload["lifecycle_contract_v1"] = _contract(
            step="D4.1",
            governing_source_refs=[],
            prior_record_refs=["2-design/review.md"],
        )
    elif defect == "wrong_contract_version_type":
        payload["lifecycle_contract_v1"]["version"] = True
    elif defect == "unknown_contract_field":
        payload["lifecycle_contract_v1"]["derived_phase"] = "D2"
    elif defect == "missing_body_navigation":
        payload["body"] = "initiative_id: initiative-1"
    else:
        commands = modules["commands"]
        boundary = commands._CommandBoundary(
            database_path=str(database_path),
            provider=provider,
            handlers={"kanban_create": commands._handle_create},
            known_profiles={"default", "independent-reviewer"},
        )

    result = _submit(boundary, payload, f"reject-{defect}")

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE id = 'task-d2'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_cards WHERE task_id = 'task-d2'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_input_manifests").fetchone()[
            0
        ] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_lifecycle_contracts"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_attachments").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
        ).fetchone()[0] == 0


def test_manifest_preparation_occurs_once_before_idempotent_replay(
    modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, modules)
    _seed_initiative(database_path)
    calls = []

    def prepare(payload, preparation_context):
        assert type(preparation_context) is modules[
            "task_inputs"
        ].TaskInputPreparationContext
        calls.append(payload["task_id"])
        return modules["task_inputs"].prepare_task_input_manifest(
            payload["task_input_manifest_v1"],
            lambda _commit, _path: pytest.fail("snapshot create must not read Git"),
        )

    commands = modules["commands"]
    boundary = commands._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands._handle_create},
        known_profiles={"default", "independent-reviewer"},
        task_input_preparer=prepare,
    )

    first = _submit(boundary, _payload(), "prepare-once")
    replay = _submit(boundary, _payload(), "prepare-once")

    assert first["result"] == "ACCEPTED"
    assert replay == first
    assert calls == ["task-d2"]


def test_boundary_rejects_non_prepared_manifest_result(
    modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, modules)
    _seed_initiative(database_path)
    commands = modules["commands"]
    boundary = commands._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_create": commands._handle_create},
        known_profiles={"default", "independent-reviewer"},
        task_input_preparer=lambda _payload, _context: object(),
    )

    result = _submit(boundary, _payload(), "bad-prepared-type")

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_command_receipts"
        ).fetchone()[0] == 0


def test_failure_after_snapshot_storage_rolls_back_database_and_files(
    modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, modules)
    _seed_initiative(database_path)
    stored_paths = []
    native = modules["private_adapter"]._kb
    original_store = native.store_attachment_bytes

    def capture_store(connection, *args, **kwargs):
        attachment_id = original_store(connection, *args, **kwargs)
        stored_paths.append(Path(native.get_attachment(connection, attachment_id).stored_path))
        return attachment_id

    class RejectingLifecycleRepository:
        def __init__(self, _connection):
            pass

        def attach(self, **_kwargs):
            raise RuntimeError("forced post-attachment failure")

    monkeypatch.setattr(native, "store_attachment_bytes", capture_store)
    monkeypatch.setattr(
        modules["commands"],
        "LifecycleContractRepository",
        RejectingLifecycleRepository,
    )
    result = _submit(_boundary(modules, database_path, provider), _payload(), "rollback")

    assert result["result"] == "REJECTED"
    assert len(stored_paths) == 2
    assert all(not path.exists() for path in stored_paths)
    with sqlite3.connect(database_path) as conn:
        for table in (
            "tasks",
            "task_attachments",
            "task_input_manifests",
            "task_input_entries",
            "task_handoff_requirements",
            "task_lifecycle_contracts",
            "adrian_kanban_command_receipts",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM adrian_kanban_cards WHERE task_id IS NOT NULL"
        ).fetchone()[0] == 0


def test_segment_lifecycle_create_requires_exact_active_workspace(
    modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, modules)
    card_id = _seed_initiative(database_path, "DEV2", "S1")
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO initiative_segment_projections ("
            "projection_id, projection_version, initiative_card_id, initiative_id, "
            "manifest_path, manifest_sha, content_digest, parsed_segment_definitions, "
            "readiness_refs, validation_result, projected_at) VALUES ("
            "'projection-1', 1, ?, 'initiative-1', 'segments.json', ?, ?, ?, ?, "
            "'accepted', 1)",
            (
                card_id,
                "a" * 40,
                "b" * 64,
                json.dumps([{"segment_id": "S1", "ordinal": 1}]),
                json.dumps({"S1": "ready:S1"}),
            ),
        )
        conn.execute(
            "INSERT INTO segment_workspaces ("
            "workspace_id, initiative_card_id, initiative_id, segment_id, "
            "projection_id, lifecycle_state, controller_binding_ref, active, "
            "created_at, updated_at) VALUES ('workspace-S1', ?, 'initiative-1', "
            "'S1', 'projection-1', 'active', 'binding-S1', 1, 1, 1)",
            (card_id,),
        )
    boundary = _boundary(modules, database_path, provider)
    contract = _contract(
        step="DEV2.1",
        governing_source_refs=[],
        prior_record_refs=["2-design/review.md"],
        segment_id="S1",
        segment_workspace_id="workspace-wrong",
    )
    payload = _payload(
        task_id="task-dev2",
        title="DEV2 brief draft",
        body="initiative_id: initiative-1\nstep: DEV2.1\nsegment_id: S1",
        lifecycle_contract_v1=contract,
    )

    result = _submit(boundary, payload, "reject-workspace")

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE id = 'task-dev2'"
        ).fetchone()[0] == 0


def _sequence_snapshot(commands, step, predecessor_ref):
    return commands.expand_contract(
        step=step,
        initiative_id="initiative-1",
        baseline_refs=("Canon/design-lifecycle.md",),
        governing_source_refs=(),
        prior_record_refs=("2-design/review.md",),
        predecessor_ref=predecessor_ref,
    )


def _row_connection(database_path):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def _seed_accepted_candidate_predecessor(
    database_path,
    initiative_card_id,
    *,
    candidate_id="candidate-d4-1",
    step="D4.1",
    initiative_id="initiative-1",
    segment_id=None,
):
    with sqlite3.connect(database_path) as conn:
        task_card_id = conn.execute(
            "INSERT INTO adrian_kanban_cards ("
            "card_type, initiative_id, task_id, title, created_at, board_slug, "
            "record_version) VALUES ('task', ?, 'task-d4-1', 'D4.1 edit set', "
            "1, 'orchestrator', 0)",
            (initiative_id,),
        ).lastrowid
        conn.execute(
            "INSERT INTO task_lifecycle_contracts VALUES ("
            "'adrian-kanban.lifecycle.d4', '1', ?, ?, 'task-d4-1', ?, ?, ?, "
            "NULL, 'independent-reviewer', '{}', 'registry', "
            "'d4-write-gated-integration', '0.1.0', ?, 1)",
            (step, task_card_id, initiative_card_id, initiative_id, segment_id, "a" * 64),
        )
        conn.execute(
            "INSERT INTO task_candidate_handoffs VALUES ("
            "?, ?, 'task-d4-1', 101, 'test-authority-reviewer', NULL, '{}', "
            "'independent-reviewer', 1)",
            (candidate_id, task_card_id),
        )
        conn.execute(
            "INSERT INTO task_reviewer_verdicts VALUES ("
            "'verdict-d4-1', ?, 'task-d4-1', ?, 102, "
            "'test-authority-reviewer', 'accepted', NULL, 2)",
            (task_card_id, candidate_id),
        )


def test_accepted_completion_predecessor_requires_exact_accepted_candidate_chain(
    modules, tmp_path, monkeypatch
):
    database_path, _provider = _database(tmp_path, monkeypatch, modules)
    card_id = _seed_initiative(database_path, phase="D4")
    _seed_accepted_candidate_predecessor(database_path, card_id)
    snapshot = _sequence_snapshot(
        modules["commands"], "D4.2", "candidate-d4-1"
    )

    modules["commands"]._validate_lifecycle_predecessor(
        _row_connection(database_path), card_id, snapshot
    )


def test_sequence_predecessor_is_enforced_in_end_to_end_lifecycle_create(
    modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, modules)
    card_id = _seed_initiative(database_path, phase="D4")
    _seed_accepted_candidate_predecessor(database_path, card_id)
    payload = _payload(
        task_id="task-d4-2",
        title="D4.2 verification",
        assignee="test-authority-reviewer",
        body="initiative_id: initiative-1\nstep: D4.2",
        lifecycle_contract_v1=_contract(
            step="D4.2",
            governing_source_refs=[],
            prior_record_refs=["2-design/review.md"],
            predecessor_ref="candidate-d4-1",
        ),
    )

    result = _submit(
        _boundary(modules, database_path, provider), payload, "create-d4-2"
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"]["step"] == "D4.2"
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_lifecycle_contracts "
            "WHERE task_id = 'task-d4-2' AND step = 'D4.2'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("defect", "value"),
    (
        ("step", "DEV1.1a"),
        ("initiative", "initiative-other"),
        ("candidate", "candidate-missing"),
    ),
)
def test_accepted_completion_predecessor_rejects_mismatched_chain(
    modules, tmp_path, monkeypatch, defect, value
):
    database_path, _provider = _database(tmp_path, monkeypatch, modules)
    card_id = _seed_initiative(database_path, phase="D4")
    kwargs = {"step": "D4.1", "initiative_id": "initiative-1"}
    predecessor_ref = "candidate-d4-1"
    if defect == "step":
        kwargs["step"] = value
    elif defect == "initiative":
        kwargs["initiative_id"] = value
    else:
        predecessor_ref = value
    _seed_accepted_candidate_predecessor(database_path, card_id, **kwargs)
    snapshot = _sequence_snapshot(modules["commands"], "D4.2", predecessor_ref)

    with pytest.raises(ValueError):
        modules["commands"]._validate_lifecycle_predecessor(
            _row_connection(database_path), card_id, snapshot
        )


def test_accepted_completion_predecessor_rejects_verdict_task_identity_mismatch(
    modules, tmp_path, monkeypatch
):
    database_path, _provider = _database(tmp_path, monkeypatch, modules)
    card_id = _seed_initiative(database_path, phase="D4")
    _seed_accepted_candidate_predecessor(database_path, card_id)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE task_reviewer_verdicts SET task_card_id = task_card_id + 1000 "
            "WHERE candidate_id = 'candidate-d4-1'"
        )
    snapshot = _sequence_snapshot(
        modules["commands"], "D4.2", "candidate-d4-1"
    )

    with pytest.raises(ValueError):
        modules["commands"]._validate_lifecycle_predecessor(
            _row_connection(database_path), card_id, snapshot
        )


def _seed_checkpoint(
    database_path,
    initiative_card_id,
    *,
    result_id="checkpoint-d4-4",
    phase="D4",
    segment_id=None,
    payload=None,
    accepted=1,
):
    if payload is None:
        payload = {"step": "D4.4"}
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO initiative_phase_results ("
            "result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES ("
            "?, ?, 'initiative-1', ?, ?, 1, 'checkpoint', "
            "'adrian-kanban.lifecycle.d4', '1', ?, '[]', '[]', 'human', ?, ?, 1)",
            (
                result_id,
                initiative_card_id,
                phase,
                segment_id,
                payload if isinstance(payload, str) else json.dumps(payload),
                f"idempotency-{result_id}",
                accepted,
            ),
        )


def test_initiative_checkpoint_predecessor_requires_exact_accepted_result(
    modules, tmp_path, monkeypatch
):
    database_path, _provider = _database(tmp_path, monkeypatch, modules)
    card_id = _seed_initiative(database_path, phase="D4")
    _seed_checkpoint(database_path, card_id)
    snapshot = _sequence_snapshot(
        modules["commands"], "D4.5", "checkpoint-d4-4"
    )

    modules["commands"]._validate_lifecycle_predecessor(
        _row_connection(database_path), card_id, snapshot
    )


@pytest.mark.parametrize(
    ("defect", "value"),
    (
        ("payload", {"step": "D4.3"}),
        ("payload", "not-json"),
        ("phase", "D3"),
        ("accepted", 0),
        ("result", "checkpoint-missing"),
    ),
)
def test_initiative_checkpoint_predecessor_rejects_mismatched_result(
    modules, tmp_path, monkeypatch, defect, value
):
    database_path, _provider = _database(tmp_path, monkeypatch, modules)
    card_id = _seed_initiative(database_path, phase="D4")
    kwargs = {}
    predecessor_ref = "checkpoint-d4-4"
    if defect == "result":
        predecessor_ref = value
    else:
        kwargs[defect] = value
    _seed_checkpoint(database_path, card_id, **kwargs)
    snapshot = _sequence_snapshot(modules["commands"], "D4.5", predecessor_ref)

    with pytest.raises(ValueError):
        modules["commands"]._validate_lifecycle_predecessor(
            _row_connection(database_path), card_id, snapshot
        )
