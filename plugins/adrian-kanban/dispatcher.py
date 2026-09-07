import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as _kb

from .capability import CapabilityBinding
from .lifecycle import LifecycleContractRepository
from .policy import (
    ExecutionLaunchFacts,
    PredecessorEvidence,
    ResolvedCompatibility,
    WorkspaceFacts,
    evaluate_execution_launch,
)
from .provider import AdrianKanbanAuthorityProvider, PLUGIN_VERSION, PROTOCOL_VERSION
from .private_adapter import _LaunchTaskArgs, _PrivateNativeAdapter

__all__ = ()


class _DispatcherRejected(RuntimeError):
    pass


def _is_nonblank_str(value: Any) -> bool:
    return type(value) is str and value.strip() != ""


def _profile_lock_path(database_path: Path, profile: str) -> Path:
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()
    return database_path.with_name(f"{database_path.name}.{digest}.profile")


@dataclass(frozen=True)
class _DispatchAttempt:
    attempt_id: str
    task_id: str
    dispatcher_session_id: str
    compatibility: ResolvedCompatibility
    predecessor: Optional[PredecessorEvidence]
    workspace: Optional[WorkspaceFacts]
    board: Optional[str]

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.attempt_id):
            raise _DispatcherRejected("attempt_id must be a nonblank string")
        if not _is_nonblank_str(self.task_id):
            raise _DispatcherRejected("task_id must be a nonblank string")
        if not _is_nonblank_str(self.dispatcher_session_id):
            raise _DispatcherRejected(
                "dispatcher_session_id must be a nonblank string"
            )
        if type(self.compatibility) is not ResolvedCompatibility:
            raise _DispatcherRejected("compatibility must be ResolvedCompatibility")
        if (
            self.predecessor is not None
            and type(self.predecessor) is not PredecessorEvidence
        ):
            raise _DispatcherRejected(
                "predecessor must be PredecessorEvidence or None"
            )
        if self.workspace is not None and type(self.workspace) is not WorkspaceFacts:
            raise _DispatcherRejected("workspace must be WorkspaceFacts or None")
        if self.board is not None and not _is_nonblank_str(self.board):
            raise _DispatcherRejected("board must be a nonblank string or None")


@dataclass(frozen=True)
class _DispatchOutcome:
    decision: Any
    native: Any


class _LifecycleDispatcher:
    def __init__(
        self,
        provider: AdrianKanbanAuthorityProvider,
        conn: sqlite3.Connection,
        *,
        spawn_fn: Optional[Callable] = None,
    ) -> None:
        if type(provider) is not AdrianKanbanAuthorityProvider:
            raise _DispatcherRejected(
                "provider must be AdrianKanbanAuthorityProvider"
            )
        if type(conn) is not sqlite3.Connection:
            raise _DispatcherRejected("conn must be sqlite3.Connection")
        if spawn_fn is not None and not callable(spawn_fn):
            raise _DispatcherRejected("spawn_fn must be callable or None")
        self._provider = provider
        self._conn = conn
        self._spawn_fn = spawn_fn

    def dispatch(self, attempt: _DispatchAttempt) -> _DispatchOutcome:
        if type(attempt) is not _DispatchAttempt:
            raise _DispatcherRejected("attempt must be _DispatchAttempt")
        record = LifecycleContractRepository(self._conn).load(attempt.task_id)
        if record is None:
            raise _DispatcherRejected("lifecycle contract not found for task")
        profile = record.snapshot.execution_profile
        if not _is_nonblank_str(profile):
            raise _DispatcherRejected("execution profile must be a nonblank string")
        lock_path = _profile_lock_path(Path(self._provider.database_path), profile)
        with _kb._dispatch_tick_lock(lock_path) as held:
            if not held:
                raise _DispatcherRejected("dispatch lock is busy")
            return self._dispatch_locked(attempt)

    def _dispatch_locked(self, attempt: _DispatchAttempt) -> _DispatchOutcome:

        task_id = attempt.task_id

        record = LifecycleContractRepository(self._conn).load(task_id)
        if record is None:
            raise _DispatcherRejected("lifecycle contract not found for task")

        task = _kb.get_task(self._conn, task_id)
        if task is None:
            raise _DispatcherRejected("native task not found")
        if not _is_nonblank_str(task.assignee):
            raise _DispatcherRejected("task is unassigned")

        cur = self._conn.execute(
            "SELECT initiative_id FROM adrian_kanban_cards "
            "WHERE card_type = 'task' AND task_id = ?",
            (task_id,),
        )
        rows = cur.fetchall()
        if len(rows) != 1:
            raise _DispatcherRejected("task card resolution failed")
        current_initiative_id = rows[0][0]
        if not _is_nonblank_str(current_initiative_id):
            raise _DispatcherRejected("invalid initiative_id")

        cur = self._conn.execute(
            """
            SELECT to_phase, to_segment_id
            FROM initiative_transitions
            WHERE initiative_id = ?
            ORDER BY transition_id DESC
            LIMIT 1
            """,
            (current_initiative_id,),
        )
        head_row = cur.fetchone()
        if head_row is None:
            raise _DispatcherRejected("no transition head found")
        current_phase = head_row[0]
        current_segment_id = head_row[1]
        if not _is_nonblank_str(current_phase):
            raise _DispatcherRejected("blank transition phase")

        cur = self._conn.execute(
            """
            SELECT p.id
            FROM task_links l
            JOIN tasks p ON p.id = l.parent_id
            WHERE l.child_id = ?
              AND p.status NOT IN ('done', 'archived')
            ORDER BY p.id
            """,
            (task_id,),
        )
        blocking_task_ids = tuple(row[0] for row in cur.fetchall())

        profile = record.snapshot.execution_profile
        cur = self._conn.execute(
            """
            SELECT t.id
            FROM tasks t
            JOIN task_lifecycle_contracts c ON c.task_id = t.id
            WHERE t.status = 'running'
              AND t.id != ?
              AND c.execution_profile = ?
            ORDER BY t.id
            """,
            (task_id, profile),
        )
        active_profile_task_ids = tuple(row[0] for row in cur.fetchall())

        current_workspace_id = (
            attempt.workspace.workspace_id
            if attempt.workspace is not None
            else None
        )
        facts = ExecutionLaunchFacts(
            attempt_id=attempt.attempt_id,
            task_id=task_id,
            status=task.status,
            assignee=task.assignee,
            current_phase=current_phase,
            current_initiative_id=current_initiative_id,
            current_segment_id=current_segment_id,
            current_workspace_id=current_workspace_id,
            compatibility=attempt.compatibility,
            predecessor=attempt.predecessor,
            blocking_task_ids=blocking_task_ids,
            active_profile_task_ids=active_profile_task_ids,
            workspace=attempt.workspace,
        )

        decision = evaluate_execution_launch(record, facts)

        if not decision.admitted:
            return _DispatchOutcome(decision=decision, native=None)

        canonical_digest = "sha256:" + hashlib.sha256(
            record.snapshot.canonical_payload().encode("utf-8")
        ).hexdigest()

        binding = CapabilityBinding(
            operation="kanban_launch",
            target=task_id,
            expected_version=record.snapshot.contract_version,
            canonical_digest=canonical_digest,
            session_id=attempt.dispatcher_session_id,
            workspace_id=record.snapshot.segment_workspace_id,
            plugin_version=PLUGIN_VERSION,
            protocol_version=PROTOCOL_VERSION,
            execution_context=attempt.attempt_id,
        )

        capability = self._provider._mint_after_admission(binding)

        adapter = _PrivateNativeAdapter(
            self._provider, self._conn, spawn_fn=self._spawn_fn
        )
        args = _LaunchTaskArgs(
            task_id=task_id,
            expected_assignee=decision.execution_profile,
            board=attempt.board,
        )
        native_evidence = adapter.execute(capability, binding, args)

        return _DispatchOutcome(decision=decision, native=native_evidence)
