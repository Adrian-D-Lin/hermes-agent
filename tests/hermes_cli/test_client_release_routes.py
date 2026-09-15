"""The remote Desktop release contract stays behind the dashboard auth gate."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.web_routers import client_releases


IDENTITY = {
    "release": "0.20.6-adrian.1",
    "release_sequence": 2026082701,
    "protocol_epoch": 1,
    "bundle_version": "1",
    "build_sha": "a" * 40,
    "platform": "win32",
    "arch": "x64",
    "install_kind": "packaged",
}


def _config(artifact):
    body = artifact.read_bytes()
    return {
        "updates": {
            "remote_clients": {
                "enabled": True,
                "mode": "enforce",
                "target_release": "0.21.3-adrian.1",
                "target_sequence": 2026091501,
                "minimum_sequence": 2026090101,
                "protocol_epoch": 1,
                "desktop_bundle_version": "1",
                "artifacts": {
                    "win32-x64": {
                        "file": str(artifact),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "signature": "test-signature",
                        "key_id": "test-release-key",
                    }
                },
            }
        }
    }


def test_policy_and_artifact_routes_require_the_dashboard_session_token(tmp_path, monkeypatch):
    artifact = tmp_path / "Hermes-Setup.exe"
    artifact.write_bytes(b"signed installer fixture")
    monkeypatch.setattr(client_releases, "load_config", lambda: _config(artifact))
    client = TestClient(web_server.app)

    policy_without_auth = client.post("/api/client-release/policy", json=IDENTITY)
    artifact_without_auth = client.get("/api/client-release/artifacts/win32/x64")

    assert policy_without_auth.status_code == 401
    assert artifact_without_auth.status_code == 401

    headers = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}
    policy = client.post("/api/client-release/policy", json=IDENTITY, headers=headers)
    assert policy.status_code == 200, policy.text
    assert policy.json()["decision"] == "update_required"

    download = client.get("/api/client-release/artifacts/win32/x64", headers=headers)
    assert download.status_code == 200, download.text
    assert download.content == b"signed installer fixture"
