"""Stopped-maintenance entry point for coordination-workspace backfill."""

from __future__ import annotations

import argparse
import json
import sys

from hermes_cli import kanban_db

from . import _trusted_repository_registry_getter
from .coordination_backfill import (
    CoordinationBackfillError,
    run_coordination_backfill,
)
from .session_startup_runtime import load_projects


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or apply the one-time primary-repository coordination "
            "workspace backfill. Planning is the default and performs no writes."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the fully preflighted plan. Omit for a read-only plan.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if kanban_db.resolve_selected_authority() != "adrian-kanban":
            raise CoordinationBackfillError(
                "kanban.mutation_authority must be adrian-kanban"
            )
        result = run_coordination_backfill(
            kanban_db.resolve_authority_path(),
            _trusted_repository_registry_getter(),
            load_projects(),
            apply=args.apply,
        )
    except CoordinationBackfillError as exc:
        print(f"COORDINATION_BACKFILL=FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    print(
        "COORDINATION_BACKFILL=APPLIED"
        if args.apply
        else "COORDINATION_BACKFILL=PLANNED"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
