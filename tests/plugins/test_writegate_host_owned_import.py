"""Host-owned package importability (Write-Gate Gate 1).

The shared WriteGate primitives live in a repo-root host package
(``writegate/``) that is importable **before plugin discovery** and without
any ``sys.path`` hack that inserts the plugin-private directory.  Core Kanban
and CLI imports must resolve the package in an ordinary process.

This test deliberately does **not** insert ``plugins/write-gate`` (or any
plugin directory) into ``sys.path``.  It asserts that ``writegate`` resolves
from the repository root alone, proving the package is host-owned rather than
plugin-private.
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate_writegate_import(monkeypatch):
    """Ensure ``writegate`` is NOT already importable via a plugin-private
    path, so the test proves real host-owned importability.

    The probe functions build their own pruned ``sys.path`` copy, so this
    fixture is a no-op guard that simply guarantees the test module can run
    in isolation.  The real proof lives in
    :func:`_writegate_resolves_from_repo_root`.
    """
    yield


def _writegate_resolves_from_repo_root():
    """Return True when ``writegate`` resolves with only the repo root on
    ``sys.path`` (no plugin directory)."""
    import importlib.util

    # Strip any plugin-private parent from a temporary copy of sys.path.
    probe_path = [
        p for p in sys.path
        if not (p and str(_REPO_ROOT / "plugins" / "write-gate") == str(p))
    ]
    if str(_REPO_ROOT) not in probe_path:
        probe_path.insert(0, str(_REPO_ROOT))
    saved = sys.path
    sys.path = probe_path
    try:
        return importlib.util.find_spec("writegate") is not None
    finally:
        sys.path = saved


def test_writegate_is_host_owned_package():
    """``writegate`` resolves from the repo root without a plugin-private
    ``sys.path`` entry."""
    assert _writegate_resolves_from_repo_root(), (
        "writegate must be importable from the repo-root host package without "
        "inserting plugins/write-gate onto sys.path"
    )


def test_writegate_modules_import_without_plugin_path():
    """Every shared primitive imports cleanly from the host package."""
    # Do the import under a temporarily pruned sys.path so we prove it does
    # not rely on the plugin-private directory.
    import importlib

    probe_path = [
        p for p in sys.path
        if not (p and str(_REPO_ROOT / "plugins" / "write-gate") == str(p))
    ]
    if str(_REPO_ROOT) not in probe_path:
        probe_path.insert(0, str(_REPO_ROOT))
    saved = sys.path
    sys.path = probe_path
    try:
        for name in ("writegate.registry", "writegate.enforcement",
                     "writegate.recovery", "writegate.binding",
                     "writegate.containment"):
            importlib.import_module(name)
    finally:
        sys.path = saved


def test_plugin_dir_has_no_writegate_package():
    """The old plugin-private location no longer carries the package — the
    host package is the single source of truth."""
    assert not (_REPO_ROOT / "plugins" / "write-gate" / "writegate").exists(), (
        "writegate/ must live at the repo root, not inside the plugin dir"
    )
