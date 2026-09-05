"""Execution launch policy for the Adrian Kanban plugin."""

from dataclasses import dataclass
from typing import Optional

from .contracts import ContractRejected, validate_snapshot
from .lifecycle import LifecycleContractRecord, SkillBinding
from .diagnostics import (
    Boundary,
    DiagnosticCollector,
    FailedCheck,
    NotEvaluatedCheck,
    RejectionEnvelope,
)

__all__ = [
    "PolicyInputRejected",
    "ResolvedCompatibility",
    "PredecessorEvidence",
    "WorkspaceFacts",
    "ExecutionLaunchFacts",
    "ExecutionLaunchDecision",
    "evaluate_execution_launch",
]


class PolicyInputRejected(ValueError):
    """Raised when policy input types are invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_nonblank_str(value: object) -> bool:
    return type(value) is str and value.strip() != ""


def _is_optional_nonblank_str(value: object) -> bool:
    return value is None or _is_nonblank_str(value)


def _is_exact_bool(value: object) -> bool:
    return type(value) is bool


def _is_positive_int_excl_bool(value: object) -> bool:
    return type(value) is int and not isinstance(value, bool) and value > 0


def _is_exact_tuple_of_nonblank_strs(value: object) -> bool:
    if type(value) is not tuple:
        return False
    seen: set[str] = set()
    for item in value:
        if not _is_nonblank_str(item):
            return False
        if item in seen:
            return False
        seen.add(item)
    return True


@dataclass(frozen=True)
class ResolvedCompatibility:
    phase: str
    contract_id: str
    contract_version: int
    step: str
    execution_profile: str
    output_validator: str
    registry_hash: str
    skill: SkillBinding

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.phase):
            raise PolicyInputRejected("ResolvedCompatibility.phase must be nonblank string")
        if not _is_nonblank_str(self.contract_id):
            raise PolicyInputRejected("ResolvedCompatibility.contract_id must be nonblank string")
        if not _is_positive_int_excl_bool(self.contract_version):
            raise PolicyInputRejected("ResolvedCompatibility.contract_version must be positive integer")
        if not _is_nonblank_str(self.step):
            raise PolicyInputRejected("ResolvedCompatibility.step must be nonblank string")
        if not _is_nonblank_str(self.execution_profile):
            raise PolicyInputRejected("ResolvedCompatibility.execution_profile must be nonblank string")
        if not _is_nonblank_str(self.output_validator):
            raise PolicyInputRejected("ResolvedCompatibility.output_validator must be nonblank string")
        if not _is_nonblank_str(self.registry_hash):
            raise PolicyInputRejected("ResolvedCompatibility.registry_hash must be nonblank string")
        if type(self.skill) is not SkillBinding:
            raise PolicyInputRejected("ResolvedCompatibility.skill must be SkillBinding")


@dataclass(frozen=True)
class PredecessorEvidence:
    reference: str
    evidence_kind: str
    initiative_id: str
    accepted: bool

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.reference):
            raise PolicyInputRejected("PredecessorEvidence.reference must be nonblank string")
        if self.evidence_kind not in ("accepted_handoff", "initiative_checkpoint"):
            raise PolicyInputRejected("PredecessorEvidence.evidence_kind must be valid kind")
        if not _is_nonblank_str(self.initiative_id):
            raise PolicyInputRejected("PredecessorEvidence.initiative_id must be nonblank string")
        if not _is_exact_bool(self.accepted):
            raise PolicyInputRejected("PredecessorEvidence.accepted must be bool")


@dataclass(frozen=True)
class WorkspaceFacts:
    segment_id: str
    workspace_id: str
    active: bool
    controller_ready: bool
    writer_contested: bool

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.segment_id):
            raise PolicyInputRejected("WorkspaceFacts.segment_id must be nonblank string")
        if not _is_nonblank_str(self.workspace_id):
            raise PolicyInputRejected("WorkspaceFacts.workspace_id must be nonblank string")
        if not _is_exact_bool(self.active):
            raise PolicyInputRejected("WorkspaceFacts.active must be bool")
        if not _is_exact_bool(self.controller_ready):
            raise PolicyInputRejected("WorkspaceFacts.controller_ready must be bool")
        if not _is_exact_bool(self.writer_contested):
            raise PolicyInputRejected("WorkspaceFacts.writer_contested must be bool")


@dataclass(frozen=True)
class ExecutionLaunchFacts:
    attempt_id: str
    task_id: str
    status: str
    assignee: str
    current_phase: str
    current_initiative_id: str
    current_segment_id: Optional[str]
    current_workspace_id: Optional[str]
    compatibility: ResolvedCompatibility
    predecessor: Optional[PredecessorEvidence]
    blocking_task_ids: tuple[str, ...]
    active_profile_task_ids: tuple[str, ...]
    workspace: Optional[WorkspaceFacts]

    def __post_init__(self) -> None:
        if not _is_nonblank_str(self.attempt_id):
            raise PolicyInputRejected("ExecutionLaunchFacts.attempt_id must be nonblank string")
        if not _is_nonblank_str(self.task_id):
            raise PolicyInputRejected("ExecutionLaunchFacts.task_id must be nonblank string")
        if not _is_nonblank_str(self.status):
            raise PolicyInputRejected("ExecutionLaunchFacts.status must be nonblank string")
        if not _is_nonblank_str(self.assignee):
            raise PolicyInputRejected("ExecutionLaunchFacts.assignee must be nonblank string")
        if not _is_nonblank_str(self.current_phase):
            raise PolicyInputRejected("ExecutionLaunchFacts.current_phase must be nonblank string")
        if not _is_nonblank_str(self.current_initiative_id):
            raise PolicyInputRejected("ExecutionLaunchFacts.current_initiative_id must be nonblank string")
        if not _is_optional_nonblank_str(self.current_segment_id):
            raise PolicyInputRejected("ExecutionLaunchFacts.current_segment_id must be null or nonblank string")
        if not _is_optional_nonblank_str(self.current_workspace_id):
            raise PolicyInputRejected("ExecutionLaunchFacts.current_workspace_id must be null or nonblank string")
        if type(self.compatibility) is not ResolvedCompatibility:
            raise PolicyInputRejected("ExecutionLaunchFacts.compatibility must be ResolvedCompatibility")
        if self.predecessor is not None and type(self.predecessor) is not PredecessorEvidence:
            raise PolicyInputRejected("ExecutionLaunchFacts.predecessor must be PredecessorEvidence")
        if not _is_exact_tuple_of_nonblank_strs(self.blocking_task_ids):
            raise PolicyInputRejected("ExecutionLaunchFacts.blocking_task_ids must be tuple of unique nonblank strings")
        if not _is_exact_tuple_of_nonblank_strs(self.active_profile_task_ids):
            raise PolicyInputRejected("ExecutionLaunchFacts.active_profile_task_ids must be tuple of unique nonblank strings")
        if self.workspace is not None and type(self.workspace) is not WorkspaceFacts:
            raise PolicyInputRejected("ExecutionLaunchFacts.workspace must be WorkspaceFacts")


@dataclass(frozen=True)
class ExecutionLaunchDecision:
    admitted: bool
    task_id: str
    execution_profile: str
    rejection: Optional[RejectionEnvelope]

    def __post_init__(self) -> None:
        if not _is_exact_bool(self.admitted):
            raise PolicyInputRejected("ExecutionLaunchDecision.admitted must be bool")
        if not _is_nonblank_str(self.task_id):
            raise PolicyInputRejected("ExecutionLaunchDecision.task_id must be nonblank string")
        if not _is_nonblank_str(self.execution_profile):
            raise PolicyInputRejected("ExecutionLaunchDecision.execution_profile must be nonblank string")
        if self.admitted and self.rejection is not None:
            raise PolicyInputRejected("ExecutionLaunchDecision.rejection must be null when admitted")
        if not self.admitted and type(self.rejection) is not RejectionEnvelope:
            raise PolicyInputRejected("ExecutionLaunchDecision.rejection must be RejectionEnvelope when not admitted")


def _make_failed_check(
    code: str,
    target: str,
    expected_desc: str,
    observed_desc: str,
    accepted_format: str,
    remediation: str,
    retry: str = "same_operation",
) -> FailedCheck:
    return FailedCheck(
        code=code,
        target=target,
        expected=expected_desc,
        observed=observed_desc,
        accepted_format=accepted_format,
        remediation=remediation,
        responsible_actor="orchestrator",
        retry=retry,
    )


def evaluate_execution_launch(
    record: LifecycleContractRecord, facts: ExecutionLaunchFacts
) -> ExecutionLaunchDecision:
    if type(record) is not LifecycleContractRecord:
        raise PolicyInputRejected("record must be LifecycleContractRecord")
    if type(facts) is not ExecutionLaunchFacts:
        raise PolicyInputRejected("facts must be ExecutionLaunchFacts")

    snap = record.snapshot
    source = facts.current_phase
    if facts.current_segment_id is not None:
        source = f"{facts.current_phase}/{facts.current_segment_id}"

    collector = DiagnosticCollector(
        attempt_id=facts.attempt_id,
        operation="execution_launch",
        boundary=Boundary(source, "execution-launch"),
    )

    # Always check task ID and status
    if facts.task_id != record.task_id:
        collector.failure(
            _make_failed_check(
                code="TASK_ID_MISMATCH",
                target="task_id",
                expected_desc="matches record task ID",
                observed_desc="differs from record task ID",
                accepted_format="nonblank string matching record task ID",
                remediation="Correct the task ID in facts to match the record",
            )
        )

    if facts.status != "ready":
        collector.failure(
            _make_failed_check(
                code="TASK_STATUS_NOT_READY",
                target="status",
                expected_desc="ready",
                observed_desc="not ready",
                accepted_format="exact string 'ready'",
                remediation="Ensure task status is set to ready before launch",
            )
        )

    # Validate snapshot
    try:
        validate_snapshot(snap)
    except ContractRejected:
        collector.failure(
            _make_failed_check(
                code="CONTRACT_SNAPSHOT_INVALID",
                target="snapshot",
                expected_desc="valid contract snapshot",
                observed_desc="invalid contract snapshot",
                accepted_format="ContractSnapshot passing validate_snapshot",
                remediation="Fix the contract snapshot to satisfy validation rules",
            )
        )
        collector.not_evaluated(
            NotEvaluatedCheck(
                code="CONTRACT_COMPATIBILITY_NOT_EVALUATED",
                requires=("CONTRACT_SNAPSHOT_INVALID",),
            )
        )
        collector.not_evaluated(
            NotEvaluatedCheck(
                code="SEQUENCE_NOT_EVALUATED",
                requires=("CONTRACT_SNAPSHOT_INVALID",),
            )
        )
        collector.not_evaluated(
            NotEvaluatedCheck(
                code="WORKSPACE_NOT_EVALUATED",
                requires=("CONTRACT_SNAPSHOT_INVALID",),
            )
        )
        collector.not_evaluated(
            NotEvaluatedCheck(
                code="PROFILE_LANE_NOT_EVALUATED",
                requires=("CONTRACT_SNAPSHOT_INVALID",),
            )
        )
        return ExecutionLaunchDecision(
            admitted=False,
            task_id=record.task_id,
            execution_profile=snap.execution_profile,
            rejection=collector.rejection(),
        )

    # Valid snapshot: collect all checks without short-circuit

    # Assignee profile mismatch
    if facts.assignee != snap.execution_profile:
        collector.failure(
            _make_failed_check(
                code="ASSIGNEE_PROFILE_MISMATCH",
                target="assignee",
                expected_desc="matches execution profile",
                observed_desc="differs from execution profile",
                accepted_format="nonblank string matching snapshot execution_profile",
                remediation="Align assignee with the execution profile",
            )
        )

    # Initiative ID mismatch
    if facts.current_initiative_id != snap.initiative_id:
        collector.failure(
            _make_failed_check(
                code="INITIATIVE_ID_MISMATCH",
                target="current_initiative_id",
                expected_desc="matches snapshot initiative ID",
                observed_desc="differs from snapshot initiative ID",
                accepted_format="nonblank string matching the contract snapshot initiative ID",
                remediation="Correct the current initiative ID to match the contract snapshot",
            )
        )

    # Initiative phase mismatch
    if facts.current_phase != snap.phase:
        collector.failure(
            _make_failed_check(
                code="INITIATIVE_PHASE_MISMATCH",
                target="current_phase",
                expected_desc="matches snapshot phase",
                observed_desc="differs from snapshot phase",
                accepted_format="nonblank string matching snapshot phase",
                remediation="Update current phase to match the snapshot",
            )
        )

    # Initiative segment mismatch
    if facts.current_segment_id != snap.segment_id:
        collector.failure(
            _make_failed_check(
                code="INITIATIVE_SEGMENT_MISMATCH",
                target="current_segment_id",
                expected_desc="matches snapshot segment_id",
                observed_desc="differs from snapshot segment_id",
                accepted_format="null or nonblank string matching snapshot segment_id",
                remediation="Align current segment with the snapshot segment",
            )
        )

    # Current workspace mismatch
    if facts.current_workspace_id != snap.segment_workspace_id:
        collector.failure(
            _make_failed_check(
                code="CURRENT_WORKSPACE_MISMATCH",
                target="current_workspace_id",
                expected_desc="matches snapshot segment_workspace_id",
                observed_desc="differs from snapshot segment_workspace_id",
                accepted_format="null or nonblank string matching snapshot segment_workspace_id",
                remediation="Align current workspace with the snapshot workspace",
            )
        )

    # Compatibility field comparisons
    comp = facts.compatibility
    if comp.phase != snap.phase:
        collector.failure(
            _make_failed_check(
                code="COMPATIBILITY_PHASE_MISMATCH",
                target="compatibility.phase",
                expected_desc="matches snapshot phase",
                observed_desc="differs from snapshot phase",
                accepted_format="nonblank string matching snapshot phase",
                remediation="Update compatibility phase to match the snapshot",
            )
        )
    if comp.contract_id != snap.contract_id:
        collector.failure(
            _make_failed_check(
                code="COMPATIBILITY_CONTRACT_ID_MISMATCH",
                target="compatibility.contract_id",
                expected_desc="matches snapshot contract_id",
                observed_desc="differs from snapshot contract_id",
                accepted_format="nonblank string matching snapshot contract_id",
                remediation="Update compatibility contract ID to match the snapshot",
            )
        )
    if comp.contract_version != snap.contract_version:
        collector.failure(
            _make_failed_check(
                code="COMPATIBILITY_CONTRACT_VERSION_MISMATCH",
                target="compatibility.contract_version",
                expected_desc="matches snapshot contract_version",
                observed_desc="differs from snapshot contract_version",
                accepted_format="positive integer matching snapshot contract_version",
                remediation="Update compatibility contract version to match the snapshot",
            )
        )
    if comp.step != snap.step:
        collector.failure(
            _make_failed_check(
                code="COMPATIBILITY_STEP_MISMATCH",
                target="compatibility.step",
                expected_desc="matches snapshot step",
                observed_desc="differs from snapshot step",
                accepted_format="nonblank string matching snapshot step",
                remediation="Update compatibility step to match the snapshot",
            )
        )
    if comp.execution_profile != snap.execution_profile:
        collector.failure(
            _make_failed_check(
                code="COMPATIBILITY_EXECUTION_PROFILE_MISMATCH",
                target="compatibility.execution_profile",
                expected_desc="matches snapshot execution_profile",
                observed_desc="differs from snapshot execution_profile",
                accepted_format="nonblank string matching snapshot execution_profile",
                remediation="Update compatibility execution profile to match the snapshot",
            )
        )
    if comp.output_validator != snap.output_validator:
        collector.failure(
            _make_failed_check(
                code="COMPATIBILITY_OUTPUT_VALIDATOR_MISMATCH",
                target="compatibility.output_validator",
                expected_desc="matches snapshot output_validator",
                observed_desc="differs from snapshot output_validator",
                accepted_format="nonblank string matching snapshot output_validator",
                remediation="Update compatibility output validator to match the snapshot",
            )
        )
    if comp.registry_hash != snap.registry_hash:
        collector.failure(
            _make_failed_check(
                code="COMPATIBILITY_REGISTRY_HASH_MISMATCH",
                target="compatibility.registry_hash",
                expected_desc="matches snapshot registry_hash",
                observed_desc="differs from snapshot registry_hash",
                accepted_format="nonblank string matching snapshot registry_hash",
                remediation="Update compatibility registry hash to match the snapshot",
            )
        )

    # Skill comparisons
    rec_skill = record.skill
    comp_skill = comp.skill
    if comp_skill.skill_id != rec_skill.skill_id:
        collector.failure(
            _make_failed_check(
                code="COMPATIBILITY_SKILL_ID_MISMATCH",
                target="compatibility.skill.skill_id",
                expected_desc="matches record skill ID",
                observed_desc="differs from record skill ID",
                accepted_format="nonblank string matching record skill ID",
                remediation="Update compatibility skill ID to match the record",
            )
        )
    if comp_skill.skill_version != rec_skill.skill_version:
        collector.failure(
            _make_failed_check(
                code="COMPATIBILITY_SKILL_VERSION_MISMATCH",
                target="compatibility.skill.skill_version",
                expected_desc="matches record skill version",
                observed_desc="differs from record skill version",
                accepted_format="nonblank string matching record skill version",
                remediation="Update compatibility skill version to match the record",
            )
        )
    if comp_skill.skill_hash != rec_skill.skill_hash:
        collector.failure(
            _make_failed_check(
                code="COMPATIBILITY_SKILL_HASH_MISMATCH",
                target="compatibility.skill.skill_hash",
                expected_desc="matches record skill hash",
                observed_desc="differs from record skill hash",
                accepted_format="nonblank string matching record skill hash",
                remediation="Update compatibility skill hash to match the record",
            )
        )

    # Predecessor logic
    expected_ref = snap.predecessor_ref
    pred = facts.predecessor

    if expected_ref is None:
        if pred is not None:
            collector.failure(
                _make_failed_check(
                    code="UNEXPECTED_PREDECESSOR_EVIDENCE",
                    target="predecessor",
                    expected_desc="null",
                    observed_desc="present",
                    accepted_format="null when no predecessor ref expected",
                    remediation="Remove predecessor evidence when no predecessor is expected",
                )
            )
    else:
        if pred is None:
            collector.failure(
                _make_failed_check(
                    code="PREDECESSOR_EVIDENCE_MISSING",
                    target="predecessor",
                    expected_desc="present evidence",
                    observed_desc="missing",
                    accepted_format="PredecessorEvidence when predecessor ref is set",
                    remediation="Provide predecessor evidence matching the expected reference",
                )
            )
            collector.not_evaluated(
                NotEvaluatedCheck(
                    code="PREDECESSOR_KIND_NOT_EVALUATED",
                    requires=("PREDECESSOR_EVIDENCE_MISSING",),
                )
            )
            collector.not_evaluated(
                NotEvaluatedCheck(
                    code="PREDECESSOR_ACCEPTANCE_NOT_EVALUATED",
                    requires=("PREDECESSOR_EVIDENCE_MISSING",),
                )
            )
        else:
            if pred.reference != expected_ref or pred.initiative_id != snap.initiative_id:
                collector.failure(
                    _make_failed_check(
                        code="PREDECESSOR_EVIDENCE_MISMATCH",
                        target="predecessor",
                        expected_desc="matches expected reference and initiative",
                        observed_desc="differs from expected reference or initiative",
                        accepted_format="PredecessorEvidence with matching reference and initiative_id",
                        remediation="Correct predecessor evidence to match the expected reference and initiative",
                    )
                )
                collector.not_evaluated(
                    NotEvaluatedCheck(
                        code="PREDECESSOR_KIND_NOT_EVALUATED",
                        requires=("PREDECESSOR_EVIDENCE_MISMATCH",),
                    )
                )
                collector.not_evaluated(
                    NotEvaluatedCheck(
                        code="PREDECESSOR_ACCEPTANCE_NOT_EVALUATED",
                        requires=("PREDECESSOR_EVIDENCE_MISMATCH",),
                    )
                )
            else:
                # Matching evidence: check kind and acceptance
                if snap.release_condition == "accepted_completion":
                    expected_kind = "accepted_handoff"
                else:
                    expected_kind = "initiative_checkpoint"

                if pred.evidence_kind != expected_kind:
                    collector.failure(
                        _make_failed_check(
                            code="PREDECESSOR_KIND_MISMATCH",
                            target="predecessor.evidence_kind",
                            expected_desc=f"{expected_kind}",
                            observed_desc="differs from expected kind",
                            accepted_format=f"exact string '{expected_kind}'",
                            remediation="Use the correct evidence kind for the release condition",
                        )
                    )

                if not pred.accepted:
                    collector.failure(
                        _make_failed_check(
                            code="PREDECESSOR_NOT_ACCEPTED",
                            target="predecessor.accepted",
                            expected_desc="True",
                            observed_desc="False",
                            accepted_format="boolean True",
                            remediation="Ensure predecessor evidence is accepted",
                        )
                    )

    # Blocking task IDs
    if len(facts.blocking_task_ids) > 0:
        collector.failure(
            _make_failed_check(
                code="EXPLICIT_DEPENDENCY_BLOCKED",
                target="blocking_task_ids",
                expected_desc="empty tuple",
                observed_desc="non-empty tuple",
                accepted_format="empty tuple when no explicit dependencies are blocked",
                remediation="Resolve blocking task dependencies before launch",
                retry="return_route",
            )
        )

    # Workspace logic
    if snap.segment_id is None and snap.segment_workspace_id is None:
        if facts.workspace is not None:
            collector.failure(
                _make_failed_check(
                    code="UNEXPECTED_WORKSPACE_FACTS",
                    target="workspace",
                    expected_desc="null",
                    observed_desc="present",
                    accepted_format="null when snapshot has no segment or workspace",
                    remediation="Remove workspace facts when snapshot has no segment or workspace",
                )
            )
    else:
        if facts.workspace is None:
            collector.failure(
                _make_failed_check(
                    code="WORKSPACE_ALIGNMENT_MISMATCH",
                    target="workspace",
                    expected_desc="present workspace facts",
                    observed_desc="missing",
                    accepted_format="WorkspaceFacts when snapshot has segment or workspace",
                    remediation="Provide workspace facts matching the snapshot segment and workspace",
                    retry="return_route",
                )
            )
            collector.not_evaluated(
                NotEvaluatedCheck(
                    code="WORKSPACE_CONTROLLER_NOT_EVALUATED",
                    requires=("WORKSPACE_ALIGNMENT_MISMATCH",),
                )
            )
            collector.not_evaluated(
                NotEvaluatedCheck(
                    code="WORKSPACE_WRITER_NOT_EVALUATED",
                    requires=("WORKSPACE_ALIGNMENT_MISMATCH",),
                )
            )
        else:
            ws = facts.workspace
            if ws.segment_id != snap.segment_id or ws.workspace_id != snap.segment_workspace_id:
                collector.failure(
                    _make_failed_check(
                        code="WORKSPACE_ALIGNMENT_MISMATCH",
                        target="workspace",
                        expected_desc="matches snapshot segment and workspace",
                        observed_desc="differs from snapshot segment or workspace",
                        accepted_format="WorkspaceFacts with matching segment_id and workspace_id",
                        remediation="Align workspace facts with the snapshot segment and workspace",
                        retry="return_route",
                    )
                )
                collector.not_evaluated(
                    NotEvaluatedCheck(
                        code="WORKSPACE_CONTROLLER_NOT_EVALUATED",
                        requires=("WORKSPACE_ALIGNMENT_MISMATCH",),
                    )
                )
                collector.not_evaluated(
                    NotEvaluatedCheck(
                        code="WORKSPACE_WRITER_NOT_EVALUATED",
                        requires=("WORKSPACE_ALIGNMENT_MISMATCH",),
                    )
                )
            else:
                if not ws.active or not ws.controller_ready:
                    collector.failure(
                        _make_failed_check(
                            code="WORKSPACE_CONTROLLER_NOT_READY",
                            target="workspace.controller_ready",
                            expected_desc="active and controller ready",
                            observed_desc="inactive or controller not ready",
                            accepted_format="active=True and controller_ready=True",
                            remediation="Ensure workspace is active and controller is ready",
                            retry="return_route",
                        )
                    )
                if ws.writer_contested:
                    collector.failure(
                        _make_failed_check(
                            code="WORKSPACE_WRITER_CONTESTED",
                            target="workspace.writer_contested",
                            expected_desc="False",
                            observed_desc="True",
                            accepted_format="boolean False",
                            remediation="Resolve writer contention in the workspace",
                            retry="return_route",
                        )
                    )

    # Active profile task IDs
    if len(facts.active_profile_task_ids) > 0:
        collector.failure(
            _make_failed_check(
                code="PROFILE_LANE_OCCUPIED",
                target="active_profile_task_ids",
                expected_desc="empty tuple",
                observed_desc="non-empty tuple",
                accepted_format="empty tuple when no active profile tasks exist",
                remediation="Wait for active profile tasks to complete before launch",
                retry="return_route",
            )
        )

    if collector.has_findings:
        return ExecutionLaunchDecision(
            admitted=False,
            task_id=record.task_id,
            execution_profile=snap.execution_profile,
            rejection=collector.rejection(),
        )

    return ExecutionLaunchDecision(
        admitted=True,
        task_id=record.task_id,
        execution_profile=snap.execution_profile,
        rejection=None,
    )
