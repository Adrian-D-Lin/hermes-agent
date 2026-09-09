"""Release identity and dark-install preflight for the Adrian Kanban plugin."""

from __future__ import annotations

from .manifest import (
    build_release_manifest,
    load_release_manifest,
    write_release_manifest,
)
from .models import (
    DarkInstallPlan,
    ReleaseFinding,
    ReleaseManifest,
    ReleaseRejected,
    ReleaseVerification,
)
from .preflight import plan_dark_install, verify_dark_install

__all__ = [
    "DarkInstallPlan",
    "ReleaseFinding",
    "ReleaseManifest",
    "ReleaseRejected",
    "ReleaseVerification",
    "build_release_manifest",
    "load_release_manifest",
    "plan_dark_install",
    "verify_dark_install",
    "write_release_manifest",
]

