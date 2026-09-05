"""Bounded approval primitive for WriteGate.

Implements the pivot's approval-transport contract (``§6``) and the
five-minute lease (``§9`` / ``§10``):

* A request is made at the **narrowest common folder** sufficient for the
  stated outcome.  The control *computes* that folder from the affected files;
  it never trusts a model-provided folder.
* The approval is visibly typed/correlated as ``write_gate`` and offers
  **only ``once`` and ``deny``**.  It never consults or mutates a
  session/permanent command allow-list, cannot be satisfied by generic tool
  approval / approval-all / a prior request, and fails closed on timeout,
  transport failure, wrong request id, or any other choice.
* Approval returns an approval reference + human-decision timestamp suitable
  for lease creation.  **Approval creates the request/lease only; it does not
  execute the blocked mutation.**  The agent then retries the original write;
  the pre-tool hook rechecks session/worktree/folder/scope/expiry and performs
  recovery immediately before allowing execution.

The transport is host-owned.  The plugin exposes it through the standard
``register_approval_transport`` seam; this module owns the data model and the
predicate logic so it is unit-testable without a live human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from . import registry as _registry


class ApprovalError(ValueError):
    """Raised when an approval request is malformed or a decision is invalid."""


@dataclass
class ExceptionRequest:
    """A structured ``request_exception`` presentation payload."""

    session_id: str
    confirmed_worktree: str
    affected_files: List[str]
    stated_outcome: str
    recovery_location: str
    lease_validity_seconds: int = _registry.LEASE_DURATION_SECONDS

    def narrowest_common_folder(self) -> str:
        """Compute the **narrowest common folder** of the affected files.

        Pure, path-safe: resolves each file to a canonical parent and walks up
        from the deepest file until the common prefix holds.  Never trusts the
        model to name the folder.
        """
        parents = []
        for f in self.affected_files:
            p = f.rstrip("/")
            if "/" in p:
                p = p.rsplit("/", 1)[0] or "/"
            else:
                p = "."
            parents.append(p)
        if not parents:
            raise ApprovalError("request_exception requires at least one file")
        common = parents[0].split("/")
        for p in parents[1:]:
            parts = p.split("/")
            n = 0
            for a, b in zip(common, parts):
                if a != b:
                    break
                n += 1
            common = common[:n] or [""]
        folder = "/".join(common)
        return folder or "/"

    def to_presentation(self) -> Dict[str, Any]:
        folder = self.narrowest_common_folder()
        return {
            "confirmation_kind": "WRITE_GATE_EXCEPTION",
            "session_id": self.session_id,
            "confirmed_worktree": self.confirmed_worktree,
            "stated_outcome": self.stated_outcome,
            "affected_files": list(self.affected_files),
            "approved_folder": folder,
            "recovery_location": self.recovery_location,
            "lease_validity": (
                f"Lease lasts {_registry.LEASE_DURATION_TEXT} from approval; "
                "it does not auto-renew."
            ),
            "choices": ["once", "deny"],
            "warning": (
                "This is a Write-Gate exception approval, distinct from a "
                "generic tool approval.  Generic approval / approval-all / a "
                "prior request cannot satisfy it."
            ),
        }


def compute_recovery_location(
    project_root: str, session_id: str, lease_id: str,
) -> str:
    """Derive the only v1 recovery location: ``<project-root>/5-archive/
    write-gate/<session-id>/<lease-id>/``.

    The model does not write this and no ``.write-gate-recovery/`` path is used.
    """
    import os
    return os.path.join(
        project_root, "5-archive", "write-gate", session_id, lease_id,
    )


def compute_narrowest_folder(files: Sequence[str]) -> str:
    """Module-level helper mirroring :meth:`ExceptionRequest.narrowest_common_folder`."""
    req = ExceptionRequest(
        session_id="",
        confirmed_worktree="",
        affected_files=list(files),
        stated_outcome="",
        recovery_location="",
    )
    return req.narrowest_common_folder()


# ---------------------------------------------------------------------------
# Approval transport contract
# ---------------------------------------------------------------------------

def build_approval_request(
    *,
    request_id: str,
    session_id: str,
    request: "ExceptionRequest",
) -> Dict[str, Any]:
    """Assemble the host-approved, redacted ``ApprovalRequest`` handed to the
    transport.  It is correlated as ``write_gate`` and carries the request id
    the transport must echo back.
    """
    return {
        "transport": "write_gate",
        "request_id": request_id,
        "confirmation_kind": "WRITE_GATE_EXCEPTION",
        "session_id": session_id,
        "summary": request.stated_outcome,
        "approved_folder": request.narrowest_common_folder(),
        "affected_files": list(request.affected_files),
        "recovery_location": request.recovery_location,
        "lease_validity_seconds": request.lease_validity_seconds,
    }


def validate_decision(decision: Optional[str]) -> bool:
    """Return true only for the two permitted human choices."""
    return decision in ("once", "deny")


def mint_lease(
    reg: _registry.Registry,
    *,
    request_id: str,
    approval_reference: Optional[str],
    decision: str,
    decision_at: str,
    request: "ExceptionRequest",
) -> Optional[_registry.LeaseRecord]:
    """Turn a ``once`` decision into a lease; a ``deny`` mints nothing.

    * ``once`` -> create the lease (``approved_at = decision_at``), expires
      exactly five minutes later, status ``active``.
    * ``deny`` -> record the decision on the request and return ``None``.
    * anything else -> raise :class:`ApprovalError` (fail closed).
    """
    if not validate_decision(decision):
        raise ApprovalError(f"write_gate: decision {decision!r} is not 'once' or 'deny'")
    reg.decide_request(request_id, decision)
    if decision != "once":
        return None
    return reg.create_lease(
        lease_id=request_id,  # lease_id correlates 1:1 with the request id
        approval_reference=approval_reference,
        session_id=request.session_id,
        confirmed_worktree=request.confirmed_worktree,
        approved_folder=request.narrowest_common_folder(),
        approved_at=decision_at,
    )


class ApprovalTransport:
    """Host-owned transport adapter.

    The plugin registers this under ``security.approval.transport`` only when
    the operator names it; by default the builtin transport is used.  The
    transport receives an :func:`build_approval_request` and returns a plain
    ``{"request_id", "decision", "decision_at"}`` dict, or raises on any
    failure so the caller fails closed.
    """

    def __init__(self, presenter: Optional[Any] = None):
        # ``presenter`` is an optional callable ``(request_dict) -> decision_dict``
        # used for tests and for the builtin surface.  Real deployments inject
        # the CLI/gateway/TUI/desktop transport through ``register_approval_transport``.
        self._presenter = presenter

    def present(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if self._presenter is None:
            raise ApprovalError(
                "write_gate: no approval presenter is configured for this "
                "surface; the request cannot be presented to a human."
            )
        try:
            result = self._presenter(request)
        except Exception as exc:  # transport failure -> fail closed
            raise ApprovalError(f"write_gate: approval transport failed: {exc}") from exc
        if not isinstance(result, dict):
            raise ApprovalError("write_gate: approval transport returned no decision")
        request_id = result.get("request_id")
        decision = result.get("decision")
        if request_id != request.get("request_id"):
            # Wrong request id -> fail closed.
            raise ApprovalError("write_gate: approval request id mismatch")
        if not validate_decision(decision):
            raise ApprovalError(f"write_gate: invalid decision {decision!r}")
        decision_at = result.get("decision_at") or _now_iso()
        return {"request_id": request_id, "decision": decision, "decision_at": decision_at}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
