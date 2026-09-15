"""Behavior contracts for the server-owned remote Desktop release policy."""

from pathlib import Path

import pytest

from hermes_cli.client_release_policy import (
    ClientReleasePolicyError,
    artifact_file,
    evaluate_client_release,
)


def _config(tmp_path: Path, *, mode: str = "enforce") -> dict:
    installer = tmp_path / "Hermes-0.21.3-adrian.1-win-x64.exe"
    installer.write_bytes(b"signed installer fixture")
    return {
        "updates": {
            "remote_clients": {
                "enabled": True,
                "mode": mode,
                "target_release": "0.21.3-adrian.1",
                "target_sequence": 2026091501,
                "minimum_sequence": 2026090101,
                "protocol_epoch": 1,
                "desktop_bundle_version": "1",
                "artifacts": {
                    "win32-x64": {
                        "file": str(installer),
                        "sha256": "a" * 64,
                        "signature": "fixture-signature",
                        "key_id": "adrian-release-1",
                    }
                },
            }
        }
    }


def _client(**overrides) -> dict:
    identity = {
        "release": "0.21.3-adrian.1",
        "release_sequence": 2026091501,
        "protocol_epoch": 1,
        "bundle_version": "1",
        "build_sha": "345cd2b",
        "platform": "win32",
        "arch": "x64",
        "install_kind": "packaged",
    }
    identity.update(overrides)
    return identity


def test_disabled_policy_never_strands_an_existing_client(tmp_path):
    result = evaluate_client_release(_client(), {"updates": {"remote_clients": {"enabled": False}}})

    assert result["decision"] == "policy_disabled"
    assert result["allow_sessions"] is True
    assert result["artifact"] is None


def test_exact_target_is_current_and_does_not_offer_an_installer(tmp_path):
    result = evaluate_client_release(_client(), _config(tmp_path))

    assert result["decision"] == "current"
    assert result["allow_sessions"] is True
    assert result["artifact"] is None


def test_compatible_old_client_updates_when_idle(tmp_path):
    result = evaluate_client_release(
        _client(release="0.21.3-adrian.0", release_sequence=2026091001),
        _config(tmp_path),
    )

    assert result["decision"] == "update_when_idle"
    assert result["allow_sessions"] is True
    assert result["artifact"]["download_path"].endswith("/win32/x64")
    assert "file" not in result["artifact"]


def test_enforced_protocol_mismatch_blocks_sessions(tmp_path):
    result = evaluate_client_release(_client(protocol_epoch=0), _config(tmp_path))

    assert result["decision"] == "update_required"
    assert result["allow_sessions"] is False
    assert result["artifact"]["signature_algorithm"] == "ed25519"


def test_advisory_protocol_mismatch_does_not_block_sessions(tmp_path):
    result = evaluate_client_release(_client(protocol_epoch=0), _config(tmp_path, mode="advisory"))

    assert result["decision"] == "update_when_idle"
    assert result["allow_sessions"] is True


def test_policy_rejects_an_inverted_release_range(tmp_path):
    config = _config(tmp_path)
    config["updates"]["remote_clients"]["minimum_sequence"] = 2026091601

    with pytest.raises(ClientReleasePolicyError, match="cannot exceed"):
        evaluate_client_release(_client(), config)


def test_artifact_download_resolves_only_the_configured_platform_file(tmp_path):
    config = _config(tmp_path)

    assert artifact_file(config, "win32", "x64").read_bytes() == b"signed installer fixture"
    with pytest.raises(ClientReleasePolicyError, match="artifacts.linux-x64.file"):
        artifact_file(config, "linux", "x64")
