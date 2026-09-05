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
    TaskIdentityError,
    TransitionIntegrityError,
    validate_initiative_identity,
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

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AdmittedStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
