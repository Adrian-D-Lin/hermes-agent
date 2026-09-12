"""Tests for the write-gate registry (session binding + supersession + migration).

Covers ``writegate/registry.py``:

  * ``create_binding`` inserts a row, marks any prior active binding for the
    same session ``superseded`` (one active binding per session), and returns
    the new :class:`BindingRecord`.
  * ``get_active_binding`` returns the active binding or ``None``.
  * ``get_binding_by_id`` / ``list_bindings`` / ``get_bindings_under``.
  * ``migrate``: a prior runtime table that carries a hard table-level
    ``UNIQUE`` on ``session_id`` (visible as
    ``sqlite_autoindex_write_gate_bindings_1``) is rebuilt to the canonical
    schema with the filtered active-binding index, preserving known fields.
    A canonical table is a no-op. Drift we cannot map fails closed.
"""

import importlib
import sqlite3
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "write-gate"
_PACKAGE = "writegate"


@pytest.fixture
def wgr(monkeypatch):
    """Import the ``writegate.registry`` module from the repo-root host
    package, so relative imports inside the modules resolve."""
    import sys
    # Import the package fresh each test.
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]
    reg_registry = importlib.import_module(_PACKAGE + ".registry")
    yield reg_registry
    # Clean up.
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]


@pytest.fixture
def reg(tmp_path, wgr):
    db_path = tmp_path / "write-gate.db"
    return wgr.set_registry_for_path(str(db_path))


def _insert_legacy_row(conn, session_id="sess-OLD", project="/proj/legacy"):
    conn.execute(
        """
        CREATE TABLE write_gate_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project TEXT,
            initiative TEXT,
            board TEXT,
            worktree_path TEXT,
            git_branch TEXT,
            profile TEXT,
            producer TEXT,
            event TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            lineage TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO write_gate_bindings
            (session_id, project, initiative, board, worktree_path, git_branch,
             profile, producer, event, status, lineage, created_at)
        VALUES (?, ?, 'init-x', 'orchestrator', '/wt', 'feature/x',
                'default', 'codex-coordinator', 'commit', 'active',
                'lineage-legacy', 't0')
        """,
        (session_id, project),
    )
    conn.commit()


def test_create_binding_is_active(reg):
    b = reg.create_binding(session_id="sess-A", worktree_path="/wt/a")
    assert b.session_id == "sess-A"
    assert b.worktree_path == "/wt/a"
    assert b.status == "active"
    assert b.is_active
    active = reg.get_active_binding("sess-A")
    assert active is not None
    assert active.session_id == "sess-A"
    assert active.member_roots == ("/wt/a",)
    assert active.logical_workspace_id is None
    assert active.binding_version is None


def test_create_logical_binding_round_trips_ordered_roots(reg):
    binding = reg.create_binding(
        session_id="sess-logical",
        worktree_path="/wt/a",
        logical_workspace_id="coord-init-1",
        member_roots=("/wt/a", "/wt/b"),
        binding_version=3,
    )
    assert binding.logical_workspace_id == "coord-init-1"
    assert binding.member_roots == ("/wt/a", "/wt/b")
    assert binding.binding_version == 3
    assert binding.to_dict()["member_roots"] == ["/wt/a", "/wt/b"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"logical_workspace_id": "coord", "member_roots": ("/wt/a",)},
        {"logical_workspace_id": "coord", "binding_version": 1},
        {"member_roots": ("/wt/a",), "binding_version": 1},
        {
            "logical_workspace_id": "coord",
            "member_roots": "/wt/a",
            "binding_version": 1,
        },
        {
            "logical_workspace_id": "coord",
            "member_roots": ("/wt/a", "/wt/a"),
            "binding_version": 1,
        },
        {
            "logical_workspace_id": "coord",
            "member_roots": ("/wt/b", "/wt/a"),
            "binding_version": 1,
        },
    ],
)
def test_logical_binding_rejects_partial_or_invalid_payload_without_mutation(
    reg, wgr, overrides
):
    with pytest.raises(wgr.RegistryError):
        reg.create_binding(
            session_id="sess-invalid", worktree_path="/wt/a", **overrides
        )
    assert reg.get_active_binding("sess-invalid") is None


@pytest.mark.parametrize(
    ("workspace", "roots", "version"),
    [
        ("coord", None, 1),
        (None, '["/wt/a"]', 1),
        ("coord", '["/wt/a"]', None),
        ("coord", "not-json", 1),
        ("coord", '[["/wt/a"]]', 1),
        ("coord", '["/wt/b"]', 1),
    ],
)
def test_binding_record_rejects_partial_or_malformed_persisted_logical_state(
    reg, wgr, workspace, roots, version
):
    reg._conn.execute(
        "INSERT INTO write_gate_bindings "
        "(session_id, worktree_path, status, created_at, "
        "logical_workspace_id, member_roots_json, binding_version) "
        "VALUES (?, ?, 'active', 'now', ?, ?, ?)",
        ("sess-corrupt", "/wt/a", workspace, roots, version),
    )
    reg._conn.commit()
    with pytest.raises(wgr.RegistryError):
        reg.get_active_binding("sess-corrupt")


def test_second_binding_supersedes_first(reg):
    reg.create_binding(session_id="sess-A", worktree_path="/wt/a1")
    reg.create_binding(session_id="sess-A", worktree_path="/wt/a2")
    active = reg.get_active_binding("sess-A")
    assert active is not None
    # Exactly one active binding per session.
    all_rows = reg.list_bindings("sess-A")
    statuses = [r.status for r in all_rows]
    assert statuses.count("active") == 1
    assert statuses.count("superseded") == 1
    assert len(all_rows) == 2


def test_superseded_by_id_points_forward_to_replacement(reg):
    """``superseded_by_id`` lives on the PRIOR (superseded) row and points to
    the NEW replacement row's id, not the other way around."""
    reg.create_binding(session_id="sess-A", worktree_path="/wt/a1")
    reg.create_binding(session_id="sess-A", worktree_path="/wt/a2")
    all_rows = reg.list_bindings("sess-A")
    assert len(all_rows) == 2
    active = [r for r in all_rows if r.status == "active"][0]
    superseded = [r for r in all_rows if r.status == "superseded"][0]
    # The active (new) row has no superseded_by_id of its own.
    assert active.superseded_by_id is None
    # The superseded (prior) row points forward to the new row's id.
    # Query the DB directly to read the row id + superseded_by_id.
    rows = reg._conn.execute(
        "SELECT id, status, superseded_by_id FROM write_gate_bindings "
        "WHERE session_id = 'sess-A' ORDER BY id"
    ).fetchall()
    prior = [r for r in rows if r["status"] == "superseded"][0]
    new = [r for r in rows if r["status"] == "active"][0]
    assert prior["superseded_by_id"] == new["id"]


def test_active_binding_none_when_unbound(reg):
    assert reg.get_active_binding("no-such-session") is None


def test_parent_binding_linkage(reg):
    reg.create_binding(session_id="sess-A", worktree_path="/wt/a")
    reg.create_binding(
        session_id="sess-B", worktree_path="/wt/b",
        parent_session_id="sess-A",
    )
    # The child records its parent in parent_session_id.
    all_rows = reg.list_bindings("sess-B")
    assert len(all_rows) == 1
    assert all_rows[0].parent_session_id == "sess-A"


def test_get_bindings_under_child_session(reg):
    reg.create_binding(session_id="sess-A", worktree_path="/wt/a")
    reg.create_binding(
        session_id="sess-B", worktree_path="/wt/b",
        parent_session_id="sess-A",
    )
    # Query the child session; its parent_session_id resolves to sess-A.
    children = reg.list_bindings("sess-B")
    assert [c.session_id for c in children] == ["sess-B"]
    assert children[0].parent_session_id == "sess-A"


def test_migrate_drops_hard_unique_and_preserves_row(tmp_path, wgr):
    """A legacy table with hard UNIQUE(session_id) migrates to the canonical
    schema, preserving the row, and supersession then works."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    _insert_legacy_row(conn)
    conn.close()

    reg = wgr.set_registry_for_path(str(db_path))
    idx = {r[0] for r in reg._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    assert "sqlite_autoindex_write_gate_bindings_1" not in idx
    assert "idx_bindings_session_active" in idx
    # The migrated row is preserved.
    rows = [tuple(r) for r in reg._conn.execute(
        "SELECT session_id, project, lineage, status FROM write_gate_bindings"
    ).fetchall()]
    assert rows == [("sess-OLD", "/proj/legacy", "lineage-legacy", "active")]

    # Supersession now works on the migrated table.
    reg.create_binding(session_id="sess-OLD", worktree_path="/wt/old")
    reg.create_binding(session_id="sess-OLD", worktree_path="/wt/old2")
    active = reg.get_active_binding("sess-OLD")
    assert active.worktree_path == "/wt/old2"
    statuses = [r.status for r in reg.list_bindings("sess-OLD")]
    assert statuses.count("active") == 1
    # 1 legacy + 2 created = 3 rows total.
    assert len(reg.list_bindings("sess-OLD")) == 3


def test_migrate_is_idempotent(tmp_path, wgr):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    _insert_legacy_row(conn)
    conn.close()

    reg = wgr.set_registry_for_path(str(db_path))
    reg.migrate()
    reg.migrate()
    reg.migrate()
    n = reg._conn.execute("SELECT COUNT(*) FROM write_gate_bindings").fetchone()[0]
    assert n == 1


def test_migrate_fails_closed_on_missing_columns(tmp_path, wgr):
    """A legacy table missing a column we must preserve (lineage) raises
    RegistryError rather than silently guessing at an unknown schema."""
    db_path = tmp_path / "drift.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE write_gate_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO write_gate_bindings (session_id, project, status, created_at) "
        "VALUES ('sess-X', '/proj/x', 'active', 't0')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(wgr.RegistryError):
        wgr.set_registry_for_path(str(db_path))


def test_migrate_canonical_table_is_noop(tmp_path, wgr):
    db_path = tmp_path / "fresh.db"
    reg = wgr.set_registry_for_path(str(db_path))
    reg.create_binding(session_id="sess-F", worktree_path="/wt/f")
    reg.migrate()
    reg.migrate()
    active = reg.get_active_binding("sess-F")
    assert active is not None
    assert active.worktree_path == "/wt/f"


def _insert_legacy_lease(conn, lease_id="lease-1", session_id="sess-OLD"):
    conn.execute(
        """
        CREATE TABLE write_gate_leases (
            lease_id TEXT PRIMARY KEY,
            approval_reference TEXT,
            session_id TEXT NOT NULL,
            confirmed_worktree TEXT,
            approved_folder TEXT,
            approved_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            FOREIGN KEY (session_id) REFERENCES write_gate_bindings(session_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO write_gate_leases
            (lease_id, approval_reference, session_id, confirmed_worktree,
             approved_folder, approved_at, expires_at, status)
        VALUES (?, NULL, ?, '/wt', '/approved', 't1', 't2', 'active')
        """,
        (lease_id, session_id),
    )
    conn.commit()


def test_migrate_drops_invalid_leases_fk(tmp_path, wgr):
    """A prior runtime's ``write_gate_leases`` carried an invalid FK to
    ``write_gate_bindings(session_id)`` (bindings.session_id is non-unique).
    The migration rebuilds the table without the FK, preserving the row."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE write_gate_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project TEXT,
            initiative TEXT,
            board TEXT,
            worktree_path TEXT,
            git_branch TEXT,
            profile TEXT,
            producer TEXT,
            event TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            lineage TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO write_gate_bindings (session_id, project, status, created_at) "
        "VALUES ('sess-OLD', '/proj/x', 'active', 't0')"
    )
    _insert_legacy_lease(conn)
    conn.commit()
    conn.close()

    reg = wgr.set_registry_for_path(str(db_path))
    # The FK is gone after migrate (the schema comment mentions "FOREIGN KEY"
    # but there must be no actual FK constraint clause).
    leases_sql = reg._conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'write_gate_leases'"
    ).fetchone()[0]
    assert "REFERENCES" not in leases_sql.upper()
    # The lease row is preserved.
    rows = [tuple(r) for r in reg._conn.execute(
        "SELECT lease_id, session_id FROM write_gate_leases"
    ).fetchall()]
    assert rows == [("lease-1", "sess-OLD")]
