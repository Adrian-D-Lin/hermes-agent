from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from hermes_cli import kanban_db as _kb

from .capability import CapabilityBinding, CapabilityRegistry, CapabilityRejected

PROVIDER_NAME = "adrian-kanban"
PLUGIN_VERSION = "0.2.0"
PROTOCOL_VERSION = "2"


class AdrianKanbanAuthorityProvider:
    name = PROVIDER_NAME

    def __init__(self, database_path: str) -> None:
        if not isinstance(database_path, str) or not database_path.strip():
            raise CapabilityRejected("database_path must be a nonblank string")
        raw = Path(database_path)
        if not raw.is_absolute():
            raise CapabilityRejected("database_path must be absolute")
        self.database_path = str(raw.expanduser().resolve())
        self._registry = CapabilityRegistry()
        self._healthy = True

    def is_healthy(self) -> bool:
        return self._healthy

    def mark_unhealthy(self) -> None:
        self._healthy = False

    def admit_operation(self, operation: str) -> bool:
        return False

    def _mint_after_admission(self, binding: CapabilityBinding):
        if type(binding) is not CapabilityBinding:
            raise CapabilityRejected("invalid binding type")
        if not self._healthy:
            raise CapabilityRejected("provider is unhealthy")
        if binding.plugin_version != PLUGIN_VERSION:
            raise CapabilityRejected("plugin version mismatch")
        if binding.protocol_version != PROTOCOL_VERSION:
            raise CapabilityRejected("protocol version mismatch")
        return self._registry._mint_after_admission(binding)

    def validate_capability(
        self,
        capability,
        context,
        canonical_path,
        *,
        allow_consumed: bool,
    ) -> bool:
        if not self._healthy:
            raise CapabilityRejected("provider is unhealthy")
        if not isinstance(canonical_path, str):
            raise CapabilityRejected("canonical path must be a string")
        if canonical_path != self.database_path:
            raise CapabilityRejected("canonical path mismatch")
        return self._registry.validate(
            capability,
            context,
            allow_consumed=allow_consumed,
        )

    def consume_capability(self, capability, context, canonical_path) -> bool:
        if not self._healthy:
            raise CapabilityRejected("provider is unhealthy")
        if not isinstance(canonical_path, str):
            raise CapabilityRejected("canonical path must be a string")
        if canonical_path != self.database_path:
            raise CapabilityRejected("canonical path mismatch")
        return self._registry.consume(capability, context)

    def is_consumed(self, capability) -> bool:
        return self._registry.is_consumed(capability)


@contextlib.contextmanager
def _capability_scope(
    provider, conn, capability, context, *, allow_multiple_writes: bool = False
):
    if type(provider) is not AdrianKanbanAuthorityProvider:
        raise CapabilityRejected("provider type mismatch")
    if type(conn) is not sqlite3.Connection:
        raise CapabilityRejected("connection type mismatch")
    if not provider.is_healthy():
        raise CapabilityRejected("provider is unhealthy")
    if type(allow_multiple_writes) is not bool:
        raise CapabilityRejected("allow_multiple_writes must be a bool")
    with _kb._scoped_authority_capability(
        conn,
        capability,
        context,
        db_path=provider.database_path,
        allow_multiple_writes=allow_multiple_writes,
    ) as scoped_conn:
        yield scoped_conn


def register_provider(provider) -> None:
    if type(provider) is not AdrianKanbanAuthorityProvider:
        raise CapabilityRejected("provider type mismatch")
    _kb.register_authority_provider(provider, provider.database_path)


__all__ = [
    "AdrianKanbanAuthorityProvider",
    "register_provider",
    "PROVIDER_NAME",
    "PLUGIN_VERSION",
    "PROTOCOL_VERSION",
]
