from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from hermes_cli import kanban_db as _kb

from .capability import CapabilityBinding, CapabilityRegistry, CapabilityRejected
from .versioning import PLUGIN_NAME, PLUGIN_VERSION, PROTOCOL_VERSION
from .workspace import _SegmentWorkspaceController

PROVIDER_NAME = PLUGIN_NAME


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
        self._command_boundary = None
        self._workspace_registry = None

    def bind_workspace_registry(self, registry) -> None:
        from .workspace import _TrustedRepositoryRegistry

        if type(registry) is not _TrustedRepositoryRegistry:
            raise CapabilityRejected(
                "workspace registry must be a _TrustedRepositoryRegistry"
            )
        if self._workspace_registry is None:
            self._workspace_registry = registry
            return
        if getattr(registry, "_registrations", None) != getattr(
            self._workspace_registry, "_registrations", None
        ):
            raise CapabilityRejected("conflicting workspace registry already bound")

    def resolve_trusted_workspace_root(self, candidate):
        registry = self._workspace_registry
        if registry is None or not isinstance(candidate, str):
            return None
        candidate = candidate.strip()
        if not candidate:
            return None
        candidate_path = Path(candidate).resolve()
        if not candidate_path.is_absolute() or not candidate_path.is_dir():
            return None
        conn = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT workspace_id, initiative_id, segment_id,
                       controller_binding_ref
                FROM segment_workspaces
                WHERE active = 1 AND lifecycle_state = 'active'
                ORDER BY workspace_id
                """
            ).fetchall()
            for row in rows:
                try:
                    plan = _SegmentWorkspaceController(conn, registry).load(
                        workspace_id=row["workspace_id"],
                        expected_initiative_id=row["initiative_id"],
                        expected_segment_id=row["segment_id"],
                        expected_controller_binding=row["controller_binding_ref"],
                    )
                except Exception:
                    continue
                root = Path(plan.segment_root).resolve()
                if root.is_dir() and root == candidate_path:
                    return str(root)
            return None
        finally:
            conn.close()

    def bind_command_boundary(self, boundary) -> None:
        if not callable(getattr(boundary, "submit", None)):
            raise CapabilityRejected("command boundary must expose callable submit")
        if self._command_boundary is not None:
            if boundary is self._command_boundary:
                return
            raise CapabilityRejected("command boundary already bound")
        self._command_boundary = boundary

    def submit_operation(self, operation, **fields):
        if self._command_boundary is None:
            raise CapabilityRejected("command boundary is unavailable")
        return self._command_boundary.submit(operation, **fields)

    def is_healthy(self) -> bool:
        return self._healthy

    @property
    def command_boundary(self):
        return self._command_boundary

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

    def _create_mutation_executor(self, conn: sqlite3.Connection):
        from .private_adapter import _PrivateNativeAdapter

        return _PrivateNativeAdapter(self, conn)


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
