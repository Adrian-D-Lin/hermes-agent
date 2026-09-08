"""Segment-manifest projection tests derived from Kanban design v0.28 section 8.3.6."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from writegate.kanban_approvals import create_kanban_approval_schema


@pytest.fixture(scope="module")
def plugin_modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_segment_projection"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    modules = {
        name: importlib.import_module(f"{package_name}.{name}")
        for name in (
            "commands",
            "provider",
            "schema",
            "segment_manifest",
            "workspace",
            "pre_tool_hook",
            "task_inputs",
        )
    }
    yield modules
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


def _manifest(*, initiative_id: str = "initiative-1") -> dict:
    return {
        "manifest_version": "1.0.0",
        "initiative_id": initiative_id,
        "initiative_title": "Initiative",
        "design_ref": {
            "path": "2-design/kanban_tracker.md",
            "commit": "b" * 40,
            "sha256": "c" * 64,
        },
        "scope_ref": {
            "path": "2-design/dev1/scope.md",
            "sha256": "d" * 64,
            "kanban_card": "task:DEV1.4",
        },
        "segmentation_ref": {
            "path": "2-design/dev1/segments.md",
            "sha256": "e" * 64,
            "kanban_card": "task:DEV1.5",
        },
        "segment_review_ref": {
            "path": "2-design/dev1/review.md",
            "sha256": "f" * 64,
            "kanban_card": "task:DEV1.6",
        },
        "repository_registry": {
            "repo-1": {
                "canonical_remote": "git@example.invalid:repo-1.git",
                "main_ref": "refs/remotes/origin/main",
                "controlled_role": "primary",
            },
            "repo-2": {
                "canonical_remote": "git@example.invalid:repo-2.git",
                "main_ref": "refs/remotes/origin/main",
                "controlled_role": "secondary",
            },
        },
        "materialization_policy": {
            "workspace_id_rule": (
                "<initiative_id>:<segment_id> after canonical escaping"
            ),
            "isolation_mechanism": (
                "trusted-registry-derived Git worktree per declared repository member"
            ),
            "path_authority": (
                "trusted workspace registry; no caller-supplied or agent-selected absolute path"
            ),
            "base_rule": (
                "materialize each segment immediately before DEV2 from each member's "
                "then-current origin/main after every required predecessor is merged"
            ),
            "writer_rule": (
                "exactly one active writer binding per logical segment workspace; "
                "all DEV2-DEV4 tasks share the same member set"
            ),
            "integration_rule": (
                "ancestry-preserving merge commit to every declared member's "
                "origin/main before successor admission"
            ),
        },
        "segments": [
            {
                "segment_id": "S1",
                "ordinal": 1,
                "title": "Foundation",
                "boundary": {
                    "in_scope": "Foundation work",
                    "out_of_scope": "Later work",
                },
                "dependency_ids": [],
                "dispatch_package_ref": {
                    "path": "2-design/dev1/dispatch/S1.md",
                    "sha256": "1" * 64,
                },
                "isolation_mechanism": "git_worktree",
                "repository_members": ["repo-1", "repo-2"],
                "segment_workspace_id": f"{initiative_id}:S1",
                "primary_scope_items": [1, 2],
                "readiness_ref": "2-design/dev1/readiness.md#s1",
            },
            {
                "segment_id": "S2",
                "ordinal": 2,
                "title": "Completion",
                "boundary": {
                    "in_scope": "Completion work",
                    "out_of_scope": "Production cutover",
                },
                "dependency_ids": ["S1"],
                "dispatch_package_ref": {
                    "path": "2-design/dev1/dispatch/S2.md",
                    "sha256": "2" * 64,
                },
                "isolation_mechanism": "git_worktree",
                "repository_members": ["repo-1"],
                "segment_workspace_id": f"{initiative_id}:S2",
                "primary_scope_items": [3],
                "readiness_ref": "2-design/dev1/readiness.md#s2",
            },
        ],
    }


def _request() -> dict:
    return {
        "result_id": "result:DEV1.7:1",
        "projection_id": "projection:1",
        "manifest_path": "2-design/dev1/segment-manifest.json",
        "manifest_sha": "a" * 40,
    }


def _canonical_bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_preparer_reads_one_pinned_git_blob_and_derives_all_operational_fields(
    plugin_modules,
):
    module = plugin_modules["segment_manifest"]
    raw = _canonical_bytes(_manifest())
    calls: list[tuple[str, str]] = []

    prepared = module.prepare_segment_manifest(
        _request(),
        lambda commit, path: calls.append((commit, path)) or raw,
        frozenset({"repo-1", "repo-2"}),
    )

    assert calls == [(_request()["manifest_sha"], _request()["manifest_path"])]
    assert prepared.initiative_id == "initiative-1"
    assert prepared.content_digest == hashlib.sha256(raw).hexdigest()
    assert prepared.projection_id == "projection:1"
    assert prepared.result_id == "result:DEV1.7:1"
    assert [segment.segment_id for segment in prepared.segments] == ["S1", "S2"]
    assert prepared.segments[0].workspace_id == "initiative-1:S1"
    assert prepared.segments[0].repository_members == ("repo-1", "repo-2")
    assert prepared.segments[0].readiness_ref.endswith("#s1")
    assert prepared.parsed_segment_definitions == json.dumps(
        _manifest()["segments"], sort_keys=True, separators=(",", ":")
    )
    assert json.loads(prepared.readiness_refs) == {
        "S1": "2-design/dev1/readiness.md#s1",
        "S2": "2-design/dev1/readiness.md#s2",
    }


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda m: m.update(extra=True), "fields"),
        (lambda m: m["segments"][1].update(ordinal=1), "ordinal"),
        (lambda m: m["segments"][1].update(dependency_ids=["S9"]), "dependency"),
        (
            lambda m: m["segments"][0].update(repository_members=["unknown"]),
            "repository",
        ),
        (
            lambda m: m["segments"][0].update(segment_workspace_id="caller-selected"),
            "workspace",
        ),
        (
            lambda m: m["segments"][0].update(isolation_mechanism="directory"),
            "isolation",
        ),
    ],
)
def test_preparer_rejects_malformed_or_untrusted_manifest(
    plugin_modules, mutate, match
):
    module = plugin_modules["segment_manifest"]
    manifest = _manifest()
    mutate(manifest)
    with pytest.raises(ValueError, match=match):
        module.prepare_segment_manifest(
            _request(),
            lambda _commit, _path: _canonical_bytes(manifest),
            frozenset({"repo-1", "repo-2"}),
        )


def test_preparer_rejects_non_full_commit_path_escape_and_initiative_mismatch(
    plugin_modules,
):
    module = plugin_modules["segment_manifest"]
    request = _request()
    request["manifest_sha"] = "a" * 12
    with pytest.raises(ValueError, match="manifest_sha"):
        module.prepare_segment_manifest(
            request,
            lambda _commit, _path: _canonical_bytes(_manifest()),
            frozenset({"repo-1", "repo-2"}),
        )

    request = _request()
    request["manifest_path"] = "../segment-manifest.json"
    with pytest.raises(ValueError, match="manifest_path"):
        module.prepare_segment_manifest(
            request,
            lambda _commit, _path: _canonical_bytes(_manifest()),
            frozenset({"repo-1", "repo-2"}),
        )

    with pytest.raises(ValueError, match="initiative"):
        module.prepare_segment_manifest(
            _request(),
            lambda _commit, _path: _canonical_bytes(_manifest(initiative_id="other")),
            frozenset({"repo-1", "repo-2"}),
            expected_initiative_id="initiative-1",
        )


@pytest.mark.parametrize(
    "mutate, match",
    [
        (
            lambda manifest: manifest["materialization_policy"].update(
                writer_rule=manifest["materialization_policy"]["base_rule"]
            ),
            "writer_rule",
        ),
        (
            lambda manifest: manifest.update(initiative_title="Initiative "),
            "whitespace",
        ),
        (
            lambda manifest: manifest["segments"][1].update(
                dependency_ids=["S1", "S1"]
            ),
            "unique",
        ),
    ],
)
def test_preparer_rejects_policy_substitution_whitespace_and_duplicate_dependency(
    plugin_modules, mutate, match
):
    manifest = _manifest()
    mutate(manifest)

    with pytest.raises(ValueError, match=match):
        plugin_modules["segment_manifest"].prepare_segment_manifest(
            _request(),
            lambda _commit, _path: _canonical_bytes(manifest),
            frozenset({"repo-1", "repo-2"}),
        )


def _database(tmp_path, monkeypatch, plugin_modules):
    database_path = (tmp_path / "kanban.sqlite3").resolve()
    with kb.connect_closing(database_path) as conn:
        plugin_modules["schema"].create_schema(conn)
        create_kanban_approval_schema(conn)
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
    provider = plugin_modules["provider"].AdrianKanbanAuthorityProvider(
        str(database_path)
    )
    plugin_modules["provider"].register_provider(provider)
    return database_path, provider


def _seed_dev1_state(database_path: Path) -> int:
    with sqlite3.connect(database_path) as conn:
        conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')")
        card_id = conn.execute(
            "INSERT INTO adrian_kanban_cards "
            "(card_type, initiative_id, task_id, title, body, created_at, "
            "board_slug, record_version) VALUES "
            "('initiative', 'initiative-1', NULL, 'Initiative', 'body', 1, "
            "'orchestrator', 0)"
        ).lastrowid
        conn.execute(
            "INSERT INTO initiative_transitions "
            "(initiative_card_id, initiative_id, previous_transition_id, "
            "transition_id, from_phase, from_segment_id, to_phase, "
            "to_segment_id, trigger, actor_evidence, canonical_payload, "
            "created_at) VALUES (?, 'initiative-1', NULL, 1, NULL, NULL, "
            "'DEV1', NULL, 'initialization', '{}', '{}', 1)",
            (card_id,),
        )
        conn.execute(
            "INSERT INTO initiative_phase_results "
            "(result_id, initiative_card_id, initiative_id, phase, segment_id, "
            "iteration, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, idempotency_key, accepted, created_at) VALUES "
            "('checkpoint:DEV1.3', ?, 'initiative-1', 'DEV1', NULL, 1, "
            "'orchestration_checkpoint', 'adrian-kanban.lifecycle.dev1', '1', "
            "?, '[]', '[]', ?, 'seed:checkpoint', 1, 2)",
            (
                card_id,
                json.dumps(
                    {"step": "DEV1.3", "dry": True},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    {"session_id": "session-seed", "actor_profile": "default"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        for sequence, step in enumerate(("DEV1.4", "DEV1.5", "DEV1.6"), start=1):
            task_id = f"task:{step}"
            task_card_id = conn.execute(
                "INSERT INTO adrian_kanban_cards "
                "(card_type, initiative_id, task_id, title, body, created_at, "
                "board_slug, record_version) VALUES "
                "('task', 'initiative-1', ?, ?, '', 1, 'orchestrator', 0)",
                (task_id, step),
            ).lastrowid
            candidate_id = f"candidate:{step}"
            conn.execute(
                "INSERT INTO task_lifecycle_contracts "
                "(contract_id, contract_version, step, task_card_id, task_id, "
                "initiative_card_id, initiative_id, segment_id, workspace_id, "
                "execution_profile, canonical_contract_payload, registry_hash, "
                "skill_id, skill_version, skill_hash, created_at) VALUES "
                "('adrian-kanban.lifecycle.dev1', '1', ?, ?, ?, ?, "
                "'initiative-1', NULL, NULL, ?, '{}', ?, ?, '1', ?, 1)",
                (
                    step,
                    task_card_id,
                    task_id,
                    card_id,
                    (
                        "independent-reviewer"
                        if step == "DEV1.5"
                        else "test-authority-reviewer"
                    ),
                    "r" * 64,
                    f"skill:{step}",
                    "s" * 64,
                ),
            )
            conn.execute(
                "INSERT INTO task_candidate_handoffs "
                "(candidate_id, task_card_id, task_id, execution_run_id, reviewer, "
                "summary, metadata_json, submitted_by, created_at) VALUES "
                "(?, ?, ?, ?, 'test-authority-reviewer', '', '{}', ?, 1)",
                (
                    candidate_id,
                    task_card_id,
                    task_id,
                    sequence,
                    (
                        "independent-reviewer"
                        if step == "DEV1.5"
                        else "test-authority-reviewer"
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO task_reviewer_verdicts "
                "(verdict_id, task_card_id, task_id, candidate_id, review_run_id, "
                "reviewer, verdict, summary, created_at) VALUES "
                "(?, ?, ?, ?, ?, 'test-authority-reviewer', 'accepted', '', 1)",
                (
                    f"verdict:{step}",
                    task_card_id,
                    task_id,
                    candidate_id,
                    100 + sequence,
                ),
            )
        conn.commit()
        return int(card_id)


def _payload() -> dict:
    return {
        "initiative_id": "initiative-1",
        "update_kind": "segment_manifest_projection",
        "update": _request(),
        "approval_id": "approval:projection:1",
        "board": "orchestrator",
    }


def _approval_digest(payload: dict) -> str:
    approved = {key: value for key, value in payload.items() if key != "approval_id"}
    return hashlib.sha256(
        json.dumps(approved, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _approve(database_path: Path, payload: dict, version: int = 0) -> None:
    now = int(time.time())
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO write_gate_kanban_approvals ("
            "approval_id, approval_type, state, request_id, operation, "
            "initiative_id, proposed_creation_id, expected_version, "
            "canonical_digest, canonicalization_version, authorizer_evidence, "
            "session_id, prepared_at, approved_at, expires_at, approval_evidence"
            ") VALUES (?, 'kanban_initiative_mutation', 'approved', ?, "
            "'kanban_update_initiative', 'initiative-1', NULL, ?, ?, 1, ?, "
            "'session-projection', ?, ?, ?, 'approved-on-second-action')",
            (
                payload["approval_id"],
                f"attempt:{payload['approval_id']}",
                version,
                _approval_digest(payload),
                '{"peer_identity":"adrian@tailnet"}',
                now - 2,
                now - 1,
                now + 600,
            ),
        )
        conn.commit()


def _boundary(plugin_modules, database_path, provider, prepared, calls=None):
    commands = plugin_modules["commands"]

    def preparer(payload, context):
        if calls is not None:
            calls.append((payload["update_kind"], context.session_id))
        assert payload["initiative_id"] == prepared.initiative_id
        assert context.session_id == "session-projection"
        return prepared

    return commands._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_update_initiative": commands._handle_update_initiative},
        segment_manifest_preparer=preparer,
    )


def _submit(boundary, payload: dict, *, profile: str = "default") -> dict:
    return boundary.submit(
        "kanban_update_initiative",
        attempt_id=f"attempt:{payload['approval_id']}",
        idempotency_key=f"key:{payload['approval_id']}",
        target="initiative-1",
        expected_version=0,
        session_id="session-projection",
        workspace_id=None,
        execution_context="model-tool",
        actor_profile=profile,
        payload=payload,
    )


def test_projection_admission_is_atomic_and_preassigns_every_workspace_member(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    card_id = _seed_dev1_state(database_path)
    manifest_bytes = _canonical_bytes(_manifest())
    prepared = plugin_modules["segment_manifest"].prepare_segment_manifest(
        _request(),
        lambda _commit, _path: manifest_bytes,
        frozenset({"repo-1", "repo-2"}),
        expected_initiative_id="initiative-1",
    )
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(
        _boundary(plugin_modules, database_path, provider, prepared), payload
    )

    assert result["result"] == "ACCEPTED"
    assert result["value"] == {
        "initiative_id": "initiative-1",
        "update_kind": "segment_manifest_projection",
        "record_version": 1,
        "result_id": "result:DEV1.7:1",
        "projection_id": "projection:1",
        "projection_version": 1,
    }
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        projection = conn.execute(
            "SELECT * FROM initiative_segment_projections"
        ).fetchone()
        assert projection["initiative_card_id"] == card_id
        assert projection["initiative_id"] == "initiative-1"
        assert projection["manifest_path"] == _request()["manifest_path"]
        assert projection["manifest_sha"] == _request()["manifest_sha"]
        assert (
            projection["content_digest"] == hashlib.sha256(manifest_bytes).hexdigest()
        )
        assert projection["validation_result"] == "accepted"
        assert (
            json.loads(projection["parsed_segment_definitions"])[1]["segment_id"]
            == "S2"
        )
        assert json.loads(projection["readiness_refs"]) == {
            "S1": "2-design/dev1/readiness.md#s1",
            "S2": "2-design/dev1/readiness.md#s2",
        }
        workspaces = conn.execute(
            "SELECT workspace_id, segment_id, projection_id, lifecycle_state, "
            "controller_binding_ref, active FROM segment_workspaces "
            "ORDER BY segment_id"
        ).fetchall()
        assert [tuple(row) for row in workspaces] == [
            (
                "initiative-1:S1",
                "S1",
                "projection:1",
                "planned",
                "adrian-kanban:workspace-controller:v1",
                1,
            ),
            (
                "initiative-1:S2",
                "S2",
                "projection:1",
                "planned",
                "adrian-kanban:workspace-controller:v1",
                1,
            ),
        ]
        members = conn.execute(
            "SELECT workspace_id, repository_identity, relative_path, branch, "
            "required_base_sha, observed_head, member_state FROM "
            "segment_workspace_members ORDER BY workspace_id, repository_identity"
        ).fetchall()
        assert [tuple(row) for row in members] == [
            (
                "initiative-1:S1",
                "repo-1",
                "initiative-1/S1/repo-1",
                "initiative-1/S1",
                None,
                None,
                "planned",
            ),
            (
                "initiative-1:S1",
                "repo-2",
                "initiative-1/S1/repo-2",
                "initiative-1/S1",
                None,
                None,
                "planned",
            ),
            (
                "initiative-1:S2",
                "repo-1",
                "initiative-1/S2/repo-1",
                "initiative-1/S2",
                None,
                None,
                "planned",
            ),
        ]
        phase_result = conn.execute(
            "SELECT phase, segment_id, result_kind, contract_id, contract_version, "
            "canonical_payload, accepted_task_refs, accepted_checkpoint_refs, "
            "actor_evidence, accepted FROM initiative_phase_results "
            "WHERE result_id='result:DEV1.7:1'"
        ).fetchone()
        assert tuple(phase_result[:5]) == (
            "DEV1",
            None,
            "segment_manifest_projection",
            "adrian-kanban.lifecycle.dev1",
            "1",
        )
        assert json.loads(phase_result["canonical_payload"])["manifest_sha"] == "a" * 40
        assert json.loads(phase_result["accepted_task_refs"]) == [
            "candidate:DEV1.4",
            "candidate:DEV1.5",
            "candidate:DEV1.6",
        ]
        assert json.loads(phase_result["accepted_checkpoint_refs"]) == [
            "checkpoint:DEV1.3"
        ]
        assert json.loads(phase_result["actor_evidence"])["actor_profile"] == "default"
        assert phase_result["accepted"] == 1
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "consumed"
        )


def test_projection_rejection_does_not_consume_approval_or_leave_partial_rows(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_dev1_state(database_path)
    prepared = plugin_modules["segment_manifest"].prepare_segment_manifest(
        _request(),
        lambda _commit, _path: _canonical_bytes(_manifest()),
        frozenset({"repo-1", "repo-2"}),
        expected_initiative_id="initiative-1",
    )
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(
        _boundary(plugin_modules, database_path, provider, prepared),
        payload,
        profile="independent-reviewer",
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "approved"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM initiative_segment_projections"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM segment_workspaces").fetchone()[0] == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM segment_workspace_members").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards "
                "WHERE card_type='initiative'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "reference_field",
    ("scope_ref", "segmentation_ref", "segment_review_ref"),
)
def test_projection_rejects_manifest_task_identity_mismatch_before_consumption(
    plugin_modules, tmp_path, monkeypatch, reference_field
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_dev1_state(database_path)
    manifest = _manifest()
    manifest[reference_field]["kanban_card"] = "task:other"
    prepared = plugin_modules["segment_manifest"].prepare_segment_manifest(
        _request(),
        lambda _commit, _path: _canonical_bytes(manifest),
        frozenset({"repo-1", "repo-2"}),
        expected_initiative_id="initiative-1",
    )
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(
        _boundary(plugin_modules, database_path, provider, prepared), payload
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "approved"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM initiative_segment_projections"
            ).fetchone()[0]
            == 0
        )


def test_projection_rejects_wrong_dev1_checkpoint_before_consumption(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_dev1_state(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE initiative_phase_results SET canonical_payload = ? "
            "WHERE result_id = 'checkpoint:DEV1.3'",
            (json.dumps({"step": "DEV1.2"}),),
        )
        conn.commit()
    prepared = plugin_modules["segment_manifest"].prepare_segment_manifest(
        _request(),
        lambda _commit, _path: _canonical_bytes(_manifest()),
        frozenset({"repo-1", "repo-2"}),
        expected_initiative_id="initiative-1",
    )
    payload = _payload()
    _approve(database_path, payload)

    result = _submit(
        _boundary(plugin_modules, database_path, provider, prepared), payload
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "approved"
        )


def test_projection_rejects_prepared_request_identity_mismatch(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_dev1_state(database_path)
    prepared = plugin_modules["segment_manifest"].prepare_segment_manifest(
        _request(),
        lambda _commit, _path: _canonical_bytes(_manifest()),
        frozenset({"repo-1", "repo-2"}),
        expected_initiative_id="initiative-1",
    )
    payload = _payload()
    payload["update"]["manifest_sha"] = "9" * 40
    _approve(database_path, payload)

    result = _submit(
        _boundary(plugin_modules, database_path, provider, prepared), payload
    )

    assert result["result"] == "REJECTED"
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT state FROM write_gate_kanban_approvals").fetchone()[0]
            == "approved"
        )


def test_projection_receipt_replay_does_not_prepare_or_mutate_twice(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_dev1_state(database_path)
    prepared = plugin_modules["segment_manifest"].prepare_segment_manifest(
        _request(),
        lambda _commit, _path: _canonical_bytes(_manifest()),
        frozenset({"repo-1", "repo-2"}),
        expected_initiative_id="initiative-1",
    )
    payload = _payload()
    _approve(database_path, payload)
    calls: list[tuple[str, str]] = []
    boundary = _boundary(plugin_modules, database_path, provider, prepared, calls)

    first = _submit(boundary, payload)
    replay = _submit(boundary, payload)

    assert replay == first
    assert calls == [("segment_manifest_projection", "session-projection")]
    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM initiative_segment_projections"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT record_version FROM adrian_kanban_cards "
                "WHERE card_type = 'initiative'"
            ).fetchone()[0]
            == 1
        )


def test_schema_allows_unknown_base_only_for_planned_member(plugin_modules):
    conn = sqlite3.connect(":memory:")
    plugin_modules["schema"].create_schema(conn)
    columns = {
        row[1]: row
        for row in conn.execute("PRAGMA table_info(segment_workspace_members)")
    }
    assert columns["required_base_sha"][3] == 0
    conn.close()


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _disposable_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    _git(repository, "config", "user.name", "Segment Projection Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "main")
    return repository.resolve(), _git(repository, "rev-parse", "HEAD")


def _committed_segment_manifest(repository):
    request = _request()
    path = repository / request["manifest_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _canonical_bytes(_manifest())
    path.write_bytes(content)
    _git(repository, "add", "--", request["manifest_path"])
    _git(repository, "commit", "-m", "segment manifest")
    request["manifest_sha"] = _git(repository, "rev-parse", "HEAD")
    return request, content


def _prepare_published_manifest(modules, repository, request):
    registry = SimpleNamespace(
        get_active_binding=lambda session_id: (
            SimpleNamespace(worktree_path=str(repository))
            if session_id == "publication-session"
            else None
        )
    )
    preparer = modules["pre_tool_hook"].GitSegmentManifestPreparer(
        lambda: frozenset({"repo-1", "repo-2"}), lambda: registry
    )
    return preparer(
        {
            "initiative_id": "initiative-1",
            "update_kind": "segment_manifest_projection",
            "update": request,
        },
        modules["task_inputs"].TaskInputPreparationContext(
            session_id="publication-session",
            execution_context="model-tool",
            workspace_id=None,
            actor_profile="default",
        ),
    )


@pytest.mark.parametrize("publication", ["unpushed", "feature_only", "rewound_main"])
def test_segment_projection_requires_actual_remote_main_containment(
    plugin_modules, tmp_path, publication
):
    repository, base = _disposable_repository(tmp_path)
    request, _ = _committed_segment_manifest(repository)
    if publication == "feature_only":
        _git(repository, "push", "origin", "HEAD:refs/heads/feature")
    elif publication == "rewound_main":
        _git(repository, "push", "origin", "main")
        # Remote maintenance bypasses this checkout's stale tracking reference.
        _git(tmp_path / "origin.git", "update-ref", "refs/heads/main", base)
        assert _git(repository, "rev-parse", "origin/main") == request["manifest_sha"]
    before = _git(repository, "show-ref")
    with pytest.raises(ValueError, match="origin/main"):
        _prepare_published_manifest(plugin_modules, repository, request)
    assert _git(repository, "show-ref") == before


def test_segment_projection_accepts_published_ancestor_despite_stale_tracking_ref(
    plugin_modules, tmp_path
):
    repository, base = _disposable_repository(tmp_path)
    request, content = _committed_segment_manifest(repository)
    _git(repository, "commit", "--allow-empty", "-m", "later main commit")
    _git(repository, "push", "origin", "main")
    _git(repository, "update-ref", "refs/remotes/origin/main", base)
    (repository / request["manifest_path"]).write_text("dirty local data")
    before = _git(repository, "show-ref")
    prepared = _prepare_published_manifest(plugin_modules, repository, request)
    assert prepared.content_digest == hashlib.sha256(content).hexdigest()
    assert _git(repository, "show-ref") == before
    assert (repository / request["manifest_path"]).read_text() == "dirty local data"


def test_segment_projection_does_not_fetch_missing_remote_tip_objects(
    plugin_modules, tmp_path
):
    repository, _ = _disposable_repository(tmp_path)
    request, _ = _committed_segment_manifest(repository)
    _git(repository, "push", "origin", "main")
    peer = tmp_path / "peer"
    _git(tmp_path, "clone", "--branch", "main", str(tmp_path / "origin.git"), str(peer))
    _git(peer, "config", "user.name", "Peer")
    _git(peer, "config", "user.email", "peer@example.invalid")
    _git(peer, "commit", "--allow-empty", "-m", "remote advance")
    _git(peer, "push", "origin", "main")
    tip = _git(peer, "rev-parse", "HEAD")
    before = _git(repository, "show-ref")
    with pytest.raises(ValueError, match="[Ff]etch"):
        _prepare_published_manifest(plugin_modules, repository, request)
    assert _git(repository, "show-ref") == before
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", tip], cwd=repository, capture_output=True
        ).returncode
        != 0
    )


def test_segment_projection_rejects_absent_remote_main(plugin_modules, tmp_path):
    repository, _ = _disposable_repository(tmp_path)
    request, _ = _committed_segment_manifest(repository)
    _git(tmp_path / "origin.git", "update-ref", "-d", "refs/heads/main")
    with pytest.raises(ValueError, match="origin/main"):
        _prepare_published_manifest(plugin_modules, repository, request)


def test_segment_projection_ignores_local_git_replacement_objects(
    plugin_modules, tmp_path
):
    repository, _ = _disposable_repository(tmp_path)
    request, content = _committed_segment_manifest(repository)
    _git(repository, "push", "origin", "main")
    replacement = _manifest()
    replacement["initiative_title"] = "locally replaced title"
    (repository / request["manifest_path"]).write_bytes(_canonical_bytes(replacement))
    _git(repository, "add", "--", request["manifest_path"])
    _git(repository, "commit", "-m", "replacement content")
    replacement_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "replace", request["manifest_sha"], replacement_sha)
    prepared = _prepare_published_manifest(plugin_modules, repository, request)
    assert prepared.content_digest == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize(
    "remote_result",
    [
        b"",
        b"invalid-secret-remote-url\n",
        b"a" * 40 + b" refs/heads/feature\n",
        (b"a" * 40 + b" refs/heads/main\n") * 2,
        b"\xff refs/heads/main\n",
    ],
)
def test_segment_projection_rejects_malformed_remote_response_without_echo(
    plugin_modules, tmp_path, monkeypatch, remote_result
):
    repository, _ = _disposable_repository(tmp_path)
    request, _ = _committed_segment_manifest(repository)
    real_run = subprocess.run

    def run(args, **kwargs):
        if "ls-remote" in args:
            return subprocess.CompletedProcess(
                args, 0, remote_result, b"private stderr"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="origin/main") as failure:
        _prepare_published_manifest(plugin_modules, repository, request)
    assert "secret" not in str(failure.value)
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("failure_kind", ["timeout", "nonzero", "oserror"])
def test_segment_projection_remote_failures_are_safe_and_actionable(
    plugin_modules, tmp_path, monkeypatch, failure_kind
):
    repository, _ = _disposable_repository(tmp_path)
    request, _ = _committed_segment_manifest(repository)
    real_run = subprocess.run

    def run(args, **kwargs):
        if "ls-remote" not in args:
            return real_run(args, **kwargs)
        if failure_kind == "timeout":
            raise subprocess.TimeoutExpired(args, 30, stderr=b"private token")
        if failure_kind == "oserror":
            raise OSError("private token")
        return subprocess.CompletedProcess(args, 128, b"", b"private token")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError) as failure:
        _prepare_published_manifest(plugin_modules, repository, request)
    assert "private" not in str(failure.value)
    assert any(
        word in str(failure.value).lower() for word in ("retry", "check", "verify")
    )


def test_planned_member_base_is_trusted_late_bound_and_idempotent(
    plugin_modules, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, plugin_modules)
    _seed_dev1_state(database_path)
    prepared = plugin_modules["segment_manifest"].prepare_segment_manifest(
        _request(),
        lambda _commit, _path: _canonical_bytes(_manifest()),
        frozenset({"repo-1", "repo-2"}),
        expected_initiative_id="initiative-1",
    )
    payload = _payload()
    _approve(database_path, payload)
    assert (
        _submit(_boundary(plugin_modules, database_path, provider, prepared), payload)[
            "result"
        ]
        == "ACCEPTED"
    )
    repository, expected_sha = _disposable_repository(tmp_path)
    workspace = plugin_modules["workspace"]
    registry = workspace._TrustedRepositoryRegistry((
        workspace._RepositoryRegistration(
            repository_identity="repo-1",
            repository_root=str(repository),
            controlled_worktree_root=str(repository / ".segment-worktrees"),
        ),
    ))
    conn = sqlite3.connect(database_path, isolation_level=None)
    controller = workspace._SegmentWorkspaceController(conn, registry)

    assert (
        controller.pin_planned_member_base("initiative-1:S1", "repo-1", pinned_at=10)
        == expected_sha
    )
    assert (
        controller.pin_planned_member_base("initiative-1:S1", "repo-1", pinned_at=11)
        == expected_sha
    )
    assert conn.execute(
        "SELECT required_base_sha, observed_head, member_state, observed_at "
        "FROM segment_workspace_members WHERE workspace_id = 'initiative-1:S1' "
        "AND repository_identity = 'repo-1'"
    ).fetchone() == (expected_sha, None, "planned", 10)

    monkeypatch.setattr(controller, "_resolve_origin_main", lambda _root: "9" * 40)
    with pytest.raises(workspace._WorkspaceRejected, match="base mismatch"):
        controller.pin_planned_member_base("initiative-1:S1", "repo-1", pinned_at=12)
    conn.close()


def test_nonplanned_member_cannot_retain_unknown_base(plugin_modules):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    plugin_modules["schema"].create_schema(conn)
    conn.execute("INSERT INTO adrian_kanban_initiatives VALUES ('initiative-1')")
    card_id = conn.execute(
        "INSERT INTO adrian_kanban_cards "
        "(card_type, initiative_id, task_id, title, created_at) VALUES "
        "('initiative', 'initiative-1', NULL, 'Initiative', 1)"
    ).lastrowid
    conn.execute(
        "INSERT INTO initiative_segment_projections VALUES "
        "('projection-1', 1, ?, 'initiative-1', 'manifest.json', ?, ?, '[]', "
        "'{}', 'accepted', 1)",
        (card_id, "a" * 40, "b" * 64),
    )
    conn.execute(
        "INSERT INTO segment_workspaces VALUES "
        "('initiative-1:S1', ?, 'initiative-1', 'S1', 'projection-1', "
        "'planned', 'controller', 1, 1, 1)",
        (card_id,),
    )
    conn.execute(
        "INSERT INTO segment_workspace_members VALUES "
        "('initiative-1:S1', 'repo-1', 'initiative-1/S1/repo-1', "
        "'initiative-1/S1', NULL, NULL, 'planned', 1)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE segment_workspace_members SET member_state = 'materialized', "
            "observed_head = ? WHERE workspace_id = 'initiative-1:S1'",
            ("c" * 40,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE segment_workspace_members SET member_state = 'materialized', "
            "required_base_sha = ? WHERE workspace_id = 'initiative-1:S1'",
            ("d" * 40,),
        )
    conn.close()
