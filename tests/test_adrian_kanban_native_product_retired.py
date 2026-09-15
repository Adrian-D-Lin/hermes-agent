"""Distribution contract for retiring the superseded native Kanban product."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_superseded_native_kanban_product_is_not_shipped() -> None:
    """The old server dashboard/service and bundled Desktop UI stay absent."""
    server_surface = ROOT / "plugins" / "kanban"
    desktop_surface = ROOT / "apps" / "desktop" / "src" / "plugins" / "kanban"
    assert not server_surface.exists() or not any(
        path.is_file() for path in server_surface.rglob("*")
    )
    assert not desktop_surface.exists() or not any(
        path.is_file() for path in desktop_surface.rglob("*")
    )


def test_adrian_tracker_and_required_shared_substrate_remain() -> None:
    """Retirement must not remove the Initiative Tracker's persistence seam."""
    assert (ROOT / "plugins" / "adrian-kanban" / "plugin.yaml").is_file()
    for module in (
        "kanban_authority.py",
        "kanban_db.py",
        "kanban_db_connect.py",
        "kanban_db_dispatch.py",
        "kanban_db_workspace.py",
    ):
        assert (ROOT / "hermes_cli" / module).is_file()
