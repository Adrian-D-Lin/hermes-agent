"""WriteGate plugin entrypoint.

Registers, **only when ``security.write_gate.enabled`` is true**:

* one service-gated tool, ``write_gate`` (action discriminator
  ``confirm_binding`` / ``request_exception`` / optional ``status``), in a
  ``write_gate`` toolset;
* one ``pre_tool_call`` enforcement hook.

The plugin is inert otherwise.  It performs no permanent prompt mutation and
never changes toolsets mid-session.  The hook and tool read the central
registry at ``get_default_hermes_root()/write-gate.db`` (cross-profile).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

TOOLSET = "write_gate"
HOOK_EVENT = "pre_tool_call"


def _ensure_host_package_importable() -> None:
    """Make the repo-root ``writegate`` host package importable.

    The shared primitives live at the repository root (not inside the
    plugin-private directory).  When the plugin is loaded as an isolated
    module the repo root may not already be on ``sys.path`` (e.g. a pip
    install), so add it if the package is not yet resolvable.  This lets the
    thin registration surface import ``writegate`` regardless of loader or
    ordering.
    """
    import importlib.util
    import os
    import sys

    if importlib.util.find_spec("writegate") is not None:
        return

    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(here, "writegate")):
            root = os.path.abspath(here)
            if root not in sys.path:
                sys.path.insert(0, root)
            break
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent


# Ensure the host package resolves before any ``register()`` import.
_host_import_ready = False
try:
    _ensure_host_package_importable()
    _host_import_ready = True
except Exception:  # pragma: no cover - defensive, loader-dependent
    _host_import_ready = False


def _write_gate_enabled() -> bool:
    """Read ``security.write_gate.enabled`` from the global config.

    ``ctx.get_config`` only exposes plugin-relative settings, so the global
    security flag is read directly through the readonly config loader.  Absent
    key -> disabled (fail closed: the plugin is inert unless explicitly enabled).
    """
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
        security = config.get("security") if isinstance(config, dict) else None
        if not isinstance(security, dict):
            return False
        return bool(security.get("write_gate", {}).get("enabled", False))
    except Exception:
        # Config unavailable -> inert, not enabled.
        return False


def register(ctx) -> None:
    if not _write_gate_enabled():
        logger.debug("write_gate: security.write_gate.enabled is false; plugin inert")
        return

    from writegate import enforcement as _enforcement
    from writegate import tool as _tool

    # -- tool -------------------------------------------------------------
    ctx.register_tool(
        name="write_gate",
        toolset=TOOLSET,
        schema={
            "name": "write_gate",
            "description": (
                "Write-Gate control: confirm the session's confirmed-worktree "
                "binding at startup (trusted runtime operation, not the model), "
                "submit a structured out-of-worktree / protected-location "
                "exception request (narrowest folder + 5-minute lease), or read "
                "the current binding/lease status. Governance is enforced by "
                "the Write-Gate pre_tool_call hook; this tool drives the "
                "trusted operations only."
            ),
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["confirm_binding", "reanchor", "request_exception", "status"],
                    "description": "The Write-Gate operation to perform.",
                },
                "stated_outcome": {
                    "type": "string",
                    "description": "Concise stated outcome for an exception request.",
                },
                "affected_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact affected files for an exception request.",
                },
            },
            "required": ["action"],
        },
        handler=lambda args, **kw: _tool.write_gate_tool(
            action=args.get("action", ""),
            # The session id is host-owned: model_tools forwards it through
            # registry.dispatch kwargs. The model may NOT supply it (or any
            # other authority field) — see the security gate in the build
            # brief §"Security review gate".
            session_id=str(kw.get("session_id") or ""),
            # The trusted top-level task id is forwarded through the same
            # kwargs and drives the host-owned CWD record the binding derives
            # from. The model may NOT supply it.
            task_id=str(kw.get("task_id") or ""),
            stated_outcome=args.get("stated_outcome", ""),
            affected_files=args.get("affected_files") or [],
        ),
        description=(
            "Confirm worktree binding, request a protected-write exception, or "
            "read binding/lease status under the central Write-Gate registry."
        ),
    )

    # -- pre_tool_call enforcement hook ----------------------------------
    ctx.register_hook(HOOK_EVENT, _on_pre_tool_call)

    logger.info(
        "write_gate: enabled — registered tool 'write_gate' (toolset '%s') "
        "and pre_tool_call enforcement hook", TOOLSET,
    )


def _on_pre_tool_call(
    tool_name: str = "",
    args: object = None,
    **kwargs: object,
) -> dict:
    """Write-Gate ``pre_tool_call`` enforcement.

    Returns a ``{"action": "block", "message": ...}`` directive when the
    mutation must be rejected, or ``None`` when the call may proceed.  Any
    registry/policy error fails closed (block).
    """
    if not tool_name:
        return None
    if not isinstance(args, dict):
        args = args or {}
    # Trusted top-level task id forwarded through the same kwargs as
    # session_id. The CWD record the binding derives from is keyed by the task
    # id (the top-level session key), which is authoritative over the possibly
    # diverged agent session_id.
    task_id = str(kwargs.get("task_id") or "")
    try:
        from writegate import enforcement as _enforcement
        from writegate import registry as _registry

        # Classification comes before identity enforcement.  Reads, the
        # Write-Gate control tool, and the exact canonical Kanban operations
        # are intentionally outside the governed-write path and therefore do
        # not require a session binding.  Requiring a host session id first
        # would turn the Write-Gate into a read gate and can deadlock startup
        # before Session Startup has established the identity it needs.
        if _enforcement.is_always_allowed(str(tool_name)):
            return None

        # Host-owned session id only. model_tools forwards it through
        # registry.dispatch kwargs; the model may NOT supply it (or any other
        # authority field). There is no args fallback — a model-supplied
        # session_id must never be trusted.
        session_id = str(kwargs.get("session_id") or "") or str(
            kwargs.get("session") or ""
        )
        if not session_id:
            # No host-owned session id: fail closed for a governed write.  The
            # intentionally exempt routes returned above.
            return {"action": "block", "message": "Write-Gate: no host-owned session id; fail-closed block"}
        reg = _registry.get_registry()

        # Bounded lazy lineage: before deciding, derive a binding only from
        # trusted *existing* lineage — the session's own active binding, or a
        # verified branch/compression/delegate parent. This never auto-binds a
        # top-level session from cwd (that requires explicit human
        # confirm_binding), and /new + ordinary unbound sessions still block.
        # ``_writegate_profile_dir`` is a host/test seam for the SessionDB
        # location; absent it, the resolver uses the live profile home.
        profile_dir = kwargs.get("_writegate_profile_dir")
        if isinstance(profile_dir, os.PathLike):
            profile_dir = os.fspath(profile_dir)
        if not isinstance(profile_dir, str):
            profile_dir = None
        _maybe_lazy_derive_binding(reg, session_id, task_id, profile_dir)

        project_root = _resolve_project_root(reg, session_id)
        decision = _enforcement.decide(
            tool_name=str(tool_name),
            args=dict(args) if isinstance(args, dict) else {},
            session_id=session_id,
            reg=reg,
            project_root=project_root,
            recovery_writer_factory=_recovery_factory(reg, session_id),
            now_iso=_now_iso(),
            base_dir=project_root,
        )
        block = decision.to_block()
        if block is not None:
            logger.info(
                "write_gate: blocked %s(%r) for session %s: %s",
                tool_name, args, session_id, block.get("message"),
            )
        return block or None
    except Exception as exc:  # registry/policy failure -> fail closed
        logger.warning("write_gate: pre_tool_call error (fail-closed block): %s", exc)
        return {"action": "block", "message": f"Write-Gate: enforcement error, fail-closed: {exc}"}


def _recovery_factory(reg, session_id):
    def _factory(worktree: str, sid: str, lease_id: str):
        from writegate import recovery as _recovery
        project_root = _recovery.derive_project_root(worktree) or worktree
        return _recovery.RecoveryWriter(reg, project_root, sid, lease_id)
    return _factory


def _maybe_lazy_derive_binding(
    reg: "_registry.Registry",
    session_id: str,
    task_id: str,
    profile_dir: "str | None" = None,
) -> None:
    """Bounded lazy lineage: derive a binding only from trusted *existing*
    lineage, before enforcement decides.

    This is NOT an auto-bind from ambient cwd.  It resolves:

    * the session's own active binding (resume / in-place compression — the
      same id keeps its binding), or
    * a verified branch/compression/delegate parent whose parent is actively
      bound.  The verified evidence comes from the profile's trusted
      SessionDB row markers (:func:`writegate.lineage.resolve_lineage`):
      ``_branched_from == parent_session_id`` (branch),
      ``_delegate_from == parent_session_id`` or ``source == "tool"``
      (delegate), or parent ``end_reason == "compression"`` with no fork
      marker (compression child).  ``/new`` / gateway resets
      (``_reset_from == parent_session_id``) and ordinary unbound sessions
      derive nothing.

    The ``task_id`` argument is accepted for seam compatibility but is NOT
    lineage proof — lineage is read from the SessionDB row only.  Fail-closed:
    any error is swallowed and the session stays as it was (unbound → blocked).
    """
    try:
        from writegate import binding as _binding
        from writegate import lineage as _lineage

        # Idempotent: a session that already has its own active binding is a
        # no-op.
        if reg.get_active_binding(session_id) is not None:
            return

        verified = _lineage.resolve_lineage(session_id, profile_dir=profile_dir)
        if not verified:
            return

        # A delegate child (source == "tool" / _delegate_from) may carry a
        # trusted per-task workspace of its own: the host-owned session cwd
        # record under the task key is authoritative (A22).  Branch and
        # compression children inherit the parent's exact worktree and must
        # not be redirected through ambient state.
        assigned_workspace = None
        if verified["kind"] == "delegate":
            try:
                assigned_workspace = _binding.resolve_trusted_worktree(
                    session_id, task_id=task_id,
                )
            except Exception:
                assigned_workspace = None

        derived = _binding.derive_binding(
            reg,
            session_id=session_id,
            task_id=task_id,
            parent_session_id=verified["parent_session_id"],
            parent_is_bound=verified["parent_is_bound"],
            assigned_workspace=assigned_workspace,
        )
        # derive_binding returns None for /new, legacy/null, or unverified
        # lineage — exactly the cases that must stay unbound.
    except Exception:
        # Never let lineage derivation break enforcement: stay as-is.
        return


def _resolve_project_root(reg, session_id: str) -> str:
    """Derive the Git project root from the bound worktree (fallback: cwd)."""
    from writegate import recovery as _recovery
    binding = reg.get_active_binding(session_id)
    if binding and binding.worktree_path:
        root = _recovery.derive_project_root(binding.worktree_path)
        if root:
            return root
    import os
    return os.getcwd()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
