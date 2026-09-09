"""Trusted host correlation reaches handlers without becoming model input."""

from __future__ import annotations

from tests.test_adrian_kanban_s3_initiatives import _database, commands_module  # noqa: F401


def test_mutation_handler_receives_exact_host_runtime_fields(
    commands_module, tmp_path, monkeypatch
):
    database_path, provider = _database(tmp_path, monkeypatch, commands_module)
    seen = {}

    def handler(context):
        seen.update(
            turn_id=context.turn_id,
            api_request_id=context.api_request_id,
            user_task=context.user_task,
        )
        return {"ok": True}

    boundary = commands_module._CommandBoundary(
        database_path=str(database_path),
        provider=provider,
        handlers={"kanban_comment": handler},
        state_resolver=lambda *_: 0,
    )
    normalizer = commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda *_: ("orchestrator", "workspace-1", "default"),
    )

    response = normalizer.submit(
        "kanban_comment",
        {
            "task_id": "task-1",
            "body": "comment",
            "idempotency_key": "runtime-context-key",
        },
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "api_request_id": "turn-1:api:1",
            "user_task": "Prepare the exact initiative override proposal.",
        },
    )

    assert response["result"] == "ACCEPTED", response
    assert seen == {
        "turn_id": "turn-1",
        "api_request_id": "turn-1:api:1",
        "user_task": "Prepare the exact initiative override proposal.",
    }


def test_model_arguments_cannot_forge_host_runtime_fields(commands_module):
    class Boundary:
        calls = []

        def submit(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"result": "ACCEPTED"}

        def _finish_response(self, response):
            return response

        def _rejection_internal(self, attempt_id, operation, code):
            return {
                "result": "REJECTED",
                "attempt_id": attempt_id,
                "operation": operation,
                "failed_checks": [{"code": code}],
            }

    boundary = Boundary()
    normalizer = commands_module._ModelToolRequestNormalizer(
        boundary,
        board_resolver=lambda *_: ("orchestrator", None, "default"),
    )

    for field in ("turn_id", "api_request_id", "user_task"):
        response = normalizer.submit(
            "kanban_comment",
            {
                "task_id": "task-1",
                "body": "comment",
                "idempotency_key": f"forge-{field}",
                field: "model-forged",
            },
            {
                "session_id": "session-1",
                "turn_id": "host-turn",
                "api_request_id": "host-api",
                "user_task": "host instruction",
            },
        )
        assert response["result"] == "REJECTED"

    assert boundary.calls == []
