import ipaddress
import json
import subprocess
import weakref
from dataclasses import InitVar, dataclass, field

_CONNECTION_MINT = object()
_EVIDENCE_MINT = object()
_PEER_MINT = object()

_authenticated_connections = weakref.WeakSet()
_evidences = weakref.WeakSet()
_authenticated_tailscale_peers = weakref.WeakSet()


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
class _AuthenticatedTailscalePeerState:
    _mint: InitVar[object]
    route: str
    connection_id: str
    peer_identity: str
    authenticated_at: int
    transport: str = field(init=False, default="tailscale")

    def __post_init__(self, mint):
        if mint is not _PEER_MINT:
            raise ValueError("invalid mint")
        for name in ("route", "connection_id", "peer_identity"):
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

    def __reduce__(self):
        raise TypeError("pickling not supported")

    def __repr__(self):
        return "<_AuthenticatedTailscalePeerState>"


def _authenticate_tailscale_peer(address, *, connection_id, authenticated_at):
    if not isinstance(address, str) or not address.strip():
        return None
    if not isinstance(connection_id, str) or not connection_id.strip():
        return None
    if (
        isinstance(authenticated_at, bool)
        or not isinstance(authenticated_at, int)
        or authenticated_at <= 0
    ):
        return None

    try:
        ip_obj = ipaddress.ip_address(address.strip())
    except ValueError:
        return None

    allowed_cidrs = [
        ipaddress.ip_network("100.64.0.0/10"),
        ipaddress.ip_network("fd7a:115c:a1e0::/48"),
    ]
    if not any(ip_obj in net for net in allowed_cidrs):
        return None

    try:
        result = subprocess.run(
            ["tailscale", "whois", "--json", address.strip()],
            shell=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    node = data.get("Node")
    if not isinstance(node, dict):
        return None
    addresses = node.get("Addresses")
    if not isinstance(addresses, list):
        return None

    ip_matched = False
    for cidr_str in addresses:
        if not isinstance(cidr_str, str):
            continue
        try:
            iface = ipaddress.ip_interface(cidr_str)
        except ValueError:
            continue
        if iface.ip == ip_obj:
            ip_matched = True
            break
    if not ip_matched:
        return None

    user_profile = data.get("UserProfile")
    if not isinstance(user_profile, dict):
        return None
    login_name = user_profile.get("LoginName")
    if not isinstance(login_name, str) or not login_name.strip():
        return None

    peer = _AuthenticatedTailscalePeerState(
        _PEER_MINT,
        route="gateway/tailscale",
        connection_id=connection_id,
        peer_identity=login_name.strip(),
        authenticated_at=authenticated_at,
    )
    _authenticated_tailscale_peers.add(peer)
    return peer


def mint_tailscale_authorizer_for_peer(
    address, *, connection_id, request_id, issued_at, ttl_seconds
):
    peer = _authenticate_tailscale_peer(
        address,
        connection_id=connection_id,
        authenticated_at=issued_at,
    )
    if peer is None:
        raise ValueError("authenticated Tailscale peer required")
    return TrustedAuthorizerEvidence._from_authenticated_peer(
        peer,
        request_id=request_id,
        issued_at=issued_at,
        ttl_seconds=ttl_seconds,
    )


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

    @classmethod
    def _from_authenticated_peer(
        cls, peer, *, request_id, issued_at, ttl_seconds
    ):
        if not isinstance(peer, _AuthenticatedTailscalePeerState):
            raise TypeError("invalid peer type")
        if peer not in _authenticated_tailscale_peers:
            raise ValueError("unregistered peer")
        connection = _record_authenticated_tailscale_connection(
            peer.route,
            peer.connection_id,
            peer.peer_identity,
            request_id,
            peer.authenticated_at,
        )
        return cls._from_authenticated_connection(
            connection,
            issued_at=issued_at,
            ttl_seconds=ttl_seconds,
        )

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


def mint_current_tailscale_authorizer(*, request_id, issued_at, ttl_seconds):
    from tui_gateway.transport import current_transport

    transport = current_transport()
    peer = getattr(transport, "_authenticated_tailscale_peer", None)
    if (
        not isinstance(peer, _AuthenticatedTailscalePeerState)
        or peer not in _authenticated_tailscale_peers
    ):
        raise ValueError("authenticated Tailscale transport not bound")
    return TrustedAuthorizerEvidence._from_authenticated_peer(
        peer,
        request_id=request_id,
        issued_at=issued_at,
        ttl_seconds=ttl_seconds,
    )


__all__ = [
    "TrustedAuthorizerEvidence",
    "mint_current_tailscale_authorizer",
    "mint_tailscale_authorizer_for_peer",
]
