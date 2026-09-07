import sqlite3
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "JournalRejected",
    "JournalIntent",
    "JournalEvent",
    "ExternalOperationJournal",
]


class JournalRejected(ValueError):
    pass


def _validate_nonblank_str(value, field_name):
    if type(value) is not str or not value.strip():
        raise JournalRejected(f"{field_name} must be a nonblank string")


def _validate_optional_nonblank_str(value, field_name):
    if value is not None and (type(value) is not str or not value.strip()):
        raise JournalRejected(f"{field_name} must be None or a nonblank string")


def _validate_positive_int(value, field_name):
    if type(value) is not int or value <= 0:
        raise JournalRejected(f"{field_name} must be a positive integer")


@dataclass(frozen=True)
class JournalIntent:
    operation_id: str
    idempotency_id: str
    member_target: str
    operation_kind: str
    workspace_id: str
    repository_identity: str
    intended_git_evidence: Optional[str]
    intended_filesystem_evidence: Optional[str]
    actor_evidence: str
    created_at: int

    def __post_init__(self):
        _validate_nonblank_str(self.operation_id, "operation_id")
        _validate_nonblank_str(self.idempotency_id, "idempotency_id")
        _validate_nonblank_str(self.member_target, "member_target")
        _validate_nonblank_str(self.operation_kind, "operation_kind")
        _validate_nonblank_str(self.workspace_id, "workspace_id")
        _validate_nonblank_str(self.repository_identity, "repository_identity")
        _validate_optional_nonblank_str(
            self.intended_git_evidence, "intended_git_evidence"
        )
        _validate_optional_nonblank_str(
            self.intended_filesystem_evidence, "intended_filesystem_evidence"
        )
        _validate_nonblank_str(self.actor_evidence, "actor_evidence")
        _validate_positive_int(self.created_at, "created_at")
        if (
            self.intended_git_evidence is None
            and self.intended_filesystem_evidence is None
        ):
            raise JournalRejected("at least one intended evidence is required")


@dataclass(frozen=True)
class JournalEvent:
    event_id: int
    operation_id: str
    idempotency_id: str
    member_target: str
    ordinal: int
    operation_kind: str
    workspace_id: str
    repository_identity: str
    state: str
    intended_git_evidence: Optional[str]
    intended_filesystem_evidence: Optional[str]
    observed_git_evidence: Optional[str]
    observed_filesystem_evidence: Optional[str]
    error_disposition: Optional[str]
    recovery_disposition: Optional[str]
    actor_evidence: str
    created_at: int

    def __post_init__(self):
        _validate_positive_int(self.event_id, "event_id")
        _validate_nonblank_str(self.operation_id, "operation_id")
        _validate_nonblank_str(self.idempotency_id, "idempotency_id")
        _validate_nonblank_str(self.member_target, "member_target")
        _validate_positive_int(self.ordinal, "ordinal")
        _validate_nonblank_str(self.operation_kind, "operation_kind")
        _validate_nonblank_str(self.workspace_id, "workspace_id")
        _validate_nonblank_str(self.repository_identity, "repository_identity")
        if type(self.state) is not str or self.state not in (
            "prepared",
            "verified",
            "failed",
        ):
            raise JournalRejected("state must be prepared, verified, or failed")
        for field_name in (
            "intended_git_evidence",
            "intended_filesystem_evidence",
            "observed_git_evidence",
            "observed_filesystem_evidence",
            "error_disposition",
            "recovery_disposition",
        ):
            _validate_optional_nonblank_str(getattr(self, field_name), field_name)
        _validate_nonblank_str(self.actor_evidence, "actor_evidence")
        _validate_positive_int(self.created_at, "created_at")
        if (
            self.intended_git_evidence is None
            and self.intended_filesystem_evidence is None
        ):
            raise JournalRejected("at least one intended evidence is required")


class ExternalOperationJournal:
    _SELECT = (
        "SELECT event_id, operation_id, idempotency_id, member_target, ordinal, "
        "operation_kind, workspace_id, repository_identity, state, "
        "intended_git_evidence, intended_filesystem_evidence, "
        "observed_git_evidence, observed_filesystem_evidence, "
        "error_disposition, recovery_disposition, actor_evidence, created_at "
        "FROM external_operation_journal"
    )

    def __init__(self, conn):
        if type(conn) is not sqlite3.Connection:
            raise JournalRejected("conn must be a sqlite3.Connection")
        self._conn = conn

    def _require_transaction(self):
        if not self._conn.in_transaction:
            raise JournalRejected("journal append requires an active transaction")

    @staticmethod
    def _hydrate(row):
        return JournalEvent(
            event_id=row[0],
            operation_id=row[1],
            idempotency_id=row[2],
            member_target=row[3],
            ordinal=row[4],
            operation_kind=row[5],
            workspace_id=row[6],
            repository_identity=row[7],
            state=row[8],
            intended_git_evidence=row[9],
            intended_filesystem_evidence=row[10],
            observed_git_evidence=row[11],
            observed_filesystem_evidence=row[12],
            error_disposition=row[13],
            recovery_disposition=row[14],
            actor_evidence=row[15],
            created_at=row[16],
        )

    def _events(self, operation_id, member_target):
        _validate_nonblank_str(operation_id, "operation_id")
        _validate_nonblank_str(member_target, "member_target")
        rows = self._conn.execute(
            self._SELECT
            + " WHERE operation_id = ? AND member_target = ? ORDER BY ordinal ASC",
            (operation_id, member_target),
        ).fetchall()
        events = [self._hydrate(row) for row in rows]
        self._validate_sequence(events)
        return events

    @staticmethod
    def _validate_sequence(events):
        if not events:
            return
        first = events[0]
        if first.state != "prepared":
            raise JournalRejected("journal sequence must begin with prepared")
        stable = (
            first.idempotency_id,
            first.operation_kind,
            first.workspace_id,
            first.repository_identity,
            first.intended_git_evidence,
            first.intended_filesystem_evidence,
        )
        for index, event in enumerate(events):
            if event.ordinal != index + 1:
                raise JournalRejected("journal ordinal sequence is not contiguous")
            if (
                event.idempotency_id,
                event.operation_kind,
                event.workspace_id,
                event.repository_identity,
                event.intended_git_evidence,
                event.intended_filesystem_evidence,
            ) != stable:
                raise JournalRejected("journal stable intent fields changed")
            if index and event.created_at < events[index - 1].created_at:
                raise JournalRejected("journal created_at sequence regressed")

            if event.state == "prepared":
                if any(
                    value is not None
                    for value in (
                        event.observed_git_evidence,
                        event.observed_filesystem_evidence,
                        event.error_disposition,
                        event.recovery_disposition,
                    )
                ):
                    raise JournalRejected("prepared event has outcome evidence")
            elif event.state == "verified":
                if (
                    event.observed_git_evidence is None
                    and event.observed_filesystem_evidence is None
                ):
                    raise JournalRejected("verified event lacks observed evidence")
                if (
                    event.error_disposition is not None
                    or event.recovery_disposition is not None
                ):
                    raise JournalRejected("verified event has failure disposition")
            elif event.state == "failed":
                if (
                    event.error_disposition is None
                    or event.recovery_disposition is None
                ):
                    raise JournalRejected("failed event lacks disposition")

            if index:
                previous = events[index - 1]
                if previous.state == "prepared" and event.state not in (
                    "verified",
                    "failed",
                ):
                    raise JournalRejected("invalid transition from prepared")
                if previous.state == "verified":
                    raise JournalRejected("verified event is terminal")
                if previous.state == "failed" and (
                    previous.recovery_disposition != "resume"
                    or event.state != "prepared"
                ):
                    raise JournalRejected("invalid transition from failed")

    def head(self, operation_id, member_target):
        events = self._events(operation_id, member_target)
        return events[-1] if events else None

    def _insert(
        self,
        *,
        operation_id,
        idempotency_id,
        member_target,
        ordinal,
        operation_kind,
        workspace_id,
        repository_identity,
        state,
        intended_git_evidence,
        intended_filesystem_evidence,
        observed_git_evidence,
        observed_filesystem_evidence,
        error_disposition,
        recovery_disposition,
        actor_evidence,
        created_at,
    ):
        self._require_transaction()
        cursor = self._conn.execute(
            "INSERT INTO external_operation_journal ("
            "operation_id, idempotency_id, member_target, ordinal, "
            "operation_kind, workspace_id, repository_identity, state, "
            "intended_git_evidence, intended_filesystem_evidence, "
            "observed_git_evidence, observed_filesystem_evidence, "
            "error_disposition, recovery_disposition, actor_evidence, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                idempotency_id,
                member_target,
                ordinal,
                operation_kind,
                workspace_id,
                repository_identity,
                state,
                intended_git_evidence,
                intended_filesystem_evidence,
                observed_git_evidence,
                observed_filesystem_evidence,
                error_disposition,
                recovery_disposition,
                actor_evidence,
                created_at,
            ),
        )
        row = self._conn.execute(
            self._SELECT + " WHERE event_id = ?", (cursor.lastrowid,)
        ).fetchone()
        if row is None:
            raise JournalRejected("inserted journal event could not be read")
        event = self._hydrate(row)
        self._events(operation_id, member_target)
        return event

    def append_prepared(self, intent):
        self._require_transaction()
        if type(intent) is not JournalIntent:
            raise JournalRejected("intent must be a JournalIntent")
        head = self.head(intent.operation_id, intent.member_target)
        if head is None:
            return self._insert(
                operation_id=intent.operation_id,
                idempotency_id=intent.idempotency_id,
                member_target=intent.member_target,
                ordinal=1,
                operation_kind=intent.operation_kind,
                workspace_id=intent.workspace_id,
                repository_identity=intent.repository_identity,
                state="prepared",
                intended_git_evidence=intent.intended_git_evidence,
                intended_filesystem_evidence=intent.intended_filesystem_evidence,
                observed_git_evidence=None,
                observed_filesystem_evidence=None,
                error_disposition=None,
                recovery_disposition=None,
                actor_evidence=intent.actor_evidence,
                created_at=intent.created_at,
            )
        if head.state == "prepared":
            if (
                head.idempotency_id,
                head.operation_kind,
                head.workspace_id,
                head.repository_identity,
                head.intended_git_evidence,
                head.intended_filesystem_evidence,
            ) == (
                intent.idempotency_id,
                intent.operation_kind,
                intent.workspace_id,
                intent.repository_identity,
                intent.intended_git_evidence,
                intent.intended_filesystem_evidence,
            ):
                return head
            raise JournalRejected("different intent for existing prepared operation")
        raise JournalRejected("operation already finalized")

    def append_verified(
        self,
        *,
        operation_id,
        member_target,
        observed_git_evidence,
        observed_filesystem_evidence,
        actor_evidence,
        created_at,
    ):
        self._require_transaction()
        _validate_nonblank_str(operation_id, "operation_id")
        _validate_nonblank_str(member_target, "member_target")
        _validate_optional_nonblank_str(
            observed_git_evidence, "observed_git_evidence"
        )
        _validate_optional_nonblank_str(
            observed_filesystem_evidence, "observed_filesystem_evidence"
        )
        _validate_nonblank_str(actor_evidence, "actor_evidence")
        _validate_positive_int(created_at, "created_at")
        if (
            observed_git_evidence is None
            and observed_filesystem_evidence is None
        ):
            raise JournalRejected("observed evidence is required for verification")
        head = self.head(operation_id, member_target)
        if head is None or head.state != "prepared":
            raise JournalRejected("verified outcome requires a prepared head")
        if created_at < head.created_at:
            raise JournalRejected("created_at precedes prepared event")
        return self._insert(
            operation_id=head.operation_id,
            idempotency_id=head.idempotency_id,
            member_target=head.member_target,
            ordinal=head.ordinal + 1,
            operation_kind=head.operation_kind,
            workspace_id=head.workspace_id,
            repository_identity=head.repository_identity,
            state="verified",
            intended_git_evidence=head.intended_git_evidence,
            intended_filesystem_evidence=head.intended_filesystem_evidence,
            observed_git_evidence=observed_git_evidence,
            observed_filesystem_evidence=observed_filesystem_evidence,
            error_disposition=None,
            recovery_disposition=None,
            actor_evidence=actor_evidence,
            created_at=created_at,
        )

    def append_failed(
        self,
        *,
        operation_id,
        member_target,
        observed_git_evidence,
        observed_filesystem_evidence,
        error_disposition,
        recovery_disposition,
        actor_evidence,
        created_at,
    ):
        self._require_transaction()
        _validate_nonblank_str(operation_id, "operation_id")
        _validate_nonblank_str(member_target, "member_target")
        _validate_optional_nonblank_str(
            observed_git_evidence, "observed_git_evidence"
        )
        _validate_optional_nonblank_str(
            observed_filesystem_evidence, "observed_filesystem_evidence"
        )
        _validate_nonblank_str(error_disposition, "error_disposition")
        _validate_nonblank_str(recovery_disposition, "recovery_disposition")
        _validate_nonblank_str(actor_evidence, "actor_evidence")
        _validate_positive_int(created_at, "created_at")
        head = self.head(operation_id, member_target)
        if head is None or head.state != "prepared":
            raise JournalRejected("failed outcome requires a prepared head")
        if created_at < head.created_at:
            raise JournalRejected("created_at precedes prepared event")
        return self._insert(
            operation_id=head.operation_id,
            idempotency_id=head.idempotency_id,
            member_target=head.member_target,
            ordinal=head.ordinal + 1,
            operation_kind=head.operation_kind,
            workspace_id=head.workspace_id,
            repository_identity=head.repository_identity,
            state="failed",
            intended_git_evidence=head.intended_git_evidence,
            intended_filesystem_evidence=head.intended_filesystem_evidence,
            observed_git_evidence=observed_git_evidence,
            observed_filesystem_evidence=observed_filesystem_evidence,
            error_disposition=error_disposition,
            recovery_disposition=recovery_disposition,
            actor_evidence=actor_evidence,
            created_at=created_at,
        )

    def append_resume_prepared(
        self, *, operation_id, member_target, actor_evidence, created_at
    ):
        self._require_transaction()
        _validate_nonblank_str(operation_id, "operation_id")
        _validate_nonblank_str(member_target, "member_target")
        _validate_nonblank_str(actor_evidence, "actor_evidence")
        _validate_positive_int(created_at, "created_at")
        head = self.head(operation_id, member_target)
        if (
            head is None
            or head.state != "failed"
            or head.recovery_disposition != "resume"
        ):
            raise JournalRejected("operation is not resumable")
        if created_at < head.created_at:
            raise JournalRejected("created_at precedes failed event")
        return self._insert(
            operation_id=head.operation_id,
            idempotency_id=head.idempotency_id,
            member_target=head.member_target,
            ordinal=head.ordinal + 1,
            operation_kind=head.operation_kind,
            workspace_id=head.workspace_id,
            repository_identity=head.repository_identity,
            state="prepared",
            intended_git_evidence=head.intended_git_evidence,
            intended_filesystem_evidence=head.intended_filesystem_evidence,
            observed_git_evidence=None,
            observed_filesystem_evidence=None,
            error_disposition=None,
            recovery_disposition=None,
            actor_evidence=actor_evidence,
            created_at=created_at,
        )

    def recovery_action(self, operation_id, member_target):
        head = self.head(operation_id, member_target)
        if head is None:
            return "prepare"
        if head.state == "prepared":
            return "verify"
        if head.state == "verified":
            return "consume_verified"
        if head.recovery_disposition == "resume":
            return "resume"
        return "halt"

    def verified_evidence(self, operation_id, member_target):
        head = self.head(operation_id, member_target)
        return head if head is not None and head.state == "verified" else None
