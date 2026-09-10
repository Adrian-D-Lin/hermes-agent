"""Dedicated Adrian Kanban dispatcher service entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3

from hermes_cli import kanban_db as _kb

from . import PLUGIN_NAME, _trusted_repository_registry_getter
from .dispatcher import _run_dispatcher_daemon
from .provider import AdrianKanbanAuthorityProvider, register_provider
from .schema import create_schema


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("interval must be a positive number")
    if not parsed > 0:
        raise argparse.ArgumentTypeError("interval must be a positive number")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max launches must be a positive integer")
    if not parsed > 0:
        raise argparse.ArgumentTypeError("max launches must be a positive integer")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the dedicated Adrian Kanban dispatcher service."
    )
    parser.add_argument(
        "--interval",
        type=_positive_float,
        default=10.0,
        help="Seconds between dispatch ticks (default: 10).",
    )
    parser.add_argument(
        "--max-launches",
        type=_positive_int,
        default=None,
        help="Maximum number of task launches per dispatch tick (default: no limit).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    authority = _kb.resolve_selected_authority()
    if authority != PLUGIN_NAME:
        raise SystemExit(f"refusing to run: selected authority is {authority!r}")

    database_path = _kb.resolve_authority_path()
    if not os.path.isabs(database_path):
        raise SystemExit(
            f"refusing to run: authority path is not absolute: {database_path!r}"
        )

    parent = os.path.dirname(database_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(database_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        create_schema(conn)

        provider = AdrianKanbanAuthorityProvider(database_path)
        registry = _trusted_repository_registry_getter()
        provider.bind_workspace_registry(registry)
        _kb.clear_authority_providers()
        register_provider(provider)

        _run_dispatcher_daemon(
            provider,
            conn,
            interval_seconds=args.interval,
            dispatcher_session_id=f"adrian-kanban-dispatcher:{os.getpid()}",
            max_launches=args.max_launches,
            workspace_registry=registry,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
