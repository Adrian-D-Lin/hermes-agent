"""Generic, provider-agnostic authority seam for replacement Kanban engines."""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from typing import Optional


AUTHORITY_NATIVE = "native"
AUTHORITY_ADRIAN_KANBAN = "adrian-kanban"

_MUTATION_AUTHORITY_UNTRUSTED = "untrusted"
_MUTATION_AUTHORITY_HUMAN_DASHBOARD = "human_dashboard"
_MUTATION_AUTHORITY_DISPATCHER_ORCHESTRATOR = "dispatcher_orchestrator"
_TRIAGE_EXIT_AUTHORITIES = frozenset(
    {
        _MUTATION_AUTHORITY_HUMAN_DASHBOARD,
        _MUTATION_AUTHORITY_DISPATCHER_ORCHESTRATOR,
    }
)
_MUTATION_AUTHORITY: ContextVar[str] = ContextVar(
    "kanban_mutation_authority", default=_MUTATION_AUTHORITY_UNTRUSTED
)

_REQUIRED_PROVIDER_ATTRS = ("name", "is_healthy", "admit_operation")
_REQUIRED_CAPABILITY_PROVIDER_ATTRS = (
    "validate_capability",
    "consume_capability",
)
_PROVIDER_REGISTRY: dict[str, object] = {}


class AuthorityAdmissionRejected(Exception):
    """A selected replacement authority could not safely admit a mutation."""

    def __init__(
        self, reason: str, *, authority: str = AUTHORITY_ADRIAN_KANBAN
    ) -> None:
        self.authority = authority
        self.reason = reason
        super().__init__(f"[{authority}] mutation rejected: {reason}")


@dataclass
class ProviderStatus:
    present: bool
    healthy: bool
    name: Optional[str]
    reason: Optional[str]


@dataclass
class _AuthorityCapabilityEnvelope:
    provider: object
    capability: object
    context: object
    canonical_path: str
    connection_identity: int
    state: str = "fresh"
    allow_multiple_writes: bool = False


_ACTIVE_AUTHORITY_CAPABILITY: ContextVar[
    Optional[_AuthorityCapabilityEnvelope]
] = ContextVar("_ACTIVE_AUTHORITY_CAPABILITY", default=None)


def _host():
    from hermes_cli import kanban_db

    return kanban_db


@contextlib.contextmanager
def _scoped_mutation_authority(authority: str):
    if authority not in _TRIAGE_EXIT_AUTHORITIES:
        raise ValueError(f"unknown Kanban mutation authority: {authority!r}")
    token = _MUTATION_AUTHORITY.set(authority)
    try:
        yield
    finally:
        _MUTATION_AUTHORITY.reset(token)


def _mutation_authority() -> str:
    return _MUTATION_AUTHORITY.get()


def can_exit_triage() -> bool:
    return _mutation_authority() in _TRIAGE_EXIT_AUTHORITIES


def resolve_selected_authority() -> str:
    try:
        from hermes_cli.config import load_config_readonly

        raw = (
            (load_config_readonly() or {})
            .get("kanban", {})
            .get("mutation_authority", AUTHORITY_NATIVE)
        )
    except Exception:
        return AUTHORITY_NATIVE
    value = raw.strip().lower() if isinstance(raw, str) else ""
    return (
        value
        if value in (AUTHORITY_NATIVE, AUTHORITY_ADRIAN_KANBAN)
        else AUTHORITY_NATIVE
    )


def _normalize_authority_path(value: Optional[str]) -> str:
    if value is None:
        raise AuthorityAdmissionRejected(
            "adrian-kanban requires kanban.database_path; no authoritative "
            "database path is configured (no fallback to the native board)"
        )
    text = str(value).strip()
    if not text:
        raise AuthorityAdmissionRejected(
            "adrian-kanban requires a non-empty kanban.database_path"
        )
    if not Path(text).is_absolute():
        raise AuthorityAdmissionRejected(
            "adrian-kanban requires an absolute kanban.database_path, got "
            f"{value!r} (relative paths are rejected; no fallback to native)"
        )
    return str(Path(text).expanduser().resolve())


def resolve_authority_path(
    *, override: Optional[str] = None, authority: Optional[str] = None
) -> str:
    authority = authority or resolve_selected_authority()
    try:
        from hermes_cli.config import load_config_readonly

        configured = (
            (load_config_readonly() or {}).get("kanban", {}).get("database_path")
        )
    except Exception:
        configured = None
    source = override if override is not None else configured
    if source is not None:
        return _normalize_authority_path(source)
    if authority != AUTHORITY_ADRIAN_KANBAN:
        return str(_host().kanban_db_path())
    raise AuthorityAdmissionRejected(
        "adrian-kanban requires kanban.database_path; no authoritative "
        "database path is configured (no fallback to the native board)"
    )


def register_authority_provider(
    provider: object, db_path: Optional[str] = None
) -> None:
    for attr in _REQUIRED_PROVIDER_ATTRS:
        if not hasattr(provider, attr):
            raise ValueError(
                f"provider must supply {', '.join(_REQUIRED_PROVIDER_ATTRS)}"
            )
    _PROVIDER_REGISTRY[resolve_authority_path(override=db_path)] = provider


def clear_authority_providers() -> None:
    _PROVIDER_REGISTRY.clear()


def _provider_for_db(db_path: Optional[str]) -> Optional[object]:
    return _PROVIDER_REGISTRY.get(resolve_authority_path(override=db_path))


def _provider_is_available(provider: Optional[object]) -> bool:
    if provider is None:
        return False
    if any(not hasattr(provider, attr) for attr in _REQUIRED_PROVIDER_ATTRS):
        return False
    if not callable(getattr(provider, "admit_operation", None)):
        return False
    try:
        return bool(provider.is_healthy())
    except Exception:
        return False


def provider_status(db_path: Optional[str] = None) -> ProviderStatus:
    provider = _provider_for_db(db_path)
    if provider is None:
        return ProviderStatus(
            False,
            False,
            None,
            "no authority provider is registered for this database path",
        )
    healthy = _provider_is_available(provider)
    return ProviderStatus(
        True,
        healthy,
        getattr(provider, "name", None),
        None
        if healthy
        else "registered authority provider is unavailable, unhealthy, or incompatible",
    )


def _require_admitted_provider(db_path: Optional[str]) -> object:
    provider = _provider_for_db(db_path)
    if provider is None:
        raise AuthorityAdmissionRejected(
            "selected authority has no configured provider available; "
            "native mutation/dispatch fails closed (no fallback)"
        )
    if not _provider_is_available(provider):
        raise AuthorityAdmissionRejected(
            "selected authority provider is unavailable, unhealthy, or "
            "incompatible; native mutation/dispatch fails closed (no fallback)"
        )
    return provider


def ensure_admitted(operation: str, *, db_path: Optional[str] = None) -> bool:
    provider = _require_admitted_provider(
        resolve_authority_path(override=db_path)
    )
    if not bool(provider.admit_operation(operation)):
        raise AuthorityAdmissionRejected(
            f"operation {operation!r} was not admitted by the selected "
            "authority provider; native mutation fails closed"
        )
    return True


def delegate_authority_operation(operation: str, **fields) -> dict:
    if resolve_selected_authority() != AUTHORITY_ADRIAN_KANBAN:
        raise AuthorityAdmissionRejected(
            "selected authority is not adrian-kanban; cross-surface "
            "delegation fails closed (no native fallback)"
        )
    provider = _require_admitted_provider(resolve_authority_path())
    submit = getattr(provider, "submit_operation", None)
    if not callable(submit):
        raise AuthorityAdmissionRejected(
            "command boundary is unavailable; cross-surface delegation "
            "fails closed (no native fallback)"
        )
    try:
        result = submit(operation, **fields)
    except Exception as exc:
        raise AuthorityAdmissionRejected(
            "command boundary is unavailable; cross-surface delegation "
            "fails closed (no native fallback)"
        ) from exc
    if type(result) is not dict:
        raise AuthorityAdmissionRejected(
            "invalid command response; cross-surface delegation fails "
            "closed (no native fallback)"
        )
    return result


def require_native_mutation_authority(operation: str) -> None:
    if not isinstance(operation, str) or not operation.strip():
        raise AuthorityAdmissionRejected(
            f"{operation!r} is not a recognized Kanban mutation"
        )
    try:
        authority = resolve_selected_authority()
    except Exception:
        raise AuthorityAdmissionRejected(
            f"{operation.strip()} is not a recognized Kanban mutation because "
            "the selected authority could not be resolved"
        ) from None
    if authority == AUTHORITY_ADRIAN_KANBAN:
        raise AuthorityAdmissionRejected(
            f"{operation.strip()} is not a recognized Kanban mutation under "
            "adrian-kanban authority"
        )


def resolve_trusted_authority_workspace(candidate: str) -> Optional[str]:
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip()
    if not candidate or not os.path.isabs(candidate) or not os.path.isdir(candidate):
        return None
    canonical_candidate = os.path.realpath(candidate)
    try:
        if resolve_selected_authority() != AUTHORITY_ADRIAN_KANBAN:
            return None
        provider = _require_admitted_provider(resolve_authority_path())
        resolver = getattr(provider, "resolve_trusted_workspace_root", None)
        result = resolver(candidate) if callable(resolver) else None
        if not isinstance(result, str):
            return None
        result = result.strip()
        if not result or not os.path.isabs(result) or not os.path.isdir(result):
            return None
        canonical_result = os.path.realpath(result)
        return canonical_result if canonical_result == canonical_candidate else None
    except Exception:
        return None


def _canonical_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def _sqlite_main_connection_file(conn: sqlite3.Connection) -> Optional[str]:
    try:
        for row in conn.execute("PRAGMA database_list"):
            keys = set(row.keys()) if hasattr(row, "keys") else set()
            name = row["name"] if "name" in keys else row[1]
            if name == "main":
                value = row["file"] if "file" in keys else row[2]
                return value or None
    except Exception:
        pass
    return None


def _require_capability_provider(provider: object) -> object:
    if not _provider_is_available(provider) or any(
        not callable(getattr(provider, attr, None))
        for attr in _REQUIRED_CAPABILITY_PROVIDER_ATTRS
    ):
        raise AuthorityAdmissionRejected(
            "capability interface is unavailable; native mutation fails "
            "closed/no fallback"
        )
    return provider


@contextlib.contextmanager
def _scoped_authority_capability(
    conn,
    capability,
    context,
    *,
    db_path: Optional[str] = None,
    allow_multiple_writes: bool = False,
):
    if type(allow_multiple_writes) is not bool:
        raise AuthorityAdmissionRejected("capability interface is unavailable")
    if resolve_selected_authority() != AUTHORITY_ADRIAN_KANBAN:
        raise AuthorityAdmissionRejected("capability interface is unavailable")
    if _ACTIVE_AUTHORITY_CAPABILITY.get() is not None or getattr(
        conn, "in_transaction", False
    ):
        raise AuthorityAdmissionRejected("capability interface is unavailable")
    canonical_path = _canonical_path(resolve_authority_path(override=db_path))
    main_path = _sqlite_main_connection_file(conn)
    if main_path is None or _canonical_path(main_path) != canonical_path:
        raise AuthorityAdmissionRejected("capability interface is unavailable")
    provider = _require_capability_provider(_require_admitted_provider(canonical_path))
    try:
        valid = provider.validate_capability(
            capability, context, canonical_path, allow_consumed=False
        )
    except Exception as exc:
        raise AuthorityAdmissionRejected("capability interface is unavailable") from exc
    if valid is not True:
        raise AuthorityAdmissionRejected("capability interface is unavailable")
    envelope = _AuthorityCapabilityEnvelope(
        provider,
        capability,
        context,
        canonical_path,
        id(conn),
        allow_multiple_writes=allow_multiple_writes,
    )
    token = _ACTIVE_AUTHORITY_CAPABILITY.set(envelope)
    try:
        yield conn
    finally:
        envelope.state = "finished"
        _ACTIVE_AUTHORITY_CAPABILITY.reset(token)


def _enforce_seam_on_write_txn(
    conn: sqlite3.Connection, *, nested: bool
) -> Optional[_AuthorityCapabilityEnvelope]:
    if resolve_selected_authority() != AUTHORITY_ADRIAN_KANBAN:
        return None
    canonical_path = _canonical_path(resolve_authority_path())
    main_path = _sqlite_main_connection_file(conn)
    if main_path is None or _canonical_path(main_path) != canonical_path:
        raise AuthorityAdmissionRejected(
            "capability interface is unavailable; native mutation fails closed/no fallback"
        )
    provider = _require_capability_provider(_require_admitted_provider(canonical_path))
    envelope = _ACTIVE_AUTHORITY_CAPABILITY.get()
    if (
        envelope is None
        or envelope.provider is not provider
        or envelope.canonical_path != canonical_path
        or envelope.connection_identity != id(conn)
    ):
        raise AuthorityAdmissionRejected(
            "capability interface is unavailable; native mutation fails closed/no fallback"
        )
    try:
        if nested:
            if envelope.state != "active":
                raise AuthorityAdmissionRejected("capability interface is unavailable")
            valid = provider.validate_capability(
                envelope.capability,
                envelope.context,
                canonical_path,
                allow_consumed=True,
            )
            if valid is not True:
                raise AuthorityAdmissionRejected("capability interface is unavailable")
        elif envelope.state == "fresh":
            valid = provider.validate_capability(
                envelope.capability,
                envelope.context,
                canonical_path,
                allow_consumed=False,
            )
            consumed = (
                valid is True
                and provider.consume_capability(
                    envelope.capability, envelope.context, canonical_path
                )
                is True
            )
            if not consumed:
                raise AuthorityAdmissionRejected("capability interface is unavailable")
            envelope.state = "active"
        elif envelope.state == "active" and envelope.allow_multiple_writes:
            valid = provider.validate_capability(
                envelope.capability,
                envelope.context,
                canonical_path,
                allow_consumed=True,
            )
            if valid is not True:
                raise AuthorityAdmissionRejected("capability interface is unavailable")
        else:
            raise AuthorityAdmissionRejected("capability interface is unavailable")
    except AuthorityAdmissionRejected:
        raise
    except Exception as exc:
        raise AuthorityAdmissionRejected("capability interface is unavailable") from exc
    return envelope
