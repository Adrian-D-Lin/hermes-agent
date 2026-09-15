"""Session Startup selection controller for adrian-kanban."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Union

from .store import AdmittedStore
from .validation import (
    SessionStartupError,
    SessionStartupRevisionError,
    SessionStartupStateError,
)
from .session_startup_onboarding import OnboardingCoordinator

PROTOCOL_VERSION = "v0.29"


class SessionStartupController:
    """Controller for session startup selection flow."""

    def __init__(
        self,
        store: AdmittedStore,
        project_loader: Callable[[], List[Dict[str, Any]]],
        initiative_loader: Callable[[str], List[Dict[str, Any]]],
        anchor_resolver: Callable[
            [Dict[str, Any], Dict[str, Any], Dict[str, Any]], Dict[str, Any]
        ],
        anchor_revalidator: Callable[
            [Dict[str, Any], Dict[str, Any], Dict[str, Any]], Dict[str, Any]
        ],
        anchor_replacer: Callable[
            [Dict[str, Any], Dict[str, Any], Dict[str, Any]], Dict[str, Any]
        ],
        initiative_creation_coordinator: Any,
        onboarding_coordinator: Optional[OnboardingCoordinator] = None,
    ) -> None:
        self._store = store
        self._project_loader = project_loader
        self._initiative_loader = initiative_loader
        self._anchor_resolver = anchor_resolver
        self._anchor_revalidator = anchor_revalidator
        self._anchor_replacer = anchor_replacer
        for method in ("derive", "execute", "cancel"):
            if not callable(getattr(initiative_creation_coordinator, method, None)):
                raise TypeError(
                    "initiative_creation_coordinator must provide callable "
                    f"{method}"
                )
        self._initiative_creation_coordinator = initiative_creation_coordinator
        if onboarding_coordinator is not None and not isinstance(
            onboarding_coordinator, OnboardingCoordinator
        ):
            raise TypeError(
                "onboarding_coordinator must be an OnboardingCoordinator"
            )
        self._onboarding_coordinator = onboarding_coordinator

    def handle(
        self,
        session_id: Optional[str],
        user_message: Optional[str],
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        first_turn: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Main hook callback."""
        if not session_id or not str(session_id).strip():
            return self._fail_closed("Missing or blank session ID.")
        # Onboarding accepts blank input (blank = accept the default for the
        # current field), so the blank guard is lifted only while the
        # durable onboarding substate is active.
        onboarding_active = False
        try:
            record = self._store.read_session_startup(session_id=session_id)
        except Exception as exc:
            return self._fail_closed(f"Failed to read session startup: {exc}")
        if record is None:
            # A brand-new record has no durable onboarding substate, so a
            # blank/None opening prompt has nothing to act on: fail closed
            # before creating the startup record. Blank input is only allowed
            # while an existing active onboarding record is in progress.
            if (
                user_message is None
                or not isinstance(user_message, str)
                or not user_message.strip()
            ):
                return self._fail_closed(
                    "User message must be a non-blank text string."
                )
            try:
                record = self._store.create_or_read_session_startup(
                    session_id=session_id,
                    opening_prompt=user_message,
                    protocol_version=PROTOCOL_VERSION,
                )
            except Exception as exc:
                return self._fail_closed(
                    f"Failed to initialize session startup: {exc}"
                )
            try:
                projects = self._load_projects()
            except Exception as exc:
                return self._fail_closed(
                    f"Failed to initialize project list: {exc}"
                )
            # The menu always appends "Create a new project" as the
            # final choice, so creation stays available even when the
            # project list is empty.
            return self._present_projects(projects)
        onboarding_active = bool(record.get("onboarding_json"))
        if (
            user_message is None
            or not isinstance(user_message, str)
            or (not user_message.strip() and not onboarding_active)
        ):
            return self._fail_closed(
                "User message must be a non-blank text string."
            )

        state = record["state"]

        if state == "anchored":
            return self._handle_anchored(
                record, user_message, conversation_history or []
            )

        if state == "failed_recoverable":
            return self._handle_failed_recoverable(record, user_message)

        if state == "awaiting_project_selection":
            return self._handle_project_selection(record, user_message)

        if state == "awaiting_initiative_selection":
            return self._handle_initiative_selection(record, user_message)

        if state in ("resolving_lifecycle_and_work", "verifying_workspace",
                     "awaiting_writegate_confirmation"):
            return self._handle_in_progress(record)

        return self._fail_closed(f"Unexpected state: {state}")

    def _fail_closed(self, message: str) -> Dict[str, Any]:
        return {"action": "respond", "response": f"[Session Startup] {message}"}

    def _handle_project_selection(
        self, record: Dict[str, Any], user_message: str
    ) -> Dict[str, Any]:
        try:
            projects = self._load_projects()
        except Exception as exc:
            return self._fail_closed(f"Failed to load projects: {exc}")

        # Durable project onboarding (S2B) is routed through the injected
        # coordinator whenever its substate is present.
        if record.get("onboarding_json"):
            coordinator = self._onboarding_coordinator
            if coordinator is None:
                return self._fail_closed(
                    "Project onboarding is active but the coordinator is "
                    "not configured."
                )
            try:
                result = coordinator.handle(record, user_message)
            except Exception as exc:
                return self._fail_closed(f"Project onboarding failed: {exc}")
            # Route the coordinator result through the existing handback
            # handling so a successful executor transitions to the initiative
            # picker instead of returning a raw handback object.
            if isinstance(result, dict) and result.get("action") == "handback":
                return self._hand_back_new_project(
                    record, result.get("project_id"), result.get("revision")
                )
            # A cancel clears the onboarding substate; the controller owns
            # project loading and menu rendering, so route the narrow
            # return-to-projects action through the normal project menu.
            if isinstance(result, dict) and result.get("action") == "return_to_projects":
                prefix = result.get("prefix")
                return self._present_projects(
                    projects, prefix=prefix or None
                )
            return result

        selection = user_message.strip()
        if not selection:
            return self._present_projects(projects)

        result = self._resolve_project(projects, selection)
        if isinstance(result, dict) and result.get("status") == "create_project":
            coordinator = self._onboarding_coordinator
            if coordinator is None:
                return self._fail_closed(
                    "New project onboarding is not configured for this "
                    "runtime."
                )
            try:
                return coordinator.start_onboarding(record)
            except Exception as exc:
                return self._fail_closed(f"Project onboarding failed: {exc}")
        if result is None:
            return self._present_projects(projects)
        if isinstance(result, str):
            return self._present_projects(projects, prefix=result)

        # Executor success hands back the new project's identity: reload the
        # project list, locate that exact project, and emit the normal
        # initiative picker immediately — no Session Startup restart.
        if isinstance(result, dict) and result.get("action") == "handback":
            return self._hand_back_new_project(record, result.get("project_id"))

        project = result
        try:
            record = self._store.transition_session_startup(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                to_state="awaiting_initiative_selection",
                selected_project_id=project["id"],
            )
        except (SessionStartupRevisionError, SessionStartupStateError) as exc:
            return self._transition_failed(record, exc)

        return self._present_initiatives(record, project)

    def _hand_back_new_project(
        self, record: Dict[str, Any], project_id: Optional[str],
        revision: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Complete project selection after successful onboarding.

        The executor already cleared the onboarding substate and returned
        the new project's identity; this reloads the project list,
        transitions the record to ``awaiting_initiative_selection``, and
        emits the initiative picker for the new project.

        ``revision`` is the newest record revision the executor's persist
        callback returned; it is used for the transition CAS so the update
        is not rejected as stale after the onboarding journal was written.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            return self._fail_closed(
                "The new project's identity is unavailable. Please re-run "
                "Session Startup."
            )
        try:
            projects = self._load_projects()
            project = next(
                (p for p in projects if p["id"] == project_id.strip()), None
            )
            if project is None:
                # The executor returned an identity that the reloaded project
                # list cannot confirm. Do not fabricate a placeholder: fail
                # closed with an actionable response.
                return self._fail_closed(
                    "The new project's identity could not be confirmed after "
                    "creation. Please re-run Session Startup."
                )
        except Exception as exc:
            return self._fail_closed(f"Failed to load projects: {exc}")

        expected = revision if revision is not None else record["revision"]
        try:
            record = self._store.transition_session_startup(
                session_id=record["session_id"],
                expected_revision=expected,
                to_state="awaiting_initiative_selection",
                selected_project_id=project["id"],
            )
        except Exception as exc:
            return self._fail_closed(
                f"Failed to complete project onboarding: {exc}"
            )
        return self._present_initiatives(record, project)

    def _handle_initiative_selection(
        self, record: Dict[str, Any], user_message: str
    ) -> Dict[str, Any]:
        project_id = record["selected_project_id"]
        if not project_id:
            return self._fail_closed("No project selected.")

        try:
            projects = self._load_projects()
            project = next((p for p in projects if p["id"] == project_id), None)
            if project is None:
                return self._fail_closed("Selected project not found.")
            initiatives = self._load_initiatives(project["board_slug"])
        except Exception as exc:
            return self._fail_closed(f"Failed to load initiatives: {exc}")

        selection = user_message.strip()
        if record.get("creation_stage") is not None:
            return self._handle_initiative_creation(record, project, selection)
        if not selection:
            return self._present_initiatives(record, project, initiatives)

        if self._is_browse_closed(selection, len(initiatives)):
            return self._browse_closed_initiatives(record, project)

        n = len(initiatives)
        if selection.isdigit():
            idx = int(selection)
            if idx == n + 2:
                return self._start_initiative_creation(record)
        elif selection.lower() == "create a new initiative":
            return self._start_initiative_creation(record)

        result = self._resolve_initiative(initiatives, selection)
        if result is None:
            return self._present_initiatives(record, project, initiatives)
        if isinstance(result, str):
            return self._present_initiatives(
                record, project, initiatives, prefix=result
            )

        initiative = result
        return self._resolve_and_anchor(record, project, initiative)

    def _start_initiative_creation(
        self, record: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            self._store.update_session_startup_creation_draft(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                creation_stage="awaiting_title",
                creation_draft={},
            )
        except (SessionStartupRevisionError, SessionStartupStateError) as exc:
            return self._fail_closed(
                f"Failed to begin initiative creation: {exc}"
            )
        return self._routing_response(
            "Enter the new initiative title, or enter Cancel to return to "
            "the initiative list."
        )

    def _handle_initiative_creation(
        self,
        record: Dict[str, Any],
        project: Dict[str, Any],
        selection: str,
    ) -> Dict[str, Any]:
        stage = record.get("creation_stage")
        lowered = selection.casefold()

        try:
            draft = self._creation_draft(record)
        except ValueError as exc:
            return self._fail_closed(f"Invalid initiative creation draft: {exc}")

        if stage == "awaiting_title":
            if lowered in {"cancel", "back"}:
                return self._clear_initiative_creation(record, project)[1]
            if not selection:
                return self._routing_response(
                    "The initiative title is required. Enter a title or Cancel."
                )
            try:
                self._store.update_session_startup_creation_draft(
                    session_id=record["session_id"],
                    expected_revision=record["revision"],
                    creation_stage="awaiting_objective",
                    creation_draft={"title": selection},
                )
            except (SessionStartupRevisionError, SessionStartupStateError) as exc:
                return self._fail_closed(
                    f"Failed to retain the initiative title: {exc}"
                )
            return self._routing_response(
                "Enter the new initiative objective, or enter Cancel to return "
                "to the initiative list."
            )

        if stage == "awaiting_objective":
            if lowered in {"cancel", "back"}:
                return self._clear_initiative_creation(record, project)[1]
            if not selection:
                return self._routing_response(
                    "The initiative objective is required. Enter an objective "
                    "or Cancel."
                )
            try:
                proposal = self._initiative_creation_coordinator.derive(
                    project, draft["title"], selection, record
                )
                self._validate_creation_proposal(proposal)
                record = self._store.update_session_startup_creation_draft(
                    session_id=record["session_id"],
                    expected_revision=record["revision"],
                    creation_stage="awaiting_approval",
                    creation_draft=proposal,
                )
            except Exception as exc:
                return self._fail_closed(
                    "Failed to prepare the initiative creation proposal: "
                    f"{exc}. The supplied title is retained; retry the objective "
                    "or enter Cancel."
                )
            return self._execute_initiative_creation(record, project)

        if stage == "awaiting_approval":
            if lowered in {"cancel", "back", "change"}:
                try:
                    self._initiative_creation_coordinator.cancel(
                        project, draft, record
                    )
                except Exception as exc:
                    return self._fail_closed(
                        "Failed to cancel the existing creation approval: "
                        f"{exc}. Retry, Change, or Cancel."
                    )
                if lowered == "change":
                    try:
                        self._store.update_session_startup_creation_draft(
                            session_id=record["session_id"],
                            expected_revision=record["revision"],
                            creation_stage="awaiting_title",
                            creation_draft={},
                        )
                    except Exception as exc:
                        return self._fail_closed(
                            f"Failed to restart initiative creation: {exc}"
                        )
                    return self._routing_response(
                        "Enter the corrected initiative title, or enter Cancel."
                    )
                return self._clear_initiative_creation(record, project)[1]
            if lowered == "retry":
                return self._execute_initiative_creation(record, project)
            return self._routing_response(
                "A creation proposal is awaiting resolution. Enter Retry to "
                "present it again, Change to replace it, or Cancel."
            )

        return self._fail_closed(f"Unexpected initiative creation stage: {stage}")

    def _execute_initiative_creation(
        self, record: Dict[str, Any], project: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            draft = self._creation_draft(record)
            self._validate_creation_proposal(draft)
            outcome = self._initiative_creation_coordinator.execute(
                project, draft, record
            )
        except Exception as exc:
            return self._fail_closed(
                "Initiative creation approval or execution failed: "
                f"{exc}. Enter Retry to reuse the exact proposal, Change to "
                "replace it, or Cancel."
            )

        if not isinstance(outcome, dict):
            return self._fail_closed(
                "Initiative creation returned an invalid result. Enter Retry, "
                "Change, or Cancel."
            )
        status = outcome.get("status")
        if status == "cancelled":
            message = outcome.get("message")
            if not isinstance(message, str) or not message.strip():
                message = "Initiative creation was cancelled."
            try:
                _, menu = self._clear_initiative_creation(record, project)
            except Exception as exc:
                return self._fail_closed(
                    f"Failed to clear the cancelled creation proposal: {exc}"
                )
            return self._present_initiatives(
                record,
                project,
                prefix=message.strip(),
            ) if menu.get("action") != "respond" else {
                "action": "respond",
                "response": f"{message.strip()}\n{menu['response']}",
            }
        if status != "created" or set(outcome) != {"status", "initiative"}:
            return self._fail_closed(
                "Initiative creation returned an invalid result. Enter Retry, "
                "Change, or Cancel."
            )

        initiative = outcome["initiative"]
        try:
            self._validate_created_initiative(initiative)
            fresh_record, _ = self._clear_initiative_creation(record, project)
        except Exception as exc:
            return self._fail_closed(
                f"Created initiative projection is invalid: {exc}"
            )
        return self._resolve_and_anchor(fresh_record, project, initiative)

    def _clear_initiative_creation(
        self, record: Dict[str, Any], project: Dict[str, Any]
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        fresh = self._store.update_session_startup_creation_draft(
            session_id=record["session_id"],
            expected_revision=record["revision"],
            creation_stage=None,
            creation_draft=None,
        )
        initiatives = self._load_initiatives(project["board_slug"])
        return fresh, self._present_initiatives(fresh, project, initiatives)

    @staticmethod
    def _creation_draft(record: Dict[str, Any]) -> Dict[str, Any]:
        raw = record.get("creation_draft_json")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("creation draft JSON is missing")
        try:
            draft = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("creation draft JSON is malformed") from exc
        if not isinstance(draft, dict):
            raise ValueError("creation draft must be an object")
        return draft

    @staticmethod
    def _validate_creation_proposal(proposal: Any) -> None:
        required = {
            "title",
            "objective",
            "initiative_id",
            "body",
            "request_id",
            "approval_id",
        }
        if not isinstance(proposal, dict) or set(proposal) != required:
            raise ValueError("creation proposal has an invalid field set")
        for field in required:
            value = proposal[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"creation proposal field {field!r} must be nonblank text"
                )

    @staticmethod
    def _validate_created_initiative(initiative: Any) -> None:
        if not isinstance(initiative, dict):
            raise ValueError("initiative must be an object")
        for field in ("initiative_id", "title", "current_phase"):
            value = initiative.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"initiative {field} must be nonblank text")
        segment = initiative.get("current_segment_id")
        if segment is not None and (
            not isinstance(segment, str) or not segment.strip()
        ):
            raise ValueError("initiative current_segment_id is invalid")
        if initiative.get("closed_at") is not None:
            raise ValueError("newly created initiative must be open")

    @staticmethod
    def _is_browse_closed(selection: str, active_count: int) -> bool:
        if selection.lower() == "browse closed initiatives":
            return True
        return selection.isdigit() and int(selection) == active_count + 1

    def _browse_closed_initiatives(
        self, record: Dict[str, Any], project: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            raw = self._initiative_loader(project["board_slug"])
        except Exception as exc:
            return self._fail_closed(f"Failed to load initiatives: {exc}")
        if not isinstance(raw, list):
            return self._fail_closed(
                "Invalid initiative projection: expected a list."
            )

        closed = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                return self._fail_closed(
                    f"Invalid initiative projection at index {index}: expected a dict."
                )
            if item.get("closed_at") is None:
                continue
            for field in ("initiative_id", "title", "current_phase"):
                value = item.get(field)
                if not isinstance(value, str) or not value.strip():
                    return self._fail_closed(
                        f"Invalid closed initiative at index {index}: "
                        f"missing or blank {field}."
                    )
            segment = item.get("current_segment_id")
            if segment is not None and (
                not isinstance(segment, str) or not segment.strip()
            ):
                return self._fail_closed(
                    f"Invalid closed initiative at index {index}: "
                    "current_segment_id must be null or nonblank text."
                )
            closed.append(item)

        lines = [f"Closed initiatives for {project['name']}:"]
        if not closed:
            lines.append("None.")
        else:
            for item in closed:
                segment = item.get("current_segment_id")
                segment_text = f" / {segment}" if segment else ""
                lines.append(
                    f"- {item['title']} [{item['initiative_id']}] "
                    f"Phase: {item['current_phase']}{segment_text} "
                    f"Closed: {item['closed_at']}"
                )
        lines.append(
            "Closed initiatives are permanently read-only and cannot create "
            "a Session Startup binding."
        )
        lines.append("")
        active_menu = self._present_initiatives(record, project)["response"]
        lines.extend(active_menu.split("\n"))
        return {"action": "respond", "response": "\n".join(lines)}

    def _resolve_and_anchor(
        self, record: Dict[str, Any], project: Dict[str, Any], initiative: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            record = self._store.transition_session_startup(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                to_state="resolving_lifecycle_and_work",
                selected_initiative_id=initiative["initiative_id"],
            )
        except (SessionStartupRevisionError, SessionStartupStateError) as exc:
            return self._transition_failed(record, exc)

        try:
            result = self._anchor_resolver(project, initiative, record)
        except Exception as exc:
            return self._transition_failed(record, exc)

        accepted_phase = result.get("accepted_phase")
        accepted_segment_id = result.get("accepted_segment_id")
        logical_workspace_id = result.get("logical_workspace_id")
        writegate_binding_version = result.get("writegate_binding_version")
        writegate_binding_ref = result.get("writegate_binding_ref")

        if not accepted_phase or not logical_workspace_id:
            return self._transition_failed(
                record, ValueError("anchor_resolver returned incomplete result")
            )
        if not writegate_binding_version or not writegate_binding_ref:
            return self._transition_failed(
                record, ValueError("anchor_resolver returned incomplete binding")
            )

        try:
            record = self._store.transition_session_startup(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                to_state="verifying_workspace",
                accepted_phase=accepted_phase,
                accepted_segment_id=accepted_segment_id,
                logical_workspace_id=logical_workspace_id,
            )
        except (SessionStartupRevisionError, SessionStartupStateError) as exc:
            return self._transition_failed(record, exc)

        try:
            record = self._store.transition_session_startup(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                to_state="awaiting_writegate_confirmation",
                writegate_binding_version=writegate_binding_version,
                writegate_binding_ref=writegate_binding_ref,
            )
        except (SessionStartupRevisionError, SessionStartupStateError) as exc:
            return self._transition_failed(record, exc)

        try:
            record = self._store.transition_session_startup(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                to_state="anchored",
            )
        except (SessionStartupRevisionError, SessionStartupStateError) as exc:
            return self._transition_failed(record, exc)

        try:
            record = self._store.set_release_status(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                release_status="issued",
            )
        except Exception as exc:
            return self._transition_failed(record, exc)

        held_prompt = record["held_opening_prompt"]
        anchor = self._build_anchor(
            project_id=project["id"],
            initiative_id=initiative["initiative_id"],
            accepted_phase=accepted_phase,
            accepted_segment_id=accepted_segment_id,
            logical_workspace_id=logical_workspace_id,
            writegate_binding_version=writegate_binding_version,
            writegate_binding_ref=writegate_binding_ref,
        )
        return {
            "action": "rewrite",
            "model_message": anchor + "\n" + held_prompt,
            "persist_message": held_prompt,
        }

    def _handle_anchored(
        self,
        record: Dict[str, Any],
        user_message: str,
        conversation_history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        try:
            projects = self._load_projects()
            project_matches = [
                item
                for item in projects
                if item["id"] == record["selected_project_id"]
            ]
            if not project_matches:
                return self._fail_closed(
                    "Selected project not found in the active project list. "
                    "Remediation: verify that it remains active and unarchived."
                )
            if len(project_matches) > 1:
                return self._fail_closed(
                    "Selected project authority is ambiguous. Remediation: "
                    "repair duplicate immutable project IDs."
                )
            project = project_matches[0]
            initiatives = self._load_initiatives(project["board_slug"])
            initiative_matches = [
                item
                for item in initiatives
                if item["initiative_id"] == record["selected_initiative_id"]
            ]
            if not initiative_matches:
                return self._fail_closed(
                    "Selected initiative not found in the active Tracker "
                    "projection. Remediation: verify that it remains active "
                    "and is not formally closed."
                )
            if len(initiative_matches) > 1:
                return self._fail_closed(
                    "Selected initiative authority is ambiguous. Remediation: "
                    "repair duplicate immutable initiative IDs."
                )
            initiative = initiative_matches[0]
        except Exception as exc:
            return self._fail_closed(f"Failed to reload anchor authority: {exc}")

        try:
            validation = self._anchor_revalidator(project, initiative, record)
        except Exception as exc:
            return self._fail_closed(f"Anchor revalidation failed: {exc}")
        if not isinstance(validation, dict):
            return self._fail_closed(
                "Anchor revalidation returned a non-dict result."
            )
        classification = validation.get("classification")
        reasons = validation.get("reasons")
        if classification not in {
            "unchanged",
            "focused_revalidation_required",
        }:
            return self._fail_closed(
                f"Invalid anchor revalidation classification: {classification!r}."
            )
        if not isinstance(reasons, list) or not all(
            isinstance(reason, str) for reason in reasons
        ):
            return self._fail_closed(
                "Invalid anchor revalidation reasons: expected a list of strings."
            )

        if classification == "focused_revalidation_required":
            try:
                replacement = self._anchor_replacer(project, initiative, record)
            except Exception as exc:
                return self._fail_closed(f"Anchor replacement failed: {exc}")
            if not isinstance(replacement, dict):
                return self._fail_closed(
                    "Anchor replacement returned a non-dict result."
                )
            accepted_phase = replacement.get("accepted_phase")
            accepted_segment_id = replacement.get("accepted_segment_id")
            logical_workspace_id = replacement.get("logical_workspace_id")
            writegate_binding_version = replacement.get(
                "writegate_binding_version"
            )
            writegate_binding_ref = replacement.get("writegate_binding_ref")
            if not isinstance(accepted_phase, str) or not accepted_phase.strip():
                return self._fail_closed(
                    "Anchor replacement missing valid accepted_phase."
                )
            if accepted_segment_id is not None and (
                not isinstance(accepted_segment_id, str)
                or not accepted_segment_id.strip()
            ):
                return self._fail_closed(
                    "Anchor replacement accepted_segment_id must be null or "
                    "nonblank text."
                )
            if (
                not isinstance(logical_workspace_id, str)
                or not logical_workspace_id.strip()
            ):
                return self._fail_closed(
                    "Anchor replacement missing valid logical_workspace_id."
                )
            if (
                not isinstance(writegate_binding_version, str)
                or not writegate_binding_version.strip()
            ):
                return self._fail_closed(
                    "Anchor replacement missing valid writegate_binding_version."
                )
            if (
                not isinstance(writegate_binding_ref, str)
                or not writegate_binding_ref.strip()
            ):
                return self._fail_closed(
                    "Anchor replacement missing valid writegate_binding_ref."
                )
            try:
                record = self._store.replace_anchored_session_startup(
                    session_id=record["session_id"],
                    expected_revision=record["revision"],
                    accepted_phase=accepted_phase,
                    accepted_segment_id=accepted_segment_id,
                    logical_workspace_id=logical_workspace_id,
                    writegate_binding_version=writegate_binding_version,
                    writegate_binding_ref=writegate_binding_ref,
                )
            except Exception as exc:
                return self._fail_closed(
                    f"Failed to persist anchor replacement: {exc}"
                )

        anchor = self._build_anchor(
            project_id=record["selected_project_id"],
            initiative_id=record["selected_initiative_id"],
            accepted_phase=record["accepted_phase"],
            accepted_segment_id=record["accepted_segment_id"],
            logical_workspace_id=record["logical_workspace_id"],
            writegate_binding_version=record["writegate_binding_version"],
            writegate_binding_ref=record["writegate_binding_ref"],
        )
        release_status = record.get("release_status", "pending")
        if release_status == "observed":
            return {
                "action": "rewrite",
                "model_message": anchor + "\n" + user_message,
                "persist_message": user_message,
            }

        held_prompt = record["held_opening_prompt"]
        if release_status == "issued":
            for entry in conversation_history:
                if entry.get("role") == "user" and entry.get("content") == held_prompt:
                    try:
                        self._store.set_release_status(
                            session_id=record["session_id"],
                            expected_revision=record["revision"],
                            release_status="observed",
                        )
                    except Exception:
                        return self._fail_closed(
                            "Failed to persist observed release status."
                        )
                    return {
                        "action": "rewrite",
                        "model_message": anchor + "\n" + user_message,
                        "persist_message": user_message,
                    }
            return {
                "action": "rewrite",
                "model_message": anchor + "\n" + held_prompt,
                "persist_message": held_prompt,
            }

        if release_status == "pending":
            try:
                record = self._store.set_release_status(
                    session_id=record["session_id"],
                    expected_revision=record["revision"],
                    release_status="issued",
                )
            except Exception as exc:
                return self._fail_closed(f"Failed to issue release status: {exc}")
            return {
                "action": "rewrite",
                "model_message": anchor + "\n" + held_prompt,
                "persist_message": held_prompt,
            }

        return self._fail_closed(f"Invalid release status: {release_status}")

    def _handle_failed_recoverable(
        self, record: Dict[str, Any], user_message: str
    ) -> Dict[str, Any]:
        project_id = record.get("selected_project_id")
        initiative_id = record.get("selected_initiative_id")

        if not project_id:
            try:
                record = self._store.transition_session_startup(
                    session_id=record["session_id"],
                    expected_revision=record["revision"],
                    to_state="awaiting_project_selection",
                )
            except (SessionStartupRevisionError, SessionStartupStateError) as exc:
                return self._transition_failed(record, exc)
            try:
                projects = self._load_projects()
            except Exception as exc:
                return self._fail_closed(f"Failed to load projects: {exc}")
            if not projects:
                return self._fail_closed("No active projects available.")
            return self._present_projects(projects)

        if not initiative_id:
            try:
                record = self._store.transition_session_startup(
                    session_id=record["session_id"],
                    expected_revision=record["revision"],
                    to_state="awaiting_initiative_selection",
                    selected_project_id=project_id,
                )
            except (SessionStartupRevisionError, SessionStartupStateError) as exc:
                return self._transition_failed(record, exc)
            try:
                projects = self._load_projects()
                project = next((p for p in projects if p["id"] == project_id), None)
                if project is None:
                    return self._fail_closed(
                        "Selected project not found. Remediation: re-select a valid project."
                    )
                initiatives = self._load_initiatives(project["board_slug"])
            except Exception as exc:
                return self._fail_closed(f"Failed to load initiatives: {exc}")
            return self._present_initiatives(record, project, initiatives)

        try:
            record = self._store.transition_session_startup(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                to_state="awaiting_initiative_selection",
                selected_project_id=project_id,
            )
        except (SessionStartupRevisionError, SessionStartupStateError) as exc:
            return self._transition_failed(record, exc)

        try:
            projects = self._load_projects()
            project = next((p for p in projects if p["id"] == project_id), None)
            if project is None:
                return self._fail_closed(
                    "Selected project not found. Remediation: re-select a valid project."
                )
            initiatives = self._load_initiatives(project["board_slug"])
            initiative = next(
                (i for i in initiatives if i["initiative_id"] == initiative_id), None
            )
            if initiative is None:
                return self._fail_closed(
                    "Selected initiative not found. Remediation: re-select a valid initiative."
                )
        except Exception as exc:
            return self._fail_closed(f"Failed to reload selection: {exc}")

        return self._resolve_and_anchor(record, project, initiative)

    def _handle_in_progress(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return self._fail_closed(
            f"Session startup is in progress ({record['state']}). Please wait."
        )

    def _transition_failed(
        self, record: Dict[str, Any], exc: Exception
    ) -> Dict[str, Any]:
        persistence_error = None
        try:
            self._store.transition_session_startup(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                to_state="failed_recoverable",
                failure_detail=str(exc),
            )
        except Exception as pexc:
            persistence_error = pexc
        if persistence_error:
            return self._fail_closed(
                f"Session startup transition failed: {exc}. "
                f"Failed to persist failure state: {persistence_error}"
            )
        return self._fail_closed(f"Session startup transition failed: {exc}")

    def _load_projects(self) -> List[Dict[str, Any]]:
        raw = self._project_loader()
        if not isinstance(raw, list):
            raise ValueError("project_loader must return a list")
        projects = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("project_loader returned a non-dict item")
            if item.get("archived"):
                continue
            if not item.get("board_slug"):
                continue
            if not item.get("id") or not item.get("name") or not item.get("primary_path"):
                raise ValueError(
                    "project missing required fields: id, name, or primary_path"
                )
            projects.append(item)
        return projects

    def _load_initiatives(self, board_slug: str) -> List[Dict[str, Any]]:
        raw = self._initiative_loader(board_slug)
        if not isinstance(raw, list):
            raise ValueError("initiative_loader must return a list")
        initiatives = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("initiative_loader returned a non-dict item")
            if item.get("closed_at") is not None:
                continue
            if not item.get("initiative_id") or not item.get("title"):
                raise ValueError(
                    "initiative missing required fields: initiative_id or title"
                )
            initiatives.append(item)
        return initiatives

    def _resolve_project(
        self, projects: List[Dict[str, Any]], selection: str
    ) -> Union[Dict[str, Any], str]:
        if selection.isdigit():
            idx = int(selection)
            if 1 <= idx <= len(projects):
                return projects[idx - 1]
            if idx == len(projects) + 1:
                # Final menu entry: "Create a new project".
                return {"status": "create_project"}
            return "Invalid project selection. Please choose a valid project number or name."
        if selection.strip().lower() == "create a new project":
            return {"status": "create_project"}
        matches = [p for p in projects if p["id"] == selection]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "Ambiguous project selection. Please use the exact project ID or number."
        matches = [p for p in projects if p["name"].lower() == selection.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "Ambiguous project selection. Please use the exact project ID or number."
        return "Unrecognized project selection. Please choose a valid project number or name."

    def _resolve_initiative(
        self, initiatives: List[Dict[str, Any]], selection: str
    ) -> Union[Dict[str, Any], str]:
        if selection.isdigit():
            idx = int(selection)
            if 1 <= idx <= len(initiatives):
                return initiatives[idx - 1]
            return "Invalid initiative selection. Please choose a valid initiative number or name."
        matches = [i for i in initiatives if i["initiative_id"] == selection]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "Ambiguous initiative selection. Please use the exact initiative ID or number."
        matches = [i for i in initiatives if i["title"].lower() == selection.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "Ambiguous initiative selection. Please use the exact initiative ID or number."
        return "Unrecognized initiative selection. Please choose a valid initiative number or name."

    def _present_projects(
        self, projects: List[Dict[str, Any]], prefix: Optional[str] = None
    ) -> Dict[str, Any]:
        lines = []
        if prefix:
            lines.append(prefix)
        lines.append("Select a project:")
        for i, p in enumerate(projects, 1):
            lines.append(f"{i}. {p['name']} ({p['id']})")
        lines.append(f"{len(projects) + 1}. Create a new project")
        return {"action": "respond", "response": "\n".join(lines)}

    def _present_initiatives(
        self,
        record: Dict[str, Any],
        project: Dict[str, Any],
        initiatives: Optional[List[Dict[str, Any]]] = None,
        prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        if initiatives is None:
            try:
                initiatives = self._load_initiatives(project["board_slug"])
            except Exception:
                return self._fail_closed("Failed to load initiatives.")
        lines = []
        if prefix:
            lines.append(prefix)
        lines.append(f"Active initiatives for {project['name']}:")
        for i, init in enumerate(initiatives, 1):
            phase = init.get("current_phase", "unknown")
            segment = init.get("current_segment_id")
            seg_text = f" / {segment}" if segment else ""
            lines.append(f"{i}. {init['title']} [{phase}{seg_text}]")
        n = len(initiatives)
        lines.append(f"{n + 1}. Browse closed initiatives")
        lines.append(f"{n + 2}. Create a new initiative")
        return {"action": "respond", "response": "\n".join(lines)}

    def _routing_response(self, message: str) -> Dict[str, Any]:
        return {"action": "respond", "response": f"[Session Startup] {message}"}

    def _build_anchor(
        self,
        project_id: str,
        initiative_id: str,
        accepted_phase: str,
        accepted_segment_id: Optional[str],
        logical_workspace_id: str,
        writegate_binding_version: str,
        writegate_binding_ref: str,
    ) -> str:
        segment_text = accepted_segment_id if accepted_segment_id else "none"
        return (
            f"[Session Startup Anchor]\n"
            f"Project: {project_id}\n"
            f"Initiative: {initiative_id}\n"
            f"Phase: {accepted_phase}\n"
            f"Segment: {segment_text}\n"
            f"Workspace: {logical_workspace_id}\n"
            f"WriteGate Version: {writegate_binding_version}\n"
            f"WriteGate Ref: {writegate_binding_ref}\n"
        )
