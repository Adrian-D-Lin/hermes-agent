"""Candidate tests for v0.29 Session Startup persistence.

CANDIDATE MATERIAL — UNTRUSTED UNTIL INDEPENDENTLY RATIFIED.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

from hermes_cli import kanban_db as kb
from gateway import trusted_authorizer_evidence as trusted


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def adrian_plugin_modules(kanban_home: Path) -> dict[str, ModuleType]:
    import yaml
    from hermes_cli.plugins import PluginManager

    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["adrian-kanban"]}}),
        encoding="utf-8",
    )
    manager = PluginManager(scope_key=str(kanban_home.resolve()))
    manager.discover_and_load()
    loaded = manager._plugins["adrian-kanban"]
    assert loaded.enabled is True, loaded.error
    assert loaded.module is not None
    package = loaded.module
    modules = {
        "schema": importlib.import_module(f"{package.__name__}.schema"),
        "store": importlib.import_module(f"{package.__name__}.store"),
        "validation": importlib.import_module(f"{package.__name__}.validation"),
        "session_startup": importlib.import_module(
            f"{package.__name__}.session_startup"
        ),
        "session_startup_runtime": importlib.import_module(
            f"{package.__name__}.session_startup_runtime"
        ),
        "session_startup_creation": importlib.import_module(
            f"{package.__name__}.session_startup_creation"
        ),
        "commands": importlib.import_module(f"{package.__name__}.commands"),
        "provider": importlib.import_module(f"{package.__name__}.provider"),
    }
    try:
        yield modules
    finally:
        manager.unload("adrian-kanban")


def _db_path(kanban_home: Path, name: str) -> Path:
    return (kanban_home.parent / name / "kanban.db").resolve()


def _advance_to_anchored(store, session_id: str = "sess-1") -> dict:
    store.create_or_read_session_startup(
        session_id=session_id,
        opening_prompt="hello",
        protocol_version="v0.29",
    )
    store.transition_session_startup(
        session_id=session_id,
        expected_revision=0,
        to_state="awaiting_initiative_selection",
        selected_project_id="proj-1",
    )
    store.transition_session_startup(
        session_id=session_id,
        expected_revision=1,
        to_state="resolving_lifecycle_and_work",
        selected_initiative_id="init-1",
    )
    store.transition_session_startup(
        session_id=session_id,
        expected_revision=2,
        to_state="verifying_workspace",
        accepted_phase="DEV1",
        logical_workspace_id="ws-1",
    )
    store.transition_session_startup(
        session_id=session_id,
        expected_revision=3,
        to_state="awaiting_writegate_confirmation",
        writegate_binding_version="1",
        writegate_binding_ref="ref-1",
    )
    return store.transition_session_startup(
        session_id=session_id,
        expected_revision=4,
        to_state="anchored",
    )


def test_session_startup_happy_sequence_preserves_prompt(
    adrian_plugin_modules, kanban_home
):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    with AdmittedStore(database_path=_db_path(kanban_home, "ss-happy")) as store:
        record = _advance_to_anchored(store)
        assert record["state"] == "anchored"
        assert record["revision"] == 5
        assert record["held_opening_prompt"] == "hello"
        assert record["selected_project_id"] == "proj-1"
        assert record["selected_initiative_id"] == "init-1"
        assert record["accepted_phase"] == "DEV1"
        assert record["logical_workspace_id"] == "ws-1"
        assert record["writegate_binding_ref"] == "ref-1"


def test_release_status_default_pending(adrian_plugin_modules, kanban_home):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    with AdmittedStore(database_path=_db_path(kanban_home, "rs-default")) as store:
        record = store.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )
        assert record["release_status"] == "pending"


def test_release_status_migration_existing_schema(adrian_plugin_modules, kanban_home):
    schema = adrian_plugin_modules["schema"]
    db = _db_path(kanban_home, "rs-migrate")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE adrian_kanban_session_startup_records ("
            "session_id TEXT PRIMARY KEY, protocol_version TEXT NOT NULL, "
            "state TEXT NOT NULL, held_opening_prompt TEXT NOT NULL, "
            "revision INTEGER NOT NULL DEFAULT 0, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO adrian_kanban_session_startup_records "
            "(session_id, protocol_version, state, held_opening_prompt, "
            "revision, created_at, updated_at) "
            "VALUES ('sess-old', 'v0.28', 'anchored', 'old prompt', 0, 0, 0)"
        )
        conn.commit()
        schema.create_schema(conn)
        row = conn.execute(
            "SELECT release_status FROM adrian_kanban_session_startup_records "
            "WHERE session_id = 'sess-old'"
        ).fetchone()
        assert row[0] == "pending"
    finally:
        conn.close()


def test_release_status_happy_path(adrian_plugin_modules, kanban_home):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    with AdmittedStore(database_path=_db_path(kanban_home, "rs-happy")) as store:
        store.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )
        issued = store.set_release_status(
            session_id="sess-1", expected_revision=0, release_status="issued"
        )
        assert issued["release_status"] == "issued"
        assert issued["revision"] == 1
        observed = store.set_release_status(
            session_id="sess-1", expected_revision=1, release_status="observed"
        )
        assert observed["release_status"] == "observed"
        assert observed["revision"] == 2


def test_release_status_idempotent_same_value(adrian_plugin_modules, kanban_home):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    with AdmittedStore(database_path=_db_path(kanban_home, "rs-idem")) as store:
        store.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )
        result = store.set_release_status(
            session_id="sess-1", expected_revision=0, release_status="pending"
        )
        assert result["release_status"] == "pending"
        assert result["revision"] == 0


def test_release_status_stale_revision(adrian_plugin_modules, kanban_home):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    RevisionError = adrian_plugin_modules["validation"].SessionStartupRevisionError
    with AdmittedStore(database_path=_db_path(kanban_home, "rs-stale")) as store:
        store.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )
        with pytest.raises(RevisionError):
            store.set_release_status(
                session_id="sess-1", expected_revision=99, release_status="issued"
            )
        record = store.read_session_startup(session_id="sess-1")
        assert record["release_status"] == "pending"
        assert record["revision"] == 0


def test_release_status_backwards_transition_rejected(
    adrian_plugin_modules, kanban_home
):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    ReleaseErr = adrian_plugin_modules["validation"].ReleaseTransitionError
    with AdmittedStore(database_path=_db_path(kanban_home, "rs-back")) as store:
        store.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )
        store.set_release_status(
            session_id="sess-1", expected_revision=0, release_status="issued"
        )
        with pytest.raises(ReleaseErr):
            store.set_release_status(
                session_id="sess-1", expected_revision=1, release_status="pending"
            )
        record = store.read_session_startup(session_id="sess-1")
        assert record["release_status"] == "issued"
        assert record["revision"] == 1


def test_release_status_skipped_transition_rejected(
    adrian_plugin_modules, kanban_home
):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    ReleaseErr = adrian_plugin_modules["validation"].ReleaseTransitionError
    with AdmittedStore(database_path=_db_path(kanban_home, "rs-skip")) as store:
        store.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )
        with pytest.raises(ReleaseErr):
            store.set_release_status(
                session_id="sess-1", expected_revision=0, release_status="observed"
            )
        record = store.read_session_startup(session_id="sess-1")
        assert record["release_status"] == "pending"
        assert record["revision"] == 0


def test_release_status_rollback_after_failure(adrian_plugin_modules, kanban_home):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    ReleaseErr = adrian_plugin_modules["validation"].ReleaseTransitionError
    with AdmittedStore(database_path=_db_path(kanban_home, "rs-rollback")) as store:
        store.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )
        with pytest.raises(ReleaseErr):
            store.set_release_status(
                session_id="sess-1", expected_revision=0, release_status="observed"
            )
        record = store.read_session_startup(session_id="sess-1")
        assert record["release_status"] == "pending"
        assert record["revision"] == 0


def test_session_startup_create_or_read_never_overwrites_prompt(
    adrian_plugin_modules, kanban_home
):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    with AdmittedStore(database_path=_db_path(kanban_home, "ss-create")) as store:
        created = store.create_or_read_session_startup(
            session_id="sess-1",
            opening_prompt="  exact opening prompt\n",
            protocol_version="v0.29",
        )
        again = store.create_or_read_session_startup(
            session_id="sess-1",
            opening_prompt="replacement must not win",
            protocol_version="v0.29",
        )
        assert created["state"] == "awaiting_project_selection"
        assert created["revision"] == 0
        assert again["held_opening_prompt"] == "  exact opening prompt\n"
        assert again["revision"] == 0


def test_session_startup_missing_required_field_is_rejected(
    adrian_plugin_modules, kanban_home
):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    StateError = adrian_plugin_modules["validation"].SessionStartupStateError
    with AdmittedStore(database_path=_db_path(kanban_home, "ss-missing")) as store:
        store.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )
        with pytest.raises(StateError):
            store.transition_session_startup(
                session_id="sess-1",
                expected_revision=0,
                to_state="awaiting_initiative_selection",
            )
        assert store.read_session_startup(session_id="sess-1")["revision"] == 0


def test_session_startup_stale_revision_is_rejected_without_mutation(
    adrian_plugin_modules, kanban_home
):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    RevisionError = (
        adrian_plugin_modules["validation"].SessionStartupRevisionError
    )
    with AdmittedStore(database_path=_db_path(kanban_home, "ss-stale")) as store:
        store.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )
        with pytest.raises(RevisionError):
            store.transition_session_startup(
                session_id="sess-1",
                expected_revision=99,
                to_state="awaiting_initiative_selection",
                selected_project_id="proj-1",
            )
        current = store.read_session_startup(session_id="sess-1")
        assert current["revision"] == 0
        assert current["state"] == "awaiting_project_selection"


def test_session_startup_terminal_states_cannot_reopen(
    adrian_plugin_modules, kanban_home
):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    StateError = adrian_plugin_modules["validation"].SessionStartupStateError
    with AdmittedStore(database_path=_db_path(kanban_home, "ss-terminal")) as store:
        anchored = _advance_to_anchored(store)
        with pytest.raises(StateError):
            store.transition_session_startup(
                session_id="sess-1",
                expected_revision=anchored["revision"],
                to_state="awaiting_project_selection",
            )


def test_replace_anchored_session_startup_preserves_identity_and_release(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "replace-anchor")) as store:
        anchored = _advance_to_anchored(store)
        issued = store.set_release_status(
            session_id="sess-1",
            expected_revision=anchored["revision"],
            release_status="issued",
        )
        observed = store.set_release_status(
            session_id="sess-1",
            expected_revision=issued["revision"],
            release_status="observed",
        )

        replaced = store.replace_anchored_session_startup(
            session_id="sess-1",
            expected_revision=observed["revision"],
            accepted_phase="DEV2",
            accepted_segment_id="S1",
            logical_workspace_id="ws-2",
            writegate_binding_version="2",
            writegate_binding_ref="42",
        )

    assert replaced["revision"] == observed["revision"] + 1
    assert replaced["state"] == "anchored"
    assert replaced["release_status"] == "observed"
    assert replaced["held_opening_prompt"] == "hello"
    assert replaced["selected_project_id"] == "proj-1"
    assert replaced["selected_initiative_id"] == "init-1"
    assert replaced["accepted_phase"] == "DEV2"
    assert replaced["accepted_segment_id"] == "S1"
    assert replaced["logical_workspace_id"] == "ws-2"
    assert replaced["writegate_binding_version"] == "2"
    assert replaced["writegate_binding_ref"] == "42"


def test_replace_anchored_session_startup_rejects_stale_and_nonanchored(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    RevisionError = adrian_plugin_modules["validation"].SessionStartupRevisionError
    StateError = adrian_plugin_modules["validation"].SessionStartupStateError
    kwargs = {
        "accepted_phase": "DEV2",
        "accepted_segment_id": None,
        "logical_workspace_id": "ws-2",
        "writegate_binding_version": "2",
        "writegate_binding_ref": "42",
    }
    with Store(database_path=_db_path(kanban_home, "replace-errors")) as store:
        store.create_or_read_session_startup(
            session_id="new", opening_prompt="hello", protocol_version="v0.29"
        )
        with pytest.raises(StateError, match="not in anchored"):
            store.replace_anchored_session_startup(
                session_id="new", expected_revision=0, **kwargs
            )
        anchored = _advance_to_anchored(store)
        with pytest.raises(RevisionError, match="stale revision"):
            store.replace_anchored_session_startup(
                session_id="sess-1", expected_revision=99, **kwargs
            )
        assert store.read_session_startup(session_id="sess-1") == anchored


def test_replace_anchored_session_startup_rejects_missing_anchor_field(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    StateError = adrian_plugin_modules["validation"].SessionStartupStateError
    with Store(database_path=_db_path(kanban_home, "replace-missing")) as store:
        anchored = _advance_to_anchored(store)
        with pytest.raises(StateError, match="logical_workspace_id"):
            store.replace_anchored_session_startup(
                session_id="sess-1",
                expected_revision=anchored["revision"],
                accepted_phase="DEV2",
                accepted_segment_id=None,
                logical_workspace_id=None,
                writegate_binding_version="2",
                writegate_binding_ref="42",
            )


def test_replace_anchored_session_startup_concurrent_cas_only_one_succeeds(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    RevisionError = adrian_plugin_modules["validation"].SessionStartupRevisionError
    database = _db_path(kanban_home, "replace-concurrent")
    with Store(database_path=database) as seed:
        anchored = _advance_to_anchored(seed)
    barrier = threading.Barrier(2)
    successes = []
    failures = []
    result_lock = threading.Lock()

    def replace() -> None:
        with Store(database_path=database) as peer:
            barrier.wait(timeout=2)
            try:
                result = peer.replace_anchored_session_startup(
                    session_id="sess-1",
                    expected_revision=anchored["revision"],
                    accepted_phase="DEV2",
                    accepted_segment_id="S1",
                    logical_workspace_id="ws-2",
                    writegate_binding_version="2",
                    writegate_binding_ref="42",
                )
                with result_lock:
                    successes.append(result)
            except BaseException as exc:
                with result_lock:
                    failures.append(exc)

    threads = [threading.Thread(target=replace) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], RevisionError)


def test_session_startup_failure_recovery_and_cancel_preserve_prompt(
    adrian_plugin_modules, kanban_home
):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    with AdmittedStore(database_path=_db_path(kanban_home, "ss-recovery")) as store:
        store.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )
        failed = store.transition_session_startup(
            session_id="sess-1",
            expected_revision=0,
            to_state="failed_recoverable",
            failure_detail="missing WriteGate reference",
        )
        recovered = store.transition_session_startup(
            session_id="sess-1",
            expected_revision=failed["revision"],
            to_state="awaiting_initiative_selection",
            selected_project_id="proj-1",
        )
        cancelled = store.cancel_session_startup(
            session_id="sess-1", expected_revision=recovered["revision"]
        )
        assert recovered["failure_detail"] is None
        assert cancelled["state"] == "cancelled"
        assert cancelled["held_opening_prompt"] == "hello"


def test_session_startup_concurrent_cas_only_one_succeeds(
    adrian_plugin_modules, kanban_home
):
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    RevisionError = (
        adrian_plugin_modules["validation"].SessionStartupRevisionError
    )
    db = _db_path(kanban_home, "ss-concurrent")
    with AdmittedStore(database_path=db) as seed:
        seed.create_or_read_session_startup(
            session_id="sess-1", opening_prompt="hello", protocol_version="v0.29"
        )

    barrier = threading.Barrier(2)
    successes = []
    failures = []
    result_lock = threading.Lock()

    def _transition() -> None:
        with AdmittedStore(database_path=db) as peer:
            barrier.wait(timeout=2.0)
            try:
                result = peer.transition_session_startup(
                    session_id="sess-1",
                    expected_revision=0,
                    to_state="awaiting_initiative_selection",
                    selected_project_id="proj-a",
                )
                with result_lock:
                    successes.append(result)
            except BaseException as exc:
                with result_lock:
                    failures.append(exc)

    threads = [threading.Thread(target=_transition) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], RevisionError)
    with AdmittedStore(database_path=db) as check:
        record = check.read_session_startup(session_id="sess-1")
        assert record["revision"] == 1
        assert record["selected_project_id"] == "proj-a"


def test_session_startup_schema_is_additive_and_idempotent(
    adrian_plugin_modules, kanban_home
):
    schema = adrian_plugin_modules["schema"]
    db = _db_path(kanban_home, "ss-schema")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        schema.create_schema(conn)
        schema.create_schema(conn)
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(adrian_kanban_session_startup_records)"
            )
        }
        assert {
            "session_id",
            "protocol_version",
            "state",
            "held_opening_prompt",
            "selected_project_id",
            "selected_initiative_id",
            "accepted_phase",
            "accepted_segment_id",
            "logical_workspace_id",
            "writegate_binding_version",
            "writegate_binding_ref",
            "failure_detail",
            "revision",
            "created_at",
            "updated_at",
            "creation_stage",
            "creation_draft_json",
        } <= columns
    finally:
        conn.close()


def test_session_startup_creation_draft_is_durable_and_compare_and_set(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    database = _db_path(kanban_home, "creation-draft")
    with Store(database_path=database) as store:
        store.create_or_read_session_startup(
            session_id="session-1",
            opening_prompt="held prompt",
            protocol_version="v0.29",
        )
        selected = store.transition_session_startup(
            session_id="session-1",
            expected_revision=0,
            to_state="awaiting_initiative_selection",
            selected_project_id="project-1",
        )
        awaiting_title = store.update_session_startup_creation_draft(
            session_id="session-1",
            expected_revision=selected["revision"],
            creation_stage="awaiting_title",
            creation_draft={},
        )
        awaiting_objective = store.update_session_startup_creation_draft(
            session_id="session-1",
            expected_revision=awaiting_title["revision"],
            creation_stage="awaiting_objective",
            creation_draft={"title": "Context discipline"},
        )
        proposal = {
            "title": "Context discipline",
            "objective": "Keep long sessions coherent.",
            "initiative_id": "i_context_discipline_12345678",
            "body": "canonical body",
            "request_id": "startup-create-request-1",
            "approval_id": "startup-create-approval-1",
        }
        awaiting_approval = store.update_session_startup_creation_draft(
            session_id="session-1",
            expected_revision=awaiting_objective["revision"],
            creation_stage="awaiting_approval",
            creation_draft=proposal,
        )

        with pytest.raises(
            adrian_plugin_modules["validation"].SessionStartupRevisionError
        ):
            store.update_session_startup_creation_draft(
                session_id="session-1",
                expected_revision=awaiting_objective["revision"],
                creation_stage=None,
                creation_draft=None,
            )

        cleared = store.update_session_startup_creation_draft(
            session_id="session-1",
            expected_revision=awaiting_approval["revision"],
            creation_stage=None,
            creation_draft=None,
        )

    assert awaiting_title["state"] == "awaiting_initiative_selection"
    assert awaiting_title["held_opening_prompt"] == "held prompt"
    assert awaiting_objective["creation_stage"] == "awaiting_objective"
    assert json.loads(awaiting_objective["creation_draft_json"]) == {
        "title": "Context discipline"
    }
    assert awaiting_approval["creation_stage"] == "awaiting_approval"
    assert json.loads(awaiting_approval["creation_draft_json"]) == proposal
    assert cleared["creation_stage"] is None
    assert cleared["creation_draft_json"] is None
    assert cleared["selected_project_id"] == "project-1"
    assert cleared["held_opening_prompt"] == "held prompt"


@pytest.mark.parametrize(
    ("stage", "draft"),
    [
        ("unknown", {}),
        ("awaiting_title", {"title": "unexpected"}),
        ("awaiting_objective", {}),
        ("awaiting_approval", {"title": "incomplete"}),
        (None, {}),
    ],
)
def test_session_startup_creation_draft_rejects_invalid_shape_without_mutation(
    adrian_plugin_modules, kanban_home, stage, draft
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    database = _db_path(kanban_home, f"invalid-creation-draft-{stage}")
    with Store(database_path=database) as store:
        store.create_or_read_session_startup(
            session_id="session-1",
            opening_prompt="held prompt",
            protocol_version="v0.29",
        )
        selected = store.transition_session_startup(
            session_id="session-1",
            expected_revision=0,
            to_state="awaiting_initiative_selection",
            selected_project_id="project-1",
        )
        with pytest.raises(
            adrian_plugin_modules["validation"].SessionStartupStateError
        ):
            store.update_session_startup_creation_draft(
                session_id="session-1",
                expected_revision=selected["revision"],
                creation_stage=stage,
                creation_draft=draft,
            )
        unchanged = store.read_session_startup(session_id="session-1")

    assert unchanged["revision"] == selected["revision"]
    assert unchanged["creation_stage"] is None
    assert unchanged["creation_draft_json"] is None


def test_creation_coordinator_derives_safe_deterministic_canonical_record(
    adrian_plugin_modules, kanban_home
):
    creation = adrian_plugin_modules["session_startup_creation"]
    database = _db_path(kanban_home, "creation-derive")
    boundary = type("Boundary", (), {"submit": lambda *_args, **_kwargs: None})()
    coordinator = creation.SessionStartupInitiativeCreationCoordinator(
        str(database), boundary, now_provider=lambda: 1000
    )
    project = {
        "id": "project-1",
        "name": "Orchestrator",
        "board_slug": "orchestrator",
        "primary_path": "/srv/orchestrator",
    }
    first = coordinator.derive(
        project,
        "  Context Discipline  ",
        "  Keep long sessions coherent.  ",
        {"session_id": "session-1"},
    )
    second = coordinator.derive(
        project,
        "Context Discipline",
        "Keep long sessions coherent.",
        {"session_id": "different-session"},
    )

    assert first["initiative_id"] == second["initiative_id"]
    assert first["body"] == second["body"]
    assert first["initiative_id"].startswith("i_context_discipline_")
    assert first["request_id"] != second["request_id"]
    assert first["approval_id"] != second["approval_id"]
    required_headings = [
        "## Initiative",
        "### Objective",
        "### Board and workspace context",
        "### Authoritative artifacts",
        "### Cleared outcomes",
        "### Open items",
        "### Related task cards",
        "### Constraints",
        "### Cold-session continuation",
    ]
    positions = [first["body"].index(heading) for heading in required_headings]
    assert positions == sorted(positions)
    assert first["body"].startswith("# [[INITIATIVE_LEDGER]]\n")
    assert "session-1" not in first["body"]
    assert "1000" not in first["body"]


def test_creation_coordinator_approves_and_uses_existing_command_boundary(
    adrian_plugin_modules, kanban_home, monkeypatch
):
    creation = adrian_plugin_modules["session_startup_creation"]
    database = _db_path(kanban_home, "creation-execute")
    database.parent.mkdir(parents=True, exist_ok=True)
    submissions = []

    class Boundary:
        def submit(self, action, **fields):
            submissions.append((action, fields))
            return {
                "result": "ACCEPTED",
                "value": {
                    "initiative_id": fields["target"],
                    "phase": "D1",
                    "record_version": 0,
                },
            }

    class Authorizer:
        def _canonical_for_writegate(self):
            return "fake-authorizer"

    class Host:
        def __init__(self, authorizer):
            assert authorizer._canonical_for_writegate() == "fake-authorizer"

        def prepare(self, conn, preparation, *, now):
            conn.execute(
                "INSERT INTO write_gate_kanban_approvals "
                "(approval_id, approval_type, state, request_id, operation, "
                "initiative_id, proposed_creation_id, expected_version, "
                "canonical_digest, canonicalization_version, authorizer_evidence, "
                "requires_distinct_authorizer, session_id, prepared_at, "
                "approved_at, expires_at, approval_evidence, cancellation_evidence, "
                "consumed_mutation_id, consumed_idempotency_ref) VALUES "
                "(?, 'kanban_initiative_mutation', 'prepared', ?, ?, NULL, ?, 0, "
                "?, 1, 'fake-authorizer', 0, ?, ?, NULL, ?, NULL, NULL, NULL, NULL)",
                (
                    preparation.approval_id,
                    preparation.request_id,
                    preparation.operation,
                    preparation.proposed_creation_id,
                    preparation.canonical_digest,
                    preparation.session_id,
                    now,
                    preparation.expires_at,
                ),
            )

        def approve(self, conn, approval_id, evidence, *, now):
            assert json.loads(evidence)["decision"] == "once"
            conn.execute(
                "UPDATE write_gate_kanban_approvals SET state='approved', "
                "approved_at=?, approval_evidence=? WHERE approval_id=?",
                (now, evidence, approval_id),
            )

        def cancel(self, conn, approval_id, evidence, *, now):
            conn.execute(
                "UPDATE write_gate_kanban_approvals SET state='cancelled', "
                "cancellation_evidence=? WHERE approval_id=?",
                (evidence, approval_id),
            )

    monkeypatch.setattr(creation, "KanbanInitiativeApprovalHost", Host)
    monkeypatch.setattr(
        creation, "mint_current_tailscale_authorizer", lambda **_kwargs: Authorizer()
    )
    approval_presentations = []
    monkeypatch.setattr(
        creation,
        "request_write_gate_approval",
        lambda **kwargs: approval_presentations.append(kwargs)
        or {
            "approved": True,
            "decision": "once",
            "decision_at": "2026-09-12T12:00:00Z",
            "approval_reference": kwargs["request_id"],
        },
    )
    coordinator = creation.SessionStartupInitiativeCreationCoordinator(
        str(database), Boundary(), now_provider=lambda: 1000
    )
    project = {
        "id": "project-1",
        "name": "Orchestrator",
        "board_slug": "orchestrator",
        "primary_path": "/srv/orchestrator",
    }
    record = {"session_id": "session-1"}
    draft = coordinator.derive(project, "Context discipline", "Stay coherent", record)
    outcome = coordinator.execute(project, draft, record)

    assert outcome["status"] == "created"
    assert outcome["initiative"]["initiative_id"] == draft["initiative_id"]
    assert len(approval_presentations) == 1
    visible_payload = json.loads(approval_presentations[0]["command"])
    assert visible_payload == {
        "initiative_id": draft["initiative_id"],
        "title": draft["title"],
        "body": draft["body"],
        "board": "orchestrator",
    }
    assert len(submissions) == 1
    action, fields = submissions[0]
    assert action == "kanban_create_initiative"
    assert fields["payload"] == visible_payload | {"approval_id": draft["approval_id"]}
    assert fields["actor_profile"] == "default"
    assert fields["execution_context"] == "session-startup"
    assert fields["expected_version"] == 0


def test_creation_coordinator_denial_never_calls_command_boundary(
    adrian_plugin_modules, kanban_home, monkeypatch
):
    creation = adrian_plugin_modules["session_startup_creation"]
    database = _db_path(kanban_home, "creation-denied")
    database.parent.mkdir(parents=True, exist_ok=True)

    class Boundary:
        def submit(self, *_args, **_kwargs):
            raise AssertionError("denied creation reached command boundary")

    class Authorizer:
        def _canonical_for_writegate(self):
            return "fake-authorizer"

    class Host:
        def __init__(self, _authorizer):
            pass

        def prepare(self, conn, p, *, now):
            conn.execute(
                "INSERT INTO write_gate_kanban_approvals "
                "(approval_id, approval_type, state, request_id, operation, "
                "initiative_id, proposed_creation_id, expected_version, "
                "canonical_digest, canonicalization_version, authorizer_evidence, "
                "requires_distinct_authorizer, session_id, prepared_at, expires_at) "
                "VALUES (?, 'kanban_initiative_mutation', 'prepared', ?, ?, NULL, "
                "?, 0, ?, 1, 'fake-authorizer', 0, ?, ?, ?)",
                (p.approval_id, p.request_id, p.operation, p.proposed_creation_id,
                 p.canonical_digest, p.session_id, now, p.expires_at),
            )

        def cancel(self, conn, approval_id, evidence, *, now):
            conn.execute(
                "UPDATE write_gate_kanban_approvals SET state='cancelled', "
                "cancellation_evidence=? WHERE approval_id=?",
                (evidence, approval_id),
            )

    monkeypatch.setattr(creation, "KanbanInitiativeApprovalHost", Host)
    monkeypatch.setattr(
        creation, "mint_current_tailscale_authorizer", lambda **_kwargs: Authorizer()
    )
    monkeypatch.setattr(
        creation,
        "request_write_gate_approval",
        lambda **_kwargs: {"approved": False, "decision": "deny"},
    )
    coordinator = creation.SessionStartupInitiativeCreationCoordinator(
        str(database), Boundary(), now_provider=lambda: 1000
    )
    project = {
        "id": "project-1",
        "name": "Orchestrator",
        "board_slug": "orchestrator",
        "primary_path": "/srv/orchestrator",
    }
    record = {"session_id": "session-1"}
    draft = coordinator.derive(project, "Denied", "Do not create", record)

    assert coordinator.execute(project, draft, record) == {
        "status": "cancelled",
        "message": "Initiative creation was not approved.",
    }


def test_creation_coordinator_end_to_end_consumes_real_approval_and_handler(
    adrian_plugin_modules, kanban_home, monkeypatch
):
    creation = adrian_plugin_modules["session_startup_creation"]
    commands = adrian_plugin_modules["commands"]
    provider_module = adrian_plugin_modules["provider"]
    database = _db_path(kanban_home, "creation-real-boundary")
    database.parent.mkdir(parents=True, exist_ok=True)
    with kb.connect_closing(database):
        pass
    (kanban_home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        "    - adrian-kanban\n"
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        f"  database_path: {database.as_posix()}\n",
        encoding="utf-8",
    )
    provider = provider_module.AdrianKanbanAuthorityProvider(str(database))
    provider_module.register_provider(provider)
    monkeypatch.setattr(
        kb, "resolve_selected_authority", lambda: kb.AUTHORITY_ADRIAN_KANBAN
    )
    handler_errors = []

    def traced_create_handler(context):
        try:
            return commands._handle_create_initiative(context)
        except Exception as exc:
            handler_errors.append(exc)
            raise

    boundary = commands._CommandBoundary(
        database_path=str(database),
        provider=provider,
        handlers={
            "kanban_create_initiative": traced_create_handler,
        },
    )
    now = int(time.time())
    connection = trusted._record_authenticated_tailscale_connection(
        "gateway/tailscale",
        "connection-1",
        "adrian@tailnet",
        "transport-request",
        now - 100,
    )
    evidence = trusted.TrustedAuthorizerEvidence._from_authenticated_connection(
        connection,
        issued_at=now,
        ttl_seconds=300,
    )
    monkeypatch.setattr(
        creation,
        "mint_current_tailscale_authorizer",
        lambda **_kwargs: evidence,
    )
    monkeypatch.setattr(
        creation,
        "request_write_gate_approval",
        lambda **kwargs: {
            "approved": True,
            "decision": "once",
            "decision_at": "2026-09-12T12:00:00Z",
            "approval_reference": kwargs["request_id"],
        },
    )
    coordinator = creation.SessionStartupInitiativeCreationCoordinator(
        str(database), boundary, now_provider=lambda: now
    )
    project = {
        "id": "project-1",
        "name": "Orchestrator",
        "board_slug": "orchestrator",
        "primary_path": "/srv/orchestrator",
    }
    record = {"session_id": "session-1"}
    draft = coordinator.derive(project, "Real creation", "Exercise the handler", record)
    try:
        outcome = coordinator.execute(project, draft, record)
    except ValueError as exc:
        if handler_errors:
            raise AssertionError(f"real create handler failed: {handler_errors!r}") from exc
        raise

    assert outcome["status"] == "created"
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        card = conn.execute(
            "SELECT initiative_id, title, board_slug, record_version, closed_at "
            "FROM adrian_kanban_cards WHERE initiative_id = ?",
            (draft["initiative_id"],),
        ).fetchone()
        approval = conn.execute(
            "SELECT state, consumed_idempotency_ref "
            "FROM write_gate_kanban_approvals WHERE approval_id = ?",
            (draft["approval_id"],),
        ).fetchone()
    assert dict(card) == {
        "initiative_id": draft["initiative_id"],
        "title": "Real creation",
        "board_slug": "orchestrator",
        "record_version": 0,
        "closed_at": None,
    }
    assert approval["state"] == "consumed"
    assert approval["consumed_idempotency_ref"] == f"{draft['request_id']}:execute"


def _startup_controller(
    modules,
    store,
    *,
    projects=None,
    initiatives=None,
    resolver=None,
    revalidator=None,
    replacer=None,
    creation_coordinator=None,
):
    if projects is None:
        projects = [
            {
                "id": "project-1",
                "name": "Orchestrator",
                "board_slug": "orchestrator",
                "primary_path": "/repos/orchestrator",
                "archived": False,
            }
        ]
    if initiatives is None:
        initiatives = [
            {
                "initiative_id": "initiative-1",
                "title": "Session Startup",
                "current_phase": "DEV2",
                "current_segment_id": "S1",
                "closed_at": None,
            }
        ]
    anchor = {
        "accepted_phase": "DEV2",
        "accepted_segment_id": "S1",
        "logical_workspace_id": "workspace-1",
        "writegate_binding_version": "1",
        "writegate_binding_ref": "binding-1",
    }
    if creation_coordinator is None:
        creation_coordinator = type(
            "CreationCoordinator",
            (),
            {
                "derive": lambda self, project, title, objective, record: {
                    "title": title,
                    "objective": objective,
                    "initiative_id": "i_new_12345678",
                    "body": "canonical body",
                    "request_id": "startup-create-request-1",
                    "approval_id": "startup-create-approval-1",
                },
                "execute": lambda self, project, draft, record: {
                    "status": "created",
                    "initiative": {
                        "initiative_id": draft["initiative_id"],
                        "title": draft["title"],
                        "current_phase": "D1",
                        "current_segment_id": None,
                        "closed_at": None,
                    },
                },
                "cancel": lambda self, project, draft, record: None,
            },
        )()
    return modules["session_startup"].SessionStartupController(
        store=store,
        project_loader=lambda: projects,
        initiative_loader=lambda _board: initiatives,
        anchor_resolver=resolver or (lambda _project, _initiative, _record: anchor),
        anchor_revalidator=revalidator
        or (
            lambda _project, _initiative, _record: {
                "classification": "unchanged",
                "reasons": [],
            }
        ),
        anchor_replacer=replacer
        or (lambda _project, _initiative, _record: anchor),
        initiative_creation_coordinator=creation_coordinator,
    )


def test_controller_holds_exact_opening_before_any_selection(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "controller-opening")) as store:
        controller = _startup_controller(adrian_plugin_modules, store)
        result = controller.handle(
            "session-1",
            "So tell me what you should be working on",
            conversation_history=[{"role": "system", "content": "system"}],
            first_turn=False,
        )
        record = store.read_session_startup(session_id="session-1")

    assert result["action"] == "respond"
    assert "1. Orchestrator" in result["response"]
    assert record["held_opening_prompt"] == "So tell me what you should be working on"
    assert record["selected_project_id"] is None


@pytest.mark.parametrize("project_choice", ["1", "project-1", "orchestrator"])
@pytest.mark.parametrize("initiative_choice", ["1", "initiative-1", "session startup"])
def test_controller_selects_by_number_id_or_unique_name_and_releases_exact_prompt(
    adrian_plugin_modules, kanban_home, project_choice, initiative_choice
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    database = _db_path(
        kanban_home, f"controller-{project_choice}-{initiative_choice}"
    )
    with Store(database_path=database) as store:
        controller = _startup_controller(adrian_plugin_modules, store)
        controller.handle("session-1", "original prompt")
        project_result = controller.handle("session-1", project_choice)
        release = controller.handle("session-1", initiative_choice)
        record = store.read_session_startup(session_id="session-1")

    assert project_result["action"] == "respond"
    assert "Session Startup [DEV2 / S1]" in project_result["response"]
    assert release["action"] == "rewrite"
    assert release["persist_message"] == "original prompt"
    assert release["model_message"].endswith("\n\noriginal prompt")
    assert "Segment: S1" in release["model_message"]
    assert "WriteGate Version: 1" in release["model_message"]
    assert record["state"] == "anchored"
    assert record["release_status"] == "issued"


def test_controller_reissues_then_observes_exact_persisted_prompt(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "controller-recovery")) as store:
        controller = _startup_controller(adrian_plugin_modules, store)
        controller.handle("session-1", "original prompt")
        controller.handle("session-1", "1")
        first_release = controller.handle("session-1", "1")
        reissued = controller.handle(
            "session-1", "retry", conversation_history=[]
        )
        observed = controller.handle(
            "session-1",
            "next real turn",
            conversation_history=[{"role": "user", "content": "original prompt"}],
        )
        record = store.read_session_startup(session_id="session-1")

    assert reissued == first_release
    assert observed["action"] == "rewrite"
    assert observed["persist_message"] == "next real turn"
    assert observed["model_message"].endswith("\nnext real turn")
    assert record["release_status"] == "observed"


def test_controller_focused_revalidation_replaces_durable_anchor(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    calls = []

    def revalidate(project, initiative, record):
        calls.append(("revalidate", project["id"], initiative["initiative_id"]))
        return {
            "classification": "focused_revalidation_required",
            "reasons": ["phase_changed"],
        }

    def replace(project, initiative, record):
        calls.append(("replace", record["revision"]))
        return {
            "accepted_phase": "DEV3",
            "accepted_segment_id": "S2",
            "logical_workspace_id": "workspace-2",
            "writegate_binding_version": "2",
            "writegate_binding_ref": "42",
        }

    with Store(database_path=_db_path(kanban_home, "focused-controller")) as store:
        controller = _startup_controller(
            adrian_plugin_modules,
            store,
            revalidator=revalidate,
            replacer=replace,
        )
        controller.handle("session-1", "original prompt")
        controller.handle("session-1", "1")
        controller.handle("session-1", "1")
        result = controller.handle(
            "session-1",
            "continue the work",
            conversation_history=[{"role": "user", "content": "original prompt"}],
        )
        record = store.read_session_startup(session_id="session-1")

    assert calls[0][0] == "revalidate"
    assert calls[1][0] == "replace"
    assert record["accepted_phase"] == "DEV3"
    assert record["accepted_segment_id"] == "S2"
    assert record["logical_workspace_id"] == "workspace-2"
    assert record["writegate_binding_version"] == "2"
    assert record["writegate_binding_ref"] == "42"
    assert record["release_status"] == "observed"
    assert result["action"] == "rewrite"
    assert result["persist_message"] == "continue the work"
    assert "Phase: DEV3" in result["model_message"]
    assert result["model_message"].endswith("\ncontinue the work")


@pytest.mark.parametrize(
    "validation",
    [
        "not-a-dict",
        {"classification": "unknown", "reasons": []},
        {"classification": "unchanged", "reasons": "not-a-list"},
    ],
)
def test_controller_malformed_revalidation_fails_closed(
    adrian_plugin_modules, kanban_home, validation
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "malformed-revalidation")) as store:
        controller = _startup_controller(
            adrian_plugin_modules,
            store,
            revalidator=lambda *_args: validation,
        )
        controller.handle("session-1", "original prompt")
        controller.handle("session-1", "1")
        controller.handle("session-1", "1")
        result = controller.handle(
            "session-1",
            "next",
            conversation_history=[{"role": "user", "content": "original prompt"}],
        )

    assert result["action"] == "respond"
    assert result["response"].startswith("[Session Startup]")


def test_controller_missing_or_ambiguous_anchored_authority_fails_closed(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    database = _db_path(kanban_home, "missing-authority")
    with Store(database_path=database) as store:
        controller = _startup_controller(adrian_plugin_modules, store)
        controller.handle("session-1", "original prompt")
        controller.handle("session-1", "1")
        controller.handle("session-1", "1")

        controller._project_loader = lambda: []
        missing = controller.handle("session-1", "next", conversation_history=[])

        duplicate = {
            "id": "project-1",
            "name": "Duplicate",
            "board_slug": "orchestrator",
            "primary_path": "/repos/orchestrator",
            "archived": False,
        }
        controller._project_loader = lambda: [
            {
                "id": "project-1",
                "name": "Orchestrator",
                "board_slug": "orchestrator",
                "primary_path": "/repos/orchestrator",
                "archived": False,
            },
            duplicate,
        ]
        ambiguous = controller.handle("session-1", "next", conversation_history=[])

    assert "Selected project not found" in missing["response"]
    assert ambiguous["action"] == "respond"
    assert "ambiguous" in ambiguous["response"].lower()


def test_controller_rejects_ambiguous_and_invalid_project_with_full_menu(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    projects = [
        {
            "id": "p1",
            "name": "Same",
            "board_slug": "one",
            "primary_path": "/one",
        },
        {
            "id": "p2",
            "name": "Same",
            "board_slug": "two",
            "primary_path": "/two",
        },
    ]
    with Store(database_path=_db_path(kanban_home, "controller-invalid")) as store:
        controller = _startup_controller(
            adrian_plugin_modules, store, projects=projects
        )
        controller.handle("session-1", "opening")
        ambiguous = controller.handle("session-1", "same")
        invalid = controller.handle("session-1", "99")

    assert ambiguous["response"].startswith("Ambiguous project selection.")
    assert "1. Same (p1)" in ambiguous["response"]
    assert "2. Same (p2)" in ambiguous["response"]
    assert invalid["response"].startswith("Invalid project selection.")
    assert "2. Same (p2)" in invalid["response"]


def test_controller_refuses_partial_project_list(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    projects = [
        {
            "id": "valid",
            "name": "Visible only if list were partial",
            "board_slug": "valid",
            "primary_path": "/valid",
        },
        {"id": "broken", "name": "Broken", "board_slug": "broken"},
    ]
    with Store(database_path=_db_path(kanban_home, "controller-partial")) as store:
        controller = _startup_controller(
            adrian_plugin_modules, store, projects=projects
        )
        result = controller.handle("session-1", "opening")

    assert result["action"] == "respond"
    assert result["response"].startswith("[Session Startup] Failed to initialize")
    assert "Visible only if list were partial" not in result["response"]


def test_controller_empty_active_list_keeps_numbered_read_only_and_create_routes(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    resolver_calls = []
    with Store(database_path=_db_path(kanban_home, "controller-actions")) as store:
        controller = _startup_controller(
            adrian_plugin_modules,
            store,
            initiatives=[
                {
                    "initiative_id": "closed",
                    "title": "Closed Initiative",
                    "current_phase": "PC1",
                    "current_segment_id": None,
                    "closed_at": 1,
                }
            ],
            resolver=lambda *_args: resolver_calls.append(True),
        )
        controller.handle("session-1", "opening")
        menu = controller.handle("session-1", "1")
        browse = controller.handle("session-1", "1")
        create = controller.handle("session-1", "2")
        record = store.read_session_startup(session_id="session-1")

    assert "Closed Initiative" not in menu["response"]
    assert "1. Browse closed initiatives" in menu["response"]
    assert "2. Create a new initiative" in menu["response"]
    assert "read-only" in browse["response"]
    assert "Closed Initiative [closed]" in browse["response"]
    assert "Closed: 1" in browse["response"]
    assert "Active initiatives for Orchestrator" in browse["response"]
    assert "initiative title" in create["response"].lower()
    assert record["state"] == "awaiting_initiative_selection"
    assert record["creation_stage"] == "awaiting_title"
    assert record["revision"] == 2
    assert resolver_calls == []


def test_controller_new_initiative_collects_only_title_and_objective_then_anchors(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    calls = []

    class Coordinator:
        def derive(self, project, title, objective, record):
            calls.append(("derive", project["id"], title, objective, record["revision"]))
            return {
                "title": title,
                "objective": objective,
                "initiative_id": "i_context_discipline_12345678",
                "body": "canonical body",
                "request_id": "startup-create-request-1",
                "approval_id": "startup-create-approval-1",
            }

        def execute(self, project, draft, record):
            calls.append(("execute", project["id"], dict(draft), record["revision"]))
            return {
                "status": "created",
                "initiative": {
                    "initiative_id": draft["initiative_id"],
                    "title": draft["title"],
                    "current_phase": "D1",
                    "current_segment_id": None,
                    "closed_at": None,
                },
            }

        def cancel(self, project, draft, record):
            calls.append(("cancel", project["id"], dict(draft), record["revision"]))

    with Store(database_path=_db_path(kanban_home, "controller-create")) as store:
        controller = _startup_controller(
            adrian_plugin_modules,
            store,
            initiatives=[],
            creation_coordinator=Coordinator(),
        )
        controller.handle("session-1", "original held prompt")
        controller.handle("session-1", "1")
        title_prompt = controller.handle("session-1", "2")
        objective_prompt = controller.handle("session-1", "Context discipline")
        release = controller.handle("session-1", "Keep long sessions coherent.")
        record = store.read_session_startup(session_id="session-1")

    assert "initiative title" in title_prompt["response"].lower()
    assert "initiative objective" in objective_prompt["response"].lower()
    assert [call[0] for call in calls] == ["derive", "execute"]
    assert release["action"] == "rewrite"
    assert release["persist_message"] == "original held prompt"
    assert "Initiative: i_context_discipline_12345678" in release["model_message"]
    assert record["state"] == "anchored"
    assert record["selected_initiative_id"] == "i_context_discipline_12345678"
    assert record["creation_stage"] is None
    assert record["creation_draft_json"] is None


@pytest.mark.parametrize("cancel_word", ["cancel", "BACK"])
def test_controller_new_initiative_cancel_returns_to_active_menu_and_keeps_prompt(
    adrian_plugin_modules, kanban_home, cancel_word
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, f"create-cancel-{cancel_word}")) as store:
        controller = _startup_controller(adrian_plugin_modules, store, initiatives=[])
        controller.handle("session-1", "original held prompt")
        controller.handle("session-1", "1")
        controller.handle("session-1", "2")
        result = controller.handle("session-1", cancel_word)
        record = store.read_session_startup(session_id="session-1")

    assert "Active initiatives for Orchestrator" in result["response"]
    assert "Create a new initiative" in result["response"]
    assert record["held_opening_prompt"] == "original held prompt"
    assert record["creation_stage"] is None
    assert record["creation_draft_json"] is None


def test_controller_creation_execution_failure_retains_exact_proposal_for_retry(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    execute_calls = []

    class Coordinator:
        def derive(self, _project, title, objective, _record):
            return {
                "title": title,
                "objective": objective,
                "initiative_id": "i_retry_12345678",
                "body": "canonical body",
                "request_id": "request-stable",
                "approval_id": "approval-stable",
            }

        def execute(self, _project, draft, _record):
            execute_calls.append(dict(draft))
            if len(execute_calls) == 1:
                raise RuntimeError("approval transport unavailable")
            return {
                "status": "created",
                "initiative": {
                    "initiative_id": draft["initiative_id"],
                    "title": draft["title"],
                    "current_phase": "D1",
                    "current_segment_id": None,
                    "closed_at": None,
                },
            }

        def cancel(self, *_args):
            raise AssertionError("cancel was not requested")

    with Store(database_path=_db_path(kanban_home, "create-retry")) as store:
        controller = _startup_controller(
            adrian_plugin_modules,
            store,
            initiatives=[],
            creation_coordinator=Coordinator(),
        )
        controller.handle("session-1", "original held prompt")
        controller.handle("session-1", "1")
        controller.handle("session-1", "2")
        controller.handle("session-1", "Retry initiative")
        failed = controller.handle("session-1", "Deliver a retry-safe flow")
        pending = store.read_session_startup(session_id="session-1")
        released = controller.handle("session-1", "retry")

    assert "approval transport unavailable" in failed["response"]
    assert pending["creation_stage"] == "awaiting_approval"
    assert json.loads(pending["creation_draft_json"])["request_id"] == "request-stable"
    assert execute_calls[0] == execute_calls[1]
    assert released["action"] == "rewrite"


def test_controller_browse_closed_phrase_and_empty_list_are_read_only(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    active = {
        "initiative_id": "active",
        "title": "Active Initiative",
        "current_phase": "D1",
        "current_segment_id": None,
        "closed_at": None,
    }
    with Store(database_path=_db_path(kanban_home, "closed-empty")) as store:
        controller = _startup_controller(
            adrian_plugin_modules, store, initiatives=[active]
        )
        controller.handle("session-1", "opening")
        controller.handle("session-1", "1")
        browse = controller.handle(
            "session-1", "BrOwSe ClOsEd InItIaTiVeS"
        )
        record = store.read_session_startup(session_id="session-1")

    assert browse["action"] == "respond"
    assert "Closed initiatives for Orchestrator:\nNone." in browse["response"]
    assert "permanently read-only" in browse["response"]
    assert record["state"] == "awaiting_initiative_selection"


def test_controller_malformed_closed_projection_fails_closed(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    malformed = {
        "initiative_id": "closed",
        "title": "Closed Initiative",
        "current_phase": 7,
        "closed_at": 1,
    }
    with Store(database_path=_db_path(kanban_home, "closed-malformed")) as store:
        controller = _startup_controller(
            adrian_plugin_modules, store, initiatives=[malformed]
        )
        controller.handle("session-1", "opening")
        controller.handle("session-1", "1")
        result = controller.handle("session-1", "1")
        record = store.read_session_startup(session_id="session-1")

    assert result["action"] == "respond"
    assert result["response"].startswith("[Session Startup]")
    assert "current_phase" in result["response"]
    assert record["state"] == "awaiting_initiative_selection"


def test_controller_collaborator_failure_is_recoverable_and_retry_resumes(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    calls = {"count": 0}

    def resolver(_project, _initiative, _record):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("workspace unavailable")
        return {
            "accepted_phase": "PC1",
            "accepted_segment_id": None,
            "logical_workspace_id": "workspace-1",
            "writegate_binding_version": "1",
            "writegate_binding_ref": "binding-1",
        }

    with Store(database_path=_db_path(kanban_home, "controller-retry")) as store:
        controller = _startup_controller(
            adrian_plugin_modules, store, resolver=resolver
        )
        controller.handle("session-1", "opening")
        controller.handle("session-1", "1")
        failed = controller.handle("session-1", "1")
        failed_record = store.read_session_startup(session_id="session-1")
        retried = controller.handle("session-1", "retry")
        final_record = store.read_session_startup(session_id="session-1")

    assert "workspace unavailable" in failed["response"]
    assert failed_record["state"] == "failed_recoverable"
    assert retried["action"] == "rewrite"
    assert final_record["state"] == "anchored"


def test_runtime_project_loader_maps_first_class_project_fields(
    adrian_plugin_modules, monkeypatch
):
    runtime = adrian_plugin_modules["session_startup_runtime"]
    projects = [
        type(
            "Project",
            (),
            {
                "id": "project-1",
                "name": "Orchestrator",
                "board_slug": "orchestrator",
                "primary_path": "/srv/orchestrator",
                "archived": False,
            },
        )(),
        type(
            "Project",
            (),
            {
                "id": "project-2",
                "name": "Archived",
                "board_slug": "archived",
                "primary_path": "/srv/archived",
                "archived": True,
            },
        )(),
    ]

    class ClosingConnection:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        runtime.projects_db, "connect_closing", lambda: ClosingConnection()
    )
    monkeypatch.setattr(
        runtime.projects_db,
        "list_projects",
        lambda _conn, include_archived: projects if include_archived else [],
    )

    assert runtime.load_projects() == [
        {
            "id": "project-1",
            "name": "Orchestrator",
            "board_slug": "orchestrator",
            "primary_path": "/srv/orchestrator",
            "archived": False,
        },
        {
            "id": "project-2",
            "name": "Archived",
            "board_slug": "archived",
            "primary_path": "/srv/archived",
            "archived": True,
        },
    ]


def test_runtime_initiative_loader_scopes_board_and_preserves_lifecycle_fields(
    adrian_plugin_modules, kanban_home
):
    runtime = adrian_plugin_modules["session_startup_runtime"]
    Store = adrian_plugin_modules["store"].AdmittedStore
    database = _db_path(kanban_home, "runtime-initiatives")
    with Store(database_path=database) as store:
        store.initiative_card(initiative_id="active", title="Active")
        store.record_transition(
            initiative_id="active", transition_id=1, to_phase="DEV3", to_segment_id="S2"
        )
        store.initiative_card(initiative_id="closed", title="Closed")
        store.record_transition(
            initiative_id="closed", transition_id=1, to_phase="PC1"
        )
    conn = sqlite3.connect(str(database))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id TEXT PRIMARY KEY, status TEXT, assignee TEXT)"
        )
        conn.execute(
            "UPDATE adrian_kanban_cards SET board_slug = 'chosen' "
            "WHERE initiative_id IN ('active', 'closed')"
        )
        conn.execute(
            "UPDATE adrian_kanban_cards SET closed_at = 123 "
            "WHERE initiative_id = 'closed'"
        )
        conn.commit()
    finally:
        conn.close()

    rows = runtime.build_initiative_loader(str(database))("chosen")
    by_id = {row["initiative_id"]: row for row in rows}

    assert set(by_id) == {"active", "closed"}
    assert by_id["active"]["current_phase"] == "DEV3"
    assert by_id["active"]["current_segment_id"] == "S2"
    assert by_id["closed"]["closed_at"] == 123


@pytest.mark.parametrize("database", ["relative.db", "", "   "])
def test_runtime_initiative_loader_rejects_invalid_database_path(
    adrian_plugin_modules, database
):
    runtime = adrian_plugin_modules["session_startup_runtime"]
    with pytest.raises(ValueError):
        runtime.build_initiative_loader(database)


def test_runtime_initiative_loader_closes_after_projection_failure(
    adrian_plugin_modules, kanban_home, monkeypatch
):
    runtime = adrian_plugin_modules["session_startup_runtime"]
    database = _db_path(kanban_home, "runtime-close")
    database.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(database)).close()
    real_connect = runtime.sqlite3.connect
    opened = []

    def tracked_connect(path):
        connection = real_connect(path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(runtime.sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(
        runtime,
        "list_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    loader = runtime.build_initiative_loader(str(database))
    with pytest.raises(RuntimeError, match="board 'chosen'"):
        loader("chosen")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_runtime_hook_excludes_nested_worker_and_every_nondefault_profile_without_store(
    adrian_plugin_modules, monkeypatch, tmp_path
):
    runtime = adrian_plugin_modules["session_startup_runtime"]
    opened = []

    class ForbiddenStore:
        def __init__(self, *args, **kwargs):
            opened.append((args, kwargs))
            raise AssertionError("excluded callback opened the store")

    monkeypatch.setattr(runtime, "AdmittedStore", ForbiddenStore)
    monkeypatch.setattr(
        runtime,
        "SessionStartupAnchorResolver",
        lambda **_kwargs: type("Resolver", (), {"resolve": lambda *args: {}})(),
    )
    hook = runtime.build_session_startup_hook(
        str((tmp_path / "tracker.db").resolve()),
        object(),
        type("Boundary", (), {"submit": lambda *_args, **_kwargs: {}})(),
    )

    assert hook(parent_session_id="parent") == {"action": "allow"}
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    assert hook() == {"action": "allow"}
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    for profile in (
        "builder-tester",
        "independent-reviewer",
        "test-authority-reviewer",
        "custom-specialist",
    ):
        monkeypatch.setattr(
            runtime.profiles,
            "get_active_profile_name",
            lambda selected=profile: selected,
        )
        assert hook() == {"action": "allow"}
    assert opened == []


def test_runtime_hook_uses_fresh_store_and_forwards_top_level_turns(
    adrian_plugin_modules, monkeypatch, tmp_path
):
    runtime = adrian_plugin_modules["session_startup_runtime"]
    stores = []
    calls = []

    class FakeStore:
        def __init__(self, database_path):
            self.database_path = database_path
            self.closed = False
            stores.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

    class FakeController:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def handle(self, **kwargs):
            calls.append(("handle", kwargs))
            return {"action": "respond", "response": "choose"}

    resolver = type(
        "Resolver",
        (),
        {
            "resolve": lambda *args: {},
            "revalidate": lambda *args: {
                "classification": "unchanged",
                "reasons": [],
            },
            "replace": lambda *args: {},
        },
    )()
    monkeypatch.setattr(runtime, "AdmittedStore", FakeStore)
    monkeypatch.setattr(runtime, "SessionStartupController", FakeController)
    monkeypatch.setattr(
        runtime, "SessionStartupAnchorResolver", lambda **_kwargs: resolver
    )
    monkeypatch.setattr(
        runtime.profiles, "get_active_profile_name", lambda: "default"
    )
    database = str((tmp_path / "tracker.db").resolve())
    hook = runtime.build_session_startup_hook(
        database,
        object(),
        type("Boundary", (), {"submit": lambda *_args, **_kwargs: {}})(),
    )

    for session in ("session-1", "session-2"):
        assert hook(
            session_id=session,
            user_message="opening",
            conversation_history=[],
            first_turn=True,
        ) == {"action": "respond", "response": "choose"}

    assert len(stores) == 2
    assert stores[0] is not stores[1]
    assert all(store.closed for store in stores)
    handles = [fields for kind, fields in calls if kind == "handle"]
    assert [fields["session_id"] for fields in handles] == [
        "session-1",
        "session-2",
    ]
    assert all(fields["user_message"] == "opening" for fields in handles)


def test_runtime_hook_dependency_failure_is_explicit_and_recoverable(
    adrian_plugin_modules, monkeypatch, tmp_path
):
    runtime = adrian_plugin_modules["session_startup_runtime"]
    monkeypatch.setattr(
        runtime,
        "SessionStartupAnchorResolver",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("registry unavailable")
        ),
    )
    monkeypatch.setattr(
        runtime.profiles, "get_active_profile_name", lambda: "default"
    )
    hook = runtime.build_session_startup_hook(
        str((tmp_path / "tracker.db").resolve()),
        object(),
        type("Boundary", (), {"submit": lambda *_args, **_kwargs: {}})(),
    )

    result = hook(session_id="session-1", user_message="opening")
    assert result["action"] == "fail_closed"
    assert result["response"].startswith("[Session Startup]")
    assert "registry unavailable" in result["response"]
