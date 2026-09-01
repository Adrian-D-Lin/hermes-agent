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
                    "enum": ["confirm_binding", "request_exception", "status"],
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
    try:
        from writegate import enforcement as _enforcement
        from writegate import registry as _registry

        # Host-owned session id only. model_tools forwards it through
        # registry.dispatch kwargs; the model may NOT supply it (or any other
        # authority field). There is no args fallback — a model-supplied
        # session_id must never be trusted.
        session_id = str(kwargs.get("session_id") or "") or str(
            kwargs.get("session") or ""
        )
        if not session_id:
            # No host-owned session id: fail closed. A governed write with no
            # trusted session identity must be blocked, not allowed through.
            return {"action": "block", "message": "Write-Gate: no host-owned session id; fail-closed block"}
        reg = _registry.get_registry()
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
