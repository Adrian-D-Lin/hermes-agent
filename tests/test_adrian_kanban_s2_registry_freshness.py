"""Live registry freshness tests for Slice A.

Two behavior tests:
1. Provider resolves a new registration via a getter-bound registry without
   rebinding.
2. The dispatcher daemon resolves the live registry once per tick and passes
   that one static snapshot into the whole tick.
"""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_authority as ka

_NAME = "s2a_registry_freshness"
_ROOT = Path(__file__).parents[1] / "plugins" / "adrian-kanban"


@pytest.fixture(scope="module")
def provider_modules():
    spec = importlib.util.spec_from_file_location(
        _NAME,
        _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = package
    spec.loader.exec_module(package)
    modules = {
        "workspace": importlib.import_module(f"{_NAME}.workspace"),
        "provider": importlib.import_module(f"{_NAME}.provider"),
        "schema": importlib.import_module(f"{_NAME}.schema"),
        "dispatcher": importlib.import_module(f"{_NAME}.dispatcher"),
        "contracts": importlib.import_module(f"{_NAME}.contracts"),
        "capability": importlib.import_module(f"{_NAME}.capability"),
        "lifecycle": importlib.import_module(f"{_NAME}.lifecycle"),
        "policy": importlib.import_module(f"{_NAME}.policy"),
    }
    yield modules
    for name in tuple(sys.modules):
        if name == _NAME or name.startswith(f"{_NAME}."):
            sys.modules.pop(name, None)


def _staged_dispatch_database(provider_modules, tmp_path, monkeypatch):
    """Create one shared native/plugin database before selecting plugin authority."""
    from tests.test_adrian_kanban_s2 import _select_plugin_authority

    database_path = (tmp_path / "authority" / "kanban.db").resolve()
    database_path.parent.mkdir()
    conn = sqlite3.connect(str(database_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(kb.SCHEMA_SQL)
    provider_modules["schema"].create_schema(conn)
    conn.execute(
        "INSERT INTO adrian_kanban_initiatives (initiative_id) VALUES (?)",
        ("I1",),
    )
    initiative_card_id = conn.execute(
        "INSERT INTO adrian_kanban_cards "
        "(card_type, initiative_id, task_id, title, created_at) "
        "VALUES ('initiative', 'I1', NULL, 'Initiative', 1)"
    ).lastrowid
    conn.execute(
        "INSERT INTO initiative_transitions "
        "(initiative_card_id, initiative_id, transition_id, to_phase, "
        "actor_evidence, created_at) VALUES (?, 'I1', 1, 'D2', 'test', 2)",
        (initiative_card_id,),
    )
    _select_plugin_authority(tmp_path, monkeypatch, database_path)
    provider = provider_modules["provider"].AdrianKanbanAuthorityProvider(
        str(database_path)
    )
    ka.clear_authority_providers()
    provider_modules["provider"].register_provider(provider)
    return conn, provider


def _swapped_registry(workspace, registry_a, identity="other-repository"):
    """Build registry B with the same trusted roots but a different repository
    identity, so only the registry freshness — not the path — changes the
    certification result."""
    registration_a = registry_a._registrations[0]
    return workspace._TrustedRepositoryRegistry(
        (
            workspace._RepositoryRegistration(
                repository_identity=identity,
                repository_root=registration_a.repository_root,
                controlled_worktree_root=registration_a.controlled_worktree_root,
                github_repository=registration_a.github_repository,
                integration_branch=registration_a.integration_branch,
            ),
        )
    )


def test_provider_resolves_new_registration_after_binding(
    provider_modules, tmp_path, monkeypatch
):
    """A getter-bound provider resolves the registry live at use time.

    A repository registration swapped into the live registry after
    ``bind_workspace_registry`` must be visible to the next
    ``resolve_trusted_workspace_root`` call without rebinding.
    """
    from tests.test_adrian_kanban_s2 import _insert_ready_segment_dispatch_task

    conn, provider = _staged_dispatch_database(
        provider_modules, tmp_path, monkeypatch
    )
    _record, registry, segment_root, _member_root = (
        _insert_ready_segment_dispatch_task(
            provider_modules, conn, tmp_path
        )
    )
    swapped = _swapped_registry(
        provider_modules["workspace"], registry
    )

    holder = {"registry": registry}

    def live_registry():
        return holder["registry"]

    provider.bind_workspace_registry(live_registry)
    assert (
        provider.resolve_trusted_workspace_root(str(segment_root))
        == str(segment_root)
    )

    holder["registry"] = swapped
    assert (
        provider.resolve_trusted_workspace_root(str(segment_root)) is None
    )

    ka.clear_authority_providers()
    conn.close()


class _StopController(threading.Event):
    """Test-local stop signal that counts ticks and swaps the holder.

    ``wait()`` is the daemon's tick barrier: the first call advances the tick
    count and, after tick one, swaps the holder's registry so the second tick
    resolves the new one.  The event is set only after the second tick
    completes, so the daemon's ``while not stop_event.is_set()`` guard sees
    two ticks before exiting.  The test joins the daemon thread against this
    controller instead of sleeping, making the tick ordering deterministic.
    """

    def __init__(self, holder, swapped):
        super().__init__()
        self._holder = holder
        self._swapped = swapped
        self.tick_count = 0

    def wait(self, timeout=None):
        self.tick_count += 1
        if self.tick_count == 1:
            self._holder["registry"] = self._swapped
        elif self.tick_count == 2:
            self.set()
        return threading.Event.wait(self, timeout)


def test_dispatcher_tick_resolves_live_registry(
    provider_modules, tmp_path, monkeypatch
):
    """The daemon resolves the live registry once per dispatch tick.

    A registry change between ticks is seen by the next tick's
    ``_dispatch_ready_once`` (new getter result) while each tick receives the
    single static snapshot resolved at its start.  A test-local stop
    controller (subclass of ``threading.Event``) drives tick ordering
    deterministically: its ``wait()`` advances the tick count and, after tick
    one, swaps the holder's registry.  The daemon thread is joined before any
    database cleanup.
    """
    from tests.test_adrian_kanban_s2 import _insert_ready_segment_dispatch_task

    conn, _provider = _staged_dispatch_database(
        provider_modules, tmp_path, monkeypatch
    )
    _record, registry, _segment_root, _member_root = (
        _insert_ready_segment_dispatch_task(
            provider_modules, conn, tmp_path
        )
    )
    swapped = _swapped_registry(provider_modules["workspace"], registry)
    dispatcher_mod = provider_modules["dispatcher"]

    holder = {"registry": registry, "calls": 0, "results": []}

    def live_registry():
        holder["calls"] += 1
        return holder["registry"]

    seen_snapshots = []

    def fake_tick(
        provider, conn, *, dispatcher_session_id, spawn_fn=None, board=None,
        max_launches=None, workspace_registry=None,
    ):
        seen_snapshots.append(workspace_registry)
        return dispatcher_mod._DispatchTickResult(
            candidate_task_ids=(), launched_task_ids=(), rejections=()
        )

    monkeypatch.setattr(dispatcher_mod, "_dispatch_ready_once", fake_tick)

    stop = _StopController(holder, swapped)
    daemon_thread = threading.Thread(
        target=dispatcher_mod._run_dispatcher_daemon,
        kwargs=dict(
            provider=_provider,
            conn=conn,
            interval_seconds=0.01,
            dispatcher_session_id="freshness-daemon",
            stop_event=stop,
            workspace_registry=live_registry,
        ),
        daemon=True,
    )
    daemon_thread.start()
    daemon_thread.join(timeout=5)

    assert not daemon_thread.is_alive()
    assert len(seen_snapshots) == 2
    # Exactly one getter call per tick: the daemon resolved once at tick start
    # and passed that static snapshot to _dispatch_ready_once.
    assert holder["calls"] == len(seen_snapshots)
    for snapshot in seen_snapshots:
        assert type(snapshot) is type(registry)
    # Tick one saw the original registry; after the stop controller swapped
    # the holder, tick two saw the swap, with each tick pinned to a single
    # registry identity.
    assert seen_snapshots[0] is registry
    assert seen_snapshots[1] is swapped

    ka.clear_authority_providers()
    conn.close()