"""Generic pre-user-turn admission resolver and result builder."""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_FAIL_CLOSED_RESPONSE = (
    "Turn blocked because pre-user-turn admission could not be completed."
)


def _deep_copy_history(
    conversation_history: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    if conversation_history is None:
        return []
    return copy.deepcopy(conversation_history)


def _clear_staged_input_if_matching(
    agent: Any,
    expected_persist_content: Any,
) -> None:
    pending = getattr(agent, "_pending_cli_user_message", None)
    if isinstance(pending, dict) and pending.get("content") == expected_persist_content:
        agent._pending_cli_user_message = None


def _build_intercepted_result(
    agent: Any,
    conversation_history_copy: List[Dict[str, Any]],
    *,
    final_response: Optional[str] = None,
    completed: bool = False,
    failed: bool = False,
) -> Dict[str, Any]:
    if final_response is None or not final_response.strip():
        final_response = _DEFAULT_FAIL_CLOSED_RESPONSE
    return {
        "final_response": final_response,
        "last_reasoning": None,
        "messages": conversation_history_copy,
        "api_calls": 0,
        "completed": completed,
        "turn_exit_reason": "pre_user_turn_intercepted",
        "failed": failed,
        "partial": False,
        "interrupted": False,
        "response_transformed": False,
        "pre_transform_response": None,
        "response_previewed": False,
        "model": getattr(agent, "model", None),
        "provider": getattr(agent, "provider", None),
        "base_url": getattr(agent, "base_url", None),
        "session_id": getattr(agent, "session_id", None),
    }


def _fail_closed(
    agent: Any,
    history_copy: List[Dict[str, Any]],
    expected_persist_content: Any,
    *,
    final_response: Optional[str] = None,
) -> Dict[str, Any]:
    _clear_staged_input_if_matching(agent, expected_persist_content)
    return {
        "action": "fail",
        "result": _build_intercepted_result(
            agent,
            history_copy,
            final_response=final_response,
            completed=False,
            failed=True,
        ),
    }


def resolve_pre_user_turn(
    agent: Any,
    user_message: Any,
    conversation_history: Optional[List[Dict[str, Any]]],
    task_id: Optional[str],
    persist_user_message: Optional[Any],
    model: Optional[str],
    platform: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Resolve the pre_user_turn hook.

    Returns None to allow the turn to proceed normally.
    Returns a dict with "action" set to "respond", "fail", or "rewrite" to intercept.
    """
    try:
        from hermes_cli import lifecycle

        if not lifecycle.has_hook("pre_user_turn"):
            return None

        history_copy = _deep_copy_history(conversation_history)
        expected_persist_content = (
            persist_user_message if persist_user_message is not None else user_message
        )

        results = lifecycle.invoke_hook(
            "pre_user_turn",
            session_id=getattr(agent, "session_id", None),
            task_id=task_id,
            user_message=user_message,
            conversation_history=history_copy,
            first_turn=len(history_copy) == 0,
            model=model,
            platform=platform,
            parent_session_id=getattr(agent, "_parent_session_id", None),
            user_id=getattr(agent, "_user_id", None),
        )
    except Exception:
        logger.warning("pre_user_turn hook inspection or invocation failed", exc_info=True)
        history_copy = _deep_copy_history(conversation_history)
        expected_persist_content = (
            persist_user_message if persist_user_message is not None else user_message
        )
        return _fail_closed(agent, history_copy, expected_persist_content)

    non_allow = []
    for result in results:
        if result is None:
            continue
        if isinstance(result, dict) and result.get("action") == "allow":
            continue
        non_allow.append(result)

    if len(non_allow) == 0:
        return None

    if len(non_allow) > 1:
        logger.warning("Multiple non-allow pre_user_turn directives; failing closed")
        return _fail_closed(agent, history_copy, expected_persist_content)

    directive = non_allow[0]
    if not isinstance(directive, dict):
        logger.warning("Malformed pre_user_turn directive; failing closed")
        return _fail_closed(agent, history_copy, expected_persist_content)

    action = directive.get("action")
    if action == "respond":
        response = directive.get("response")
        if not isinstance(response, str) or not response.strip():
            logger.warning("pre_user_turn respond missing valid response; failing closed")
            return _fail_closed(agent, history_copy, expected_persist_content)
        _clear_staged_input_if_matching(agent, expected_persist_content)
        return {
            "action": "respond",
            "result": _build_intercepted_result(
                agent,
                history_copy,
                final_response=response,
                completed=True,
                failed=False,
            ),
        }

    if action == "fail_closed":
        response = directive.get("response")
        if not isinstance(response, str) or not response.strip():
            logger.warning("pre_user_turn fail_closed missing valid response; failing closed")
            return _fail_closed(agent, history_copy, expected_persist_content)
        return _fail_closed(
            agent,
            history_copy,
            expected_persist_content,
            final_response=response,
        )

    if action == "rewrite":
        model_message = directive.get("model_message")
        persist_message = directive.get("persist_message")
        if model_message is None or persist_message is None:
            logger.warning("pre_user_turn rewrite missing required fields; failing closed")
            return _fail_closed(agent, history_copy, expected_persist_content)
        return {
            "action": "rewrite",
            "model_message": model_message,
            "persist_message": persist_message,
        }

    logger.warning("Unknown pre_user_turn action %r; failing closed", action)
    return _fail_closed(agent, history_copy, expected_persist_content)
