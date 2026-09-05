"""adrian-kanban foundational schema (S1).

The machine-global SQLite authority schema. S1 adds foundational tables for:

* unified cards (initiative + task identity),
* initiative transitions (monotonic IDs + predecessor integrity),
* segment-manifest projection,
* segment workspaces, and
* workspace members.

These tables are **non-public** persistence primitives. They do NOT implement
S2 lifecycle admission, transition policy, workspace operations, dispatcher
behavior, or Write-Gate consumption. They exist so the S1 store can create and
validate disposable foundational state (brief §4.5 / §5.3).

The schema is additive: ``CREATE TABLE IF NOT EXISTS`` so re-init is idempotent
and never drops or rewrites existing native Kanban tables.
"""

from __future__ import annotations

# Foundational schema. Kept intentionally small and additive.
#
# Note on uniqueness: SQLite only allows a single table-level UNIQUE constraint
# per column list. A initiative card (task_id NULL) must still be unique by
# initiative_id alone, and a task card (task_id NOT NULL) must be unique by the
# task_id value globally. Two partial unique indexes with an
# ``WHERE`` clause express exactly that: the initiative uniqueness applies only
# to NULL-task rows, the task uniqueness only to non-null-task rows (global on
# task_id alone).
SCHEMA_SQL = """
-- Unified card: one row per initiative or task card. A card is an initiative
-- when ``task_id`` is NULL; it is a task when ``task_id`` is NOT NULL.
-- Initiative identity is always required and unique across the board. A task
-- card additionally requires a unique task identity within its initiative,
-- so the (initiative_id, task_id) pair is globally unique and never NULL.
CREATE TABLE IF NOT EXISTS adrian_kanban_cards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    card_type       TEXT NOT NULL,            -- 'initiative' | 'task'
    initiative_id   TEXT NOT NULL,            -- unified initiative identity
    task_id         TEXT,                     -- nullable; NULL for initiatives
    title           TEXT NOT NULL,
    created_at      INTEGER NOT NULL
);

-- Unique initiative-card identity: at most one row per initiative_id among the
-- initiative cards (those with a NULL task_id).
CREATE UNIQUE INDEX IF NOT EXISTS uq_adrian_kanban_cards_initiative
    ON adrian_kanban_cards (initiative_id)
    WHERE task_id IS NULL;

-- Global non-null task identity: at most one task card per task_id across the
-- whole board. A task card is any row whose task_id is NOT NULL, so the index
-- is global on task_id alone (no WHERE clause) and never scoped to a single
-- initiative.
CREATE UNIQUE INDEX IF NOT EXISTS uq_adrian_kanban_cards_task
    ON adrian_kanban_cards (task_id)
    WHERE task_id IS NOT NULL;

-- Initiative transition log. ``transition_id`` is the plugin-owned monotonic
-- identifier; it starts at the first transition (no ``previous_transition_id``
-- seed) and every later row names the current accepted predecessor transition.
-- S1 establishes the structural row shape only; S2 owns admission policy.
-- Segment fields, from_phase, and repository_reconciliation_ref may be null
-- because initialization and non-segment phases permit that.
CREATE TABLE IF NOT EXISTS adrian_kanban_initiative_transitions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    initiative_id         TEXT NOT NULL,
    previous_transition_id INTEGER,           -- accepted predecessor transition (nullable on first)
    transition_id         INTEGER NOT NULL,
    from_phase            TEXT,                -- nullable: initialization / non-segment phases
    from_segment_id       TEXT,                -- nullable
    to_phase              TEXT NOT NULL,
    to_segment_id         TEXT,                -- nullable
    canon_route           TEXT,                -- nullable
    repository_reconciliation_ref TEXT,        -- nullable
    trigger               TEXT,                -- nullable
    actor_evidence        TEXT,                -- nullable
    canonical_payload     TEXT,                -- nullable
    rendered_history_ref  TEXT,                -- nullable
    created_at            INTEGER NOT NULL,
    UNIQUE(initiative_id, transition_id),
    FOREIGN KEY (initiative_id) REFERENCES adrian_kanban_cards(initiative_id)
);

-- Segment-manifest projection: the projected view of segment membership.
CREATE TABLE IF NOT EXISTS adrian_kanban_segment_manifest (
    segment_id          TEXT NOT NULL,
    initiative_id       TEXT NOT NULL,
    manifest_path       TEXT,                 -- projected manifest path
    manifest_sha        TEXT,                 -- full commit SHA of the manifest
    digest              TEXT,                 -- content digest
    readiness           TEXT,                 -- projected readiness
    validation          TEXT,                 -- projected validation state
    projected_at        INTEGER NOT NULL,
    PRIMARY KEY (segment_id, initiative_id)
);

-- Segment workspaces: binding an initiative + segment to a manifest, lifecycle,
-- and controller.
CREATE TABLE IF NOT EXISTS adrian_kanban_segment_workspaces (
    workspace_id    TEXT PRIMARY KEY,
    segment_id      TEXT NOT NULL,
    initiative_id   TEXT NOT NULL,
    segment         TEXT,
    manifest        TEXT,
    lifecycle       TEXT,
    controller      TEXT,
    path            TEXT,
    kind            TEXT NOT NULL DEFAULT 'scratch',
    created_at      INTEGER NOT NULL
);

-- Workspace members: repository, relative path, branch, base SHA, head, state.
CREATE TABLE IF NOT EXISTS adrian_kanban_workspace_members (
    workspace_id    TEXT NOT NULL,
    member_id       TEXT NOT NULL,
    repository      TEXT,
    relative_path   TEXT,
    branch          TEXT,
    base_sha        TEXT,
    head            TEXT,
    state           TEXT,
    joined_at       INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, member_id)
);
"""


def create_schema(conn: object) -> None:
    """Apply the foundational schema (idempotent)."""
    conn.executescript(SCHEMA_SQL)
