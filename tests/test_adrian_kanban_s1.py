"""Candidate tests for the S1 inactive generic authority seam.

These tests encode the S1 acceptance criteria (brief §5 / §9):

* A backward-safe default ``kanban.mutation_authority: native`` preserves
  existing native Kanban behavior unchanged.
* When ``adrian-kanban`` is selected, every supported native mutation and
  dispatch entry point fails closed with a specific authority diagnostic
  unless the configured provider supplies the admitted-operation interface.
* A test-only provider fixture proves the seam can delegate an
  already-admitted operation without implementing S2's production capability.
* No native fallback occurs when the selected authority is missing,
  unhealthy, or incompatible.
* The non-public schema/store foundation enforces unified-card identity,
  monotonic initiative transitions, and segment-manifest projection.
* All simulated processes resolve one machine-global database path, and no
  profile-local / native-board write is admitted when plugin authority is
  selected.

The seam is generic and core-owned (``hermes_cli.kanban_db``); the plugin
registers a provider *through* it. The tests explicitly select authority via
the supported config seam and exercise the real shared/public mutation and
dispatch paths.

CANDIDATE MATERIAL — UNTRUSTED UNTIL INDEPENDENTLY RATIFIED.
These tests are evidence for review, not proof of correctness. They must be
ratified by test-authority before they enter the exhaustive test store.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from hermes_cli import kanban_db as kb

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh HERMES_HOME with the default ``native`` authority.

    ``init_db`` runs so the native board exists; the default authority is
    ``native`` (backward-safe) unless a test overrides the config.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # No kanban.mutation_authority override -> defaults to ``native``.
    kb.init_db()
    return home


@pytest.fixture
def adrian_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Fresh HERMES_HOME with ``kanban.mutation_authority: adrian-kanban``.

    The authority is selected through the supported config surface
    (``config.yaml``), not by reaching into private seams. The board is
    initialized under the same home so the DB path resolves identically.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Bootstrap the disposable native schema before selecting the fail-closed
    # replacement authority. Authority selection must block operational native
    # mutation, not make first-time database creation impossible.
    kb.init_db()
    authority_db = (tmp_path / "machine-global" / "kanban.db").resolve()
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        f"  database_path: {authority_db.as_posix()}\n",
        encoding="utf-8",
    )
    return home


@pytest.fixture
def adrian_plugin_modules(
    kanban_home: Path,
) -> dict[str, ModuleType]:
    """Load the bundled plugin through Hermes' native PluginManager.

    This deliberately does not make ``adrian_kanban`` a top-level importable
    package. The loaded package name is owned by PluginManager and may include
    a profile-scope suffix, so every submodule is resolved from the actual
    loaded module name.
    """
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
        "package": package,
        "seam": importlib.import_module(f"{package.__name__}.seam"),
        "schema": importlib.import_module(f"{package.__name__}.schema"),
        "store": importlib.import_module(f"{package.__name__}.store"),
        "validation": importlib.import_module(f"{package.__name__}.validation"),
    }
    try:
        yield modules
    finally:
        manager.unload("adrian-kanban")


# ---------------------------------------------------------------------------
# 4.1 / 5.1 — backward-safe default configuration
# ---------------------------------------------------------------------------


def test_bundled_plugin_loads_through_native_manager(
    adrian_plugin_modules: dict[str, ModuleType],
):
    """Oracle: brief §5.1 and correction R433-02.

    Path: happy. Behaviour: Hermes' existing directory-plugin loader imports
    the S1 package and its relative submodules. Expected: the actual package
    name is in Hermes' loader-owned namespace. Out-of-scope: selecting the
    plugin as production mutation authority.
    """
    package = adrian_plugin_modules["package"]
    assert package.__name__.startswith("hermes_plugins.adrian_kanban")


def test_config_default_authority_is_native(kanban_home: Path):
    """Oracle: brief §7 Inactive/backward-safe configuration.

    Path: happy. Behaviour: the resolved default is ``native``.
    Fixture: DEFAULT_CONFIG. Expected: ``kanban.mutation_authority == 'native'``.
    Out-of-scope: an explicit user override to ``adrian-kanban``.
    """
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["mutation_authority"] == "native"
    assert DEFAULT_CONFIG["kanban"]["database_path"] is None


def test_native_authority_resolves_native(kanban_home: Path):
    """Oracle: brief §5.2 (native selection unchanged).

    Path: happy. Behaviour: the generic seam resolves ``native`` under the
    default config.
    Fixture: fresh HERMES_HOME. Expected: ``resolve_selected_authority()``
    returns ``'native'``.
    Out-of-scope: selected-plugin rejection (see seam tests).
    """
    assert kb.resolve_selected_authority() == "native"


def test_native_authority_preserves_native_mutation(kanban_home: Path):
    """Oracle: brief §5.2 (native selection unchanged).

    Path: happy. Behaviour: with the default authority, a native mutation
    succeeds unchanged.
    Fixture: fresh board + connection. Expected: ``create_task`` returns an id.
    Out-of-scope: selected-plugin rejection (see seam tests).
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="seed")
        assert tid.startswith("t_")


# ---------------------------------------------------------------------------
# 4.3 / 5.2 — generic authority seam, fail-closed
# ---------------------------------------------------------------------------


def test_native_mutation_rejected_when_plugin_authority_selected(adrian_authority: Path):
    """Oracle: brief §5.2 + §9.3 fail-closed.

    Path: unhappy. Behaviour: with ``adrian-kanban`` selected (via config) and
    no provider, a native write fails closed with a specific authority
    diagnostic.
    Fixture: fresh board; authority selected through config.
    Expected: ``AuthorityAdmissionRejected`` mentioning the selected
    authority. No row is written.
    Out-of-scope: the provider-present delegation path.
    """
    from hermes_cli.kanban_db import AuthorityAdmissionRejected

    with kb.connect() as conn:
        with pytest.raises(AuthorityAdmissionRejected) as excinfo:
            with kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO tasks (id, title, status, created_at) "
                    "VALUES ('t_probe', 'p', 'ready', 0)"
                )
        assert "adrian-kanban" in str(excinfo.value)
        # No partial state: the probe task must not exist.
        row = conn.execute("SELECT id FROM tasks WHERE id = 't_probe'").fetchone()
        assert row is None


def test_dispatch_rejected_when_plugin_authority_selected(adrian_authority: Path):
    """Oracle: brief §5.2 (dispatch fails closed).

    Path: unhappy. Behaviour: ``dispatch_once`` fails closed when the selected
    authority is ``adrian-kanban`` and no provider is present.
    Fixture: fresh board with a ready task; authority selected through config.
    Expected: ``AuthorityAdmissionRejected`` mentioning the selected authority.
    Out-of-scope: the provider-present dispatch path.
    """
    from hermes_cli.kanban_db import AuthorityAdmissionRejected

    # Seed a ready task while native is still selected; the behavior under
    # test is the later dispatch boundary, not task creation.
    config_path = adrian_authority / "config.yaml"
    config_path.write_text(
        "kanban:\n"
        "  mutation_authority: native\n",
        encoding="utf-8",
    )
    with kb.connect() as conn:
        kb.create_task(conn, title="ready-task")
        config_path.write_text(
            "kanban:\n"
            "  mutation_authority: adrian-kanban\n",
            encoding="utf-8",
        )
        with pytest.raises(AuthorityAdmissionRejected) as excinfo:
            kb.dispatch_once(conn)
        assert "adrian-kanban" in str(excinfo.value)


def test_native_board_write_rejected_when_plugin_selected(adrian_authority: Path):
    """Oracle: brief §5.4 + §9.4 no profile-local/native-board fallback.

    Path: unhappy. Behaviour: no native-board write is admitted when plugin
    authority is selected.
    Fixture: fresh board; authority selected through config.
    Expected: any native write raises the authority diagnostic.
    Out-of-scope: the provider-present path.
    """
    from hermes_cli.kanban_db import AuthorityAdmissionRejected

    with kb.connect() as conn:
        with pytest.raises(AuthorityAdmissionRejected):
            with kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO tasks (id, title, status, created_at) "
                    "VALUES ('t_native', 'n', 'ready', 0)"
                )


def test_unhealthy_provider_fails_closed(kanban_home: Path):
    """Oracle: brief §9.5 unhappy (unhealthy selected authority).

    Path: unhappy. Behaviour: a provider present but unhealthy makes the seam
    fail closed — no fallback to native.
    Fixture: fresh home; a provider that reports ``is_healthy() == False``
    registered through the generic seam with authority selected via config.
    Expected: native mutation raises ``AuthorityAdmissionRejected``.
    Out-of-scope: the healthy delegation path.
    """
    from hermes_cli.kanban_db import AuthorityAdmissionRejected

    # Select the authority via config on a second home so the default stays native.
    home = kanban_home
    db_path = str(kb.kanban_db_path().resolve())
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        f"  database_path: {Path(db_path).as_posix()}\n"
    )

    class _UnhealthyProvider:
        name = "unhealthy-provider"

        def is_healthy(self) -> bool:
            return False

        def admit_operation(self, operation: str) -> bool:
            return False

    kb.register_authority_provider(_UnhealthyProvider(), db_path)
    try:
        with kb.connect() as conn:
            with pytest.raises(AuthorityAdmissionRejected):
                with kb.write_txn(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, title, status, created_at) "
                        "VALUES ('t_unhealthy', 'u', 'ready', 0)"
                    )
    finally:
        kb.clear_authority_providers()


# ---------------------------------------------------------------------------
# 4.3 / 5.2 — test-only provider fixture proves delegation
# ---------------------------------------------------------------------------


def test_test_only_provider_delegates_admitted_operation(kanban_home: Path):
    """Oracle: brief §5.2 + §9.5 happy delegation.

    Path: happy. Behaviour: a test-only provider that supplies the
    admitted-operation interface lets a synthetic already-admitted operation
    cross the seam.
    Fixture: fresh home; provider registered through the generic seam with
    authority selected via config.
    Expected: ``ensure_admitted`` returns True for an admitted operation and
    raises for an un-admitted one; no production capability is created.
    Out-of-scope: production capability creation/validation/binding.
    """
    from hermes_cli.kanban_db import AuthorityAdmissionRejected

    home = kanban_home
    db_path = str(kb.kanban_db_path().resolve())
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        f"  database_path: {Path(db_path).as_posix()}\n"
    )

    class _TestProvider:
        name = "test-only-provider"

        def is_healthy(self) -> bool:
            return True

        def admit_operation(self, operation: str) -> bool:
            return operation == "admitted-synthetic"

    kb.register_authority_provider(_TestProvider(), db_path)
    try:
        assert kb.ensure_admitted("admitted-synthetic", db_path=db_path) is True
        with pytest.raises(AuthorityAdmissionRejected):
            kb.ensure_admitted("not-admitted", db_path=db_path)
    finally:
        kb.clear_authority_providers()


# ---------------------------------------------------------------------------
# 4.5 / 5.3 — foundational schema/store invariants
# ---------------------------------------------------------------------------


def test_unified_card_initiative_required_task_nullable(
    adrian_plugin_modules: dict[str, ModuleType], kanban_home: Path
):
    """Oracle: brief §5.3 + ratified design §4.1.

    Path: fringe. Behaviour: a unified card requires initiative identity;
    task identity is nullable only for initiatives.
    Fixture: the foundational store. Expected: creating an initiative card
    without initiative identity raises; a task card requires both.
    Out-of-scope: full lifecycle admission (S2).
    """
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    InitiativeIdentityError = (
        adrian_plugin_modules["validation"].InitiativeIdentityError
    )

    store = AdmittedStore(
        database_path=kanban_home.parent / "adrian-db" / "kanban.db"
    )
    with pytest.raises(InitiativeIdentityError):
        store.initiative_card(initiative_id=None, title="orphan")


def test_task_card_requires_both_identities(
    adrian_plugin_modules: dict[str, ModuleType], kanban_home: Path
):
    """Oracle: brief §5.3 + ratified design §4.1.

    Path: fringe. Behaviour: a task card requires BOTH initiative and task
    identity.
    Fixture: the foundational store. Expected: a task card without a task id
    raises ``TaskIdentityError``.
    Out-of-scope: full lifecycle admission (S2).
    """
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    TaskIdentityError = adrian_plugin_modules["validation"].TaskIdentityError

    store = AdmittedStore(
        database_path=kanban_home.parent / "adrian-taskdb" / "kanban.db"
    )
    with pytest.raises(TaskIdentityError):
        store.task_card(initiative_id="init_a", task_id=None, title="task")


def test_unified_card_initiative_uniqueness(
    adrian_plugin_modules: dict[str, ModuleType], kanban_home: Path
):
    """Oracle: brief §5.3 + ratified design §4.1.

    Path: fringe. Behaviour: initiative identity is unique across the board.
    Fixture: the foundational store. Expected: two initiative cards with the
    same initiative id raise ``sqlite3.IntegrityError``.
    Out-of-scope: task-card uniqueness (see next test).
    """
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore

    store = AdmittedStore(
        database_path=kanban_home.parent / "adrian-unq" / "kanban.db"
    )
    store.initiative_card(initiative_id="init_dup", title="first")
    with pytest.raises(sqlite3.IntegrityError):
        store.initiative_card(initiative_id="init_dup", title="second")


def test_task_identity_is_globally_unique(
    adrian_plugin_modules: dict[str, ModuleType], kanban_home: Path
):
    """Oracle: correction R433-06 and unified-card identity.

    Path: fringe. Behaviour: a non-null task identity cannot be reused under a
    different initiative. Native Hermes task IDs are board-global identities,
    not initiative-relative labels.
    """
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    store = AdmittedStore(
        database_path=kanban_home.parent / "adrian-task-unq" / "kanban.db"
    )
    store.task_card(initiative_id="init_a", task_id="t_global", title="first")
    with pytest.raises(sqlite3.IntegrityError):
        store.task_card(initiative_id="init_b", task_id="t_global", title="second")


def test_initiative_transition_monotonic_and_predecessor(
    adrian_plugin_modules: dict[str, ModuleType], kanban_home: Path
):
    """Oracle: brief §5.3 + ratified design §7.12 / §5 invariant 16.

    Path: fringe. Behaviour: initiative transitions enforce monotonic IDs and
    predecessor integrity.
    Fixture: a store with an initiative at a given transition id.
    Expected: a non-monotonic transition id is rejected.
    Out-of-scope: transition policy (S2).
    """
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    TransitionIntegrityError = (
        adrian_plugin_modules["validation"].TransitionIntegrityError
    )

    store = AdmittedStore(
        database_path=kanban_home.parent / "adrian-db2" / "kanban.db"
    )
    store.initiative_card(initiative_id="init_a", title="A")
    first = store.record_transition(
        initiative_id="init_a",
        transition_id=10,
        previous_transition_id=None,
        from_phase=None,
        from_segment_id=None,
        to_phase="DEV1",
        to_segment_id=None,
        canon_route="design-lifecycle:DEV1",
        repository_reconciliation_ref=None,
        trigger="model_assessment",
        actor_evidence="test-actor",
        canonical_payload="{}",
        rendered_history_ref="history:10",
    )
    assert first == 10
    second = store.record_transition(
        initiative_id="init_a",
        transition_id=20,
        previous_transition_id=10,
        from_phase="DEV1",
        from_segment_id=None,
        to_phase="DEV2",
        to_segment_id="S1",
        canon_route="design-lifecycle:DEV1-to-DEV2",
        repository_reconciliation_ref="reconcile:1",
        trigger="model_assessment",
        actor_evidence="test-actor",
        canonical_payload="{}",
        rendered_history_ref="history:20",
    )
    assert second == 20

    # A duplicate/non-increasing identity is rejected.
    with pytest.raises(TransitionIntegrityError):
        store.record_transition(
            initiative_id="init_a",
            transition_id=20,
            previous_transition_id=20,
            from_phase="DEV2",
            from_segment_id="S1",
            to_phase="DEV3",
            to_segment_id="S1",
            canon_route="design-lifecycle:DEV2-to-DEV3",
            repository_reconciliation_ref="reconcile:2",
            trigger="model_assessment",
            actor_evidence="test-actor",
            canonical_payload="{}",
            rendered_history_ref="history:duplicate",
        )

    # A stale predecessor is rejected even when the proposed ID is higher.
    with pytest.raises(TransitionIntegrityError):
        store.record_transition(
            initiative_id="init_a",
            transition_id=30,
            previous_transition_id=10,
            from_phase="DEV2",
            from_segment_id="S1",
            to_phase="DEV3",
            to_segment_id="S1",
            canon_route="design-lifecycle:DEV2-to-DEV3",
            repository_reconciliation_ref="reconcile:3",
            trigger="model_assessment",
            actor_evidence="test-actor",
            canonical_payload="{}",
            rendered_history_ref="history:stale",
        )


def test_transition_schema_matches_ratified_record_shape(
    adrian_plugin_modules: dict[str, ModuleType], kanban_home: Path
):
    """Oracle: ratified design §7.12 and durable-data overview.

    Path: fringe. Behaviour: S1 creates the complete structural row shape
    needed by later S2 policy without implementing that policy.
    """
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    store = AdmittedStore(
        database_path=kanban_home.parent
        / "adrian-transition-shape"
        / "kanban.db"
    )
    rows = store._conn.execute(
        "PRAGMA table_info(adrian_kanban_initiative_transitions)"
    ).fetchall()
    cols = {row["name"] for row in rows}
    assert {
        "transition_id",
        "initiative_id",
        "previous_transition_id",
        "from_phase",
        "from_segment_id",
        "to_phase",
        "to_segment_id",
        "canon_route",
        "repository_reconciliation_ref",
        "trigger",
        "actor_evidence",
        "canonical_payload",
        "rendered_history_ref",
        "created_at",
    } <= cols
    assert "next_initiative_id" not in cols


def test_segment_manifest_projection_exists(
    adrian_plugin_modules: dict[str, ModuleType], kanban_home: Path
):
    """Oracle: brief §5.3 + ratified design §8.3.6.

    Path: fringe. Behaviour: segment-manifest projection structures exist with
    the design-required manifest identifier fields.
    Fixture: the foundational store schema. Expected: the projection table
    carries manifest_path / manifest_sha / digest / readiness / validation.
    Out-of-scope: projection population (S2).
    """
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore

    store = AdmittedStore(
        database_path=kanban_home.parent / "adrian-seg" / "kanban.db"
    )
    rows = store._conn.execute(
        "PRAGMA table_info(adrian_kanban_segment_manifest)"
    ).fetchall()
    cols = {row["name"] for row in rows}
    for expected in (
        "manifest_path",
        "manifest_sha",
        "digest",
        "readiness",
        "validation",
    ):
        assert expected in cols, f"missing segment_manifest field: {expected}"


# ---------------------------------------------------------------------------
# 4.5 / 5.4 — machine-global database identity
# ---------------------------------------------------------------------------


def test_all_processes_resolve_single_absolute_db_path(
    adrian_plugin_modules: dict[str, ModuleType], kanban_home: Path
):
    """Oracle: brief §5.4 + ratified design §10.2.

    Path: happy. Behaviour: every simulated process resolves the same absolute
    database path.
    Fixture: two separate Python processes with different profile homes but
    the same configured machine-global path. Expected: equal, absolute,
    normalized paths.
    Out-of-scope: profile-local resolution.
    """
    del adrian_plugin_modules  # loader coverage is supplied by the fixture
    authority_db = (kanban_home.parent / "machine-global" / "kanban.db").resolve()
    homes = [kanban_home.parent / "profile-a", kanban_home.parent / "profile-b"]
    script = (
        "import json; from hermes_cli import kanban_db as kb; "
        "print(json.dumps(kb.resolve_authority_path()))"
    )
    resolved: list[str] = []
    for home in homes:
        home.mkdir()
        (home / "config.yaml").write_text(
            "kanban:\n"
            "  mutation_authority: adrian-kanban\n"
            f"  database_path: {authority_db.as_posix()}\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["HERMES_HOME"] = str(home)
        run = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        resolved.append(json.loads(run.stdout.strip()))

    assert resolved == [str(authority_db), str(authority_db)]
    assert Path(resolved[0]).is_absolute()


def test_plugin_authority_rejects_missing_database_path(
    kanban_home: Path,
):
    """Oracle: brief §5.4 + ratified design §10.2.

    Path: unhappy. Selecting the replacement authority without the explicit
    machine-global identity fails closed; it cannot inherit a native board.
    """
    from hermes_cli.kanban_db import AuthorityAdmissionRejected

    (kanban_home / "config.yaml").write_text(
        "kanban:\n  mutation_authority: adrian-kanban\n",
        encoding="utf-8",
    )
    with pytest.raises(AuthorityAdmissionRejected, match="database_path"):
        kb.resolve_authority_path()


def test_plugin_authority_rejects_relative_database_path(
    kanban_home: Path,
):
    """Oracle: brief §5.4 + ratified design §10.2.

    Path: fringe. A relative value is rejected before normalization rather
    than silently becoming profile-local through the current working folder.
    """
    from hermes_cli.kanban_db import AuthorityAdmissionRejected

    (kanban_home / "config.yaml").write_text(
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        "  database_path: relative/kanban.db\n",
        encoding="utf-8",
    )
    with pytest.raises(AuthorityAdmissionRejected, match="absolute"):
        kb.resolve_authority_path()


def test_provider_registration_isolated_by_database_path(kanban_home: Path):
    """Oracle: brief §5.2/§5.4; no process-global provider fallback.

    Path: unhappy. A healthy provider registered for database A is absent for
    database B. Expected: public status reports the distinction and admission
    on B fails closed.
    """
    from hermes_cli.kanban_db import AuthorityAdmissionRejected

    class _Provider:
        name = "path-a-only"

        def is_healthy(self) -> bool:
            return True

        def admit_operation(self, operation: str) -> bool:
            return True

    path_a = str((kanban_home.parent / "a" / "kanban.db").resolve())
    path_b = str((kanban_home.parent / "b" / "kanban.db").resolve())
    kb.register_authority_provider(_Provider(), path_a)
    try:
        status_a = kb.provider_status(path_a)
        status_b = kb.provider_status(path_b)
        assert status_a.present is True
        assert status_a.healthy is True
        assert status_a.name == "path-a-only"
        assert status_b.present is False
        with pytest.raises(AuthorityAdmissionRejected):
            kb.ensure_admitted("synthetic", db_path=path_b)
    finally:
        kb.clear_authority_providers()


def test_store_and_health_report_authoritative_path(
    adrian_plugin_modules: dict[str, ModuleType], kanban_home: Path
):
    """Oracle: brief §5.4 and S1 health/readiness evidence.

    Path: happy. Store construction and plugin health use the same public
    resolver and report the exact absolute database identity.
    """
    AdmittedStore = adrian_plugin_modules["store"].AdmittedStore
    health_report = adrian_plugin_modules["seam"].health_report

    authority_db = (kanban_home.parent / "health" / "kanban.db").resolve()
    store = AdmittedStore(database_path=authority_db)
    assert store.db_path == authority_db

    class _Provider:
        name = "health-provider"

        def is_healthy(self) -> bool:
            return True

        def admit_operation(self, operation: str) -> bool:
            return True

    (kanban_home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        "    - adrian-kanban\n"
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        f"  database_path: {authority_db.as_posix()}\n",
        encoding="utf-8",
    )
    kb.register_authority_provider(_Provider(), str(authority_db))
    try:
        report = health_report()
        assert Path(report["db_path"]) == authority_db
        assert report["provider_present"] is True
        assert report["provider_healthy"] is True
    finally:
        kb.clear_authority_providers()


# ---------------------------------------------------------------------------
# 4.4 / 5.2 — generic seam is core-owned (no concrete plugin import in core)
# ---------------------------------------------------------------------------


def test_core_seam_is_generic_not_plugin_bound(kanban_home: Path):
    """Oracle: correction R433-01.

    Path: fringe. Behaviour: the core seam is generic and core-owned — it
    defines its own ``AuthorityAdmissionRejected`` and provider registry and
    does not depend on importing concrete plugin code.
    Fixture: the core module. Expected: ``AuthorityAdmissionRejected`` and
    ``register_authority_provider`` exist on ``kanban_db``.
    Out-of-scope: plugin-side registration details.
    """
    assert hasattr(kb, "AuthorityAdmissionRejected")
    assert hasattr(kb, "register_authority_provider")
    assert hasattr(kb, "resolve_selected_authority")
    assert hasattr(kb, "ensure_admitted")
