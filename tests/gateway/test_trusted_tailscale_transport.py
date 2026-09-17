import asyncio
import json
from types import SimpleNamespace

from gateway import trusted_authorizer_evidence as trusted
from tui_gateway.transport import (
    FanoutTransport,
    bind_authorizing_transport,
    bind_transport,
    reset_authorizing_transport,
    reset_transport,
)
from tui_gateway.ws import WSTransport
from tui_gateway import ws as ws_module


def _whois_result(*, login="adrian@example.com", address="100.115.246.102"):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "Node": {"Addresses": [f"{address}/32"]},
                "UserProfile": {"LoginName": login},
            }
        ),
        stderr="",
    )


def test_tailscale_whois_mints_registered_transport_authority(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _whois_result()

    monkeypatch.setattr(trusted.subprocess, "run", fake_run)
    peer = trusted._authenticate_tailscale_peer(
        "100.115.246.102",
        connection_id="ws-connection-1",
        authenticated_at=1_000,
    )

    assert peer is not None
    assert calls[0][0] == ["tailscale", "whois", "--json", "100.115.246.102"]
    assert calls[0][1].get("shell", False) is False
    assert 0 < calls[0][1]["timeout"] <= 3

    loop = asyncio.new_event_loop()
    try:
        transport = WSTransport(
            SimpleNamespace(),
            loop,
            peer="100.115.246.102:54321",
            authenticated_tailscale_peer=peer,
        )
        token = bind_transport(transport)
        try:
            evidence = trusted.mint_current_tailscale_authorizer(
                request_id="turn-1",
                issued_at=1_001,
                ttl_seconds=60,
            )
        finally:
            reset_transport(token)
    finally:
        loop.close()

    payload = json.loads(evidence._canonical_for_writegate())
    assert payload == {
        "connection_id": "ws-connection-1",
        "type": "trusted_authorizer",
        "version": 1,
        "expires_at": 1_061,
        "issued_at": 1_001,
        "peer_identity": "adrian@example.com",
        "request_id": "turn-1",
        "route": "gateway/tailscale",
    }


def test_tailscale_whois_rejects_non_tailnet_or_non_human_results(monkeypatch):
    monkeypatch.setattr(
        trusted.subprocess,
        "run",
        lambda *args, **kwargs: _whois_result(address="100.115.246.103"),
    )
    assert trusted._authenticate_tailscale_peer(
        "100.115.246.102", connection_id="c1", authenticated_at=1_000
    ) is None

    monkeypatch.setattr(
        trusted.subprocess,
        "run",
        lambda *args, **kwargs: _whois_result(login=""),
    )
    assert trusted._authenticate_tailscale_peer(
        "100.115.246.102", connection_id="c2", authenticated_at=1_000
    ) is None


def test_no_bound_authenticated_transport_cannot_mint_authority():
    try:
        trusted.mint_current_tailscale_authorizer(
            request_id="turn-1", issued_at=1_001, ttl_seconds=60
        )
    except ValueError as exc:
        assert "authenticated Tailscale transport" in str(exc)
    else:
        raise AssertionError("authority unexpectedly minted without a bound transport")


def test_same_identity_authenticated_fanout_can_mint_authority(monkeypatch):
    monkeypatch.setattr(
        trusted.subprocess,
        "run",
        lambda command, **_kwargs: _whois_result(address=command[-1]),
    )
    loop = asyncio.new_event_loop()
    try:
        transports = []
        for index, address in enumerate(("100.115.246.102", "100.115.246.103"), 1):
            peer = trusted._authenticate_tailscale_peer(
                address,
                connection_id=f"ws-connection-{index}",
                authenticated_at=1_000,
            )
            transports.append(
                WSTransport(
                    SimpleNamespace(),
                    loop,
                    peer=f"{address}:54321",
                    authenticated_tailscale_peer=peer,
                )
            )
        token = bind_transport(FanoutTransport(*transports))
        try:
            evidence = trusted.mint_current_tailscale_authorizer(
                request_id="turn-fanout",
                issued_at=1_001,
                ttl_seconds=60,
            )
        finally:
            reset_transport(token)
    finally:
        loop.close()

    payload = json.loads(evidence._canonical_for_writegate())
    assert payload["peer_identity"] == "adrian@example.com"
    assert payload["request_id"] == "turn-fanout"


def test_fanout_rejects_unauthenticated_or_mixed_identity_peers(monkeypatch):
    def fake_run(command, **_kwargs):
        address = command[-1]
        login = "other@example.com" if address.endswith("103") else "adrian@example.com"
        return _whois_result(login=login, address=address)

    monkeypatch.setattr(trusted.subprocess, "run", fake_run)
    loop = asyncio.new_event_loop()
    try:
        authenticated = []
        for index, address in enumerate(("100.115.246.102", "100.115.246.103"), 1):
            peer = trusted._authenticate_tailscale_peer(
                address,
                connection_id=f"ws-connection-{index}",
                authenticated_at=1_000,
            )
            authenticated.append(
                WSTransport(
                    SimpleNamespace(),
                    loop,
                    peer=f"{address}:54321",
                    authenticated_tailscale_peer=peer,
                )
            )

        unauthenticated = WSTransport(SimpleNamespace(), loop)
        for fanout in (
            FanoutTransport(authenticated[0], unauthenticated),
            FanoutTransport(*authenticated),
        ):
            token = bind_transport(fanout)
            try:
                try:
                    trusted.mint_current_tailscale_authorizer(
                        request_id="turn-rejected",
                        issued_at=1_001,
                        ttl_seconds=60,
                    )
                except ValueError as exc:
                    assert "authenticated Tailscale transport" in str(exc)
                else:
                    raise AssertionError("ambiguous fanout unexpectedly minted authority")
            finally:
                reset_transport(token)
    finally:
        loop.close()


def test_exact_authorizing_peer_is_not_diluted_by_output_fanout(monkeypatch):
    monkeypatch.setattr(
        trusted.subprocess,
        "run",
        lambda command, **_kwargs: _whois_result(address=command[-1]),
    )
    loop = asyncio.new_event_loop()
    try:
        peer = trusted._authenticate_tailscale_peer(
            "100.115.246.102",
            connection_id="ws-authorizing",
            authenticated_at=1_000,
        )
        authorizing = WSTransport(
            SimpleNamespace(), loop,
            peer="100.115.246.102:54321",
            authenticated_tailscale_peer=peer,
        )
        unauthenticated_viewer = WSTransport(SimpleNamespace(), loop)
        output_token = bind_transport(FanoutTransport(authorizing, unauthenticated_viewer))
        authority_token = bind_authorizing_transport(authorizing)
        try:
            evidence = trusted.mint_current_tailscale_authorizer(
                request_id="turn-exact-peer",
                issued_at=1_001,
                ttl_seconds=60,
            )
        finally:
            reset_authorizing_transport(authority_token)
            reset_transport(output_token)
    finally:
        loop.close()

    payload = json.loads(evidence._canonical_for_writegate())
    assert payload["connection_id"] == "ws-authorizing"
    assert payload["peer_identity"] == "adrian@example.com"


def test_explicitly_authorityless_turn_cannot_borrow_output_transport(monkeypatch):
    monkeypatch.setattr(
        trusted.subprocess,
        "run",
        lambda command, **_kwargs: _whois_result(address=command[-1]),
    )
    peer = trusted._authenticate_tailscale_peer(
        "100.115.246.102",
        connection_id="ws-viewer-only",
        authenticated_at=1_000,
    )
    loop = asyncio.new_event_loop()
    try:
        viewer = WSTransport(
            SimpleNamespace(), loop,
            peer="100.115.246.102:54321",
            authenticated_tailscale_peer=peer,
        )
        output_token = bind_transport(viewer)
        authority_token = bind_authorizing_transport(None)
        try:
            try:
                trusted.mint_current_tailscale_authorizer(
                    request_id="automatic-turn",
                    issued_at=1_001,
                    ttl_seconds=60,
                )
            except ValueError as exc:
                assert "authenticated Tailscale transport" in str(exc)
            else:
                raise AssertionError("authorityless turn borrowed its output transport")
        finally:
            reset_authorizing_transport(authority_token)
            reset_transport(output_token)
    finally:
        loop.close()


def test_exact_websocket_reauthenticates_after_transient_connection_auth_failure(monkeypatch):
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _whois_result(address=command[-1])

    monkeypatch.setattr(trusted.subprocess, "run", fake_run)
    loop = asyncio.new_event_loop()
    try:
        transport = WSTransport(
            SimpleNamespace(),
            loop,
            peer="100.115.246.102:54321",
            authenticated_tailscale_peer=None,
            tailscale_peer_address="100.115.246.102",
            tailscale_connection_id="ws-transient-auth",
        )
        authority_token = bind_authorizing_transport(transport)
        try:
            evidence = trusted.mint_current_tailscale_authorizer(
                request_id="turn-retry-whois",
                issued_at=1_001,
                ttl_seconds=60,
            )
        finally:
            reset_authorizing_transport(authority_token)
    finally:
        loop.close()

    payload = json.loads(evidence._canonical_for_writegate())
    assert calls == [["tailscale", "whois", "--json", "100.115.246.102"]]
    assert payload["connection_id"] == "ws-transient-auth"


def test_handle_ws_authenticates_and_binds_tailscale_peer(monkeypatch):
    authenticated_peer = object()
    captured = {}

    class StopAfterTransportConstruction(Exception):
        pass

    class FakeWS:
        client = SimpleNamespace(host="100.115.246.102", port=54321)

        async def accept(self, **_kwargs):
            return None

        async def close(self):
            return None

    def fake_authenticate(address, *, connection_id, authenticated_at):
        captured["authentication"] = (address, connection_id, authenticated_at)
        return authenticated_peer

    def capture_transport(_ws, _loop, **kwargs):
        captured["transport_kwargs"] = kwargs
        raise StopAfterTransportConstruction

    monkeypatch.setattr(ws_module, "_note_dashboard_client_activity", lambda **_kwargs: None)
    monkeypatch.setattr(ws_module, "_disable_nagle", lambda _ws: None)
    monkeypatch.setattr(trusted, "_authenticate_tailscale_peer", fake_authenticate)
    monkeypatch.setattr(ws_module, "WSTransport", capture_transport)

    async def run():
        try:
            await ws_module.handle_ws(FakeWS(), auth_identity={"user_id": "adrian"})
        except StopAfterTransportConstruction:
            return
        raise AssertionError("handle_ws did not construct the transport")

    asyncio.run(run())

    address, connection_id, authenticated_at = captured["authentication"]
    assert address == "100.115.246.102"
    assert isinstance(connection_id, str) and connection_id
    assert isinstance(authenticated_at, int) and authenticated_at > 0
    assert captured["transport_kwargs"]["auth_identity"] == {"user_id": "adrian"}
    assert captured["transport_kwargs"]["authenticated_tailscale_peer"] is authenticated_peer
