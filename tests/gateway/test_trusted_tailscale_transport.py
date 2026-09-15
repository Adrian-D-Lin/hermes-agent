import asyncio
import json
from types import SimpleNamespace

from gateway import trusted_authorizer_evidence as trusted
from tui_gateway.transport import bind_transport, reset_transport
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
