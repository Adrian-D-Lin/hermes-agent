"""The ``write_gate`` control tool.

One service-gated tool, registered in the ``write_gate`` toolset, with an
``action`` discriminator:

* ``confirm_binding`` — trigger the trusted top-level binding operation.  The
  model may *trigger* the request but cannot write the registry or supply a
  raw authority path.
* ``request_exception`` — build a structured out-of-worktree / protected
  exception request (narrowest folder + recovery location computed by the
  control, not trusted from the model).
* ``status`` (read-only, optional) — report the session's binding / lease state
  for recovery and diagnosis.

The tool is inert unless ``security.write_gate.enabled`` is true (the plugin
entrypoint gates on that before registering anything).

Authority model
---------------
The ``session_id`` is **host-owned**: ``model_tools`` forwards it into
``registry.dispatch`` kwargs, and the registered handler passes it in as a
trusted keyword.  The model may *not* supply ``session_id`` (or
``confirmed_worktree``, ``project``, ``board``, ``profile``, ``lease_id``,
``request_id``, or ``recovery_location``) as authority — those values are
ignored when they appear in the model's argument payload, and the binding /
worktree / recovery location are always derived from trusted state (the
central registry, and the real Git project root).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from . import approval as _approval
from . import binding as _binding
from . import enforcement as _enforcement
from . import recovery as _recovery
from . import registry as _registry
from .containment import canonicalize_target as _canonicalize_safe

# Model-supplied argument keys that carry authority and must never be trusted.
# The tool resolves all of these from trusted state instead.
_UNTRUSTED_MODEL_FIELDS = frozenset({
    "session_id",
    "confirmed_worktree",
    "project",
    "initiative",
    "board",
    "profile",
    "git_branch",
    "lease_id",
    "request_id",
    "recovery_location",
    "kanban_task",
})


def _get_reg() -> _registry.Registry:
    return _registry.get_registry()


def write_gate_tool(action: str = "", session_id: str = "", task_id: str = "", **kwargs: Any) -> str:
    """Entry point for the ``write_gate`` tool.  Returns a JSON status string.

    ``session_id`` is the **host-owned** value forwarded by ``model_tools``
    through ``registry.dispatch`` kwargs.  Any ``session_id`` (or other
    authority field) present in the model's argument payload is ignored.

    ``task_id`` is the trusted top-level session key the runtime forwards
    through the same kwargs.  The per-session CWD record that drives top-level
    binding is keyed by the task id (not the possibly-diverged agent
    ``session_id``), so it is threaded into the derivation.  A model-supplied
    ``task_id`` is ignored — only the runtime-forwarded value is trusted.
    """
    try:
        import json
        # Unknown action is reported regardless of session state, so callers get
        # a precise error. The trusted session_id is only required for the
        # real actions, which resolve authority from host-owned state.
        if action not in ("confirm_binding", "reanchor", "request_exception", "status"):
            return json.dumps({
                "success": False,
                "error": (
                    f"write_gate: unknown action {action!r}. "
                    "Use 'confirm_binding', 'reanchor', 'request_exception', or 'status'."
                ),
            })
        if not session_id:
            return json.dumps({
                "success": False,
                "error": (
                    "write_gate: the runtime could not resolve this session's "
                    "trusted id; refusing to act."
                ),
            })
        if action == "confirm_binding":
            return _handle_confirm_binding(session_id, task_id)
        if action == "reanchor":
            return _handle_reanchor(session_id, task_id)
        if action == "request_exception":
            return _handle_request_exception(session_id, kwargs)
        if action == "status":
            return _handle_status(session_id)
        return json.dumps({
            "success": False,
            "error": (
                f"write_gate: unknown action {action!r}. "
                "Use 'confirm_binding', 'request_exception', or 'status'."
            ),
        })
    except _approval.ApprovalError as exc:
        import json
        return json.dumps({"success": False, "error": str(exc), "recoverable": True})
    except Exception as exc:  # fail closed: surface, never silently allow
        import json
        return json.dumps({"success": False, "error": f"write_gate: {exc}"})


def _build_binding_candidate(
    reg: "_registry.Registry",
    session_id: str,
    model_args: Dict[str, Any],
    *,
    task_id: Optional[str] = None,
    reanchor: bool = False,
) -> Optional["_binding.BindingCandidate"]:
    """Derive the binding candidate from **trusted** state, never a model path.

    The model may not supply a raw authority path.  ``confirmed_worktree``,
    ``project``, ``board``, ``profile``, etc. supplied by the model are
    ignored: the authoritative candidate is resolved from trusted
    project/board/task state when available (via the dispatcher-owned lineage
    on the central registry), then from the host-owned session cwd record
    (:func:`tools.terminal_tool.get_session_cwd`) resolved to its real Git
    worktree root, otherwise from the runtime's current real Git worktree.  A
    model-selected arbitrary path is rejected.

    ``reanchor=True`` re-derives from the current trusted source (the host-owned
    session cwd record) even when the session already has an active binding, so
    a re-anchor of a changed worktree supersedes the old one rather than
    re-presenting it.  A normal ``confirm_binding`` on an already-bound session
    short-circuits to ``already_bound`` before this is reached.
    """
    producer = _binding.TrustedBindingProducer(reg)

    # A model-supplied ``confirmed_worktree`` is diagnostic evidence only: it is
    # compared against the trusted candidate but never selects the worktree.
    # Adopting it here would let the model supply the binding path, so the
    # branch is intentionally absent -- see the trust-boundary note below.

    # Prefer a trusted dispatcher-derived binding already recorded for this
    # session (the Kanban worker preassignment path).  When one exists it is
    # authoritative; the model cannot override it.  A re-anchor deliberately
    # re-derives from the current trusted source instead.
    existing = reg.get_active_binding(session_id)

    # Resolve the authoritative worktree from trusted state: an existing
    # binding's worktree, the host-owned session cwd record resolved to its
    # real Git worktree root, or the current real Git worktree.
    worktree_path = ""
    source = "derived-from-card"
    if existing is not None and existing.worktree_path and not reanchor:
        worktree_path = existing.worktree_path
        source = "derived-from-card"
    else:
        # The trusted top-level candidate: derive from the host-owned session
        # cwd record (resolved to its real Git worktree root) when present.
        trusted = _binding.resolve_trusted_worktree(
            session_id, task_id=task_id,
            profile=existing.profile if existing else None,
        )
        if trusted:
            worktree_path = trusted
            source = "derived-from-session-cwd"

    if not worktree_path:
        return None

    candidate = producer.derive_candidate(
        session_id=session_id,
        worktree_path=worktree_path,
        project=existing.project if existing else None,
        initiative=existing.initiative if existing else None,
        board=existing.board if existing else None,
        git_branch=existing.git_branch if existing else None,
        profile=existing.profile if existing else None,
        source=source,
        model_supplied_path=None,
    )
    if candidate is None:
        return None
    return candidate


def _handle_confirm_binding(session_id: str, task_id: Optional[str] = None) -> str:
    import json
    reg = _get_reg()

    # The model may NOT supply a raw authority path.  A candidate worktree may
    # be provided as a diagnostic hint, but the binding is always derived from
    # trusted state (the trusted producer writes the row, not the model).
    producer = _binding.TrustedBindingProducer(reg)

    # If the session already has an active binding, report it rather than
    # silently re-binding.  Re-anchoring is another explicit confirmation.
    existing = reg.get_active_binding(session_id)
    if existing is not None:
        return json.dumps({
            "success": True,
            "status": "already_bound",
            "binding": existing.to_dict(),
            "note": (
                "This session already has an active binding. Re-anchoring is "
                "another explicit confirmation."
            ),
        })

    # Present the derived candidate to the human via the approval transport.
    # The transport returns a decision; only a ``once`` decision persists.
    candidate = _build_binding_candidate(reg, session_id, {}, task_id=task_id)
    if candidate is None:
        return json.dumps({
            "success": False,
            "error": (
                "write_gate: cannot present a binding candidate — the worktree "
                "must be derived from trusted project/board/task state, not a "
                "model-selected path."
            ),
        })

    approval = _present_and_get_decision(
        candidate.to_presentation(), kind="CONFIRMED_WORKTREE_BINDING"
    )
    record = producer.confirm(candidate, confirmed=bool(approval["approved"]))
    if record is None:
        return json.dumps({
            "success": True,
            "status": "declined",
            "note": "No binding persisted (declined / timed out / denied).",
        })
    return json.dumps({"success": True, "status": "bound", "binding": record.to_dict()})


def _handle_reanchor(session_id: str, task_id: Optional[str] = None) -> str:
    """Explicit re-anchor: a second human confirmation that supersedes the
    prior active binding and preserves a coherent supersession history.

    The candidate is derived from the same trusted state as ``confirm_binding``
    (the host-owned session cwd record resolved to its real Git worktree root,
    or the existing binding's worktree).  The model may not supply a path; the
    approval supersedes the prior active binding, and a deny/timeout leaves the
    old binding active.
    """
    import json
    reg = _get_reg()

    existing = reg.get_active_binding(session_id)
    if existing is None:
        # No prior binding to re-anchor: re-anchor is a privileged operation on
        # an already-bound session. A fresh top-level binding is the
        # ``confirm_binding`` path, not re-anchor.
        return json.dumps({
            "success": False,
            "error": (
                "write_gate: reanchor requires an already-bound session; use "
                "confirm_binding to establish the initial binding."
            ),
        })

    # Re-derive from trusted state. The diagnostic hint is ignored as authority.
    candidate = _build_binding_candidate(reg, session_id, {}, task_id=task_id, reanchor=True)
    if candidate is None:
        return json.dumps({
            "success": False,
            "error": (
                "write_gate: cannot re-anchor — the worktree must be derived "
                "from trusted project/board/task state, not a model-selected path."
            ),
        })

    approval = _present_and_get_decision(
        candidate.to_presentation(), kind="CONFIRMED_WORKTREE_BINDING"
    )
    # Fail closed: only an explicit confirmed decision persists/supersedes. A
    # deny / timeout persists nothing and leaves the prior binding active.
    producer = _binding.TrustedBindingProducer(reg)
    record = producer.confirm(
        candidate, confirmed=bool(approval["approved"]), profile=candidate.profile,
    )
    if record is None:
        return json.dumps({
            "success": True,
            "status": "declined",
            "note": "No binding persisted (declined / timed out / denied). The prior binding stays active.",
        })
    return json.dumps({
        "success": True,
        "status": "reanchored",
        "binding": record.to_dict(),
        "note": (
            "Re-anchored: the prior active binding was superseded and a "
            "coherent supersession history is preserved."
        ),
    })


def _handle_request_exception(session_id: str, model_args: Dict[str, Any]) -> str:
    import json
    reg = _get_reg()

    affected = model_args.get("affected_files") or []
    if not isinstance(affected, list) or not affected:
        return json.dumps({
            "success": False,
            "error": "write_gate: request_exception requires 'affected_files' (a non-empty list)",
        })
    outcome = str(model_args.get("stated_outcome") or "").strip()

    # A current active binding is required before minting external/protected
    # authority.  Falling back to ``worktree="."`` (the ambient process cwd)
    # would let an unbound session lease out-of-worktree / protected writes
    # relative to wherever the process happens to run — an unbound session has
    # no confirmed worktree to scope a lease to.  Require the binding;
    # otherwise return a structured bind-first denial and create no request or
    # lease row.
    existing = reg.get_active_binding(session_id)
    if existing is None or not existing.worktree_path:
        return json.dumps({
            "success": False,
            "error": (
                "write_gate: request_exception requires a confirmed worktree "
                "binding; confirm or re-anchor the session first via "
                "confirm_binding / reanchor. An unbound session cannot mint "
                "out-of-worktree authority."
            ),
        })
    worktree = existing.worktree_path

    lease_id = uuid.uuid4().hex
    recovery_location = _approval.compute_recovery_location(
        worktree, session_id, lease_id
    )
    request = _approval.ExceptionRequest(
        session_id=session_id,
        confirmed_worktree=worktree,
        affected_files=[str(f) for f in affected],
        stated_outcome=outcome,
        recovery_location=recovery_location,
    )
    # request_id is host-generated; the model may not supply one.
    request_id = f"wg-{uuid.uuid4().hex}"
    reg.create_request(
        request_id=request_id,
        session_id=session_id,
        confirmed_worktree=worktree,
        narrowest_folder=request.narrowest_common_folder(),
        recovery_location=recovery_location,
        stated_outcome=outcome,
    )
    presentation = request.to_presentation()
    presentation["request_id"] = request_id
    # Present for approval; mint the lease only on a ``once`` decision.
    approval = _present_and_get_decision(
        presentation, kind="WRITE_GATE_EXCEPTION"
    )
    lease = _approval.mint_lease(
        reg,
        request_id=request_id,
        approval_reference=approval["approval_reference"],
        decision="once" if approval["approved"] else "deny",
        decision_at=approval["decision_at"],
        request=request,
    )
    if lease is None:
        return json.dumps({
            "success": True,
            "status": "denied",
            "request_id": request_id,
            "note": "Exception denied; no lease minted. Retry the write after a fresh approval.",
        })
    return json.dumps({
        "success": True,
        "status": "lease_created",
        "request_id": request_id,
        "lease": lease.to_dict(),
        "note": (
            "Lease created. Retry the original write; the pre-tool hook will "
            "recheck the lease and snapshot recovery evidence before allowing it."
        ),
    })


def _handle_status(session_id: str) -> str:
    import json
    reg = _get_reg()
    binding = reg.get_active_binding(session_id)
    leases = [l.to_dict() for l in reg.list_active_leases(session_id=session_id)]
    return json.dumps({
        "success": True,
        "session_id": session_id,
        "binding": binding.to_dict() if binding else None,
        "active_leases": leases,
    })


# ---------------------------------------------------------------------------
# Approval presentation seam
# ---------------------------------------------------------------------------

def _format_approval_command(
    presentation: Dict[str, Any], kind: str
) -> str:
    def _get(key: str) -> str:
        value = presentation.get(key)
        if value is None:
            return ""
        return str(value)

    if kind == "WRITE_GATE_EXCEPTION":
        lines = [
            "WRITE-GATE EXCEPTION REQUEST",
            f"Outcome: {_get('stated_outcome')}",
            "Affected files:",
        ]
        affected = presentation.get("affected_files")
        if affected is None:
            items = []
        elif isinstance(affected, str):
            items = [affected]
        elif isinstance(affected, (list, tuple)):
            items = list(affected)
        else:
            items = [affected]
        for item in items:
            lines.append(f"  - {item}")
        lines.append(f"Approved folder: {_get('approved_folder')}")
        lines.append(f"Recovery location: {_get('recovery_location')}")
        lines.append(f"Lease validity: {_get('lease_validity')}")
        lines.append(f"Confirmed worktree: {_get('confirmed_worktree')}")
        lines.append(f"Session: {_get('session_id')}")
        lines.append(f"Warning: {_get('warning')}")
        return "\n".join(lines)

    if kind == "CONFIRMED_WORKTREE_BINDING":
        lines = [
            "WRITE-GATE WORKTREE BINDING",
            f"Project: {_get('project')}",
            f"Initiative: {_get('initiative')}",
            f"Board: {_get('board')}",
            f"Worktree: {_get('worktree_path')}",
            f"Branch: {_get('git_branch')}",
            f"Profile: {_get('profile')}",
            f"Confirmation basis: {_get('confirmation_basis')}",
            f"Session: {_get('session_id')}",
            f"Note: {_get('note')}",
        ]
        return "\n".join(lines)

    return "\n".join([
        "WRITE-GATE REQUEST",
        f"Kind: {kind}",
        f"Session: {_get('session_id')}",
    ])


def _present_and_get_decision(
    presentation: Dict[str, Any], kind: str
) -> Dict[str, Any]:
    """Ask a human to authorize this Write-Gate request (``once`` / ``deny``).

    Routes through the host-owned, ``once``-only approval primitive in
    :mod:`tools.approval` — the same CLI / gateway / TUI / desktop path used for
    dangerous-command approvals.  It does **not** register a new approval
    transport (Hermes transports are presentation backends, not request types).

    Returns the host-owned structured approval result.  Only an explicit
    ``once`` is normalized to ``approved=True``; every other outcome fails
    closed.  The host's approval reference and decision timestamp are kept so
    an exception lease is auditable against the exact surfaced approval.
    """
    request_id = str(
        presentation.get("request_id")
        or f"wg-binding-{uuid.uuid4().hex}"
    )
    deny = {
        "approved": False,
        "decision": "deny",
        "approval_reference": request_id,
        "decision_at": "",
    }
    try:
        from tools.approval import request_write_gate_approval
        session_id = presentation.get("session_id", "")
        requested_change = (
            presentation.get("stated_outcome")
            or presentation.get("worktree_path")
            or ""
        )
        description = (
            f"{kind}: {requested_change} for session {session_id}"
        )
        command = _format_approval_command(presentation, kind)
        result = request_write_gate_approval(
            request_id=request_id,
            command=command,
            description=description,
            session_key=session_id,
        )
    except Exception:
        # Transport failure -> fail closed (deny), never auto-confirm.
        return deny
    if not isinstance(result, dict) or not result.get("approved"):
        deny["approval_reference"] = str(
            result.get("approval_reference") or request_id
        ) if isinstance(result, dict) else request_id
        return deny
    if result.get("decision") != "once" or not result.get("decision_at"):
        return deny
    return {
        "approved": True,
        "decision": "once",
        "approval_reference": str(
            result.get("approval_reference") or request_id
        ),
        "decision_at": str(result["decision_at"]),
    }
