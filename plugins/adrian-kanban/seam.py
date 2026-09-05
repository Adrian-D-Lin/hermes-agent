"""adrian-kanban: the inactive, generic mutation-authority plugin adapter.

This package is the *only* thing S1 implements on the plugin side. It is a thin
adapter over the **generic, core-owned** seam in
``hermes_cli.kanban_db``:

* :func:`register_provider` wires the plugin's provider through the core's
  generic ``register_authority_provider`` interface. The core owns the seam; the
  plugin only registers *through* it and never reaches into core internals.
* :func:`resolve_authority` — reads the machine-global selection from config.
* :func:`ensure_admitted` — delegate an already-admitted operation across the
  seam.
* :func:`health_report` — a read-only health result reporting the selected
  authority, plugin version, database path, and native-surface state.

Provider validation and status are core-owned: the generic seam in
``hermes_cli.kanban_db`` performs the health/interface gate, and core
``write_txn`` owns enforcement of the seam. The plugin delegates to the core and
does not re-implement that gate.

The seam is deliberately generic: it knows only that a provider must expose
``name``, ``is_healthy() -> bool``, and ``admit_operation(operation) -> bool``.
It never reaches into S2's production capability model, and it never imports
core-private APIs.

Inactive by default. The installed default is ``native`` (see
``hermes_cli/config_defaults.py``), so no production behavior changes until an
operator explicitly selects ``adrian-kanban``.
"""

from __future__ import annotations

from typing import Any, Optional

from hermes_cli import kanban_db as _kb

# The fail-closed diagnostic is defined in the generic, core-owned seam. The
# plugin re-exports it so existing import sites (tests, S2) keep resolving
# ``from adrian_kanban.seam import AuthorityAdmissionRejected`` without the
# plugin importing core internals directly. This is a plain alias of the core
# class.
AuthorityAdmissionRejected = _kb.AuthorityAdmissionRejected

# The single canonical authority identifier. Kept in lockstep with the core
# seam's ``AUTHORITY_ADRIAN_KANBAN`` and ``config_defaults``.
AUTHORITY_ADRIAN_KANBAN = "adrian-kanban"
AUTHORITY_NATIVE = "native"


class AdmittedAuthorityProvider:
    """The narrow authority-provider/capability interface S2 implements.

    S1 does NOT implement a production provider. This type is the contract:

    * ``name`` — a stable provider identifier.
    * ``is_healthy() -> bool`` — liveness/compatibility probe.
    * ``admit_operation(operation: str) -> bool`` — returns True only for an
      operation the provider has already admitted under S2's policy.

    S1's test-only fixture satisfies this interface so the seam can be proven
    without any production capability.
    """

    name: str = "adrian-kanban"

    def is_healthy(self) -> bool:  # pragma: no cover - interface stub
        raise NotImplementedError

    def admit_operation(self, operation: str) -> bool:  # pragma: no cover
        raise NotImplementedError


def register_provider(
    provider: AdmittedAuthorityProvider, db_path: Optional[str] = None
) -> None:
    """Register a provider through the core's generic seam.

    The plugin registers *through* ``kanban_db.register_authority_provider`` —
    it does not own or mutate the core seam. Scoping to ``db_path`` mirrors the
    machine-global DB identity the seam enforces.
    """
    _kb.register_authority_provider(provider, db_path)


def resolve_authority() -> str:
    """Return the configured mutation authority, fail-closed on error.

    Delegates to the generic, core-owned ``kanban_db.resolve_selected_authority``.
    """
    return _kb.resolve_selected_authority()


def ensure_admitted(operation: str, *, db_path: Optional[str] = None) -> bool:
    """Delegate an already-admitted operation across the generic seam.

    Returns True only when a healthy provider admits *operation*. Raises
    :class:`AuthorityAdmissionRejected` when no healthy provider is present.

    This is the S1 delegation proof: it never creates, validates, binds, or
    consumes a production capability — it only asks the provider whether an
    operation was already admitted.
    """
    return _kb.ensure_admitted(operation, db_path=db_path)


def health_report(db_path: Optional[str] = None) -> dict[str, Any]:
    """Return a read-only health result for the selected authority.

    Reports the selected authority, plugin version, resolved database path, and
    native-surface state. If ``adrian-kanban`` is selected but missing or
    unhealthy, ``native_surface_ok`` is False and ``reason`` explains the
    fail-closed state.

    The absolute path is resolved through the public core resolver and the
    provider is queried only through the public :func:`provider_status`
    query — the seam never reaches the private core registry lookup.
    """
    authority = resolve_authority()
    info: dict[str, Any] = {
        "selected_authority": authority,
        "plugin_version": "0.1.0",
        "plugin_name": AUTHORITY_ADRIAN_KANBAN,
        "db_path": db_path,
        "native_surface_ok": True,
        "reason": None,
    }
    if authority == AUTHORITY_ADRIAN_KANBAN:
        # Resolve the configured authoritative path (raises when missing or
        # relative) and query the provider through the public status query.
        authoritative = _kb.resolve_authority_path()
        info["db_path"] = authoritative
        status = _kb.provider_status(authoritative)
        available = status.present and status.healthy
        info["provider_present"] = status.present
        info["provider_healthy"] = status.healthy
        info["native_surface_ok"] = False
        reason = status.reason
        if not available:
            info["reason"] = reason or (
                "selected 'adrian-kanban' is unavailable/unhealthy/incompatible; "
                "native mutation and dispatch fail closed without fallback"
            )
    return info
