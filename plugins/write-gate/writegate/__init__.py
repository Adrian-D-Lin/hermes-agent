"""WriteGate plugin package.

Integrated Session-Startup + WriteGate runtime for the Hermes AI Harness.

The package is laid out as four concerns, all under ``writegate``:

* :mod:`writegate.registry` — the **central security registry**: one
  cross-profile SQLite database (``get_default_hermes_root()/write-gate.db``)
  holding bindings, requests and leases.  WAL mode, ``0600`` permissions,
  busy timeout, idempotent migration, fail-closed on error.
* :mod:`writegate.containment` — canonical-path containment, traversal and
  symlink-escape checks used by every enforcement path.  Pure, no I/O.
* :mod:`writegate.binding` — the trusted top-level binding producer
  (``confirm_binding``) and the lineage-derivation rules (resume / compression
  / branch / delegate / ``/new`` / legacy).
* :mod:`writegate.approval` — the bounded approval primitive: the ``once`` /
  ``deny`` approval transport contract, the five-minute lease and the
  ``request_exception`` presentation data.
* :mod:`writegate.recovery` — the ``5-archive`` pre-edit snapshot writer
  (modify / create / move-rename / recovery-failure) under
  ``<project-root>/5-archive/write-gate/<session>/<lease>/``.
* :mod:`writegate.enforcement` — the ``pre_tool_call`` enforcement decision
  (allow / govern / block) that the plugin registers as a hook.

The plugin is **inert** unless ``security.write_gate.enabled`` is true.  It
registers exactly one service-gated tool, ``write_gate`` (action discriminator
``confirm_binding`` / ``request_exception`` / optional ``status``), and one
``pre_tool_call`` hook.  It never mutates the prompt or changes toolsets
mid-session.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = [
    "registry",
    "containment",
    "binding",
    "approval",
    "recovery",
    "enforcement",
]
