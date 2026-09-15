"""Write-Gate's typed, one-shot approval adapter.

The public approval facade was decomposed in Hermes v0.21.3.  Adrian's
Write-Gate plugin still needs one deliberately narrow host primitive: present
an exact ``write_gate`` request on the active human surface and approve only
an explicit, correlated ``once`` decision.

All facade state is resolved at call time so the adapter shares the live
gateway queues and remains compatible with tests/integrations that patch the
public ``tools.approval`` seam.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging


logger = logging.getLogger(__name__)


def _host():
    from tools import approval

    return approval


def _now_iso() -> str:
    """Return the host decision time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


def request_write_gate_approval(
    *,
    request_id: str,
    command: str,
    description: str,
    session_key: str = "",
    timeout_seconds: int | None = None,
) -> dict:
    """Request one correlated Write-Gate approval on the active surface.

    Session and permanent grants are never offered.  Unattended operation,
    missing presentation surfaces, timeouts, transport failures, and every
    decision other than ``once`` fail closed.

    ``timeout_seconds`` is retained in the stable plugin-facing signature.
    The active Hermes surface owns its configured human-wait window.
    """
    del timeout_seconds
    host = _host()

    if (
        host._is_single_query_approval_context()
        or host._is_cron_approval_context()
        or host._is_unattended_platform_approval_context()
    ):
        logger.info(
            "Write-Gate approval denied (no human present): %s", request_id
        )
        return _denied(request_id)

    pattern_key = f"write_gate:{request_id}"
    surface = "gateway" if host._is_gateway_approval_context() else "cli"
    transport_attempt = host._present_with_selected_transport(
        command=command,
        description=description,
        pattern_key=pattern_key,
        pattern_keys=[pattern_key],
        session_key=session_key,
        surface=surface,
        allow_session=False,
        allow_permanent=False,
    )
    if transport_attempt.get("selected"):
        return _write_gate_from_transport_result(transport_attempt, request_id)

    if host._is_gateway_approval_context():
        with host._lock:
            notify_cb = host._gateway_notify_cbs.get(session_key)
        if notify_cb is None:
            logger.info(
                "Write-Gate approval denied (gateway has no presenter): %s",
                request_id,
            )
            return _denied(request_id)

        approval_data = {
            "command": command,
            "pattern_key": pattern_key,
            "pattern_keys": [pattern_key],
            "description": description,
            "allow_permanent": False,
            "allow_session": False,
        }
        host_reference = request_id

        def _notify_and_capture(data: dict) -> None:
            nonlocal host_reference
            host_reference = data.get("request_id") or request_id
            notify_cb(data)

        decision = host._await_gateway_decision(
            session_key,
            _notify_and_capture,
            approval_data,
            surface="gateway",
        )
        decision.setdefault("request_id", host_reference)
        return _write_gate_from_gateway_decision(decision, request_id)

    try:
        choice = host.prompt_dangerous_approval(
            command,
            description,
            allow_permanent=False,
            allow_session=False,
        )
    except Exception as exc:
        logger.warning("Write-Gate approval transport failed: %s", exc)
        return _denied(request_id)
    return _write_gate_from_cli_choice(choice, request_id)


def _denied(request_id: str, decision: str = "deny") -> dict:
    return {
        "approved": False,
        "decision": decision or "deny",
        "approval_reference": request_id,
        "decision_at": "",
    }


def _write_gate_from_transport_result(
    transport_attempt: dict, request_id: str
) -> dict:
    if transport_attempt.get("failure"):
        return _denied(request_id)
    return _write_gate_from_cli_choice(
        transport_attempt.get("choice"), request_id
    )


def _write_gate_from_gateway_decision(decision: dict, request_id: str) -> dict:
    host_ref = decision.get("request_id") or request_id
    if not decision.get("resolved") or decision.get("choice") != "once":
        return _denied(host_ref, decision.get("choice") or "deny")
    return {
        "approved": True,
        "decision": "once",
        "approval_reference": host_ref,
        "decision_at": _now_iso(),
    }


def _write_gate_from_cli_choice(choice: str | None, request_id: str) -> dict:
    if choice == "once":
        return {
            "approved": True,
            "decision": "once",
            "approval_reference": request_id,
            "decision_at": _now_iso(),
        }
    return _denied(request_id, choice or "deny")
