"""adrian-kanban foundational SQLite store (S1).

The machine-global store. :class:`AdmittedStore` resolves a single, absolute,
machine-global database path (brief §5.4 / ratified design §10.2), creates the
foundational schema, and offers the minimal persistence primitives S1 needs to
prove the schema invariants:

* :meth:`AdmittedStore.initiative_card` — create an initiative card (requires
  initiative identity).
* :meth:`AdmittedStore.record_transition` — record a transition on an
  initiative, enforcing strictly-increasing transition IDs and predecessor
  integrity.

These are internal persistence primitives. The store does NOT implement S2
lifecycle admission, transition policy, workspace operations, dispatcher
behavior, or Write-Gate consumption. It uses disposable databases only.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as _kb

from . import schema as _schema
from .validation import (
    InitiativeIdentityError,
    SessionStartupRecordMissingError,
    SessionStartupRevisionError,
    SessionStartupStateError,
    TaskIdentityError,
    TransitionIntegrityError,
    validate_held_opening_prompt,
    validate_initiative_identity,
    validate_protocol_version,
    validate_session_id,
    validate_session_startup_fields,
    validate_session_startup_creation_draft,
    validate_session_startup_state,
    validate_release_status,
    validate_release_transition,
    validate_session_startup_transition,
    validate_task_identity,
    validate_transition_id,
)


class AdmittedStore:
    """Foundational, machine-global store for disposable state."""

    def __init__(self, database_path: Optional[str] = None) -> None:
        # Resolve to a single absolute path through the public, core-owned
        # authority resolver. A missing or relative value is rejected (fail
        # closed) by the resolver, and store initialization creates the
        # parent/database for disposable use.
        self.db_path = Path(
            _kb.resolve_authority_path(override=database_path)
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        # Production-friendly pragmas, applied before schema use so the store
        # supports concurrent readers/writers (WAL), structural FK enforcement,
        # and a bounded, non-hanging wait on a held write lock.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        _schema.create_schema(self._conn)
        self._conn.commit()

    # -- initiative cards -------------------------------------------------

    def initiative_card(
        self, *, initiative_id: object, title: str
    ) -> str:
        """Create an initiative card. Requires a non-null initiative id."""
        canonical = validate_initiative_identity(initiative_id)
        now = int(time.time())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # Create the canonical initiative identity first; the card's FK
            # requires it to exist before the card row can be inserted.
            self._conn.execute(
                "INSERT INTO adrian_kanban_initiatives (initiative_id) VALUES (?)",
                (canonical,),
            )
            self._conn.execute(
                "INSERT INTO adrian_kanban_cards (card_type, initiative_id, "
                "task_id, title, created_at) VALUES (?, ?, ?, ?, ?)",
                ("initiative", canonical, None, title, now),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return canonical

    def task_card(
        self, *, initiative_id: object, task_id: object, title: str
    ) -> tuple[str, str]:
        """Create a task card. Requires BOTH initiative and task identity."""
        init_id, tid = validate_task_identity(initiative_id, task_id)
        now = int(time.time())
        try:
            self._conn.execute(
                "INSERT INTO adrian_kanban_cards (card_type, initiative_id, "
                "task_id, title, created_at) VALUES (?, ?, ?, ?, ?)",
                ("task", init_id, tid, title, now),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return init_id, tid


    # -- initiative transitions ------------------------------------------

    def record_transition(
        self,
        *,
        initiative_id: object,
        transition_id: int,
        previous_transition_id: Optional[int] = None,
        from_phase: Optional[str] = None,
        from_segment_id: Optional[str] = None,
        to_phase: Optional[str] = None,
        to_segment_id: Optional[str] = None,
        canon_route: Optional[str] = None,
        repository_reconciliation_ref: Optional[str] = None,
        trigger: Optional[str] = None,
        actor_evidence: Optional[str] = None,
        canonical_payload: Optional[str] = None,
        rendered_history_ref: Optional[str] = None,
    ) -> int:
        """Record a transition on an initiative and return its transition_id.

        Enforces the transition chain and global task identity:

        * ``previous_transition_id`` must be ``None`` only when no transition
          exists yet for the initiative; otherwise it must equal the current
          highest recorded ``transition_id`` for that initiative.
        * ``transition_id`` must be strictly greater than the current highest
          ``transition_id`` for the initiative.

        The structural row is then inserted and its ``transition_id`` returned.
        This method does NOT evaluate routes, actors, or reconciliation
        content; S2 owns admission policy.
        """
        current_identity = validate_initiative_identity(initiative_id)
        now = int(time.time())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            card = self._conn.execute(
                "SELECT id FROM adrian_kanban_cards WHERE card_type = 'initiative' "
                "AND initiative_id = ? AND task_id IS NULL",
                (current_identity,),
            ).fetchone()
            if card is None:
                raise InitiativeIdentityError(
                    f"no canonical initiative card for initiative_id {initiative_id!r}"
                )
            initiative_card_id = card["id"]
            row = self._conn.execute(
                "SELECT MAX(transition_id) AS m FROM initiative_transitions "
                "WHERE initiative_id = ?",
                (current_identity,),
            ).fetchone()
            current_max = row["m"] if row else None
            if current_max is None:
                if previous_transition_id is not None:
                    raise TransitionIntegrityError(
                        "first transition must not name a predecessor"
                    )
            elif previous_transition_id != current_max:
                raise TransitionIntegrityError(
                    f"transition predecessor {previous_transition_id!r} does not "
                    f"match current accepted transition {current_max}"
                )
            validated_id = validate_transition_id(current_max, transition_id)
            if to_phase is None:
                raise TransitionIntegrityError("transition requires a to_phase")
            self._conn.execute(
                "INSERT INTO initiative_transitions ("
                "initiative_card_id, initiative_id, previous_transition_id, "
                "transition_id, from_phase, from_segment_id, to_phase, "
                "to_segment_id, canon_route, repository_reconciliation_ref, "
                "trigger, actor_evidence, canonical_payload, "
                "rendered_history_ref, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    initiative_card_id,
                    current_identity,
                    previous_transition_id,
                    validated_id,
                    from_phase,
                    from_segment_id,
                    to_phase,
                    to_segment_id,
                    canon_route,
                    repository_reconciliation_ref,
                    trigger,
                    actor_evidence,
                    canonical_payload,
                    rendered_history_ref,
                    now,
                ),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return validated_id

    # -- session startup persistence -------------------------------------

    def create_or_read_session_startup(
        self,
        *,
        session_id: object,
        opening_prompt: object,
        protocol_version: object,
    ) -> dict:
        """Create the initial record or return the existing record unchanged."""
        canonical_session = validate_session_id(session_id)
        prompt = validate_held_opening_prompt(opening_prompt)
        protocol = validate_protocol_version(protocol_version)
        now = int(time.time())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT * FROM adrian_kanban_session_startup_records "
                "WHERE session_id = ?",
                (canonical_session,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO adrian_kanban_session_startup_records ("
                    "session_id, protocol_version, state, held_opening_prompt, "
                    "revision, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (
                        canonical_session,
                        protocol,
                        "awaiting_project_selection",
                        prompt,
                        now,
                        now,
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM adrian_kanban_session_startup_records "
                    "WHERE session_id = ?",
                    (canonical_session,),
                ).fetchone()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return dict(row)

    def set_release_status(
        self,
        *,
        session_id: object,
        expected_revision: int,
        release_status: object,
    ) -> dict:
        """Compare-and-set the release_status on a session startup record."""
        canonical_session = validate_session_id(session_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise SessionStartupRevisionError(
                "expected_revision must be a non-negative integer"
            )
        target_status = validate_release_status(release_status)
        now = int(time.time())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self._conn.execute(
                "SELECT * FROM adrian_kanban_session_startup_records "
                "WHERE session_id = ?",
                (canonical_session,),
            ).fetchone()
            if current is None:
                raise SessionStartupRecordMissingError(
                    f"no session startup record for session_id {canonical_session!r}"
                )
            if current["revision"] != expected_revision:
                raise SessionStartupRevisionError(
                    f"stale revision: expected {expected_revision}, "
                    f"current {current['revision']}"
                )
            current_status = current["release_status"]
            validate_release_transition(current_status, target_status)
            if current_status == target_status:
                self._conn.commit()
                return dict(current)
            updated = self._conn.execute(
                "UPDATE adrian_kanban_session_startup_records SET "
                "release_status = ?, revision = ?, updated_at = ? "
                "WHERE session_id = ? AND revision = ?",
                (
                    target_status,
                    expected_revision + 1,
                    now,
                    canonical_session,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise SessionStartupRevisionError(
                    "concurrent modification detected during release status update"
                )
            row = self._conn.execute(
                "SELECT * FROM adrian_kanban_session_startup_records "
                "WHERE session_id = ?",
                (canonical_session,),
            ).fetchone()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return dict(row)

    def replace_anchored_session_startup(
        self,
        *,
        session_id: object,
        expected_revision: int,
        accepted_phase: object,
        accepted_segment_id: object,
        logical_workspace_id: object,
        writegate_binding_version: object,
        writegate_binding_ref: object,
    ) -> dict:
        """Compare-and-set the five anchor fields on an anchored record."""
        canonical_session = validate_session_id(session_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise SessionStartupRevisionError(
                "expected_revision must be a non-negative integer"
            )
        validate_session_startup_fields(
            to_state="anchored",
            selected_project_id="placeholder",
            selected_initiative_id="placeholder",
            accepted_phase=accepted_phase,
            accepted_segment_id=accepted_segment_id,
            logical_workspace_id=logical_workspace_id,
            writegate_binding_version=writegate_binding_version,
            writegate_binding_ref=writegate_binding_ref,
        )
        now = int(time.time())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self._conn.execute(
                "SELECT * FROM adrian_kanban_session_startup_records "
                "WHERE session_id = ?",
                (canonical_session,),
            ).fetchone()
            if current is None:
                raise SessionStartupRecordMissingError(
                    "no session startup record for session_id "
                    f"{canonical_session!r}"
                )
            if current["revision"] != expected_revision:
                raise SessionStartupRevisionError(
                    f"stale revision: expected {expected_revision}, "
                    f"current {current['revision']}"
                )
            if current["state"] != "anchored":
                raise SessionStartupStateError(
                    f"record is not in anchored state: {current['state']!r}"
                )
            updated = self._conn.execute(
                "UPDATE adrian_kanban_session_startup_records SET "
                "accepted_phase = ?, accepted_segment_id = ?, "
                "logical_workspace_id = ?, writegate_binding_version = ?, "
                "writegate_binding_ref = ?, revision = ?, updated_at = ?, "
                "failure_detail = NULL "
                "WHERE session_id = ? AND revision = ? AND state = 'anchored'",
                (
                    accepted_phase,
                    accepted_segment_id,
                    logical_workspace_id,
                    writegate_binding_version,
                    writegate_binding_ref,
                    expected_revision + 1,
                    now,
                    canonical_session,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise SessionStartupRevisionError(
                    "concurrent modification detected during anchored replacement"
                )
            row = self._conn.execute(
                "SELECT * FROM adrian_kanban_session_startup_records "
                "WHERE session_id = ?",
                (canonical_session,),
            ).fetchone()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return dict(row)

    def read_session_startup(self, *, session_id: object) -> Optional[dict]:
        """Read the current session startup record, or ``None`` if absent."""
        canonical_session = validate_session_id(session_id)
        row = self._conn.execute(
            "SELECT * FROM adrian_kanban_session_startup_records "
            "WHERE session_id = ?",
            (canonical_session,),
        ).fetchone()
        return dict(row) if row is not None else None

    def update_session_startup_creation_draft(
        self,
        *,
        session_id: object,
        expected_revision: int,
        creation_stage: object,
        creation_draft: object,
    ) -> dict:
        """Compare-and-set the durable initiative-creation substate."""
        canonical_session = validate_session_id(session_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise SessionStartupRevisionError(
                "expected_revision must be a non-negative integer"
            )
        stage, canonical_json = validate_session_startup_creation_draft(
            creation_stage, creation_draft
        )
        now = int(time.time())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self._conn.execute(
                "SELECT * FROM adrian_kanban_session_startup_records "
                "WHERE session_id = ?",
                (canonical_session,),
            ).fetchone()
            if current is None:
                raise SessionStartupRecordMissingError(
                    f"no session startup record for session_id {canonical_session!r}"
                )
            if current["revision"] != expected_revision:
                raise SessionStartupRevisionError(
                    f"stale revision: expected {expected_revision}, "
                    f"current {current['revision']}"
                )
            if current["state"] != "awaiting_initiative_selection":
                raise SessionStartupStateError(
                    "creation draft update is permitted only while awaiting "
                    "initiative selection"
                )
            updated = self._conn.execute(
                "UPDATE adrian_kanban_session_startup_records SET "
                "creation_stage = ?, creation_draft_json = ?, "
                "revision = ?, updated_at = ? "
                "WHERE session_id = ? AND revision = ?",
                (
                    stage,
                    canonical_json,
                    expected_revision + 1,
                    now,
                    canonical_session,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise SessionStartupRevisionError(
                    "concurrent modification detected during creation draft update"
                )
            row = self._conn.execute(
                "SELECT * FROM adrian_kanban_session_startup_records "
                "WHERE session_id = ?",
                (canonical_session,),
            ).fetchone()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return dict(row)

    def transition_session_startup(
        self,
        *,
        session_id: object,
        expected_revision: int,
        to_state: object,
        selected_project_id: Optional[str] = None,
        selected_initiative_id: Optional[str] = None,
        accepted_phase: Optional[str] = None,
        accepted_segment_id: Optional[str] = None,
        logical_workspace_id: Optional[str] = None,
        writegate_binding_version: Optional[str] = None,
        writegate_binding_ref: Optional[str] = None,
        failure_detail: Optional[str] = None,
    ) -> dict:
        """Compare-and-set a validated session startup transition."""
        canonical_session = validate_session_id(session_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise SessionStartupRevisionError(
                "expected_revision must be a non-negative integer"
            )
        target_state = validate_session_startup_state(to_state)
        now = int(time.time())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self._conn.execute(
                "SELECT * FROM adrian_kanban_session_startup_records "
                "WHERE session_id = ?",
                (canonical_session,),
            ).fetchone()
            if current is None:
                raise SessionStartupRecordMissingError(
                    f"no session startup record for session_id {canonical_session!r}"
                )
            if current["revision"] != expected_revision:
                raise SessionStartupRevisionError(
                    f"stale revision: expected {expected_revision}, "
                    f"current {current['revision']}"
                )
            validate_session_startup_transition(current["state"], target_state)

            effective_project = (
                selected_project_id
                if selected_project_id is not None
                else current["selected_project_id"]
            )
            effective_initiative = (
                selected_initiative_id
                if selected_initiative_id is not None
                else current["selected_initiative_id"]
            )
            effective_phase = (
                accepted_phase
                if accepted_phase is not None
                else current["accepted_phase"]
            )
            effective_segment = (
                accepted_segment_id
                if accepted_segment_id is not None
                else current["accepted_segment_id"]
            )
            effective_workspace = (
                logical_workspace_id
                if logical_workspace_id is not None
                else current["logical_workspace_id"]
            )
            effective_binding_version = (
                writegate_binding_version
                if writegate_binding_version is not None
                else current["writegate_binding_version"]
            )
            effective_binding_ref = (
                writegate_binding_ref
                if writegate_binding_ref is not None
                else current["writegate_binding_ref"]
            )
            effective_failure = (
                failure_detail if target_state == "failed_recoverable" else None
            )

            validate_session_startup_fields(
                to_state=target_state,
                selected_project_id=effective_project,
                selected_initiative_id=effective_initiative,
                accepted_phase=effective_phase,
                accepted_segment_id=effective_segment,
                logical_workspace_id=effective_workspace,
                writegate_binding_version=effective_binding_version,
                writegate_binding_ref=effective_binding_ref,
                failure_detail=effective_failure,
            )

            updated = self._conn.execute(
                "UPDATE adrian_kanban_session_startup_records SET "
                "state = ?, revision = ?, updated_at = ?, "
                "selected_project_id = ?, selected_initiative_id = ?, "
                "accepted_phase = ?, accepted_segment_id = ?, "
                "logical_workspace_id = ?, writegate_binding_version = ?, "
                "writegate_binding_ref = ?, failure_detail = ? "
                "WHERE session_id = ? AND revision = ?",
                (
                    target_state,
                    expected_revision + 1,
                    now,
                    effective_project,
                    effective_initiative,
                    effective_phase,
                    effective_segment,
                    effective_workspace,
                    effective_binding_version,
                    effective_binding_ref,
                    effective_failure,
                    canonical_session,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise SessionStartupRevisionError(
                    "concurrent modification detected during transition"
                )
            row = self._conn.execute(
                "SELECT * FROM adrian_kanban_session_startup_records "
                "WHERE session_id = ?",
                (canonical_session,),
            ).fetchone()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return dict(row)

    def cancel_session_startup(
        self, *, session_id: object, expected_revision: int
    ) -> dict:
        """Cancel startup while preserving the held opening prompt."""
        return self.transition_session_startup(
            session_id=session_id,
            expected_revision=expected_revision,
            to_state="cancelled",
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AdmittedStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
