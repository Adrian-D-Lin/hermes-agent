"""adrian-kanban plugin package (S1 foundation skeleton).

This package is intentionally INACTIVE by default. The installed Kanban
default is ``kanban.mutation_authority: native`` (see
``hermes_cli/config_defaults.py``), so the in-tree engine remains the sole
mutation + dispatch authority until an operator explicitly selects
``adrian-kanban``.

S1 establishes only:

* the generic, fail-closed authority seam (:mod:`adrian_kanban.seam`),
* a test-only provider fixture that proves the seam can delegate an
  already-admitted operation (no production capability is implemented),
* the non-public foundational schema/store foundation
  (:mod:`adrian_kanban.schema`, :mod:`adrian_kanban.store`,
  :mod:`adrian_kanban.validation`), and
* a command-boundary stub (:mod:`adrian_kanban.commands`).

Production capability creation, validation, binding, consumption, typed
initiative approval, the private worker adapter, plugin eligibility dispatch,
and Write-Gate admission are **S2** and are deliberately not implemented here.
"""

from __future__ import annotations

# Canonical plugin identity. Kept in lockstep with plugin.yaml ``version``.
PLUGIN_NAME = "adrian-kanban"
PLUGIN_VERSION = "0.1.0"


def register(ctx) -> None:
    """PluginManager-required entry point.

    Accepts the plugin context and returns ``None`` without registering any
    provider, tool, hook, or behavior. The plugin remains intentionally
    inactive; no capability is wired in at this stage.
    """
    return None


__all__ = ["PLUGIN_NAME", "PLUGIN_VERSION", "register"]
