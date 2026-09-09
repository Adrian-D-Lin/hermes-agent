"""Plugin-owned dashboard API for adrian-kanban.

All state reads and commands are delegated to the single command boundary
(``hermes_cli.kanban_db.delegate_authority_operation``). This module never
imports or invokes native dashboard handlers or native DB mutators.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from hermes_cli import kanban_db

from ..versioning import release_identity

router = APIRouter()


def _unavailable_envelope(attempt_id: str, operation: str) -> dict:
    return {
        "result": "REJECTED",
        "state_changed": False,
        "attempt_id": attempt_id,
        "operation": operation,
        "boundary": {"from": "adrian-kanban", "to": "adrian-kanban"},
        "failed_checks": [
            {
                "code": "AUTHORITY_BOUNDARY_UNAVAILABLE",
                "target": operation,
                "expected": "a reachable authority command boundary",
                "observed": "delegation raised or returned a non-dict envelope",
                "accepted_format": "an ACCEPTED or REJECTED boundary envelope",
                "remediation": "restore the selected authority boundary and retry the same operation",
                "responsible_actor": "session_agent",
                "retry": "same_operation",
            }
        ],
        "not_evaluated_checks": [],
    }


def _delegate(operation: str, payload: dict, attempt_id: str):
    try:
        envelope = kanban_db.delegate_authority_operation(
            operation,
            attempt_id=attempt_id,
            payload=payload,
        )
    except Exception:
        return None
    if not isinstance(envelope, dict):
        return None
    return envelope


def _respond(envelope, attempt_id: str, operation: str):
    if not isinstance(envelope, dict) or envelope.get("result") not in (
        "ACCEPTED",
        "REJECTED",
    ):
        return JSONResponse(
            status_code=503,
            content=_unavailable_envelope(attempt_id, operation),
        )
    if envelope.get("result") == "REJECTED":
        return JSONResponse(status_code=409, content=envelope)
    return JSONResponse(status_code=200, content=envelope)


@router.get("/handshake")
def handshake():
    try:
        authority = kanban_db.resolve_selected_authority()
    except Exception:
        authority = None
    return {
        "authority": authority,
        "versions": release_identity(),
        "mutation_controls_enabled": authority == "adrian-kanban",
    }


@router.get("/board")
def board(
    board: str | None = None,
    initiative_id: str | None = None,
    assignee: str | None = None,
    status: str | None = None,
    tenant: str | None = None,
    include_archived: bool | None = None,
    limit: int | None = None,
):
    payload: dict = {}
    if board is not None:
        payload["board"] = board
    if initiative_id is not None:
        payload["initiative_id"] = initiative_id
    if assignee is not None:
        payload["assignee"] = assignee
    if status is not None:
        payload["status"] = status
    if tenant is not None:
        payload["tenant"] = tenant
    if include_archived is not None:
        payload["include_archived"] = include_archived
    if limit is not None:
        payload["limit"] = limit
    attempt_id = str(uuid.uuid4())
    envelope = _delegate("kanban_list", payload, attempt_id)
    return _respond(envelope, attempt_id, "kanban_list")


@router.get("/tasks/{task_id}")
def task(task_id: str):
    attempt_id = str(uuid.uuid4())
    envelope = _delegate("kanban_show", {"task_id": task_id}, attempt_id)
    return _respond(envelope, attempt_id, "kanban_show")


@router.get("/initiatives/{initiative_id}")
def initiative(initiative_id: str):
    attempt_id = str(uuid.uuid4())
    envelope = _delegate(
        "kanban_show", {"initiative_id": initiative_id}, attempt_id
    )
    return _respond(envelope, attempt_id, "kanban_show")


@router.get("/tasks/{task_id}/attachments")
def task_attachments(task_id: str):
    attempt_id = str(uuid.uuid4())
    envelope = _delegate(
        "kanban_attachments", {"task_id": task_id}, attempt_id
    )
    return _respond(envelope, attempt_id, "kanban_attachments")


@router.post("/commands/{operation}")
async def command(operation: str, request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")
    attempt_id = body.get("attempt_id") or str(uuid.uuid4())
    try:
        envelope = kanban_db.delegate_authority_operation(operation, **body)
    except Exception:
        envelope = None
    return _respond(envelope, attempt_id, operation)
