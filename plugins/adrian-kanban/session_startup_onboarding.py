"""Lean, durable Session Startup project-onboarding conversation (S2B).

Drives "Create/bind a new project" end to end using the S2A
``onboarding_json`` object as durable state. Field progression is
table-driven; every response is persisted via
``store.set_session_startup_onboarding`` before the next prompt.

External side effects (repository inspection, proposal preparation, project
creation) are injected callbacks. No Git, client, config writer, Project DB
write, or board mutation is implemented here.

Onboarding object::

    {"stage": str, "draft": dict, "journal": dict}

Stages: ``choose_mode``, ``create_details``/``bind_details``
(``draft["next"]`` holds the next field), ``confirm`` (literal ``confirm``
executes), ``blocked_recoverable`` (journal carries a nonblank
``recovery_error``; only retry / cancel accepted).

Executor contract::

    executor(record, proposal, persist) -> {"status": "created",
                                            "project": {"id": ...}}

``persist`` re-saves a full onboarding object through the S2A CAS and
returns the new record. Cancellation returns ``return_to_projects``; the
controller owns project loading and menu rendering.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from .repository_binding import (
    validate_github_repository_name,
    validate_integration_branch,
)
from .validation import (
    SessionStartupRevisionError,
    SessionStartupStateError,
)

OWNER = "Adrian-D-Lin"
CREATE_MODE = "create new GitHub repository"
BIND_MODE = "bind existing GitHub repository"
MAX_FIELD_LEN = 100
PROPOSAL_FIELDS = (
    "mode",
    "display_name",
    "project_slug",
    "repository",
    "integration_branch",
    "canonical_checkout",
    "controlled_worktree_root",
    "board_slug",
)
# Table-driven field progression: (field name, prompt template).
CREATE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("display_name", "Enter the project display name:"),
    ("repository", "Enter the GitHub repository name:"),
    ("visibility",
     "Repository visibility: private (default) or public. Enter 'public' "
     "explicitly for a public repository; press Enter for private."),
    ("branch", "Integration branch (default: main):"),
)
BIND_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("repository", "Enter the GitHub repository name to bind:"),
    ("display_name", "Enter the project display name:"),
    ("branch", "Integration branch (default: %s):"),
)


def _resp(message: str) -> Dict[str, Any]:
    return {"action": "respond", "response": f"[Session Startup] {message}"}


def _field_key(name: str, mode: str) -> str:
    return f"branch_{mode}" if name == "branch" else name


def _validate_field(name: str, value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{name} is required")
    if len(text) > MAX_FIELD_LEN:
        raise ValueError(f"{name} must be at most {MAX_FIELD_LEN} characters")
    if name == "repository":
        # The bare repository name is validated by the shared repository
        # name validator (repository_binding); the owner is fixed.
        return validate_github_repository_name(text)
    if name == "branch":
        # User-typed and inspector-returned branches share one complete
        # validator (repository_binding); multi-component names allowed.
        return validate_integration_branch(text)
    if name == "visibility":
        # Visibility must be the literal deliberate selection. Reject
        # "Public"/"PUBLIC" and any other non-exact token like any other
        # invalid value; only the exact lowercase tokens are accepted after
        # normal input trimming (a blank is handled by the caller before
        # validation runs).
        if text in ("private", "public"):
            return text
        raise ValueError("visibility must be 'private' or 'public'")
    return text


def _field_prompt(name: str, mode: str, draft: Dict[str, Any]) -> str:
    fields = CREATE_FIELDS if mode == "create" else BIND_FIELDS
    for field, prompt in fields:
        if field == name:
            if name == "branch" and mode == "bind":
                return prompt % (draft.get("branch_default") or "(unavailable)")
            return prompt
    raise ValueError(f"unknown field {name!r} for mode {mode!r}")


class OnboardingCoordinator:
    """Drives project onboarding on top of the S2A onboarding substate."""

    def __init__(
        self,
        store: Any,
        repo_inspector: Optional[Callable[[str], str]] = None,
        proposal_preparer: Optional[
            Callable[[Dict[str, Any]], Dict[str, str]]
        ] = None,
        operation_executor: Optional[
            Callable[
                [Dict[str, Any], Dict[str, Any],
                 Callable[[Dict[str, Any]], Dict[str, Any]]],
                Dict[str, Any],
            ]
        ] = None,
    ) -> None:
        self._store = store
        self._repo_inspector = repo_inspector
        self._proposal_preparer = proposal_preparer
        self._operation_executor = operation_executor

    def start_onboarding(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return self._persist(record, "choose_mode", {}, {})

    def handle(self, record: Dict[str, Any], user_message: str) -> Dict[str, Any]:
        try:
            stage, draft, journal = self._state(record)
        except ValueError as exc:
            return _resp(f"Stored onboarding state is invalid: {exc}")
        if stage in ("create_details", "bind_details"):
            return self._handle_details(record, stage, draft, journal, user_message)
        if stage == "choose_mode":
            return self._choose_mode(record, user_message)
        if stage == "confirm":
            return self._confirm(record, journal, user_message)
        if stage == "blocked_recoverable":
            return self._blocked(record, journal, user_message)
        return _resp(f"Unexpected onboarding stage: {stage!r}")

    # -- stages -------------------------------------------------------------

    def _choose_mode(self, record: Dict[str, Any], user_message: str) -> Dict[str, Any]:
        selection = user_message.strip()
        lowered = selection.casefold()
        if lowered in ("cancel", "back"):
            return self._cancel(record, prefix="Project onboarding cancelled.")
        if selection == "1" or lowered == CREATE_MODE.casefold():
            draft = {"mode": "create", "next": "display_name"}
            return self._persist(record, "create_details", draft, {})
        if selection == "2" or lowered == BIND_MODE.casefold():
            draft = {"mode": "bind", "next": "repository"}
            return self._persist(record, "bind_details", draft, {})
        return self._mode_prompt()

    def _handle_details(
        self,
        record: Dict[str, Any],
        stage: str,
        draft: Dict[str, Any],
        journal: Dict[str, Any],
        user_message: str,
    ) -> Dict[str, Any]:
        mode = draft.get("mode")
        if mode not in ("create", "bind"):
            return _resp("Onboarding draft is missing its mode.")
        name = draft.get("next")
        if not isinstance(name, str) or not name.strip():
            return _resp("Onboarding draft is missing the next field.")
        lowered = user_message.strip().casefold()
        if lowered in ("cancel", "back"):
            return self._cancel(record, prefix="Project onboarding cancelled.")

        fields = dict(CREATE_FIELDS if mode == "create" else BIND_FIELDS)
        if name not in fields:
            return _resp(f"Unknown onboarding field {name!r}.")
        value = user_message.strip()
        new_draft = dict(draft)
        new_journal = dict(journal)
        new_journal["events"] = list(new_journal.get("events") or [])

        # Bind: validate the repository before inspecting it, so invalid
        # input stays in bind_details and never reaches the read-only
        # inspector.
        if (
            name == "repository"
            and mode == "bind"
            and "branch_default" not in new_draft
        ):
            try:
                validated_repo = _validate_field(name, value)
            except ValueError as exc:
                return _resp(
                    f"Invalid repository: {exc} "
                    "Please try again, or enter Cancel."
                )
            new_draft["repository"] = validated_repo
            try:
                new_draft["branch_default"] = self._inspect_repository(
                    validated_repo
                )
            except Exception as exc:
                # Inspector failure happens before the executor and cannot be
                # fixed by retrying the proposal; keep the detail stage durable
                # and ask the user to retry the input or cancel.
                return _resp(
                    f"Failed to inspect the repository default branch: {exc} "
                    "Please try again, or enter Cancel."
                )
            new_journal["events"].append("bind:default_branch")

        if not value:
            if name == "visibility" and mode == "create":
                validated = "private"
            elif name == "branch" and mode == "create":
                validated = "main"
            elif name == "branch" and mode == "bind":
                default = new_draft.get("branch_default")
                if not isinstance(default, str) or not default.strip():
                    return _resp(
                        "The default branch suggestion is unavailable. Enter "
                        "a branch name, or enter Cancel."
                    )
                validated = default
            else:
                return _resp(
                    "Please enter a value, or enter Cancel to stop onboarding."
                )
        else:
            try:
                validated = _validate_field(name, value)
            except ValueError as exc:
                return _resp(
                    f"Invalid {name.replace('_', ' ')}: {exc} "
                    "Please try again, or enter Cancel."
                )

        new_draft[_field_key(name, mode)] = validated
        new_journal["events"].append(f"field:{name}")
        remaining = [f for f in fields if _field_key(f, mode) not in new_draft]
        if remaining:
            new_draft["next"] = remaining[0]
            return self._persist(record, stage, new_draft, new_journal)
        return self._prepare(record, new_draft, new_journal)

    def _prepare(
        self,
        record: Dict[str, Any],
        draft: Dict[str, Any],
        journal: Dict[str, Any],
    ) -> Dict[str, Any]:
        preparer = self._proposal_preparer
        if preparer is None:
            return _resp(
                "The proposal preparer is not configured. Enter Cancel "
                "to stop onboarding."
            )
        try:
            proposal = preparer(self._collect(draft, draft["mode"]))
            self._validate_proposal(proposal, draft["mode"])
        except Exception as exc:
            # Proposal-preparer failure happens before the executor and
            # cannot be fixed by retrying the proposal; keep the detail stage
            # durable and ask the user to retry or cancel.
            return _resp(
                f"Failed to prepare the project proposal: {exc} "
                "Please try again, or enter Cancel."
            )
        new_journal = dict(journal)
        new_journal["events"] = list(new_journal.get("events") or [])
        new_journal["events"].append("proposal:prepared")
        new_journal["proposal"] = proposal
        cleaned = {k: v for k, v in draft.items() if k != "next"}
        return self._persist(record, "confirm", cleaned, new_journal)

    def _confirm(
        self,
        record: Dict[str, Any],
        journal: Dict[str, Any],
        user_message: str,
    ) -> Dict[str, Any]:
        proposal = journal.get("proposal")
        if not isinstance(proposal, dict):
            return _resp(
                "The stored proposal is missing. Enter Cancel to stop onboarding."
            )
        lowered = user_message.strip().casefold()
        if lowered in ("cancel", "back"):
            return self._cancel(record, prefix="Project onboarding cancelled.")
        if user_message.strip() != "confirm":
            return _resp(
                f"{self._render(proposal)}\n"
                "Enter confirm to create this project, or Cancel."
            )
        executor = self._operation_executor
        if executor is None:
            return self._block(
                record, journal, "The operation executor is not configured."
            )
        return self._run_proposal(record, journal, proposal, executor)

    def _blocked(
        self,
        record: Dict[str, Any],
        journal: Dict[str, Any],
        user_message: str,
    ) -> Dict[str, Any]:
        error = journal.get("recovery_error")
        error_text = error if isinstance(error, str) and error.strip() else ""
        lowered = user_message.strip().casefold()
        if lowered in ("cancel", "back"):
            return self._cancel(record, prefix="Project onboarding cancelled.")
        if lowered != "retry":
            return _resp(
                f"Project creation is blocked: {error_text} "
                "Enter retry to run the persisted proposal again, or Cancel."
            )
        proposal = journal.get("proposal")
        if not isinstance(proposal, dict):
            return _resp(
                f"Project creation is blocked: {error_text} "
                "The persisted proposal is missing; enter Cancel."
            )
        executor = self._operation_executor
        if executor is None:
            return _resp(
                f"Project creation is blocked: {error_text} "
                "The operation executor is not configured; enter Cancel."
            )
        return self._run_proposal(record, journal, proposal, executor)

    def _run_proposal(
        self,
        record: Dict[str, Any],
        journal: Dict[str, Any],
        proposal: Dict[str, Any],
        executor: Any,
    ) -> Dict[str, Any]:
        # Shared confirm/retry execution. The persist callback closes over a
        # mutable holder so that if the executor calls one or more checkpoint
        # journals and then raises, the CAS is attempted against the newest
        # holder record, not the pre-execution record.
        try:
            current, persist = self._make_persist(record)
            outcome = executor(current["record"], proposal, persist)
        except Exception as exc:
            return self._block(current["record"], journal, str(exc))
        return self._finish(current["record"], journal, outcome)

    def _finish(
        self,
        record: Dict[str, Any],
        journal: Dict[str, Any],
        outcome: Any,
    ) -> Dict[str, Any]:
        if not isinstance(outcome, dict) or outcome.get("status") != "created":
            detail = outcome.get("status") if isinstance(outcome, dict) else outcome
            return self._block(
                record, journal,
                f"Project creation failed: unexpected result {detail!r}.",
            )
        project = outcome.get("project")
        if (
            not isinstance(project, dict)
            or not isinstance(project.get("id"), str)
            or not project["id"].strip()
        ):
            return self._block(
                record, journal,
                "The executor did not return a valid project identity.",
            )
        try:
            cleared = self._store.set_session_startup_onboarding(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                onboarding=None,
            )
        except (SessionStartupRevisionError, SessionStartupStateError) as exc:
            return self._persist_failed(record, exc)
        return {
            "action": "handback",
            "project_id": project["id"].strip(),
            "revision": cleared["revision"],
        }

    # -- proposal -----------------------------------------------------------

    def _collect(self, draft: Dict[str, Any], mode: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "mode": mode,
            "owner": OWNER,
            "display_name": draft.get("display_name", ""),
            "repository": draft.get("repository", ""),
            "branch": draft.get(f"branch_{mode}", ""),
        }
        if mode == "create":
            payload["visibility"] = draft.get("visibility", "private")
        return payload

    @staticmethod
    def _validate_proposal(proposal: Any, mode: str) -> None:
        required = set(PROPOSAL_FIELDS)
        if mode == "create":
            required.add("visibility")
        if not isinstance(proposal, dict) or set(proposal) != required:
            raise ValueError("proposal has an invalid field set")
        for field in required:
            value = proposal[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"proposal field {field!r} must be nonblank text"
                )
        if mode == "create" and proposal["visibility"] not in ("private", "public"):
            raise ValueError("proposal visibility must be private or public")

    @staticmethod
    def _render(proposal: Dict[str, Any]) -> str:
        lines = [
            "Proposed project:",
            f"  Mode: {proposal.get('mode', '')}",
            f"  Display name: {proposal.get('display_name', '')}",
            f"  Project slug: {proposal.get('project_slug', '')}",
            f"  Repository: {proposal.get('repository', '')}",
        ]
        if proposal.get("mode") == "create":
            lines.append(f"  Visibility: {proposal.get('visibility', '')}")
        lines.extend(
            [
                f"  Integration branch: {proposal.get('integration_branch', '')}",
                f"  Canonical checkout: {proposal.get('canonical_checkout', '')}",
                f"  Controlled worktree root: "
                f"{proposal.get('controlled_worktree_root', '')}",
                f"  Board slug: {proposal.get('board_slug', '')}",
            ]
        )
        return "\n".join(lines)

    def _mode_prompt(self) -> Dict[str, Any]:
        return _resp(
            "New project onboarding:\n"
            f"1. {CREATE_MODE}\n"
            f"2. {BIND_MODE}\n"
            "Or enter Cancel to return to the project list."
        )

    # -- persistence --------------------------------------------------------

    def render(self, record: Dict[str, Any]) -> Dict[str, Any]:
        try:
            stage, draft, journal = self._state(record)
        except ValueError as exc:
            return _resp(f"Stored onboarding state is invalid: {exc}")
        if stage == "choose_mode":
            return self._mode_prompt()
        if stage in ("create_details", "bind_details"):
            mode = draft.get("mode")
            next_field = draft.get("next")
            if (
                not isinstance(mode, str)
                or mode not in ("create", "bind")
                or not isinstance(next_field, str)
                or not next_field
            ):
                return _resp("Onboarding draft is incomplete.")
            return _resp(
                f"{_field_prompt(next_field, mode, draft)}\n"
                "Enter Cancel to stop onboarding."
            )
        if stage == "confirm":
            proposal = (journal or {}).get("proposal")
            if not isinstance(proposal, dict):
                return _resp("The stored proposal is missing.")
            return _resp(
                f"{self._render(proposal)}\n"
                "Enter confirm to create this project, or Cancel."
            )
        if stage == "blocked_recoverable":
            error = (journal or {}).get("recovery_error")
            error_text = error if isinstance(error, str) and error.strip() else ""
            return _resp(
                f"Project creation is blocked: {error_text}\n"
                "Enter retry to run the persisted proposal again, or Cancel."
            )
        return _resp(f"Unexpected onboarding stage: {stage!r}")

    def _persist(
        self,
        record: Dict[str, Any],
        stage: str,
        draft: Dict[str, Any],
        journal: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            record = self._store.set_session_startup_onboarding(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                onboarding={"stage": stage, "draft": draft, "journal": journal},
            )
        except (SessionStartupRevisionError, SessionStartupStateError) as exc:
            return self._persist_failed(record, exc)
        return self.render(record)

    def _cancel(
        self, record: Dict[str, Any], prefix: str = ""
    ) -> Dict[str, Any]:
        # Clear the onboarding substate, then hand a narrow action back to the
        # controller. The controller owns project loading and menu rendering,
        # so it routes this through its normal project menu.
        try:
            self._store.set_session_startup_onboarding(
                session_id=record["session_id"],
                expected_revision=record["revision"],
                onboarding=None,
            )
        except (SessionStartupRevisionError, SessionStartupStateError) as exc:
            return self._persist_failed(record, exc)
        return {"action": "return_to_projects", "prefix": prefix}

    def _block(
        self, record: Dict[str, Any], journal: Dict[str, Any], error: Any
    ) -> Dict[str, Any]:
        error_text = str(error).strip() or "Project creation failed with an unknown error."
        draft, stored = self._draft_and_journal(record)
        new_journal = dict(stored)
        new_journal["events"] = list(new_journal.get("events") or [])
        new_journal["events"].append("blocked")
        new_journal["recovery_error"] = error_text
        return self._persist(record, "blocked_recoverable", draft, new_journal)

    def _make_persist(
        self, record: Dict[str, Any]
    ) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        # The persist callback closes over a mutable holder so multiple
        # sequential journal checkpoints each CAS against the newest returned
        # revision instead of the pre-execution record.
        current = {"record": record}

        def persist(onboarding: Dict[str, Any]) -> Dict[str, Any]:
            if not isinstance(onboarding, dict):
                raise ValueError("onboarding object must be a mapping")
            try:
                current["record"] = self._store.set_session_startup_onboarding(
                    session_id=current["record"]["session_id"],
                    expected_revision=current["record"]["revision"],
                    onboarding=onboarding,
                )
            except (SessionStartupRevisionError, SessionStartupStateError) as exc:
                raise ValueError(
                    f"onboarding journal persistence failed: {exc}"
                ) from exc
            return current["record"]

        return current, persist

    def _inspect_repository(self, repository: str) -> str:
        inspector = self._repo_inspector
        if inspector is None:
            raise ValueError(
                "the repository inspector is not configured; bind onboarding "
                "cannot obtain the remote default branch"
            )
        branch = inspector(repository)
        if not isinstance(branch, str) or not branch.strip():
            raise ValueError("the repository inspector returned a blank branch")
        # Validate the returned branch through the same validator as typed
        # branches: only invalid patterns are rejected, not multi-component
        # names.
        return _validate_field("branch", branch)

    @staticmethod
    def _state(record: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        raw = record.get("onboarding_json")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("no onboarding state is recorded")
        try:
            onboarding = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("onboarding state is not valid JSON") from exc
        if not isinstance(onboarding, dict):
            raise ValueError("onboarding state is not an object")
        stage = onboarding.get("stage")
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("onboarding state is missing its stage")
        draft = onboarding.get("draft")
        journal = onboarding.get("journal")
        if not isinstance(draft, dict) or not isinstance(journal, dict):
            raise ValueError("onboarding state is missing its draft or journal")
        return stage.strip(), draft, journal

    @staticmethod
    def _draft_and_journal(
        record: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return OnboardingCoordinator._state(record)[1:]

    @staticmethod
    def _persist_failed(record: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
        return _resp(
            f"Failed to persist project onboarding: {exc}. "
            "Please re-run Session Startup."
        )
