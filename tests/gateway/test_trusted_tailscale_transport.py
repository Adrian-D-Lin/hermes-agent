import asyncio
import json
from types import SimpleNamespace

from gateway import trusted_authorizer_evidence as trusted
from tui_gateway.transport import bind_transport, reset_transport
from tui_gateway.ws import WSTransport


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


def test_direct_tailscale_peer_can_mint_request_bound_authority(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _whois_result(login="adrian@example.com", address="100.115.246.102")

    monkeypatch.setattr(trusted.subprocess, "run", fake_run)

    evidence = trusted.mint_tailscale_authorizer_for_peer(
        "100.115.246.102",
        connection_id="http-connection-1",
        request_id="dashboard-request-1",
        issued_at=2_000,
        ttl_seconds=60,
    )

    assert calls[0][0] == ["tailscale", "whois", "--json", "100.115.246.102"]
    assert json.loads(evidence._canonical_for_writegate()) == {
        "connection_id": "http-connection-1",
        "type": "trusted_authorizer",
        "version": 1,
        "expires_at": 2_060,
        "issued_at": 2_000,
        "peer_identity": "adrian@example.com",
        "request_id": "dashboard-request-1",
        "route": "gateway/tailscale",
    }


def test_direct_tailscale_peer_mint_rejects_unverified_peer(monkeypatch):
    monkeypatch.setattr(
        trusted.subprocess,
        "run",
        lambda *args, **kwargs: _whois_result(address="100.115.246.103"),
    )

    try:
        trusted.mint_tailscale_authorizer_for_peer(
            "100.115.246.102",
            connection_id="http-connection-2",
            request_id="dashboard-request-2",
            issued_at=2_000,
            ttl_seconds=60,
        )
    except ValueError as exc:
        assert "authenticated Tailscale peer" in str(exc)
    else:
        raise AssertionError("authority unexpectedly minted for an unverified peer")
