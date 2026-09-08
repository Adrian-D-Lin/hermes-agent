from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path

from .diagnostics import FailedCheck

_TABLE = "adrian_kanban_rejection_audit"
_UNRECOGNIZED = "UNRECOGNIZED_OPERATION"


def _as_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{field_name} must be a nonblank string")
    return trimmed


def _as_nonempty_str_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must be a nonempty list of strings")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must contain only strings")
        trimmed = item.strip()
        if not trimmed:
            raise ValueError(f"{field_name} must contain only nonblank strings")
        cleaned.append(trimmed)
    return cleaned


def _safe_projection(response: dict, recognized_operations: frozenset[str]) -> dict:
    if not isinstance(response, dict):
        raise ValueError("response must be a mapping")
    if response.get("result") != "REJECTED":
        raise ValueError("response must be a rejection envelope")
    if response.get("state_changed") is not False:
        raise ValueError("rejection must not report state changed")

    attempt_id = _as_str(response.get("attempt_id"), "attempt_id")
    operation = response.get("operation")
    operation = operation if operation in recognized_operations else _UNRECOGNIZED

    boundary = response.get("boundary")
    if not isinstance(boundary, dict):
        raise ValueError("boundary must be a mapping")
    source = _as_str(boundary.get("from"), "boundary.from")
    destination = _as_str(boundary.get("to"), "boundary.to")
    boundary_projection = {"from": source, "to": destination}

    failed_checks = response.get("failed_checks")
    if not isinstance(failed_checks, list):
        raise ValueError("failed_checks must be a list")
    failed_projection = []
    for check in failed_checks:
        if not isinstance(check, dict):
            raise ValueError("failed_checks entries must be mappings")
        code = _as_str(check.get("code"), "failed_checks.code")
        target = (
            "operation"
            if operation == _UNRECOGNIZED
            else _as_str(check.get("target"), "failed_checks.target")
        )
        failed_projection.append({"code": code, "target": target})

    not_evaluated_checks = response.get("not_evaluated_checks")
    if not isinstance(not_evaluated_checks, list):
        raise ValueError("not_evaluated_checks must be a list")
    not_evaluated_projection = []
    for check in not_evaluated_checks:
        if not isinstance(check, dict):
            raise ValueError("not_evaluated_checks entries must be mappings")
        code = _as_str(check.get("code"), "not_evaluated_checks.code")
        requires = _as_nonempty_str_list(
            check.get("requires"), "not_evaluated_checks.requires"
        )
        not_evaluated_projection.append({"code": code, "requires": requires})

    return {
        "attempt_id": attempt_id,
        "operation": operation,
        "boundary": boundary_projection,
        "failed_checks": failed_projection,
        "not_evaluated_checks": not_evaluated_projection,
    }


def record_rejection(
    database_path: str,
    response: dict,
    *,
    recognized_operations: frozenset[str],
) -> dict:
    if response.get("result") != "REJECTED":
        return response
    try:
        projection = _safe_projection(response, recognized_operations)
        with contextlib.closing(
            sqlite3.connect(Path(database_path).as_uri() + "?mode=rw", uri=True)
        ) as conn:
            with conn:
                conn.execute(
                    f"INSERT INTO {_TABLE} "
                    "(attempt_id, operation, boundary_json, failed_checks_json, "
                    "  not_evaluated_checks_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        projection["attempt_id"],
                        projection["operation"],
                        json.dumps(projection["boundary"]),
                        json.dumps(projection["failed_checks"]),
                        json.dumps(projection["not_evaluated_checks"]),
                        int(time.time()),
                    ),
                )
        return response
    except Exception:
        return _audit_failure(response)


def _audit_failure(response: dict) -> dict:
    failure = FailedCheck(
        code="REJECTION_AUDIT_UNAVAILABLE",
        target="rejection_audit",
        expected="durable diagnostic audit in configured Kanban database",
        observed="audit persistence unavailable",
        accepted_format="writable configured Kanban database with matching plugin schema",
        remediation=(
            "Ask the system operator to restore audit storage, then retry through "
            "the same Kanban boundary; do not bypass controls."
        ),
        responsible_actor="system_operator",
        retry="same_operation",
    )
    failure_dict = failure.as_dict()

    result = dict(response)
    failed_checks = result.get("failed_checks")
    if not isinstance(failed_checks, list):
        failed_checks = []
    else:
        failed_checks = [dict(item) for item in failed_checks if isinstance(item, dict)]

    if not any(
        isinstance(item, dict) and item.get("code") == failure_dict["code"]
        for item in failed_checks
    ):
        failed_checks.append(failure_dict)

    result["failed_checks"] = failed_checks
    return result
