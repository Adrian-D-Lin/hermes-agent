"""Plugin-owned dashboard API for adrian-kanban.

All state reads and commands are delegated to the single command boundary
(``hermes_cli.kanban_db.delegate_authority_operation``). This module never
imports or invokes native dashboard handlers or native DB mutators.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from gateway.trusted_authorizer_evidence import mint_tailscale_authorizer_for_peer
from hermes_cli import kanban_db, projects_db

if __package__:
    from ..versioning import release_identity
    from ..notifications import list_notifications
else:
    release_identity = importlib.import_module(
        "plugins.adrian-kanban.versioning"
    ).release_identity
    list_notifications = importlib.import_module(
        "plugins.adrian-kanban.notifications"
    ).list_notifications

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


@router.get("/projects")
def list_projects():
    try:
        with projects_db.connect_closing() as conn:
            projects = projects_db.list_projects(conn, include_archived=False)
            result = []
            for project in projects:
                if (
                    isinstance(project.board_slug, str)
                    and project.board_slug.strip()
                ):
                    result.append(
                        {
                            "id": project.id,
                            "slug": project.slug,
                            "name": project.name,
                            "board": project.board_slug.strip(),
                        }
                    )
        return {"projects": result}
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"failed to list projects: {exc}",
        ) from exc


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

    attempt_id = body.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        attempt_id = str(uuid.uuid4())

    client_host = request.client.host
    connection_id = f"dashboard-http-{uuid.uuid4()}"
    now = int(time.time())

    try:
        evidence = await asyncio.to_thread(
            mint_tailscale_authorizer_for_peer,
            client_host,
            connection_id=connection_id,
            request_id=attempt_id,
            issued_at=now,
            ttl_seconds=300,
        )
    except Exception:
        return JSONResponse(
            status_code=403,
            content={
                "result": "REJECTED",
                "state_changed": False,
                "attempt_id": attempt_id,
                "operation": operation,
                "boundary": {"from": "adrian-kanban", "to": "adrian-kanban"},
                "failed_checks": [
                    {
                        "code": "ACTOR_NOT_AUTHORIZED",
                        "target": operation,
                        "expected": "authenticated Tailscale ingress",
                        "observed": "peer verification failed",
                        "accepted_format": "valid Tailscale peer identity",
                        "remediation": "connect via Tailscale and retry the same operation",
                        "responsible_actor": "session_agent",
                        "retry": "same_operation",
                    }
                ],
                "not_evaluated_checks": [],
            },
        )

    fields = dict(body)
    fields["attempt_id"] = attempt_id
    fields["execution_context"] = "dashboard"
    fields["api_request_id"] = attempt_id
    fields["initial_authorizer"] = evidence
    fields["override_now"] = now

    try:
        envelope = kanban_db.delegate_authority_operation(operation, **fields)
    except Exception:
        envelope = None
    return _respond(envelope, attempt_id, operation)


_EVENT_POLL_SECONDS = 0.25


def _ws_upgrade_authorized(ws: WebSocket) -> bool:
    try:
        from hermes_cli import web_server
        return bool(web_server._ws_auth_ok(ws))
    except Exception:
        return False


def _read_notification_page(cursor: int) -> dict:
    db_path = Path(kanban_db.resolve_authority_path()).resolve()
    conn = sqlite3.connect(
        db_path.as_uri() + "?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    try:
        conn.row_factory = sqlite3.Row
        return list_notifications(conn, after_event_id=cursor, limit=100)
    finally:
        conn.close()


@router.websocket("/events")
async def dashboard_events(websocket: WebSocket) -> None:
    try:
        authority = kanban_db.resolve_selected_authority()
    except Exception:
        await websocket.close(code=1008)
        return
    if authority != "adrian-kanban":
        await websocket.close(code=1008)
        return

    if not _ws_upgrade_authorized(websocket):
        await websocket.close(code=1008)
        return

    since_raw = websocket.query_params.get("since", "0")
    try:
        cursor = int(since_raw)
    except (TypeError, ValueError):
        await websocket.close(code=1008)
        return
    if cursor < 0:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    try:
        while True:
            page = await asyncio.to_thread(_read_notification_page, cursor)
            if page["events"]:
                await websocket.send_json({"events": page["events"], "cursor": page["cursor"]})
                cursor = page["cursor"]
            try:
                await asyncio.wait_for(websocket.receive(), timeout=_EVENT_POLL_SECONDS)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return
