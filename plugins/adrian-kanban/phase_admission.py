from .diagnostics import CommandRejected, FailedCheck
from .phase_d1 import PreparedD1Result, admit_d1_result
from .phase_d2 import PreparedD2Result, admit_d2_result
from .phase_d3 import PreparedD3Result, admit_d3_result
from .phase_d4_execution import PreparedD4Execution
from .phase_preparer import GitPhaseResultPreparer


def _reject(
    code: str, diagnostic: str, responsible_actor: str, remediation: str
) -> CommandRejected:
    return CommandRejected(
        failed_checks=(
            FailedCheck(
                code=code,
                target="update.result",
                expected="valid immutable phase-close evidence",
                observed=diagnostic,
                accepted_format="D1/D2/D3 fixed result schema with exact path, full commit SHA, sha256 and complete prior finding references",
                remediation=remediation,
                responsible_actor=responsible_actor,
                retry="same_operation",
            ),
        ),
    )


def _reject_d4(
    code: str, diagnostic: str, responsible_actor: str, remediation: str
) -> CommandRejected:
    return CommandRejected(
        failed_checks=(
            FailedCheck(
                code=code,
                target="update.result",
                expected="valid D4.4 execution evidence (write-gate lease, pinned post-write documents, item determinations)",
                observed=diagnostic,
                accepted_format="D4.4 fixed result schema with write_gate_approval_ref, approval_lease_ref, approved_change_set_digest, execution_result, pinned post_write_documents and complete item_determinations",
                remediation=remediation,
                responsible_actor=responsible_actor,
                retry="same_operation",
            ),
        ),
    )


def prepare_phase_result(payload, preparation_context, preparer):
    update = payload.get("update") if isinstance(payload, dict) else None
    if not isinstance(update, dict):
        return None
    update_kind = payload.get("update_kind")
    if update_kind == "orchestration_checkpoint":
        result = update.get("result")
        if (
            update.get("phase") != "D4"
            or not isinstance(result, dict)
            or result.get("step") != "D4.4"
        ):
            return None
        if preparer is None:
            raise _reject_d4(
                code="PHASE_RESULT_PREPARER",
                diagnostic="preparer is None",
                responsible_actor="system_operator",
                remediation="Configure a valid GitPhaseResultPreparer instance before invoking D4.4 execution preparation.",
            )
        try:
            proof = preparer(payload, preparation_context)
        except ValueError as exc:
            if type(preparer) is GitPhaseResultPreparer:
                diagnostic = str(exc)
            else:
                diagnostic = "preparation failed"
            raise _reject_d4(
                code="PHASE_RESULT_PREPARATION",
                diagnostic=diagnostic,
                responsible_actor="orchestrator",
                remediation="Ensure the payload contains valid D4.4 write-gate lease, pinned post-write document and item determination evidence.",
            )
        if type(proof) is not PreparedD4Execution:
            raise _reject_d4(
                code="PHASE_RESULT_PREPARER",
                diagnostic="expected PreparedD4Execution",
                responsible_actor="system_operator",
                remediation="Verify the preparer returns the correct typed result object for the D4.4 execution checkpoint.",
            )
        return proof
    if update_kind != "phase_result":
        return None
    result_kind = update.get("result_kind")
    if result_kind != "phase_close":
        return None
    phase = update.get("phase")
    if phase not in ("D1", "D2", "D3"):
        return None
    if preparer is None:
        raise _reject(
            code="PHASE_RESULT_PREPARER",
            diagnostic="preparer is None",
            responsible_actor="system_operator",
            remediation="Configure a valid GitPhaseResultPreparer instance before invoking phase result preparation.",
        )
    try:
        proof = preparer(payload, preparation_context)
    except ValueError as exc:
        if type(preparer) is GitPhaseResultPreparer:
            diagnostic = str(exc)
        else:
            diagnostic = "preparation failed"
        raise _reject(
            code="PHASE_RESULT_PREPARATION",
            diagnostic=diagnostic,
            responsible_actor="orchestrator",
            remediation="Ensure the payload contains valid Git evidence and the preparation context is correctly initialized.",
        )
    if phase == "D1":
        if type(proof) is not PreparedD1Result:
            raise _reject(
                code="PHASE_RESULT_PREPARER",
                diagnostic="expected PreparedD1Result",
                responsible_actor="system_operator",
                remediation="Verify the preparer returns the correct typed result object for D1 phase close.",
            )
    elif phase == "D2":
        if type(proof) is not PreparedD2Result:
            raise _reject(
                code="PHASE_RESULT_PREPARER",
                diagnostic="expected PreparedD2Result",
                responsible_actor="system_operator",
                remediation="Verify the preparer returns the correct typed result object for D2 phase close.",
            )
    elif phase == "D3":
        if type(proof) is not PreparedD3Result:
            raise _reject(
                code="PHASE_RESULT_PREPARER",
                diagnostic="expected PreparedD3Result",
                responsible_actor="system_operator",
                remediation="Verify the preparer returns the correct typed result object for D3 phase close.",
            )
    return proof


def admit_phase_result(context, card_id, initiative_id, update):
    result_kind = update.get("result_kind")
    if result_kind != "phase_close":
        return None
    phase = update.get("phase")
    if phase not in ("D1", "D2", "D3"):
        return None
    prepared = context.prepared_phase_result
    try:
        if phase == "D1":
            return admit_d1_result(context, card_id, initiative_id, update, prepared)
        elif phase == "D2":
            return admit_d2_result(context, card_id, initiative_id, update, prepared)
        else:
            return admit_d3_result(context, card_id, initiative_id, update, prepared)
    except ValueError as exc:
        raise _reject(
            code="PHASE_RESULT_EVIDENCE",
            diagnostic=str(exc),
            responsible_actor="orchestrator",
            remediation="Ensure the phase result evidence matches the expected immutable schema and prior findings are fully referenced.",
        )
