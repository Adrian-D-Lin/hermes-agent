"""Central release identity for every Adrian Kanban surface."""

from __future__ import annotations

PLUGIN_NAME = "adrian-kanban"
PLUGIN_VERSION = "0.2.0"
PROTOCOL_VERSION = "2"
SCHEMA_VERSION = "1"
REGISTRY_VERSION = "v0.28-s2-1"
SKILL_BUNDLE_VERSION = "0.1.0"
DESKTOP_EXTENSION_VERSION = "0.2.0"


def release_identity() -> dict:
    return {
        "plugin_version": PLUGIN_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "registry_version": REGISTRY_VERSION,
        "skill_bundle_version": SKILL_BUNDLE_VERSION,
        "desktop_extension_version": DESKTOP_EXTENSION_VERSION,
    }


__all__ = [
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
    "PROTOCOL_VERSION",
    "SCHEMA_VERSION",
    "REGISTRY_VERSION",
    "SKILL_BUNDLE_VERSION",
    "DESKTOP_EXTENSION_VERSION",
    "release_identity",
]
