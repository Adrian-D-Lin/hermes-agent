"""adrian-kanban foundational schema (S1) + S2 policy/execution records.

The machine-global SQLite authority schema. S1 adds foundational tables for:

* unified cards (initiative + task identity),
* initiative transitions (monotonic IDs + predecessor integrity),
* segment-manifest projection,
* segment workspaces, and
* workspace members.

S2 adds the fixed internal policy and execution engine records:

* ``task_lifecycle_contracts`` (one-per-task immutable lifecycle contract),
* ``initiative_phase_results`` (append-only phase result chain),
* ``initiative_transitions`` (corrected S1 provisional name),
* ``initiative_segment_projections`` (corrected S1 provisional name),
* ``segment_workspaces`` (corrected S1 provisional name),
* ``segment_workspace_members`` (corrected S1 provisional name),
* ``external_operation_journal`` (append-only external-effect event sequence).

S3 adds the additive command-receipt table ``adrian_kanban_command_receipts``
that makes accepted mutations durable and idempotent. Rejections are never
stored in this table.

These tables are **non-public** persistence primitives. They exist so the S1
store can create and validate disposable foundational state and so the S2 engine
can exercise lifecycle admission, transition policy, workspace operations, and
Write-Gate consumption against disposable state (brief §4.5 / §5.3).

The schema is additive: ``CREATE TABLE IF NOT EXISTS`` so re-init is idempotent
and never drops or rewrites existing native Kanban tables. No migration, alias,
compatibility view, trigger, or side effect is introduced (brief §4.1 / §10.2).
The S2 records declare fixed record shapes, foreign keys, and append-only /
uniqueness rules; they do **not** implement lifecycle admission, transition
policy, workspace operations, dispatcher behavior, or Write-Gate consumption.
"""

from __future__ import annotations

# Foundational schema. Kept intentionally small and additive.
#
# Initiative identity is a first-class, canonical row (``adrian_kanban_initiatives``)
# that the unified cards reference. A card is an initiative when
# ``task_id`` is NULL; it is a task when ``task_id`` is NOT NULL. Cards therefore
# require a valid parent initiative identity through the FK below: an initiative
# card creates the parent, and a task card requires the parent to already exist.
#
# Note on uniqueness: SQLite only allows a single table-level UNIQUE constraint
# per column list. A initiative card (task_id NULL) must still be unique by
# initiative_id alone, and a task card (task_id NOT NULL) must be unique by the
# task_id value globally. Two partial unique indexes with an
# ``WHERE`` clause express exactly that: the initiative uniqueness applies only
# to NULL-task rows, the task uniqueness only to non-null-task rows (global on
# task_id alone).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS adrian_kanban_rejection_audit (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    boundary_json TEXT NOT NULL,
    failed_checks_json TEXT NOT NULL,
    not_evaluated_checks_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS adrian_kanban_rejection_audit_attempt_event_idx
    ON adrian_kanban_rejection_audit (attempt_id, event_id);

-- Canonical initiative identity. One row per initiative_id; this is the parent
-- the unified cards reference. It is created explicitly by the initiative-card
-- operation and must already exist before a task card can name it.
CREATE TABLE IF NOT EXISTS adrian_kanban_initiatives (
    initiative_id TEXT PRIMARY KEY
);

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
    created_at      INTEGER NOT NULL,
    board_slug      TEXT NOT NULL DEFAULT 'default',
    record_version  INTEGER NOT NULL DEFAULT 0 CHECK (record_version >= 0),
    body            TEXT,
    closed_at       INTEGER,
    -- Unified-card shape: an initiative card carries no task identity and a
    -- task card carries one; any other card_type is invalid. The CHECK pins
    -- card_type to the two valid shapes and ties each to the correct task_id
    -- nullability, so an initiative with a task id, a task without one, or an
    -- unknown card_type is rejected at the schema level.
    CHECK (
        (card_type = 'initiative' AND task_id IS NULL)
        OR
        (card_type = 'task' AND task_id IS NOT NULL)
    ),
    FOREIGN KEY (initiative_id) REFERENCES adrian_kanban_initiatives(initiative_id)
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_adrian_kanban_cards_id_initiative
    ON adrian_kanban_cards (id, initiative_id);

-- Composite parent key used by immutable task-owned metadata. ``id`` is
-- already unique; retaining ``task_id`` in the key makes accidental metadata
-- attachment to the wrong unified task identity fail at the FK boundary.
CREATE UNIQUE INDEX IF NOT EXISTS uq_adrian_kanban_cards_id_task
    ON adrian_kanban_cards (id, task_id);

-- Initiative transition log. ``transition_id`` is the plugin-owned monotonic
-- identifier; it starts at the first transition (no ``previous_transition_id``
-- seed) and every later row names the current accepted predecessor transition.
-- S1 establishes the structural row shape only; S2 owns admission policy.
-- Segment fields, from_phase, and repository_reconciliation_ref may be null
-- because initialization and non-segment phases permit that.
CREATE TABLE IF NOT EXISTS initiative_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    initiative_card_id INTEGER NOT NULL,
    initiative_id TEXT NOT NULL,
    previous_transition_id INTEGER,
    transition_id INTEGER NOT NULL,
    from_phase TEXT,
    from_segment_id TEXT,
    to_phase TEXT NOT NULL,
    to_segment_id TEXT,
    canon_route TEXT,
    repository_reconciliation_ref TEXT,
    trigger TEXT,
    actor_evidence TEXT,
    canonical_payload TEXT,
    rendered_history_ref TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (initiative_id, transition_id),
    FOREIGN KEY (initiative_card_id, initiative_id)
        REFERENCES adrian_kanban_cards (id, initiative_id)
);

-- Predecessor integrity: one predecessor cannot have two successors. A
-- non-first transition names exactly one accepted predecessor; the partial
-- unique index on (initiative_id, previous_transition_id) applies only to
-- non-null predecessors so the first transition (NULL predecessor) is exempt.
CREATE UNIQUE INDEX IF NOT EXISTS uq_initiative_transitions_predecessor
    ON initiative_transitions (initiative_id, previous_transition_id)
    WHERE previous_transition_id IS NOT NULL;

-- Segment-manifest projection: the immutable projected view of segment
-- membership. The manifest path, full commit SHA, digest, parsed
-- readiness/readiness content, and validation result are immutable per
-- (initiative_card_id, segment_id). Each projection carries its own
-- projection/version identity so history is directly addressable and a later
-- projection never overwrites an earlier (initiative, segment) row.
CREATE TABLE IF NOT EXISTS initiative_segment_projections (
    projection_id TEXT PRIMARY KEY,
    projection_version INTEGER NOT NULL,
    initiative_card_id INTEGER NOT NULL,
    initiative_id TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_sha TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    parsed_segment_definitions TEXT NOT NULL,
    readiness_refs TEXT,
    validation_result TEXT NOT NULL,
    projected_at INTEGER NOT NULL,
    FOREIGN KEY (initiative_card_id, initiative_id)
        REFERENCES adrian_kanban_cards (id, initiative_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_initiative_card_version
    ON initiative_segment_projections (initiative_card_id, projection_version);

-- Segment workspaces: binding an initiative + segment to a manifest, lifecycle,
-- and controller. Exactly one active workspace per initiative/segment is
-- enforced by the partial unique index below.
CREATE TABLE IF NOT EXISTS segment_workspaces (
    workspace_id TEXT PRIMARY KEY,
    initiative_card_id INTEGER NOT NULL,
    initiative_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    projection_id TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    controller_binding_ref TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (initiative_card_id, initiative_id)
        REFERENCES adrian_kanban_cards (id, initiative_id),
    FOREIGN KEY (projection_id)
        REFERENCES initiative_segment_projections (projection_id)
);

-- One active workspace per initiative/segment: the partial unique index applies
-- only to active rows so retired workspaces free the slot for a successor.
CREATE UNIQUE INDEX IF NOT EXISTS uq_segment_workspaces_active
    ON segment_workspaces (initiative_card_id, segment_id)
    WHERE active = 1;

-- Workspace members: repository identity, deterministic relative path, branch,
-- required base SHA, observed head, and member state. The
-- (workspace_id, member_id) pair is the primary key; relationship verification
-- is left to the policy/store layer to avoid a globally ambiguous bare
-- member-ID foreign key.
CREATE TABLE IF NOT EXISTS segment_workspace_members (
    workspace_id TEXT NOT NULL,
    repository_identity TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    required_base_sha TEXT,
    observed_head TEXT,
    member_state TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, repository_identity),
    FOREIGN KEY (workspace_id) REFERENCES segment_workspaces (workspace_id),
    CHECK (member_state IN ('planned', 'materialized', 'merged', 'retired')),
    CHECK (
        (member_state = 'planned' AND observed_head IS NULL)
        OR
        (member_state IN ('materialized', 'merged', 'retired')
            AND observed_head IS NOT NULL)
    ),
    CHECK (
        member_state = 'planned'
        OR required_base_sha IS NOT NULL
    )
);

-- Task lifecycle contract: nullable one-per-task immutable contract. A contract
-- fixes the step, required inputs, immutable execution profile, expected
-- output/handoff metadata, skill/version/hash, and exact predecessor
-- result/checkpoint. Ordinary or historical native tasks receive no inferred
-- contract; they remain diagnostically legacy until S3 migration. D1 through
-- DEV1 permit a nullable segment_id and workspace_id; a non-null workspace
-- relationship is validated by the policy/store layer.
CREATE TABLE IF NOT EXISTS task_lifecycle_contracts (
    contract_id TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    step TEXT NOT NULL,
    task_card_id INTEGER PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL,
    initiative_card_id INTEGER NOT NULL,
    initiative_id TEXT NOT NULL,
    segment_id TEXT,
    workspace_id TEXT,
    execution_profile TEXT NOT NULL,
    canonical_contract_payload TEXT NOT NULL,
    registry_hash TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    skill_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (task_card_id, initiative_id)
        REFERENCES adrian_kanban_cards (id, initiative_id),
    FOREIGN KEY (initiative_card_id, initiative_id)
        REFERENCES adrian_kanban_cards (id, initiative_id),
    FOREIGN KEY (workspace_id) REFERENCES segment_workspaces (workspace_id)
);

-- Phase result chain: append-only result ID, phase/segment/iteration, kind,
-- contract ID/version, canonical payload, accepted task/checkpoint refs, actor
-- evidence, and idempotency key. Two accepted results for the same
-- phase/segment/iteration/kind are prevented by the partial unique index.
CREATE TABLE IF NOT EXISTS initiative_phase_results (
    result_id TEXT PRIMARY KEY NOT NULL,
    initiative_card_id INTEGER NOT NULL,
    initiative_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    segment_id TEXT CHECK(segment_id IS NULL OR TRIM(segment_id) != ''),
    iteration INTEGER NOT NULL,
    result_kind TEXT NOT NULL,
    contract_id TEXT,
    contract_version TEXT,
    canonical_payload TEXT NOT NULL,
    accepted_task_refs TEXT,
    accepted_checkpoint_refs TEXT,
    actor_evidence TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
    created_at INTEGER NOT NULL,
    FOREIGN KEY (initiative_card_id, initiative_id)
        REFERENCES adrian_kanban_cards (id, initiative_id)
);

-- Accepted-result uniqueness: at most one accepted result per
-- phase/segment/iteration/kind. The partial unique index applies only to
-- accepted rows so historical/rejected results never collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_initiative_phase_results_accepted
    ON initiative_phase_results (
        initiative_card_id, phase, COALESCE(segment_id, ''), iteration, result_kind
    )
    WHERE accepted = 1;

-- External-operation journal: append-only event sequence keyed by a stable
-- operation/idempotency ID and member target. Each event records operation
-- kind, workspace/member identity, ordinal, state (prepared/verified/failed),
-- exact intended or observed Git/filesystem evidence, error/recovery
-- disposition, actor, and timestamp. A later Kanban transaction may consume
-- only verified journal evidence. Relationship verification is left to the
-- policy/store layer to avoid a globally ambiguous bare member-ID foreign key.
CREATE TABLE IF NOT EXISTS external_operation_journal (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    idempotency_id TEXT NOT NULL,
    member_target TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    operation_kind TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    repository_identity TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared', 'verified', 'failed')),
    intended_git_evidence TEXT,
    intended_filesystem_evidence TEXT,
    observed_git_evidence TEXT,
    observed_filesystem_evidence TEXT,
    error_disposition TEXT,
    recovery_disposition TEXT,
    actor_evidence TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (operation_id, member_target, ordinal),
    FOREIGN KEY (workspace_id) REFERENCES segment_workspaces (workspace_id),
    FOREIGN KEY (workspace_id, repository_identity)
        REFERENCES segment_workspace_members (workspace_id, repository_identity)
);

-- Command receipts: one row per accepted mutation, keyed by the authority-global
-- idempotency key. The row records the operation, target, request digest, and
-- the exact accepted response envelope. Its existence proves the accepted
-- mutation and response committed. Rejections are never stored here, so a
-- failed key remains eligible for retry. The primary key plus the adapter's
-- outer BEGIN IMMEDIATE transaction serialize concurrent retries safely.
CREATE TABLE IF NOT EXISTS adrian_kanban_command_receipts (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    target TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

-- Immutable task-input manifest. Historical and human-created ordinary tasks
-- may have no manifest; an admitted agent/automation source-dependent task has
-- exactly one manifest with one or more child entries.
CREATE TABLE IF NOT EXISTS task_input_manifests (
    task_card_id INTEGER PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    canonical_payload TEXT NOT NULL,
    declared_inputs_accessible INTEGER NOT NULL
        CHECK (declared_inputs_accessible IN (0, 1)),
    created_at INTEGER NOT NULL,
    UNIQUE (task_card_id, task_id),
    FOREIGN KEY (task_card_id, task_id)
        REFERENCES adrian_kanban_cards (id, task_id)
);

CREATE TABLE IF NOT EXISTS task_input_entries (
    task_card_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('git_commit', 'snapshot_attachment')),
    source_locator TEXT NOT NULL,
    context_guidance TEXT NOT NULL,
    PRIMARY KEY (task_card_id, workspace_path),
    FOREIGN KEY (task_card_id, task_id)
        REFERENCES task_input_manifests (task_card_id, task_id)
);

-- Nullable, immutable one-per-task declaration governing the same-card review
-- route. Absence means the ordinary task retains native completion behavior.
CREATE TABLE IF NOT EXISTS task_handoff_requirements (
    task_card_id INTEGER PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL CHECK (version = 1),
    execution_profile TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    canonical_payload TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (task_card_id, task_id)
        REFERENCES adrian_kanban_cards (id, task_id)
);

-- Each structurally admitted review request is one immutable candidate tied
-- to the exact execution run it ended. Rework produces a new candidate row.
CREATE TABLE IF NOT EXISTS task_candidate_handoffs (
    candidate_id TEXT PRIMARY KEY NOT NULL,
    task_card_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    execution_run_id INTEGER NOT NULL UNIQUE,
    reviewer TEXT NOT NULL,
    summary TEXT,
    metadata_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (task_card_id, candidate_id),
    FOREIGN KEY (task_card_id, task_id)
        REFERENCES adrian_kanban_cards (id, task_id),
    FOREIGN KEY (execution_run_id) REFERENCES task_runs (id)
);

-- Append-only diagnostic log for malformed governed review submissions.
-- Records only safe structural metadata and canonical field names; never
-- stores caller-submitted values.
CREATE TABLE IF NOT EXISTS task_handoff_rejections (
    rejection_id TEXT PRIMARY KEY NOT NULL,
    task_card_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    execution_run_id INTEGER NOT NULL,
    attempt_id TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (task_card_id, task_id)
        REFERENCES adrian_kanban_cards (id, task_id),
    FOREIGN KEY (execution_run_id) REFERENCES task_runs (id)
);

-- Reviewer acceptance is immutable and claim-scoped. A task can have several
-- rejected candidates but only the accepted candidate receives this verdict.
CREATE TABLE IF NOT EXISTS task_reviewer_verdicts (
    verdict_id TEXT PRIMARY KEY NOT NULL,
    task_card_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL UNIQUE,
    review_run_id INTEGER NOT NULL UNIQUE,
    reviewer TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict = 'accepted'),
    summary TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (task_card_id, task_id)
        REFERENCES adrian_kanban_cards (id, task_id),
    FOREIGN KEY (task_card_id, candidate_id)
        REFERENCES task_candidate_handoffs (task_card_id, candidate_id),
    FOREIGN KEY (review_run_id) REFERENCES task_runs (id)
);

-- Immutable gate-override proposal metadata. One row per prepared override
-- proposal, keyed by the host-owned durable human interaction (the initial
-- event/message). The proposal is immutable metadata only: the Write-Gate
-- owns prepared/approved/cancelled/consumed states via its own table, and no
-- plugin approval-state column or independent approval registry exists here.
-- A reused initial event cannot back a second proposal even under different
-- request/approval/idempotency keys.
CREATE TABLE IF NOT EXISTS gate_override_proposals (
    request_id TEXT PRIMARY KEY NOT NULL,
    approval_id TEXT NOT NULL UNIQUE,
    initial_event_id TEXT NOT NULL UNIQUE,
    initial_session_id TEXT NOT NULL,
    initial_message_id TEXT NOT NULL,
    canonical_payload TEXT NOT NULL,
    canonical_digest TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
"""


SCHEMA_SQL += """
CREATE TABLE IF NOT EXISTS task_purge_replacements (
    replacement_id TEXT PRIMARY KEY,
    initiative_card_id INTEGER NOT NULL,
    initiative_id TEXT NOT NULL,
    board_slug TEXT NOT NULL,
    predecessor_task_card_id INTEGER NOT NULL,
    predecessor_task_id TEXT NOT NULL UNIQUE,
    successor_task_card_id INTEGER NOT NULL,
    successor_task_id TEXT NOT NULL UNIQUE,
    eligibility_classification TEXT NOT NULL,
    eligibility_evidence TEXT NOT NULL,
    authorization_approval_id TEXT NOT NULL,
    requester_evidence TEXT NOT NULL,
    repository_disposition_reference TEXT NOT NULL,
    repository_disposition_action TEXT NOT NULL CHECK (repository_disposition_action IN ('retain', 'cleanup_after_commit')),
    repository_disposition_preservation_ref TEXT NOT NULL,
    transferred_relations TEXT NOT NULL,
    cleanup_required INTEGER NOT NULL CHECK (cleanup_required IN (0, 1)),
    predecessor_workspace_snapshot TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (initiative_card_id, initiative_id)
        REFERENCES adrian_kanban_cards (id, initiative_id)
);

CREATE INDEX IF NOT EXISTS idx_task_purge_replacements_initiative
    ON task_purge_replacements (initiative_card_id, initiative_id);
"""


SCHEMA_SQL += """
CREATE TABLE IF NOT EXISTS task_purge_cleanup_items (
    replacement_id TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('scratch','worktree','attachment')),
    expected_inventory TEXT NOT NULL,
    board          TEXT NOT NULL,
    predecessor_task_id TEXT NOT NULL,
    PRIMARY KEY (replacement_id, source_path),
    FOREIGN KEY (replacement_id) REFERENCES task_purge_replacements(replacement_id)
);

CREATE TABLE IF NOT EXISTS task_purge_cleanup_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    replacement_id TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('prepared','verified','failed')),
    evidence_json  TEXT,
    error          TEXT,
    created_at     INTEGER NOT NULL,
    FOREIGN KEY (replacement_id, source_path)
        REFERENCES task_purge_cleanup_items(replacement_id, source_path)
);
"""


def create_schema(conn: object) -> None:
    """Apply the foundational schema (idempotent)."""
    conn.executescript(SCHEMA_SQL)
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(adrian_kanban_cards)")
    }
    if "board_slug" not in existing:
        conn.execute(
            "ALTER TABLE adrian_kanban_cards ADD COLUMN "
            "board_slug TEXT NOT NULL DEFAULT 'default'"
        )
    if "record_version" not in existing:
        conn.execute(
            "ALTER TABLE adrian_kanban_cards ADD COLUMN "
            "record_version INTEGER NOT NULL DEFAULT 0 "
            "CHECK (record_version >= 0)"
        )
    if "body" not in existing:
        conn.execute("ALTER TABLE adrian_kanban_cards ADD COLUMN body TEXT")
    if "closed_at" not in existing:
        conn.execute("ALTER TABLE adrian_kanban_cards ADD COLUMN closed_at INTEGER")
