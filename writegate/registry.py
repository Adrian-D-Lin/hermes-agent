"""Central WriteGate security registry.

One shared SQLite database, resolved as::

    get_default_hermes_root() / "write-gate.db"

It is intentionally **default-root anchored** because it is cross-profile
security state: the default Hermes instance and every isolated specialist
profile read the same bindings / requests / leases, while each profile's
conversation ``state.db`` stays isolated.  This is the pivot's
``§3b central security registry``.

The registry owns three distinct lifecycles (bindings, requests, leases are
never conflated):

* **bindings** — durable session identity: the confirmed worktree a session is
  allowed to write in ordinary fashion.  Produced only by the trusted
  ``confirm_binding`` runtime operation (see :mod:`writegate.binding`).
* **requests** — a structured record that a human was asked to approve a
  protected/out-of-worktree mutation.  A request is *not* approval.
* **leases** — the five-minute authority window minted by a ``once`` approval.
  A lease expires exactly five minutes after human approval and never
  auto-renews.

Guarantees:

* WAL mode, foreign keys, a busy timeout, transactional writes.
* The database file (and any created parent/artifact directory) is mode
  ``0600``; an existing file's permissions are never *weakened*.
* Schema/version metadata + idempotent migration.
* Fail-closed: any registry error propagates as :class:`RegistryError` rather
  than a silent skip, so the enforcement hook fails closed.
* Legacy ``sessions.confirmed_worktree_binding`` rows are historical only:
  nothing here bulk-copies or guesses them.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Policy constants (kept here so every module agrees on them).
LEASE_DURATION_SECONDS = 5 * 60
LEASE_DURATION_TEXT = "5 minutes"

# Registry schema version.  Bumped only on a structural migration.
REGISTRY_SCHEMA_VERSION = 2

# Status values for a binding row.
BINDING_ACTIVE = "active"
BINDING_SUPERSEDED = "superseded"
BINDING_ABANDONED = "abandoned"

# Status values for a lease row.
LEASE_ACTIVE = "active"
LEASE_EXPIRED = "expired"
LEASE_ENDED = "ended"

# Producer / event types recorded on a binding row.
PRODUCER_CONFIRM_BINDING = "confirm_binding"
PRODUCER_DISPATCHER = "dispatcher"
PRODUCER_LINEAGE = "lineage"

EVENT_CONFIRM = "human_confirm"
EVENT_DISPATCH = "dispatcher_dispatch"


def _clear_enforcement_cache() -> None:
    """Invalidate the WriteGate enforcement cache after any registry mutation.

    The ``pre_tool_call`` hook caches the (session, tool, targets) decision so
    the hot path stays under 100 ms.  Every registry mutation (binding /
    request / lease) clears the cache so a fresh decision is recomputed on the
    next governed tool call.  Imported lazily to avoid a circular import
    (enforcement.py imports registry.py).
    """
    from . import enforcement as _enforcement
    _enforcement._cache_clear()

EVENT_LINEAGE = "lineage_derive"

DEFAULT_BUSY_TIMEOUT_MS = 5000


class RegistryError(Exception):
    """Raised for any registry failure.  Callers treat it as fail-closed."""


class BindingRecord:
    """A single binding row, exposed as a small data object."""

    __slots__ = (
        "id", "session_id", "project", "initiative", "board", "worktree_path",
        "git_branch", "profile", "producer", "event",
        "parent_binding_id", "parent_session_id", "status", "created_at",
        "confirmed_at", "superseded_by_id", "lineage",
    )

    def __init__(self, row: Dict[str, Any]):
        self.id = row.get("id")
        self.session_id = row["session_id"]
        self.project = row.get("project")
        self.initiative = row.get("initiative")
        self.board = row.get("board")
        self.worktree_path = row.get("worktree_path")
        self.git_branch = row.get("git_branch")
        self.profile = row.get("profile")
        self.producer = row.get("producer")
        self.event = row.get("event")
        self.parent_binding_id = row.get("parent_binding_id")
        self.parent_session_id = row.get("parent_session_id")
        self.status = row.get("status", BINDING_ACTIVE)
        self.created_at = row.get("created_at")
        self.confirmed_at = row.get("confirmed_at")
        self.superseded_by_id = row.get("superseded_by_id")
        # Free-form lineage metadata (JSON text) for branch/compression/
        # delegate derivation: the recorded parent_session_id + workspace.
        self.lineage = row.get("lineage")

    @property
    def is_active(self) -> bool:
        return self.status == BINDING_ACTIVE

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


class LeaseRecord:
    """A single lease row."""

    __slots__ = (
        "lease_id", "approval_reference", "session_id", "confirmed_worktree",
        "approved_folder", "approved_at", "expires_at", "status",
    )

    def __init__(self, row: Dict[str, Any]):
        self.lease_id = row["lease_id"]
        self.approval_reference = row.get("approval_reference")
        self.session_id = row["session_id"]
        self.confirmed_worktree = row.get("confirmed_worktree")
        self.approved_folder = row.get("approved_folder")
        self.approved_at = row.get("approved_at")
        self.expires_at = row.get("expires_at")
        self.status = row.get("status", LEASE_ACTIVE)

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


class RequestRecord:
    """A single request row (a structured Write-Gate request)."""

    __slots__ = (
        "request_id", "session_id", "confirmed_worktree", "narrowest_folder",
        "recovery_location", "stated_outcome", "status", "created_at",
        "decision", "decision_at",
    )

    def __init__(self, row: Dict[str, Any]):
        self.request_id = row["request_id"]
        self.session_id = row["session_id"]
        self.confirmed_worktree = row.get("confirmed_worktree")
        self.narrowest_folder = row.get("narrowest_folder")
        self.recovery_location = row.get("recovery_location")
        self.stated_outcome = row.get("stated_outcome")
        self.status = row.get("status", "pending")
        self.created_at = row.get("created_at")
        self.decision = row.get("decision")
        self.decision_at = row.get("decision_at")

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS write_gate_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS write_gate_bindings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    project           TEXT,
    initiative        TEXT,
    board             TEXT,
    worktree_path     TEXT,
    git_branch        TEXT,
    profile           TEXT,
    producer          TEXT,
    event             TEXT,
    parent_binding_id INTEGER,
    parent_session_id TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    lineage           TEXT,
    created_at        TEXT NOT NULL,
    confirmed_at      TEXT,
    superseded_by_id  INTEGER,
    FOREIGN KEY (parent_binding_id) REFERENCES write_gate_bindings(id),
    FOREIGN KEY (superseded_by_id) REFERENCES write_gate_bindings(id)
);
-- One ACTIVE binding per session. Superseded/abandoned rows are retained for a
-- coherent supersession history, so the uniqueness is a *filtered* index, not
-- a table-level UNIQUE constraint (which would reject re-anchoring a bound
-- session). A session may have many historical rows, exactly one active.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bindings_session_active
    ON write_gate_bindings(session_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_bindings_session ON write_gate_bindings(session_id);
CREATE INDEX IF NOT EXISTS idx_bindings_status  ON write_gate_bindings(status);
CREATE INDEX IF NOT EXISTS idx_bindings_parent  ON write_gate_bindings(parent_session_id);

CREATE TABLE IF NOT EXISTS write_gate_requests (
    request_id          TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL,
    confirmed_worktree  TEXT,
    narrowest_folder    TEXT,
    recovery_location   TEXT,
    stated_outcome      TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    decision            TEXT,
    decision_at         TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_session ON write_gate_requests(session_id);

CREATE TABLE IF NOT EXISTS write_gate_leases (
    lease_id           TEXT PRIMARY KEY,
    approval_reference TEXT,
    session_id         TEXT NOT NULL,
    confirmed_worktree TEXT,
    approved_folder    TEXT,
    approved_at        TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active'
    -- NOTE: intentionally NO foreign key to write_gate_bindings(session_id).
    -- bindings.session_id is not unique (supersession history retains one row
    -- per binding but many rows per session), so it cannot be a FK parent key
    -- in SQLite. Leases match session_id + confirmed_worktree + folder/scope
    -- + expiry at the application layer (see enforcement._matching_lease); the
    -- index below preserves lookup performance.
);
CREATE INDEX IF NOT EXISTS idx_leases_session ON write_gate_leases(session_id);
CREATE INDEX IF NOT EXISTS idx_leases_active  ON write_gate_leases(status, expires_at);
"""


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat()


def add_iso(a: str, seconds: int) -> str:
    """Add ``seconds`` to an ISO timestamp string ``a`` and return ISO."""
    dt = datetime.fromisoformat(a)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Registry:
    """A handle to the central WriteGate registry database.

    A single process-wide connection is shared (SQLite in WAL mode tolerates
    the dispatcher / gateway / worker processes opening the same file).  Each
    public method opens a short transaction and returns promptly.
    """

    def __init__(self, path: "os.PathLike[str] | str"):
        self.path = os.fspath(path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_parent_dir()
        self._open()
        self.migrate()

    # -- lifecycle ----------------------------------------------------------
    def _ensure_parent_dir(self) -> None:
        parent = os.path.dirname(self.path)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, mode=0o700, exist_ok=True)
                # Do not weaken an existing directory's mode.
            except OSError:
                pass
        # The directory that holds the DB must be user-only too.
        if parent and os.path.isdir(parent):
            self._chmod_strict(parent)

    def _open(self) -> None:
        try:
            conn = sqlite3.connect(
                self.path,
                timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000.0,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise RegistryError(f"WriteGate: cannot open registry {self.path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
        # A strict busy_timeout means a contended writer *waits* rather than
        # failing; combined with the outer try/except, a hard failure still
        # surfaces as RegistryError (fail closed).
        self._conn = conn

    def _chmod_strict(self, path: str) -> None:
        try:
            st = os.stat(path)
            current = stat.S_IMODE(st.st_mode)
            # Only tighten; never loosen an existing, more-permissive mode.
            if current & 0o077:
                os.chmod(path, 0o700)
        except OSError:
            pass

    def _chmod_db_strict(self) -> None:
        try:
            st = os.stat(self.path)
            current = stat.S_IMODE(st.st_mode)
            if current & 0o077:
                os.chmod(self.path, 0o600)
        except OSError:
            pass

    def migrate(self) -> None:
        """Idempotently create the schema and record the schema version.

        Fail-closed: any DDL error raises :class:`RegistryError`.
        """
        if self._conn is None:
            raise RegistryError("WriteGate: registry connection is not open")
        try:
            with self._lock:
                # Primus already shipped a `write-gate.db` from the prior
                # runtime whose `write_gate_bindings` table could be missing
                # canonical columns (parent_session_id, lineage, confirmed_at,
                # superseded_by_id) AND carried a hard table-level UNIQUE
                # constraint on session_id. ``executescript(_SCHEMA)`` uses
                # CREATE TABLE IF NOT EXISTS, which will NOT recreate a table
                # that already exists but is missing columns — so detect and
                # rebuild a legacy table *before* the schema script runs.
                self._migrate_legacy_table()
                # A prior runtime's ``write_gate_leases`` carried a foreign
                # key to ``write_gate_bindings(session_id)``. bindings.session_id
                # is intentionally non-unique (supersession history), so SQLite
                # rejects that parent key. Detect and rebuild the leases table
                # to drop the invalid FK, preserving rows. ``executescript``
                # would not rebuild an existing table, so this runs before it.
                self._migrate_leases_fk()
                # ``executescript`` issues its own implicit COMMIT, so it must
                # NOT run inside the ``with self._conn:`` transaction context
                # (that context would then try to commit a transaction that
                # ``executescript`` already ended, corrupting the bind state
                # of the following statement).  We hold ``self._lock`` only.
                self._conn.executescript(_SCHEMA)
                # The filtered active-binding index now exists on the rebuilt
                # table. Idempotent: a no-op once the autoindex is gone.
                self._migrate_uniqueness()
                self._conn.execute(
                    "INSERT OR REPLACE INTO write_gate_meta (key, value) "
                    "VALUES ('schema_version', ?)",
                    (str(REGISTRY_SCHEMA_VERSION),),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO write_gate_meta (key, value) "
                    "VALUES ('role', 'write-gate-central-registry')",
                )
                self._conn.commit()
            self._chmod_db_strict()
        except sqlite3.Error as exc:
            raise RegistryError(f"WriteGate: migration failed: {exc}") from exc

    def _table_columns(self, table: str) -> set[str]:
        if self._conn is None:
            return set()
        return {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def _migrate_legacy_table(self) -> None:
        """Rebuild ``write_gate_bindings`` if it predates the canonical schema.

        The prior runtime created ``write_gate_bindings`` with a hard
        table-level ``UNIQUE`` on ``session_id`` and the same column set as
        the canonical schema. ``CREATE TABLE IF NOT EXISTS`` will not fix a
        table that already exists, so detect that prior schema (by the
        presence of its ``sqlite_autoindex_write_gate_bindings_1``) and rebuild
        the table without the hard constraint, preserving the known fields
        (``session_id``, ``project``, ``initiative``, ``board``,
        ``worktree_path``, ``git_branch``, ``profile``, ``producer``,
        ``event``, ``status``, ``lineage``, ``created_at``). Any recovery
        evidence lives on disk, not in the table, so it is preserved.

        Fail closed on unexpected drift: if the table exists but does not carry
        the known prior autoindex, or is missing a column we must preserve,
        raise :class:`RegistryError` and require operator recovery rather than
        silently guessing at an unknown schema. Idempotent: a no-op once the
        autoindex is gone.
        """
        if self._conn is None:
            return
        existing = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        if "sqlite_autoindex_write_gate_bindings_1" not in existing:
            return  # Already on the canonical schema.

        have = self._table_columns("write_gate_bindings")
        # Every column we must preserve has to exist on the prior table.
        required_preserve = {
            "session_id", "project", "initiative", "board", "worktree_path",
            "git_branch", "profile", "producer", "event",
            "status", "lineage", "created_at",
        }
        missing = required_preserve - have
        if missing:
            raise RegistryError(
                "WriteGate: cannot migrate write_gate_bindings — prior "
                f"schema is missing expected column(s) {sorted(missing)}; "
                "operator recovery required."
            )

        # Map known prior columns to their canonical names.
        field_map = {
            "session_id": "session_id",
            "project": "project",
            "initiative": "initiative",
            "board": "board",
            "worktree_path": "worktree_path",
            "git_branch": "git_branch",
            "profile": "profile",
            "producer": "producer",
            "event": "event",
            "status": "status",
            "lineage": "lineage",
            "created_at": "created_at",
        }
        rows = self._conn.execute("SELECT * FROM write_gate_bindings").fetchall()

        # Capture the legacy column names BEFORE we drop the table.
        legacy_cols = [c["name"] for c in self._conn.execute(
            "PRAGMA table_info(write_gate_bindings)"
        )]
        legacy_map = dict(zip(legacy_cols, range(len(legacy_cols))))

        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute("DROP TABLE write_gate_bindings")
            self._conn.execute(
                """
                CREATE TABLE write_gate_bindings (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id        TEXT NOT NULL,
                    project           TEXT,
                    initiative        TEXT,
                    board             TEXT,
                    worktree_path     TEXT,
                    git_branch        TEXT,
                    profile           TEXT,
                    producer          TEXT,
                    event             TEXT,
                    parent_binding_id INTEGER,
                    parent_session_id TEXT,
                    status            TEXT NOT NULL DEFAULT 'active',
                    lineage           TEXT,
                    created_at        TEXT NOT NULL,
                    confirmed_at      TEXT,
                    superseded_by_id  INTEGER,
                    FOREIGN KEY (parent_binding_id) REFERENCES write_gate_bindings(id),
                    FOREIGN KEY (superseded_by_id) REFERENCES write_gate_bindings(id)
                )
                """
            )
            canonical_cols = list(field_map.values())
            placeholder = ",".join("?" * len(canonical_cols))
            col_clause = ",".join(canonical_cols)
            for row in rows:
                values = []
                for c in field_map.values():
                    if c in legacy_map:
                        values.append(row[legacy_map[c]])
                    else:
                        # A field we must preserve is absent from the prior
                        # table — fail closed rather than insert NULL.
                        raise RegistryError(
                            f"WriteGate: prior write_gate_bindings is missing "
                            f"column {c!r}; operator recovery required."
                        )
                self._conn.execute(
                    f"INSERT INTO write_gate_bindings ({col_clause}) VALUES ({placeholder})",
                    values,
                )
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_leases_fk(self) -> None:
        """Drop the invalid FK on ``write_gate_leases`` if the prior runtime
        declared one.

        A prior runtime declared ``FOREIGN KEY (session_id) REFERENCES
        write_gate_bindings(session_id)``. ``bindings.session_id`` is
        intentionally non-unique (supersession history retains one row per
        binding but many rows per session), so SQLite rejects it as a FK parent
        key — every write to the leases table would fail with
        ``foreign key mismatch``. Rebuild the table without the FK, preserving
        rows; leases match ``session_id`` + ``confirmed_worktree`` +
        folder/scope + expiry at the application layer instead. Idempotent.
        """
        if self._conn is None:
            return
        existing = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "write_gate_leases" not in existing:
            return  # Will be created by executescript(_SCHEMA).

        # Detect the invalid FK in the existing table's SQL.
        sql = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'write_gate_leases'"
        ).fetchone()
        if sql is None or "FOREIGN KEY" not in sql[0].upper():
            return  # Already on the canonical schema.

        rows = self._conn.execute("SELECT * FROM write_gate_leases").fetchall()
        legacy_cols = [c["name"] for c in self._conn.execute(
            "PRAGMA table_info(write_gate_leases)"
        )]

        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute("DROP TABLE write_gate_leases")
            self._conn.execute(
                """
                CREATE TABLE write_gate_leases (
                    lease_id           TEXT PRIMARY KEY,
                    approval_reference TEXT,
                    session_id         TEXT NOT NULL,
                    confirmed_worktree TEXT,
                    approved_folder    TEXT,
                    approved_at        TEXT NOT NULL,
                    expires_at         TEXT NOT NULL,
                    status             TEXT NOT NULL DEFAULT 'active'
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leases_session "
                "ON write_gate_leases(session_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leases_active "
                "ON write_gate_leases(status, expires_at)"
            )
            for row in rows:
                values = [row[legacy_cols[i]] for i in range(len(legacy_cols))]
                self._conn.execute(
                    f"INSERT INTO write_gate_leases "
                    f"({','.join(legacy_cols)}) VALUES ({','.join('?' * len(legacy_cols))})",
                    values,
                )
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_uniqueness(self) -> None:
        """Drop a hard UNIQUE(session_id) autoindex if the prior runtime set it.

        The autoindex is a no-op target once the table has been rebuilt by
        :meth:`_migrate_legacy_table` (which already produces the canonical
        table without a hard constraint). Kept here for the defensive path
        where a table already had the full column set but retained the
        autoindex. Idempotent.
        """
        existing = {
            row["name"]
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        if "sqlite_autoindex_write_gate_bindings_1" not in existing:
            # Already on the filtered-index schema (fresh DB or prior migrate).
            return

        conn = self._conn
        if conn is None:
            return
        # Snapshot the current rows so we can restore them after the rebuild.
        rows = conn.execute("SELECT * FROM write_gate_bindings").fetchall()

        # Preserve FK integrity during the swap: disable then re-enable.
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("DROP TABLE write_gate_bindings")
            conn.execute(
                """
                CREATE TABLE write_gate_bindings (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id        TEXT NOT NULL,
                    project           TEXT,
                    initiative        TEXT,
                    board             TEXT,
                    worktree_path     TEXT,
                    git_branch        TEXT,
                    profile           TEXT,
                    producer          TEXT,
                    event             TEXT,
                    parent_binding_id INTEGER,
                    parent_session_id TEXT,
                    status            TEXT NOT NULL DEFAULT 'active',
                    lineage           TEXT,
                    created_at        TEXT NOT NULL,
                    confirmed_at      TEXT,
                    superseded_by_id  INTEGER,
                    FOREIGN KEY (parent_binding_id) REFERENCES write_gate_bindings(id),
                    FOREIGN KEY (superseded_by_id) REFERENCES write_gate_bindings(id)
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_bindings_session_active
                    ON write_gate_bindings(session_id) WHERE status = 'active'
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bindings_session "
                "ON write_gate_bindings(session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bindings_status "
                "ON write_gate_bindings(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bindings_parent "
                "ON write_gate_bindings(parent_session_id)"
            )
            for row in rows:
                conn.execute(
                    """INSERT INTO write_gate_bindings
                       (id, session_id, project, initiative, board, worktree_path,
                        git_branch, profile, producer, event, parent_binding_id,
                        parent_session_id, status, lineage, created_at,
                        confirmed_at, superseded_by_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(row),
                )
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._conn = None

    # -- low-level helpers --------------------------------------------------
    def _tx(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RegistryError("WriteGate: registry connection is not open")
        return self._conn

    # -- bindings -----------------------------------------------------------
    def create_binding(
        self,
        *,
        session_id: str,
        worktree_path: str,
        project: Optional[str] = None,
        initiative: Optional[str] = None,
        board: Optional[str] = None,
        git_branch: Optional[str] = None,
        profile: Optional[str] = None,
        producer: str = PRODUCER_CONFIRM_BINDING,
        event: str = EVENT_CONFIRM,
        parent_session_id: Optional[str] = None,
        parent_binding_id: Optional[int] = None,
        lineage: Optional[Dict[str, Any]] = None,
    ) -> BindingRecord:
        """Insert a new binding row and mark any prior active binding for the
        same session ``superseded`` (one active binding per session).

        Returns the new :class:`BindingRecord`.
        """
        now = utcnow_iso()
        conn = self._tx()
        try:
            with self._lock, conn:
                # Find the current active binding for this session so we can
                # record a coherent supersession history.
                cur = conn.execute(
                    "SELECT id FROM write_gate_bindings "
                    "WHERE session_id = ? AND status = 'active'",
                    (session_id,),
                )
                row = cur.fetchone()
                old_id = row["id"] if row is not None else None
                # Mark the prior active row superseded first (so the filtered
                # active-binding unique index frees up its session_id), then
                # insert the replacement. ``superseded_by_id`` reads "this row
                # was superseded by <this id>", so it lives on the PRIOR row
                # and points forward to the replacement's id.
                if old_id is not None:
                    conn.execute(
                        """UPDATE write_gate_bindings
                           SET status = ?, superseded_by_id = ? WHERE id = ?""",
                        (BINDING_SUPERSEDED, None, old_id),
                    )
                cur = conn.execute(
                    """INSERT INTO write_gate_bindings (
                         session_id, project, initiative, board, worktree_path,
                         git_branch, profile, producer, event,
                         parent_binding_id, parent_session_id, status,
                         lineage, created_at, confirmed_at, superseded_by_id
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id, project, initiative, board, worktree_path,
                        git_branch, profile, producer, event,
                        parent_binding_id, parent_session_id, BINDING_ACTIVE,
                        _dump_lineage(lineage), now, now, None,
                    ),
                )
                new_id = cur.lastrowid
                if old_id is not None:
                    # Point the prior row's superseded_by_id at the new row.
                    conn.execute(
                        """UPDATE write_gate_bindings
                           SET superseded_by_id = ? WHERE id = ?""",
                        (new_id, old_id),
                    )
                rec = self.get_binding_by_id(new_id)
                if rec is None:
                    raise RegistryError("WriteGate: failed to read back binding")
                _clear_enforcement_cache()
                return rec
        except sqlite3.Error as exc:
            raise RegistryError(f"WriteGate: create_binding failed: {exc}") from exc

    def get_binding_by_id(self, binding_id: int) -> Optional[BindingRecord]:
        conn = self._tx()
        with self._lock:
            try:
                row = conn.execute(
                    "SELECT * FROM write_gate_bindings WHERE id = ?",
                    (binding_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise RegistryError(f"WriteGate: get_binding_by_id failed: {exc}") from exc
        return BindingRecord(dict(row)) if row else None

    def get_active_binding(self, session_id: str) -> Optional[BindingRecord]:
        """Return the active binding for ``session_id``, or ``None``."""
        conn = self._tx()
        with self._lock:
            try:
                row = conn.execute(
                    "SELECT * FROM write_gate_bindings "
                    "WHERE session_id = ? AND status = 'active' "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise RegistryError(f"WriteGate: get_active_binding failed: {exc}") from exc
        return BindingRecord(dict(row)) if row else None

    def list_bindings(self, session_id: str) -> List[BindingRecord]:
        conn = self._tx()
        with self._lock:
            try:
                rows = conn.execute(
                    "SELECT * FROM write_gate_bindings WHERE session_id = ? "
                    "ORDER BY id",
                    (session_id,),
                ).fetchall()
            except sqlite3.Error as exc:
                raise RegistryError(f"WriteGate: list_bindings failed: {exc}") from exc
        return [BindingRecord(dict(r)) for r in rows]

    def mark_binding_abandoned(self, session_id: str) -> None:
        """Mark all active bindings for ``session_id`` as ``abandoned``.

        Used after a dispatcher spawn failure so a failed/abandoned dispatch
        never appears as an active worker authority.
        """
        conn = self._tx()
        with self._lock, conn:
            try:
                conn.execute(
                    "UPDATE write_gate_bindings SET status = ? "
                    "WHERE session_id = ? AND status = 'active'",
                    (BINDING_ABANDONED, session_id),
                )
            except sqlite3.Error as exc:
                raise RegistryError(
                    f"WriteGate: mark_binding_abandoned failed: {exc}"
                ) from exc

    # -- requests -----------------------------------------------------------
    def create_request(
        self,
        *,
        request_id: str,
        session_id: str,
        confirmed_worktree: str,
        narrowest_folder: str,
        recovery_location: str,
        stated_outcome: str,
    ) -> RequestRecord:
        now = utcnow_iso()
        conn = self._tx()
        try:
            with self._lock, conn:
                conn.execute(
                    """INSERT INTO write_gate_requests (
                         request_id, session_id, confirmed_worktree,
                         narrowest_folder, recovery_location, stated_outcome,
                         status, created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        request_id, session_id, confirmed_worktree,
                        narrowest_folder, recovery_location, stated_outcome,
                        "pending", now,
                    ),
                )
                rec = self.get_request(request_id)
                if rec is None:
                    raise RegistryError("WriteGate: failed to read back request")
                _clear_enforcement_cache()
                return rec
        except sqlite3.Error as exc:
            raise RegistryError(f"WriteGate: create_request failed: {exc}") from exc

    def get_request(self, request_id: str) -> Optional[RequestRecord]:
        conn = self._tx()
        with self._lock:
            try:
                row = conn.execute(
                    "SELECT * FROM write_gate_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise RegistryError(f"WriteGate: get_request failed: {exc}") from exc
        return RequestRecord(dict(row)) if row else None

    def decide_request(self, request_id: str, decision: str) -> Optional[RequestRecord]:
        """Record a human ``once``/``deny`` decision timestamp on a request."""
        now = utcnow_iso()
        conn = self._tx()
        try:
            with self._lock, conn:
                conn.execute(
                    "UPDATE write_gate_requests SET decision = ?, decision_at = ? "
                    "WHERE request_id = ?",
                    (decision, now, request_id),
                )
            return self.get_request(request_id)
        except sqlite3.Error as exc:
            raise RegistryError(f"WriteGate: decide_request failed: {exc}") from exc

    # -- leases -------------------------------------------------------------
    def create_lease(
        self,
        *,
        lease_id: str,
        approval_reference: Optional[str],
        session_id: str,
        confirmed_worktree: str,
        approved_folder: str,
        approved_at: Optional[str] = None,
    ) -> LeaseRecord:
        """Create a lease that expires exactly five minutes after approval.

        The lease is created with status ``active`` and never auto-renews.
        Approval creates the lease only; it does not execute the mutation.
        """
        approved_at = approved_at or utcnow_iso()
        expires_at = add_iso(approved_at, LEASE_DURATION_SECONDS)
        conn = self._tx()
        try:
            with self._lock, conn:
                conn.execute(
                    """INSERT INTO write_gate_leases (
                         lease_id, approval_reference, session_id,
                         confirmed_worktree, approved_folder, approved_at,
                         expires_at, status
                       ) VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        lease_id, approval_reference, session_id,
                        confirmed_worktree, approved_folder, approved_at,
                        expires_at, LEASE_ACTIVE,
                    ),
                )
                rec = self.get_lease(lease_id)
                if rec is None:
                    raise RegistryError("WriteGate: failed to read back lease")
                _clear_enforcement_cache()
                return rec
        except sqlite3.Error as exc:
            raise RegistryError(f"WriteGate: create_lease failed: {exc}") from exc

    def get_lease(self, lease_id: str) -> Optional[LeaseRecord]:
        conn = self._tx()
        with self._lock:
            try:
                row = conn.execute(
                    "SELECT * FROM write_gate_leases WHERE lease_id = ?",
                    (lease_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise RegistryError(f"WriteGate: get_lease failed: {exc}") from exc
        return LeaseRecord(dict(row)) if row else None

    def get_active_lease_for(self, *, lease_id: Optional[str] = None,
                             session_id: Optional[str] = None,
                             now_iso: Optional[str] = None) -> Optional[LeaseRecord]:
        """Return a lease only if it is ``active`` and not past ``expires_at``.

        This is the authoritative lease check: a lease that is expired by time
        or by status is never returned, so the enforcement hook fails closed.
        """
        if not lease_id:
            return None
        conn = self._tx()
        with self._lock:
            try:
                params: List[Any] = [lease_id]
                sql = "SELECT * FROM write_gate_leases WHERE lease_id = ?"
                if session_id:
                    sql += " AND session_id = ?"
                    params.append(session_id)
                row = conn.execute(sql, params).fetchone()
            except sqlite3.Error as exc:
                raise RegistryError(f"WriteGate: get_active_lease failed: {exc}") from exc
        if not row:
            return None
        lease = LeaseRecord(dict(row))
        # Honour an explicit status filter: only an 'active' lease qualifies.
        if lease.status != LEASE_ACTIVE:
            return None
        now = parse_iso(now_iso) or datetime.now(timezone.utc)
        expires = parse_iso(lease.expires_at)
        if expires is not None and now >= expires:
            return None
        return lease

    def list_active_leases(self, session_id: Optional[str] = None) -> List[LeaseRecord]:
        conn = self._tx()
        with self._lock:
            try:
                if session_id:
                    rows = conn.execute(
                        "SELECT * FROM write_gate_leases "
                        "WHERE status = ? AND session_id = ? "
                        "ORDER BY approved_at DESC",
                        (LEASE_ACTIVE, session_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM write_gate_leases "
                        "WHERE status = ? ORDER BY approved_at DESC",
                        (LEASE_ACTIVE,),
                    ).fetchall()
            except sqlite3.Error as exc:
                raise RegistryError(f"WriteGate: list_active_leases failed: {exc}") from exc
        now_iso = utcnow_iso()
        out: List[LeaseRecord] = []
        for r in rows:
            rec = LeaseRecord(dict(r))
            if self._lease_is_live(rec, now_iso):
                out.append(rec)
        return out

    @staticmethod
    def _lease_is_live(lease: LeaseRecord, now_iso: str) -> bool:
        if lease.status != LEASE_ACTIVE:
            return False
        expires = parse_iso(lease.expires_at)
        now = parse_iso(now_iso)
        if expires is None or now is None:
            return False
        return now < expires


def _dump_lineage(lineage: Optional[Dict[str, Any]]) -> Optional[str]:
    if not lineage:
        return None
    import json
    try:
        return json.dumps(lineage, sort_keys=True)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Module-level default-root resolution + singleton
# ---------------------------------------------------------------------------

_default_registry: Optional[Registry] = None
_default_registry_lock = threading.Lock()


def resolve_registry_path() -> str:
    """Resolve ``get_default_hermes_root() / 'write-gate.db'``.

    Imported lazily so this module stays importable (and unit-testable) in
    environments where the full Hermes constants path is not on the path.
    """
    from hermes_constants import get_default_hermes_root
    return os.path.join(str(get_default_hermes_root()), "write-gate.db")


def get_registry() -> Registry:
    """Return a process-wide :class:`Registry` at the default-root path.

    The first call creates it (which runs migration); subsequent calls return
    the cached instance.  Tests may override the path via
    :func:`set_registry_for_path`.
    """
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = Registry(resolve_registry_path())
        return _default_registry


def set_registry_for_path(path: "os.PathLike[str] | str") -> Registry:
    """(Re)install the process-wide registry at ``path`` (test hook)."""
    global _default_registry
    with _default_registry_lock:
        if _default_registry is not None:
            _default_registry.close()
        _default_registry = Registry(os.fspath(path))
        return _default_registry


def reset_registry() -> None:
    """Drop the cached registry (test teardown)."""
    global _default_registry
    with _default_registry_lock:
        if _default_registry is not None:
            _default_registry.close()
        _default_registry = None
