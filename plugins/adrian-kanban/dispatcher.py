import hashlib
import json
import logging
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as _kb

from .capability import CapabilityBinding
from .contracts import template_for
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
from .skill_bundle import resolve_skill_binding as _default_resolve_skill_binding
from .workspace import (
    _GitWorkspaceExecutor,
    _SegmentWorkspaceController,
    _TrustedRepositoryRegistry,
)

__all__ = ()

_LOGGER = logging.getLogger("adrian_kanban.dispatcher")

# The dispatcher derives every launch fact from authoritative state.  The one
# external seam is the live phase-skill binding; it defaults to the fixed
# plugin skill bundle and may be rebound by the service or tests.
resolve_skill_binding = _default_resolve_skill_binding


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
        workspace_registry: Optional[_TrustedRepositoryRegistry] = None,
    ) -> None:
        if type(provider) is not AdrianKanbanAuthorityProvider:
            raise _DispatcherRejected(
                "provider must be AdrianKanbanAuthorityProvider"
            )
        if type(conn) is not sqlite3.Connection:
            raise _DispatcherRejected("conn must be sqlite3.Connection")
        if spawn_fn is not None and not callable(spawn_fn):
            raise _DispatcherRejected("spawn_fn must be callable or None")
        if (
            workspace_registry is not None
            and type(workspace_registry) is not _TrustedRepositoryRegistry
        ):
            raise _DispatcherRejected(
                "workspace_registry must be _TrustedRepositoryRegistry or None"
            )
        self._provider = provider
        self._conn = conn
        self._spawn_fn = spawn_fn
        self._workspace_registry = workspace_registry

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

        snap = record.snapshot
        if snap.segment_id is not None and snap.segment_workspace_id is not None:
            if self._workspace_registry is None:
                raise _DispatcherRejected(
                    "trusted workspace registry is required for segment dispatch"
                )
            controller = _SegmentWorkspaceController(
                self._conn, self._workspace_registry
            )
            cur = self._conn.execute(
                "SELECT controller_binding_ref FROM segment_workspaces "
                "WHERE workspace_id = ? AND active = 1",
                (snap.segment_workspace_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise _DispatcherRejected(
                    "active segment workspace not found in database"
                )
            stored_controller_binding = row[0]
            if not _is_nonblank_str(stored_controller_binding):
                raise _DispatcherRejected("invalid stored controller binding")
            plan = controller.load(
                workspace_id=snap.segment_workspace_id,
                expected_initiative_id=snap.initiative_id,
                expected_segment_id=snap.segment_id,
                expected_controller_binding=stored_controller_binding,
            )
            if task.workspace_kind != "dir":
                raise _DispatcherRejected(
                    "segment task workspace kind must be 'dir'"
                )
            if Path(task.workspace_path).resolve() != Path(plan.segment_root).resolve():
                raise _DispatcherRejected(
                    "task workspace path does not match derived segment root"
                )
            executor = _GitWorkspaceExecutor()
            controller_ready = True
            for member in plan.members:
                if member.member_state != "materialized":
                    controller_ready = False
                    break
                verification = executor.verify(member)
                if not verification.ready:
                    controller_ready = False
                    break
            cur = self._conn.execute(
                """
                SELECT t.id
                FROM tasks t
                JOIN task_lifecycle_contracts c ON c.task_id = t.id
                WHERE t.status = 'running'
                  AND t.id != ?
                  AND c.workspace_id = ?
                ORDER BY t.id
                """,
                (task_id, snap.segment_workspace_id),
            )
            writer_contested = cur.fetchone() is not None
            workspace_facts = WorkspaceFacts(
                segment_id=snap.segment_id,
                workspace_id=snap.segment_workspace_id,
                active=True,
                controller_ready=controller_ready,
                writer_contested=writer_contested,
            )
            current_workspace_id = snap.segment_workspace_id
        else:
            workspace_facts = attempt.workspace
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
            workspace=workspace_facts,
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


@dataclass(frozen=True)
class _DispatchRejection:
    task_id: str
    diagnostic: str

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.task_id):
            raise _DispatcherRejected("rejection task_id must be a nonblank string")
        if not _is_nonblank_str(self.diagnostic):
            raise _DispatcherRejected(
                "rejection diagnostic must be a nonblank string"
            )


@dataclass(frozen=True)
class _DispatchTickResult:
    candidate_task_ids: tuple[str, ...]
    launched_task_ids: tuple[str, ...]
    rejections: tuple[_DispatchRejection, ...]

    def __post_init__(self) -> None:
        for name in ("candidate_task_ids", "launched_task_ids", "rejections"):
            value = getattr(self, name)
            if type(value) is not tuple:
                raise _DispatcherRejected(f"{name} must be a tuple")
        for item in self.rejections:
            if type(item) is not _DispatchRejection:
                raise _DispatcherRejected("rejections must contain _DispatchRejection")


def _safe_dispatcher_diagnostic(task_id: str, exc: BaseException) -> str:
    """Render a safe, actionable diagnostic for an unexpected dispatcher error.

    Only the exception class and a stable reason are surfaced; no database
    values or stack material leak into the result record.
    """
    return (
        f"DISPATCHER_ERROR — task {task_id}: "
        f"{type(exc).__name__}; remediation: inspect the dispatcher log and "
        "retry the tick once the failure is cleared"
    )


def _derive_dispatch_attempt(
    conn: sqlite3.Connection,
    record: "LifecycleContractRepository",
    *,
    attempt_id: str,
    dispatcher_session_id: str,
    board: Optional[str],
) -> _DispatchAttempt:
    """Derive one dispatch attempt from authoritative state only.

    Compatibility comes from the current contract registry/template plus the
    live phase-skill binding.  Predecessor evidence is read from the database
    and never manufactured: missing or unaccepted evidence stays missing so
    the existing policy rejects it.
    """
    snap = record.snapshot
    skill = resolve_skill_binding(snap.phase)
    compatibility = ResolvedCompatibility(
        phase=snap.phase,
        contract_id=snap.contract_id,
        contract_version=snap.contract_version,
        step=snap.step,
        execution_profile=snap.execution_profile,
        output_validator=snap.output_validator,
        registry_hash=snap.registry_hash,
        skill=skill,
    )

    predecessor: Optional[PredecessorEvidence] = None
    expected_ref = snap.predecessor_ref
    if expected_ref is not None:
        template = template_for(snap.step)
        if (
            template.sequence is None
            or template.sequence.predecessor_step is None
        ):
            raise _DispatcherRejected("expected predecessor_step is required")
        expected_step = template.sequence.predecessor_step
        if snap.release_condition == "accepted_completion":
            row = conn.execute(
                """
                SELECT 1
                FROM task_reviewer_verdicts v
                JOIN task_candidate_handoffs h ON h.candidate_id = v.candidate_id
                  AND h.task_card_id = v.task_card_id AND h.task_id = v.task_id
                JOIN task_lifecycle_contracts c ON c.task_card_id = h.task_card_id
                  AND c.task_id = h.task_id
                WHERE h.candidate_id = ?
                  AND h.task_card_id = c.task_card_id
                  AND h.task_id = c.task_id
                  AND c.initiative_card_id = ?
                  AND c.initiative_id = ?
                  AND c.segment_id IS ?
                  AND c.step = ?
                  AND v.verdict = 'accepted'
                """,
                (
                    expected_ref,
                    record.initiative_card_id,
                    snap.initiative_id,
                    snap.segment_id,
                    expected_step,
                ),
            ).fetchone()
            accepted = row is not None
            predecessor = PredecessorEvidence(
                reference=expected_ref,
                evidence_kind="accepted_handoff",
                initiative_id=snap.initiative_id,
                accepted=accepted,
            )
        else:
            row = conn.execute(
                """
                SELECT canonical_payload
                FROM initiative_phase_results
                WHERE result_id = ?
                  AND initiative_card_id = ?
                  AND initiative_id = ?
                  AND phase = ?
                  AND segment_id IS ?
                  AND accepted = 1
                """,
                (
                    expected_ref,
                    record.initiative_card_id,
                    snap.initiative_id,
                    snap.phase,
                    snap.segment_id,
                ),
            ).fetchone()
            accepted = False
            if row is not None:
                try:
                    payload_obj = json.loads(row[0])
                except (json.JSONDecodeError, TypeError):
                    payload_obj = None
                if type(payload_obj) is dict and payload_obj.get("step") == expected_step:
                    accepted = True
            predecessor = PredecessorEvidence(
                reference=expected_ref,
                evidence_kind="initiative_checkpoint",
                initiative_id=snap.initiative_id,
                accepted=accepted,
            )

    return _DispatchAttempt(
        attempt_id=attempt_id,
        task_id=record.task_id,
        dispatcher_session_id=dispatcher_session_id,
        compatibility=compatibility,
        predecessor=predecessor,
        workspace=None,
        board=board,
    )


def _dispatch_ready_once(
    provider: AdrianKanbanAuthorityProvider,
    conn: sqlite3.Connection,
    *,
    dispatcher_session_id: str,
    spawn_fn: Optional[Callable] = None,
    board: Optional[str] = None,
    max_launches: Optional[int] = None,
    workspace_registry: Optional[_TrustedRepositoryRegistry] = None,
) -> _DispatchTickResult:
    """Run one governed dispatch tick around the lifecycle dispatcher engine.

    Only ``ready`` native tasks with a valid task-card/lifecycle-contract
    association are candidates, ordered by priority descending then creation
    time/id.  Legacy/uncontracted tasks are never launched.  A rejection leaves
    the task unchanged and is returned with an actionable diagnostic.
    """
    if type(provider) is not AdrianKanbanAuthorityProvider:
        raise _DispatcherRejected("provider must be AdrianKanbanAuthorityProvider")
    if type(conn) is not sqlite3.Connection:
        raise _DispatcherRejected("conn must be sqlite3.Connection")
    if not _is_nonblank_str(dispatcher_session_id):
        raise _DispatcherRejected("dispatcher_session_id must be a nonblank string")
    if max_launches is not None and (
        type(max_launches) is not int or isinstance(max_launches, bool) or max_launches <= 0
    ):
        raise _DispatcherRejected("max_launches must be a positive integer or None")

    rows = conn.execute(
        """
        SELECT t.id, t.created_at
        FROM tasks t
        JOIN adrian_kanban_cards c ON c.task_id = t.id
          AND c.card_type = 'task'
        JOIN task_lifecycle_contracts lc ON lc.task_card_id = c.id
        WHERE t.status = 'ready'
        ORDER BY t.priority DESC, t.created_at ASC, t.id ASC
        """
    ).fetchall()

    engine = _LifecycleDispatcher(
        provider,
        conn,
        spawn_fn=spawn_fn,
        workspace_registry=workspace_registry,
    )

    candidate_task_ids: list[str] = []
    launched_task_ids: list[str] = []
    rejections: list[_DispatchRejection] = []

    for index, row in enumerate(rows):
        task_id = row[0]
        created_at = row[1]
        candidate_task_ids.append(task_id)

        if max_launches is not None and len(launched_task_ids) >= max_launches:
            continue

        attempt_id = f"{dispatcher_session_id}:{created_at}:{index}"
        try:
            record = LifecycleContractRepository(conn).load(task_id)
            if record is None:
                raise _DispatcherRejected("lifecycle contract not found for task")
            attempt = _derive_dispatch_attempt(
                conn,
                record,
                attempt_id=attempt_id,
                dispatcher_session_id=dispatcher_session_id,
                board=board,
            )
            outcome = engine.dispatch(attempt)
        except Exception as exc:  # noqa: BLE001 - survive corrupt/rejected task
            rejections.append(
                _DispatchRejection(
                    task_id=task_id,
                    diagnostic=_safe_dispatcher_diagnostic(task_id, exc),
                )
            )
            continue

        if outcome.decision.admitted:
            launched_task_ids.append(task_id)
        else:
            rejections.append(
                _DispatchRejection(
                    task_id=task_id,
                    diagnostic=outcome.decision.rejection.render(),
                )
            )

    return _DispatchTickResult(
        candidate_task_ids=tuple(candidate_task_ids),
        launched_task_ids=tuple(launched_task_ids),
        rejections=tuple(rejections),
    )


def _run_dispatcher_daemon(
    provider: AdrianKanbanAuthorityProvider,
    conn: sqlite3.Connection,
    *,
    interval_seconds: float,
    dispatcher_session_id: str,
    spawn_fn: Optional[Callable] = None,
    board: Optional[str] = None,
    max_launches: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    workspace_registry: Optional[_TrustedRepositoryRegistry] = None,
) -> None:
    """Bounded plugin daemon loop around :func:`_dispatch_ready_once`.

    Logs launch/rejection results, survives an individual rejected/corrupt
    task and a failed tick, and exits via SIGINT/SIGTERM or an injected stop
    event.  It never runs native Hermes dispatch, auto-decomposition, or review
    dispatch.
    """
    if type(interval_seconds) is not int and type(interval_seconds) is not float:
        raise _DispatcherRejected("interval_seconds must be numeric")
    if interval_seconds <= 0:
        raise _DispatcherRejected("interval_seconds must be positive")
    if stop_event is None:
        stop_event = threading.Event()

    def _signal_handler(_signum, _frame):
        stop_event.set()

    # Signal handlers may only be installed from the main thread; when the
    # daemon is driven from another thread (e.g. tests), fall back to relying
    # on the injected stop event alone.
    signal_handlers_installed = False
    if threading.current_thread() is threading.main_thread():
        previous_int = signal.signal(signal.SIGINT, _signal_handler)
        previous_term = signal.signal(signal.SIGTERM, _signal_handler)
        signal_handlers_installed = True
    try:
        while not stop_event.is_set():
            try:
                result = _dispatch_ready_once(
                    provider,
                    conn,
                    dispatcher_session_id=dispatcher_session_id,
                    spawn_fn=spawn_fn,
                    board=board,
                    max_launches=max_launches,
                    workspace_registry=workspace_registry,
                )
            except Exception as exc:  # noqa: BLE001 - survive a failed tick
                _LOGGER.error(
                    "dispatch tick failed: %s", type(exc).__name__, exc_info=True
                )
            else:
                for task_id in result.launched_task_ids:
                    _LOGGER.info("launched task %s", task_id)
                for rejection in result.rejections:
                    _LOGGER.warning(
                        "rejected task %s: %s",
                        rejection.task_id,
                        rejection.diagnostic,
                    )
            stop_event.wait(interval_seconds)
    finally:
        if signal_handlers_installed:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
