"""adrian-kanban: the inactive, generic mutation-authority plugin adapter.

This package is the *only* thing S1 implements on the plugin side. It is a thin
adapter over the **generic, core-owned** seam in
``hermes_cli.kanban_db``:

* :func:`register` wires the plugin's provider through the core's generic
  ``register_authority_provider`` interface. The core owns the seam; the plugin
  only registers *through* it and never reaches into core internals.
* :func:`resolve_authority` — reads the machine-global selection from config.
* :func:`provider_is_available` — the health + interface gate.
* :func:`scoped_authority` — a context manager that enforces the seam: while
  active, native mutation and dispatch either delegate to a healthy provider or
  fail closed.
* :func:`ensure_admitted` — delegate an already-admitted operation across the
  seam.
* :func:`health_report` — a read-only health result reporting the selected
  authority, plugin version, database path, and native-surface state.

The seam is deliberately generic: it knows only that a provider must expose
``name``, ``is_healthy() -> bool``, and ``admit_operation(operation) -> bool``.
It never reaches into S2's production capability model, and it never imports
core-private APIs.

Inactive by default. The installed default is ``native`` (see
``hermes_cli/config_defaults.py``), so no production behavior changes until an
operator explicitly selects ``adrian-kanban``.
"""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from hermes_cli import kanban_db as _kb

_log = logging.getLogger(__name__)

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

# The minimal interface a provider must supply for the seam to delegate.
# S2's production capability is expected to implement this same surface.
_REQUIRED_PROVIDER_ATTRS = ("name", "is_healthy", "admit_operation")


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


# The active seam scope. ``None`` or ``native`` = no enforcement.
_SCOPE_CTX: "ContextVar[Optional[str]]" = ContextVar(
    "adrian_kanban_seam_scope", default=None
)


def register_provider(
    provider: AdmittedAuthorityProvider, db_path: Optional[str] = None
) -> None:
    """Register a provider through the core's generic seam.

    The plugin registers *through* ``kanban_db.register_authority_provider`` —
    it does not own or mutate the core seam. Scoping to ``db_path`` mirrors the
    machine-global DB identity the seam enforces.
    """
    _kb.register_authority_provider(provider, db_path)


@contextlib.contextmanager
def scoped_authority(
    authority: str, *, db_path: Optional[str] = None
) -> "Iterator[None]":
    """Establish a scoped authority for the duration of a mutation/dispatch.

    While active:

    * If ``authority == 'native'`` the scope is a no-op (existing behavior).
    * If ``authority == 'adrian-kanban'`` the seam requires a healthy,
      interface-compatible provider. When present, callers may delegate an
      already-admitted operation via :func:`ensure_admitted`. When absent,
      callers must raise :class:`AuthorityAdmissionRejected` before touching
      the DB.

    ``db_path`` scopes a test-only provider to a specific board DB.
    """
    token = _SCOPE_CTX.set(authority)
    try:
        yield
    finally:  # pragma: no cover - defensive
        _SCOPE_CTX.reset(token)


def current_scope_authority() -> Optional[str]:
    """Return the active seam scope authority, or ``None`` if unset."""
    return _SCOPE_CTX.get()


def resolve_authority() -> str:
    """Return the configured mutation authority, fail-closed on error.

    Delegates to the generic, core-owned ``kanban_db.resolve_selected_authority``.
    """
    return _kb.resolve_selected_authority()


def provider_is_available(provider: Optional[AdmittedAuthorityProvider]) -> bool:
    """True only if *provider* is present, healthy, and interface-compatible.

    This is the fail-closed gate. A provider that is present but unhealthy, or
    present but missing ``name``/``is_healthy``/``admit_operation``, returns
    False so the seam refuses to delegate.
    """
    if provider is None:
        return False
    for attr in _REQUIRED_PROVIDER_ATTRS:
        if not hasattr(provider, attr):
            return False
    try:
        if not bool(provider.is_healthy()):
            return False
    except Exception:  # pragma: no cover - defensive
        _log.warning("kanban seam: provider health probe failed")
        return False
    if not callable(getattr(provider, "admit_operation", None)):
        return False
    return True


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
