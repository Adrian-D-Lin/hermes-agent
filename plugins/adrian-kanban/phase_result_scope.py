from .diagnostics import CommandRejected, FailedCheck, NotEvaluatedCheck
from .skill_bundle import skill_contract

ALLOWED_KINDS = ("phase_close", "repository_reconciliation", "initiative_closure")
RESERVED_KINDS = ("orchestration_checkpoint", "segment_manifest_projection")


def validate_phase_result_scope(context, initiative_id: str, update: dict) -> None:
    failures = []
    pending = []

    def add(code, target, expected, remediation, actor="orchestrator"):
        failures.append(
            FailedCheck(
                code=code,
                target=target,
                expected=expected,
                observed="submitted value does not satisfy the fixed phase-result contract",
                accepted_format=expected,
                remediation=remediation,
                responsible_actor=actor,
                retry="same_operation",
            )
        )

    # 1. actor_profile check
    if context.binding.actor_profile != "default":
        add(
            "PHASE_RESULT_ACTOR",
            "actor_profile",
            "default",
            "Ask the default Orchestrator session to submit this phase result; do not change or spoof the authenticated profile.",
        )

    # 2. result_kind check
    result_kind = update.get("result_kind")
    if result_kind not in ALLOWED_KINDS:
        if result_kind in RESERVED_KINDS:
            remediation = (
                "Use kanban_update_initiative with update_kind='"
                + result_kind
                + "' through its dedicated validator."
            )
        else:
            remediation = (
                "Use one of the allowed generic result kinds: "
                + ", ".join(ALLOWED_KINDS)
                + "."
            )
        add(
            "PHASE_RESULT_KIND",
            "update.result_kind",
            "one of " + ", ".join(ALLOWED_KINDS),
            remediation,
        )

    # 3. contract_id and contract_version checks
    phase = update.get("phase")
    expected_contract_id, expected_contract_version = skill_contract(phase)

    if update.get("contract_id") != expected_contract_id:
        add(
            "PHASE_RESULT_CONTRACT_ID",
            "update.contract_id",
            expected_contract_id,
            "Set contract_id to the value returned by skill_contract for the current phase.",
        )

    if update.get("contract_version") != expected_contract_version:
        add(
            "PHASE_RESULT_CONTRACT_VERSION",
            "update.contract_version",
            expected_contract_version,
            "Set contract_version to the value returned by skill_contract for the current phase.",
        )

    # 4 & 5. transition checks
    cursor = context.connection.execute(
        "SELECT transition_id, to_phase, to_segment_id FROM initiative_transitions WHERE initiative_id=? ORDER BY transition_id DESC LIMIT 1",
        (initiative_id,),
    )
    row = cursor.fetchone()

    if row is None:
        add(
            "PHASE_RESULT_TRANSITION_MISSING",
            "initiative.transition",
            "latest transition record exists",
            "Investigate missing initiative position; ensure a transition has been recorded for this initiative.",
            actor="system_operator",
        )
        pending.append(
            NotEvaluatedCheck(
                code="PHASE_RESULT_POSITION",
                requires=("PHASE_RESULT_TRANSITION_MISSING",),
            )
        )
    else:
        latest_phase = row[1]
        latest_segment_id = row[2]

        if update.get("phase") != latest_phase:
            add(
                "PHASE_RESULT_PHASE",
                "update.phase",
                latest_phase,
                "Read the initiative's latest position and resubmit the result with the aligned phase.",
            )

        if update.get("segment_id") != latest_segment_id:
            add(
                "PHASE_RESULT_SEGMENT",
                "update.segment_id",
                "null" if latest_segment_id is None else latest_segment_id,
                "Read the initiative's latest position and resubmit the result with the aligned segment_id.",
            )

    if failures:
        raise CommandRejected(
            failed_checks=tuple(failures),
            not_evaluated_checks=tuple(pending),
        )
