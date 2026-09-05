"""adrian-kanban command-boundary stub (S1).

This is a *stub*. S1 does not replace any public CLI/API/Desktop surface, and
it does not implement typed initiative approval, the private worker adapter, or
plugin eligibility dispatch. The boundary exists so S2 can wire real commands
against the seam without changing the native surface.

No command here performs a native mutation. Any future command must route
through :func:`adrian_kanban.seam.ensure_admitted` so the fail-closed seam is
never bypassed.
"""

from __future__ import annotations

from typing import Any


def command_boundary(action: str, **fields: Any) -> dict[str, Any]:
    """Command-boundary entry point stub.

    S1 returns a status marker only. It performs no native mutation and does
    not implement any production capability. S2 extends this boundary.
    """
    return {
        "plugin": "adrian-kanban",
        "version": "0.1.0",
        "action": action,
        "status": "stub",
        "note": "S1 command boundary is inert; no native mutation occurs.",
    }
