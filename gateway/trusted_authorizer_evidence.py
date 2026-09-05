import json
import weakref
from dataclasses import InitVar, dataclass, field

_CONNECTION_MINT = object()
_EVIDENCE_MINT = object()

_authenticated_connections = weakref.WeakSet()
_evidences = weakref.WeakSet()


@dataclass(frozen=True, eq=False)
class _AuthenticatedTailscaleConnectionState:
    _mint: InitVar[object]
    route: str
    connection_id: str
    peer_identity: str
    request_id: str
    authenticated_at: int
    transport: str = field(init=False, default="tailscale")

    def __post_init__(self, mint):
        if mint is not _CONNECTION_MINT:
            raise ValueError("invalid mint")
        for name in ("route", "connection_id", "peer_identity", "request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"invalid {name}")
        if (
            isinstance(self.authenticated_at, bool)
            or not isinstance(self.authenticated_at, int)
            or self.authenticated_at <= 0
        ):
            raise ValueError("invalid authenticated_at")
        if self.transport != "tailscale":
            raise ValueError("invalid transport")


def _record_authenticated_tailscale_connection(
    route, connection_id, peer_identity, request_id, authenticated_at
):
    state = _AuthenticatedTailscaleConnectionState(
        _CONNECTION_MINT,
        route=route,
        connection_id=connection_id,
        peer_identity=peer_identity,
        request_id=request_id,
        authenticated_at=authenticated_at,
    )
    _authenticated_connections.add(state)
    return state


@dataclass(frozen=True, eq=False)
class TrustedAuthorizerEvidence:
    _mint: InitVar[object]
    route: str
    connection_id: str
    peer_identity: str
    request_id: str
    issued_at: int
    expires_at: int
    evidence_type: str = field(init=False, default="trusted_authorizer")
    evidence_version: int = field(init=False, default=1)

    def __post_init__(self, mint):
        if mint is not _EVIDENCE_MINT:
            raise ValueError("invalid mint")
        for name in ("route", "connection_id", "peer_identity", "request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"invalid {name}")
        for name in ("issued_at", "expires_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"invalid {name}")
        if self.expires_at <= self.issued_at:
            raise ValueError("invalid expiry")
        if self.evidence_type != "trusted_authorizer":
            raise ValueError("invalid evidence_type")
        if self.evidence_version != 1:
            raise ValueError("invalid evidence_version")

    @classmethod
    def _from_authenticated_connection(cls, connection, *, issued_at, ttl_seconds):
        if not isinstance(connection, _AuthenticatedTailscaleConnectionState):
            raise TypeError("invalid connection type")
        if connection not in _authenticated_connections:
            raise ValueError("unregistered connection")
        if isinstance(issued_at, bool) or not isinstance(issued_at, int) or issued_at <= 0:
            raise ValueError("invalid issued_at")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or ttl_seconds <= 0
            or ttl_seconds > 300
        ):
            raise ValueError("invalid ttl_seconds")
        if issued_at < connection.authenticated_at:
            raise ValueError("issued_at before authenticated_at")
        expires_at = issued_at + ttl_seconds
        evidence = cls(
            _EVIDENCE_MINT,
            route=connection.route,
            connection_id=connection.connection_id,
            peer_identity=connection.peer_identity,
            request_id=connection.request_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        _evidences.add(evidence)
        return evidence

    def _canonical_for_writegate(self):
        if self not in _evidences:
            raise ValueError("unregistered evidence")
        payload = {
            "type": self.evidence_type,
            "version": self.evidence_version,
            "route": self.route,
            "connection_id": self.connection_id,
            "peer_identity": self.peer_identity,
            "request_id": self.request_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def __reduce__(self):
        raise TypeError("pickling not supported")

    def __repr__(self):
        return "<TrustedAuthorizerEvidence>"


__all__ = ["TrustedAuthorizerEvidence"]
