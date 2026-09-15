"""Server-owned compatibility decisions for remote Hermes Desktop clients.

The policy is data from ``config.yaml``.  It never carries a command: clients
receive an authenticated artifact path plus integrity metadata and own the
download, verification, installation, and rollback lifecycle themselves.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping


POLICY_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_PLATFORM_PART_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


class ClientReleasePolicyError(ValueError):
    """The configured remote-client release policy is incomplete or invalid."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required_text(value: Any, field: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ClientReleasePolicyError(f"updates.remote_clients.{field} must be a non-empty string")
    return text


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ClientReleasePolicyError(f"updates.remote_clients.{field} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ClientReleasePolicyError(
            f"updates.remote_clients.{field} must be a non-negative integer"
        ) from exc
    if number < 0:
        raise ClientReleasePolicyError(f"updates.remote_clients.{field} must be a non-negative integer")
    return number


def _client_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def platform_key(platform: str, arch: str) -> str:
    """Return the canonical configured artifact key for a client platform."""
    normalized = []
    for value, field in ((platform, "platform"), (arch, "arch")):
        part = value.strip().lower() if isinstance(value, str) else ""
        if not _SAFE_PLATFORM_PART_RE.fullmatch(part):
            raise ClientReleasePolicyError(f"client {field} is invalid")
        normalized.append(part)
    return "-".join(normalized)


def _policy(config: Mapping[str, Any]) -> Mapping[str, Any]:
    updates = _mapping(config.get("updates"))
    return _mapping(updates.get("remote_clients"))


def _target(policy: Mapping[str, Any]) -> dict[str, Any]:
    target_sequence = _non_negative_int(policy.get("target_sequence"), "target_sequence")
    minimum_sequence = _non_negative_int(policy.get("minimum_sequence"), "minimum_sequence")
    protocol_epoch = _non_negative_int(policy.get("protocol_epoch"), "protocol_epoch")
    if target_sequence == 0:
        raise ClientReleasePolicyError("updates.remote_clients.target_sequence must be greater than zero")
    if protocol_epoch == 0:
        raise ClientReleasePolicyError("updates.remote_clients.protocol_epoch must be greater than zero")
    if minimum_sequence > target_sequence:
        raise ClientReleasePolicyError(
            "updates.remote_clients.minimum_sequence cannot exceed target_sequence"
        )
    return {
        "release": _required_text(policy.get("target_release"), "target_release"),
        "sequence": target_sequence,
        "protocol_epoch": protocol_epoch,
        "bundle_version": _required_text(policy.get("desktop_bundle_version"), "desktop_bundle_version"),
        "minimum_sequence": minimum_sequence,
    }


def _artifact(policy: Mapping[str, Any], platform: str, arch: str) -> dict[str, Any] | None:
    key = platform_key(platform, arch)
    raw = _mapping(_mapping(policy.get("artifacts")).get(key))
    if not raw:
        return None
    digest = _required_text(raw.get("sha256"), f"artifacts.{key}.sha256").lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ClientReleasePolicyError(
            f"updates.remote_clients.artifacts.{key}.sha256 must contain 64 hexadecimal characters"
        )
    _required_text(raw.get("file"), f"artifacts.{key}.file")
    return {
        "platform": platform.strip().lower(),
        "arch": arch.strip().lower(),
        "download_path": f"/api/client-release/artifacts/{platform.strip().lower()}/{arch.strip().lower()}",
        "sha256": digest,
        "signature": _required_text(raw.get("signature"), f"artifacts.{key}.signature"),
        "key_id": _required_text(raw.get("key_id"), f"artifacts.{key}.key_id"),
        "signature_algorithm": "ed25519",
    }


def evaluate_client_release(client: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one client identity against the server's configured policy."""
    policy = _policy(config)
    if policy.get("enabled") is not True:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "policy_state": "disabled",
            "decision": "policy_disabled",
            "allow_sessions": True,
            "mode": "disabled",
            "reason": "Remote-client release enforcement is not enabled on this server.",
            "target": None,
            "artifact": None,
        }

    mode = policy.get("mode", "advisory")
    if mode not in {"advisory", "enforce"}:
        raise ClientReleasePolicyError("updates.remote_clients.mode must be 'advisory' or 'enforce'")

    target = _target(policy)
    client_sequence = _client_int(client.get("release_sequence"))
    client_epoch = _client_int(client.get("protocol_epoch"))
    client_release = client.get("release") if isinstance(client.get("release"), str) else ""
    client_bundle = client.get("bundle_version") if isinstance(client.get("bundle_version"), str) else ""

    exact_target = (
        client_release == target["release"]
        and client_sequence == target["sequence"]
        and client_epoch == target["protocol_epoch"]
        and client_bundle == target["bundle_version"]
    )
    compatible = (
        client_sequence is not None
        and client_sequence >= target["minimum_sequence"]
        and client_epoch == target["protocol_epoch"]
    )

    if exact_target:
        decision = "current"
        allow_sessions = True
        reason = "The Desktop client matches this server's target release."
        artifact = None
    else:
        artifact = _artifact(
            policy,
            str(client.get("platform") or ""),
            str(client.get("arch") or ""),
        )
        if compatible:
            decision = "update_when_idle"
            allow_sessions = True
            reason = "The Desktop client is compatible but does not match the target release."
        else:
            decision = "update_required" if mode == "enforce" else "update_when_idle"
            allow_sessions = mode != "enforce"
            reason = (
                "The Desktop client protocol or release sequence is outside the server's supported range."
            )

        if artifact is None:
            reason += " No installer is configured for this platform."

    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_state": "enabled",
        "decision": decision,
        "allow_sessions": allow_sessions,
        "mode": mode,
        "reason": reason,
        "target": {
            "release": target["release"],
            "release_sequence": target["sequence"],
            "protocol_epoch": target["protocol_epoch"],
            "bundle_version": target["bundle_version"],
            "minimum_client_sequence": target["minimum_sequence"],
        },
        "artifact": artifact,
    }


def artifact_file(config: Mapping[str, Any], platform: str, arch: str) -> Path:
    """Resolve the exact configured artifact path for an authenticated download."""
    policy = _policy(config)
    if policy.get("enabled") is not True:
        raise ClientReleasePolicyError("remote-client release policy is disabled")
    key = platform_key(platform, arch)
    raw = _mapping(_mapping(policy.get("artifacts")).get(key))
    configured = _required_text(raw.get("file"), f"artifacts.{key}.file")
    path = Path(configured).expanduser().resolve()
    if not path.is_file():
        raise ClientReleasePolicyError(f"configured client artifact is unavailable for {key}")
    return path
