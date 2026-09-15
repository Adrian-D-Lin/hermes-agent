"""Candidate tests for S2B Session Startup project onboarding.

CANDIDATE MATERIAL — UNTRUSTED UNTIL INDEPENDENTLY RATIFIED.

Exercises the lean onboarding coordinator and its controller integration:
menu position, create defaults, deliberate public selection, bind default
suggestion, proposal rendering/confirmation, cancel from each stage,
executor success hand-back, failure persistence, retry with the persisted
journal, stale CAS, and unchanged existing-project selection.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def adrian_plugin_modules(kanban_home: Path) -> dict[str, ModuleType]:
    import yaml
    from hermes_cli.plugins import PluginManager

    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["adrian-kanban"]}}),
        encoding="utf-8",
    )
    manager = PluginManager(scope_key=str(kanban_home.resolve()))
    manager.discover_and_load()
    loaded = manager._plugins["adrian-kanban"]
    assert loaded.enabled is True, loaded.error
    assert loaded.module is not None
    package = loaded.module
    modules = {
        "schema": importlib.import_module(f"{package.__name__}.schema"),
        "store": importlib.import_module(f"{package.__name__}.store"),
        "validation": importlib.import_module(f"{package.__name__}.validation"),
        "session_startup": importlib.import_module(
            f"{package.__name__}.session_startup"
        ),
        "session_startup_onboarding": importlib.import_module(
            f"{package.__name__}.session_startup_onboarding"
        ),
    }
    return modules


def _db_path(kanban_home: Path, name: str) -> str:
    return str((kanban_home / f"{name}.db").resolve())


PROJECTS = [
    {"id": "project-1", "name": "Project One",
     "board_slug": "board-1", "primary_path": "/srv/projects/project-1"},
    {"id": "project-2", "name": "Project Two",
     "board_slug": "board-2", "primary_path": "/srv/projects/project-2"},
]
INITIATIVES = [
    {"initiative_id": "initiative-1", "title": "Initiative One",
     "current_phase": "DEV", "current_segment_id": "DEV3"},
]


def _proposal_preparer(payload: dict) -> dict:
    mode = payload["mode"]
    proposal = {
        "mode": mode,
        "display_name": payload["display_name"],
        "project_slug": payload["display_name"].casefold().replace(" ", "-"),
        "repository": f"{payload['owner']}/{payload['repository']}",
        "integration_branch": payload["branch"],
        "canonical_checkout": f"/srv/worktrees/{payload['owner']}/{payload['repository']}",
        "controlled_worktree_root": "/home/progenitor/AI-worktrees",
        "board_slug": f"board-{payload['repository']}",
    }
    if mode == "create":
        proposal["visibility"] = payload["visibility"]
    return proposal


class _FakeExecutor:
    def __init__(self, outcome=None, error=None, calls=None, persist_log=None):
        self.outcome = outcome
        self.error = error
        self.calls = calls if calls is not None else []
        self.persist_log = persist_log if persist_log is not None else []

    def __call__(self, record, proposal, persist):
        self.calls.append({"record": record, "proposal": proposal})
        if self.error is not None:
            raise self.error
        return self.outcome


def _coordinator(modules, store, projects=None, *, executor=None,
                 inspector=None, preparer=_proposal_preparer):
    projects = projects if projects is not None else PROJECTS
    return modules["session_startup_onboarding"].OnboardingCoordinator(
        store=store,
        repo_inspector=inspector,
        proposal_preparer=preparer,
        operation_executor=executor,
    )


def _controller(modules, store, *, projects=None, coordinator=None):
    projects = projects if projects is not None else PROJECTS
    return modules["session_startup"].SessionStartupController(
        store=store,
        project_loader=lambda: projects,
        initiative_loader=lambda _board: INITIATIVES,
        anchor_resolver=lambda *args: {},
        anchor_revalidator=lambda *args: {"classification": "unchanged", "reasons": []},
        anchor_replacer=lambda *args: {},
        initiative_creation_coordinator=type("C", (), {"derive": lambda s: None, "execute": lambda s: None, "cancel": lambda s, p, d, r: None})(),
        onboarding_coordinator=coordinator,
    )


def _start_controller_session(modules, kanban_home, name, *, coordinator=None, projects=None):
    store = modules["store"].AdmittedStore(
        database_path=_db_path(kanban_home, name)
    )
    store.__enter__()
    controller = _controller(
        modules, store, coordinator=coordinator, projects=projects
    )
    return store, controller


def _record(store, session_id):
    return store.read_session_startup(session_id=session_id)


# -- menu position ---------------------------------------------------------


def test_menu_appends_create_entry_with_existing_projects(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "menu-with")) as store:
        controller = _controller(adrian_plugin_modules, store)
        result = controller.handle("session-1", "hello")
        assert result["action"] == "respond"
        text = result["response"]
        assert "1. Project One (project-1)" in text
        assert "2. Project Two (project-2)" in text
        assert "3. Create a new project" in text
        # Create entry is the final line.
        assert text.strip().splitlines()[-1] == "3. Create a new project"


def test_menu_appends_create_entry_with_no_projects(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "menu-empty")) as store:
        controller = _controller(
            adrian_plugin_modules, store, projects=[]
        )
        result = controller.handle("session-1", "hello")
        assert result["action"] == "respond"
        text = result["response"]
        # No fail-closed behaviour: creation remains available.
        assert "No active projects" not in text
        assert "1. Create a new project" in text
        assert text.strip().splitlines()[-1] == "1. Create a new project"


def test_existing_project_selection_unchanged_with_projects(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "unchanged-selection")
    ) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        # Selecting a real project (by number and by name) still anchors the
        # record at initiative selection — onboarding is not triggered.
        result = controller.handle("session-1", "2")
        assert result["action"] == "respond"
        assert "Active initiatives for Project Two" in result["response"]
        record = _record(store, "session-1")
        assert record["state"] == "awaiting_initiative_selection"
        assert record["onboarding_json"] is None
        assert record["selected_project_id"] == "project-2"


def test_existing_project_selection_unchanged_without_coordinator(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "unchanged-no-coordinator")
    ) as store:
        controller = _controller(adrian_plugin_modules, store)
        controller.handle("session-1", "hello")
        result = controller.handle("session-1", "Project One")
        assert result["action"] == "respond"
        assert "Active initiatives for Project One" in result["response"]
        record = _record(store, "session-1")
        assert record["state"] == "awaiting_initiative_selection"
        assert record["onboarding_json"] is None


# -- create flow: defaults and deliberate public ---------------------------


def test_create_flow_defaults_private_and_main(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "create-defaults")) as store:
        executor = _FakeExecutor(
            outcome={"status": "created", "project": {"id": "project-new"}}
        )
        coordinator = _coordinator(adrian_plugin_modules, store, executor=executor)
        controller = _controller(
            adrian_plugin_modules,
            store,
            coordinator=coordinator,
            projects=[
                {"id": "project-1", "name": "Project One",
                 "board_slug": "board-1", "primary_path": "/srv/projects/project-1"},
                {"id": "project-2", "name": "Project Two",
                 "board_slug": "board-2", "primary_path": "/srv/projects/project-2"},
                {"id": "project-new", "name": "My New Project",
                 "board_slug": "board-new-repo",
                 "primary_path": "/srv/projects/project-new"},
            ],
        )
        controller.handle("session-1", "hello")
        result = controller.handle("session-1", "Create a new project")
        assert "create new GitHub repository" in result["response"]

        result = controller.handle("session-1", "1")
        assert "Enter the project display name:" in result["response"]
        result = controller.handle("session-1", "My New Project")
        assert "Enter the GitHub repository name:" in result["response"]
        result = controller.handle("session-1", "new-repo")
        assert "private (default)" in result["response"]
        result = controller.handle("session-1", "")  # blank accepts private
        assert "Integration branch (default: main)" in result["response"]
        result = controller.handle("session-1", "")  # blank accepts main
        # Proposal rendered with private + main.
        assert "Visibility: private" in result["response"]
        assert "Integration branch: main" in result["response"]
        assert "Adrian-D-Lin/new-repo" in result["response"]
        assert "confirm" in result["response"].lower()

        # A non-literal selection does not execute.
        result = controller.handle("session-1", "yes")
        assert "Enter confirm" in result["response"]

        result = controller.handle("session-1", "confirm")
        record = _record(store, "session-1")
        assert record["state"] == "awaiting_initiative_selection"
        assert record["selected_project_id"] == "project-new"
        assert record["onboarding_json"] is None
        assert "Active initiatives" in result["response"]
        # The proposal handed to the executor carried the defaults.
        proposal = executor.calls[0]["proposal"]
        assert proposal["visibility"] == "private"
        assert proposal["integration_branch"] == "main"
        assert proposal["mode"] == "create"


def test_create_public_requires_literal_selection(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "create-public")
    ) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Public Project")
        controller.handle("session-1", "public-repo")
        # Deliberate public selection.
        result = controller.handle("session-1", "public")
        assert "Integration branch" in result["response"]
        controller.handle("session-1", "")
        # Proposal must visibly show public.
        result = controller.handle("session-1", "no")
        assert "Visibility: public" in result["response"]

        record = _record(store, "session-1")
        draft = store.read_session_startup(session_id="session-1")
        import json as _json
        onboarding = _json.loads(draft["onboarding_json"])
        assert onboarding["stage"] == "confirm"
        assert onboarding["journal"]["proposal"]["visibility"] == "public"


def test_create_invalid_visibility_rejected(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "create-bad-visibility")
    ) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        result = controller.handle("session-1", "open")
        assert "Invalid visibility" in result["response"]
        record = _record(store, "session-1")
        import json as _json
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["draft"]["next"] == "visibility"


@pytest.mark.parametrize(
    "bad_branch",
    [
        "a/b/lockfile.lock",  # component ends in .lock
        "@",  # bare at
        "@{main}",  # at-open-brace
        "main.",  # trailing dot
        "a:b",  # invalid char
        "a\\b",  # backslash
        "a/b/",  # trailing slash
    ],
)
def test_create_invalid_branch_rejected_stays_in_field(
    adrian_plugin_modules, kanban_home, bad_branch
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "create-bad-branch")
    ) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        controller.handle("session-1", "")  # private default
        result = controller.handle("session-1", bad_branch)
        assert "Invalid branch" in result["response"]
        record = _record(store, "session-1")
        import json as _json
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["draft"]["next"] == "branch"


# -- bind flow: default branch suggestion ----------------------------------


def test_bind_flow_uses_inspector_default_suggestion(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "bind-default")
    ) as store:
        inspections = []

        def inspector(repo):
            inspections.append(repo)
            return "develop"

        coordinator = _coordinator(
            adrian_plugin_modules, store, inspector=inspector
        )
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        result = controller.handle("session-1", "3")
        assert "bind existing GitHub repository" in result["response"]
        result = controller.handle("session-1", "2")
        assert "Enter the GitHub repository name to bind:" in result["response"]
        result = controller.handle("session-1", "existing-repo")
        assert "Enter the project display name:" in result["response"]
        result = controller.handle("session-1", "Existing Project")
        # Branch prompt now shows the inspector's remote default.
        assert "Integration branch (default: develop)" in result["response"]
        assert inspections == ["existing-repo"]
        # Blank accepts the suggestion as the chosen branch.
        result = controller.handle("session-1", "")
        assert "Integration branch: develop" in result["response"]
        assert "Adrian-D-Lin/existing-repo" in result["response"]
        assert "Visibility:" not in result["response"]
        import json as _json
        record = _record(store, "session-1")
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["stage"] == "confirm"
        assert onboarding["draft"]["branch_bind"] == "develop"
        assert onboarding["draft"]["branch_default"] == "develop"
        assert onboarding["journal"]["proposal"]["integration_branch"] == "develop"


def test_bind_flow_user_overrides_suggested_branch(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "bind-override")
    ) as store:
        coordinator = _coordinator(
            adrian_plugin_modules,
            store,
            inspector=lambda repo: "develop",
        )
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "2")
        controller.handle("session-1", "existing-repo")
        controller.handle("session-1", "Existing")
        result = controller.handle("session-1", "release")
        # The user's entry is authoritative, not the suggestion.
        assert "Integration branch: release" in result["response"]
        import json as _json
        record = _record(store, "session-1")
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["draft"]["branch_bind"] == "release"
        assert onboarding["journal"]["proposal"]["integration_branch"] == "release"


# -- proposal rendering / confirmation --------------------------------------


def test_confirm_requires_literal_confirm(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "confirm-literal")
    ) as store:
        calls = []
        executor = _FakeExecutor(
            outcome={"status": "created", "project": {"id": "project-x"}},
            calls=calls,
        )
        coordinator = _coordinator(adrian_plugin_modules, store, executor=executor)
        controller = _controller(
            adrian_plugin_modules,
            store,
            coordinator=coordinator,
            projects=[
                {"id": "project-1", "name": "Project One",
                 "board_slug": "board-1", "primary_path": "/srv/projects/project-1"},
                {"id": "project-2", "name": "Project Two",
                 "board_slug": "board-2", "primary_path": "/srv/projects/project-2"},
                {"id": "project-x", "name": "Proj X",
                 "board_slug": "board-x", "primary_path": "/srv/projects/project-x"},
            ],
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "Create a new project")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        for attempt in ("Confirm", "CONFIRM", "confirm please", "y"):
            result = controller.handle("session-1", attempt)
            assert result["action"] == "respond"
            assert "Enter confirm" in result["response"]
        assert not calls, "executor must not run without literal confirm"
        assert _record(store, "session-1")["onboarding_json"] is not None

        result = controller.handle("session-1", "confirm")
        assert len(calls) == 1
        assert _record(store, "session-1")["state"] == "awaiting_initiative_selection"


def test_proposal_persisted_before_confirm(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "proposal-persisted")
    ) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        import json as _json
        record = _record(store, "session-1")
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["stage"] == "confirm"
        proposal = onboarding["journal"]["proposal"]
        for field in (
            "mode", "display_name", "project_slug", "repository",
            "integration_branch", "canonical_checkout",
            "controlled_worktree_root", "board_slug", "visibility",
        ):
            assert proposal[field], field


# -- cancel from each stage --------------------------------------------------


@pytest.mark.parametrize(
    "advance,stage",
    [
        (["3"], "choose_mode"),
        (["3", "1", "Proj"], "create_details"),
        (["3", "1", "Proj", "repo", "", ""], "confirm"),
    ],
)
def test_cancel_from_each_stage_clears_and_returns_menu(
    adrian_plugin_modules, kanban_home, advance, stage
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, f"cancel-{stage}")
    ) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        for message in advance:
            controller.handle("session-1", message)
        result = controller.handle("session-1", "cancel")
        assert result["action"] == "respond"
        assert "cancelled" in result["response"].lower()
        assert "Select a project" in result["response"]
        assert "Create a new project" in result["response"]
        record = _record(store, "session-1")
        assert record["onboarding_json"] is None
        assert record["state"] == "awaiting_project_selection"
        # Normal selection still works afterwards.
        result = controller.handle("session-1", "1")
        assert "Active initiatives for Project One" in result["response"]


def test_cancel_from_blocked_stage(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "cancel-blocked")
    ) as store:
        executor = _FakeExecutor(error=RuntimeError("boom"))
        coordinator = _coordinator(adrian_plugin_modules, store, executor=executor)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        controller.handle("session-1", "confirm")  # -> blocked
        result = controller.handle("session-1", "cancel")
        assert "cancelled" in result["response"].lower()
        assert "Create a new project" in result["response"]
        record = _record(store, "session-1")
        assert record["onboarding_json"] is None
        assert record["state"] == "awaiting_project_selection"


# -- executor success hand-back ----------------------------------------------


def test_executor_success_handback_without_restart(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "handback")
    ) as store:
        calls = []
        executor = _FakeExecutor(
            outcome={"status": "created", "project": {"id": "project-new"}},
            calls=calls,
        )
        projects = PROJECTS + [
            {"id": "project-new", "name": "My New Project",
             "board_slug": "board-new",
             "primary_path": "/srv/projects/project-new"}
        ]
        coordinator = _coordinator(
            adrian_plugin_modules, store, projects=projects, executor=executor
        )
        controller = _controller(
            adrian_plugin_modules, store,
            projects=projects, coordinator=coordinator,
        )
        controller.handle("session-1", "hello")
        result = controller.handle("session-1", "4")
        assert "create new GitHub repository" in result["response"]
        controller.handle("session-1", "1")
        controller.handle("session-1", "My New Project")
        controller.handle("session-1", "new-repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        result = controller.handle("session-1", "confirm")
        # Initiative picker for the new project, immediately, no restart.
        assert "Active initiatives for My New Project" in result["response"]
        record = _record(store, "session-1")
        assert record["state"] == "awaiting_initiative_selection"
        assert record["selected_project_id"] == "project-new"
        assert record["onboarding_json"] is None
        assert len(calls) == 1


def test_executor_success_persist_callback_returns_new_revision(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "handback-persist")
    ) as store:
        persist_log = []

        def executor(record, proposal, persist):
            onboarding = {
                "stage": "confirm",
                "draft": {"mode": "create"},
                "journal": {
                    "events": ["side-effect:1"],
                    "proposal": proposal,
                },
            }
            fresh = persist(onboarding)
            persist_log.append(fresh)
            assert fresh["revision"] > record["revision"]
            return {"status": "created", "project": {"id": "project-new"}}

        projects = PROJECTS + [
            {"id": "project-new", "name": "New", "board_slug": "b",
             "primary_path": "/srv/projects/project-new"}
        ]
        coordinator = _coordinator(
            adrian_plugin_modules, store, projects=projects,
            executor=executor,
        )
        controller = _controller(
            adrian_plugin_modules, store,
            projects=projects, coordinator=coordinator,
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "4")
        controller.handle("session-1", "1")
        controller.handle("session-1", "New")
        controller.handle("session-1", "new-repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        result = controller.handle("session-1", "confirm")
        assert "Active initiatives for New" in result["response"]
        assert len(persist_log) == 1


# -- executor failure: persistence and retry --------------------------------


def test_executor_failure_persists_blocked_recoverable(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "failure")
    ) as store:
        calls = []
        executor = _FakeExecutor(error=RuntimeError("git remote failed"),
                                 calls=calls)
        coordinator = _coordinator(adrian_plugin_modules, store, executor=executor)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        result = controller.handle("session-1", "confirm")
        assert result["action"] == "respond"
        assert "blocked" in result["response"].lower()
        assert "git remote failed" in result["response"]
        import json as _json
        record = _record(store, "session-1")
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["stage"] == "blocked_recoverable"
        assert onboarding["journal"]["recovery_error"] == "git remote failed"
        assert onboarding["journal"]["proposal"], "proposal retained"
        assert onboarding["draft"], "draft retained"
        # Retry is the only offered action besides cancel; other input
        # re-presents the blocked message.
        result = controller.handle("session-1", "do it")
        assert "retry" in result["response"].lower()
        assert "git remote failed" in result["response"]
        assert len(calls) == 1


def test_retry_uses_persisted_journal_then_succeeds(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "retry")
    ) as store:
        attempts = {"n": 0}
        proposals_seen = []

        def executor(record, proposal, persist):
            attempts["n"] += 1
            proposals_seen.append(proposal)
            if attempts["n"] == 1:
                raise RuntimeError("first attempt failed")
            return {"status": "created", "project": {"id": "project-retry"}}

        projects = PROJECTS + [
            {"id": "project-retry", "name": "Retry", "board_slug": "b",
             "primary_path": "/srv/projects/project-retry"}
        ]
        coordinator = _coordinator(
            adrian_plugin_modules, store, projects=projects, executor=executor
        )
        controller = _controller(
            adrian_plugin_modules, store,
            projects=projects, coordinator=coordinator,
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "4")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Retry")
        controller.handle("session-1", "retry-repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        result = controller.handle("session-1", "confirm")
        assert "first attempt failed" in result["response"]
        # Retry re-runs the persisted proposal (identical content).
        result = controller.handle("session-1", "retry")
        assert "Active initiatives for Retry" in result["response"]
        assert attempts["n"] == 2
        assert proposals_seen[0] == proposals_seen[1]
        record = _record(store, "session-1")
        assert record["state"] == "awaiting_initiative_selection"
        assert record["onboarding_json"] is None


def test_retry_failure_stays_blocked(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "retry-keeps")
    ) as store:
        calls = []
        executor = _FakeExecutor(error=RuntimeError("still failing"),
                                 calls=calls)
        coordinator = _coordinator(adrian_plugin_modules, store, executor=executor)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        controller.handle("session-1", "confirm")
        result = controller.handle("session-1", "retry")
        assert "still failing" in result["response"]
        assert len(calls) == 2
        import json as _json
        record = _record(store, "session-1")
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["stage"] == "blocked_recoverable"
        assert onboarding["journal"]["recovery_error"] == "still failing"
        assert onboarding["journal"]["proposal"], "journal retained for retry"


# -- stale CAS ---------------------------------------------------------------


def test_stale_cas_fail_closed_preserves_state(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "stale-cas")
    ) as store:
        calls = []
        executor = _FakeExecutor(
            outcome={"status": "created", "project": {"id": "project-new"}},
            calls=calls,
        )

        class StaleStore:
            """Wraps the store; after the first onboarding write, subsequent
            writes carry an out-of-date revision."""

            def __init__(self, inner, after):
                self._inner = inner
                self._count = 0
                self._after = after

            def __getattr__(self, item):
                return getattr(self._inner, item)

            def set_session_startup_onboarding(self, **kwargs):
                self._count += 1
                if self._count > self._after:
                    kwargs["expected_revision"] = 0  # stale
                return self._inner.set_session_startup_onboarding(**kwargs)

        wrapped = StaleStore(store, after=2)
        coordinator = _coordinator(adrian_plugin_modules, wrapped,
                                   executor=executor)
        controller = _controller(adrian_plugin_modules, store,
                                 coordinator=coordinator)
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        # This write (3rd) uses a stale expected revision.
        result = controller.handle("session-1", "Proj")
        assert "Failed to persist project onboarding" in result["response"]
        import json as _json
        record = _record(store, "session-1")
        onboarding = _json.loads(record["onboarding_json"])
        # The state was preserved at the last good revision.
        assert onboarding["draft"]["next"] == "display_name"
        assert len(calls) == 0


# -- coordinator-level: persistence after every response ---------------------


def test_state_persisted_after_every_response(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, "every-response")
    ) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        record = store.create_or_read_session_startup(
            session_id="session-1",
            opening_prompt="hello",
            protocol_version="v0.29",
        )
        result = coordinator.start_onboarding(record)
        assert "create new GitHub repository" in result["response"]
        assert _record(store, "session-1")["onboarding_json"] is not None
        record = _record(store, "session-1")
        result = coordinator.handle(record, "1")
        assert "Enter the project display name:" in result["response"]
        record = _record(store, "session-1")
        import json as _json
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["stage"] == "create_details"
        assert onboarding["draft"]["next"] == "display_name"


# -- finding 1: hand-back must not fabricate a project ----------------------


class _FakeExecutorWithProjectList(_FakeExecutor):
    """Executor that records the reloaded project list it returns in handback."""

    def __init__(self, outcome=None, error=None, reloaded=None, persist_log=None):
        super().__init__(outcome=outcome, error=error)
        self.reloaded = reloaded
        self.persist_log = persist_log if persist_log is not None else []

    def __call__(self, record, proposal, persist):
        self.calls.append({"record": record, "proposal": proposal})
        if self.error is not None:
            raise self.error
        # The controller reloads projects after handback; the create-default
        # success path must supply the created project in that list.
        if self.reloaded is not None:
            return {"status": "created", "project": self.reloaded[0]}
        return self.outcome


def test_handback_fabricates_no_project_when_reload_is_empty(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "handback-empty")) as store:
        executor = _FakeExecutorWithProjectList(
            outcome={"status": "created", "project": {"id": "project-new"}},
            reloaded=None,
        )
        coordinator = _coordinator(adrian_plugin_modules, store, executor=executor)
        controller = _controller(
            adrian_plugin_modules,
            store,
            coordinator=coordinator,
            projects=[
                {"id": "project-1", "name": "Project One",
                 "board_slug": "board-1", "primary_path": "/srv/projects/project-1"},
                {"id": "project-2", "name": "Project Two",
                 "board_slug": "board-2", "primary_path": "/srv/projects/project-2"},
            ],
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "My Project")
        controller.handle("session-1", "my-repo")
        controller.handle("session-1", "")  # private
        controller.handle("session-1", "")  # main
        result = controller.handle("session-1", "confirm")
        # The executor-returned identity is not in the reloaded project list,
        # so the controller must fail closed rather than fabricate a project.
        assert result["action"] == "respond"
        assert "handback" not in result
        assert "could not be confirmed" in result["response"].lower()


def test_missing_project_handback_is_reported(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "handback-missing")) as store:
        # Executor returns a created status but a project with no usable id.
        executor = _FakeExecutor(
            outcome={"status": "created", "project": {"id": ""}}
        )
        coordinator = _coordinator(adrian_plugin_modules, store, executor=executor)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "My Project")
        controller.handle("session-1", "my-repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        result = controller.handle("session-1", "confirm")
        assert result["action"] == "respond"
        assert "handback" not in result
        assert "project identity" in result["response"].lower()


def test_create_default_success_supplies_created_project(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    created = {
        "id": "project-new",
        "name": "Project New",
        "board_slug": "board-new",
        "primary_path": "/srv/projects/project-new",
    }
    with Store(database_path=_db_path(kanban_home, "handback-ok")) as store:
        executor = _FakeExecutorWithProjectList(
            outcome={"status": "created", "project": created},
            reloaded=[created],
        )
        coordinator = _coordinator(adrian_plugin_modules, store, executor=executor)
        controller = _controller(
            adrian_plugin_modules,
            store,
            coordinator=coordinator,
            projects=[created],
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "My Project")
        controller.handle("session-1", "my-repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        result = controller.handle("session-1", "confirm")
        # A successful executor handback is routed through the controller's
        # normal initiative-picker response, never a raw "handback" action.
        assert result["action"] == "respond"
        assert "Active initiatives for Project New" in result["response"]
        record = _record(store, "session-1")
        assert record["state"] == "awaiting_initiative_selection"
        assert record["selected_project_id"] == "project-new"


# -- finding 2: render the proposal repository once ------------------------


def test_render_shows_repository_once(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "render-once")) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "My Project")
        controller.handle("session-1", "my-repo")
        controller.handle("session-1", "")  # private
        result = controller.handle("session-1", "no")  # re-render the proposal
        rendered = result["response"]
        # The preparer returns the full repository; the coordinator must not
        # prefix the owner again, so the doubled owner must not appear.
        assert "Adrian-D-Lin/Adrian-D-Lin/my-repo" not in rendered
        # The full proposal repository appears exactly once as the repository line.
        assert rendered.count("Repository: Adrian-D-Lin/my-repo") == 1
        # A full rendered line is asserted verbatim.
        assert "  Repository: Adrian-D-Lin/my-repo" in rendered


# -- finding 3: text mode selection is case-insensitive --------------------


@pytest.mark.parametrize("selection", ["Create New GitHub Repository", "create new github repository", "CREATE NEW GITHUB REPOSITORY"])
def test_text_create_selection_is_case_insensitive(
    adrian_plugin_modules, kanban_home, selection
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "text-create")) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")  # select create mode
        result = controller.handle("session-1", selection)
        assert "Enter the project display name:" in result["response"]
        # The display label is unchanged (mixed-case "GitHub").
        assert "Create new GitHub repository" in result["response"] or \
            "Enter the project display name:" in result["response"]


@pytest.mark.parametrize("selection", ["Bind Existing GitHub Repository", "bind existing github repository"])
def test_text_bind_selection_is_case_insensitive(
    adrian_plugin_modules, kanban_home, selection
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "text-bind")) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")  # start onboarding (choose_mode)
        result = controller.handle("session-1", selection)
        assert "Enter the GitHub repository name to bind:" in result["response"]


# -- finding 4: visibility must be the literal "public" --------------------


@pytest.mark.parametrize("bad", ["Public", "PUBLIC", "publi"])
def test_public_variants_rejected(adrian_plugin_modules, kanban_home, bad):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, f"vis-{abs(hash(bad)) % 100000}")
    ) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        result = controller.handle("session-1", bad)
        assert "Invalid visibility" in result["response"]
        record = _record(store, "session-1")
        import json as _json
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["draft"]["next"] == "visibility"


def test_blank_visibility_stays_private(adrian_plugin_modules, kanban_home):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "vis-blank")) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        controller.handle("session-1", "")  # blank -> private
        record = _record(store, "session-1")
        import json as _json
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["draft"]["visibility"] == "private"


# -- finding 5: git-ref validation rejects .., slashes, doubled slash ------


@pytest.mark.parametrize(
    "bad_ref",
    ["..", "../x", "x/", "/x", "a//b", ".", "feature//x"],
)
def test_git_ref_rejects_bad_components(adrian_plugin_modules, kanban_home, bad_ref):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, f"ref-{abs(hash(bad_ref)) % 100000}")
    ) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        controller.handle("session-1", "")  # blank -> private
        result = controller.handle("session-1", bad_ref)
        assert "Invalid branch" in result["response"]
        record = _record(store, "session-1")
        import json as _json
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["draft"]["next"] == "branch"


@pytest.mark.parametrize(
    "good_ref",
    ["main", "feature/x", "release-1.2", "hotfix_42", "a/b/c"],
)
def test_git_ref_accepts_good_components(adrian_plugin_modules, kanban_home, good_ref):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(
        database_path=_db_path(kanban_home, f"refgood-{abs(hash(good_ref)) % 100000}")
    ) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "Proj")
        controller.handle("session-1", "repo")
        controller.handle("session-1", "")  # blank -> private
        result = controller.handle("session-1", good_ref)
        assert "Enter confirm" in result["response"]


# -- finding 6: validate bind repository before inspector ------------------


def test_bind_invalid_repository_skips_inspector(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    inspections = []

    def inspector(repo):
        inspections.append(repo)
        return "develop"

    with Store(database_path=_db_path(kanban_home, "bind-invalid-repo")) as store:
        coordinator = _coordinator(
            adrian_plugin_modules, store, inspector=inspector
        )
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "2")
        # Invalid repository (space is not allowed).
        result = controller.handle("session-1", "bad repo")
        assert "Invalid repository" in result["response"]
        # The inspector must not have been called.
        assert inspections == []
        record = _record(store, "session-1")
        import json as _json
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["stage"] == "bind_details"
        assert onboarding["draft"]["next"] == "repository"


# -- finding 7: checkpoint-then-exception uses the newest holder record ----


def test_checkpoint_then_exception_uses_newest_holder(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "checkpoint-exc")) as store:
        persist_log = []

        _original_set = store.set_session_startup_onboarding

        def set_with_log(*args, **kwargs):
            onboarding = kwargs.get("onboarding")
            if onboarding is not None:
                persist_log.append(dict(onboarding))
            return _original_set(*args, **kwargs)

        store.set_session_startup_onboarding = set_with_log

        def executor(record, proposal, persist):
            # A schema-valid checkpoint before the failure: the CAS must be
            # attempted against the newest holder record, not the old one.
            # The persisted checkpoint is a normal ``confirm`` stage carrying
            # the current proposal in the draft and an events list in the
            # journal; the recovery_error is only added by the controller on
            # the subsequent block.
            persist(
                {
                    "stage": "confirm",
                    "draft": dict(proposal),
                    "journal": {"events": ["executor.checkpoint"]},
                }
            )
            raise RuntimeError("git commit failed")

        coordinator = _coordinator(adrian_plugin_modules, store, executor=executor)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "My Project")
        controller.handle("session-1", "my-repo")
        controller.handle("session-1", "")
        controller.handle("session-1", "")
        result = controller.handle("session-1", "confirm")
        assert result["action"] == "respond"
        # State is preserved as blocked with the recovery error.
        record = _record(store, "session-1")
        import json as _json
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["stage"] == "blocked_recoverable"
        assert onboarding["journal"]["recovery_error"] == "git commit failed"
        # A checkpoint was recorded before the exception.
        assert len(persist_log) >= 1


# -- finding 8: pre-executor failures do not enter blocked_recoverable -----


def test_inspector_failure_returns_actionable_retry(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "inspector-fail")) as store:
        def inspector(repo):
            raise RuntimeError("git remote failed")

        coordinator = _coordinator(
            adrian_plugin_modules, store, inspector=inspector
        )
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "2")
        controller.handle("session-1", "my-repo")
        result = controller.handle("session-1", "my-branch")
        # The failure occurred before the executor: it must not enter the
        # blocked_recoverable stage (which could only offer an impossible
        # executor retry). The detail stage stays durable.
        assert result["action"] == "respond"
        assert "git remote failed" in result["response"]
        record = _record(store, "session-1")
        import json as _json
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["stage"] == "bind_details"


def test_proposal_preparer_failure_returns_actionable_retry(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore

    def failing_preparer(payload):
        raise RuntimeError("proposal failed")

    with Store(database_path=_db_path(kanban_home, "preparer-fail")) as store:
        coordinator = _coordinator(
            adrian_plugin_modules, store, preparer=failing_preparer
        )
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "My Project")
        controller.handle("session-1", "my-repo")
        controller.handle("session-1", "")  # private
        result = controller.handle("session-1", "")  # main -> prepare
        assert result["action"] == "respond"
        assert "proposal failed" in result["response"]
        record = _record(store, "session-1")
        import json as _json
        onboarding = _json.loads(record["onboarding_json"])
        assert onboarding["stage"] == "create_details"


# -- finding 9: blank message fails closed on a brand-new record -----------


def test_blank_message_fails_closed_on_new_record(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "blank-new")) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        # A brand-new record with a blank message must fail closed without
        # creating a startup record.
        result = controller.handle("session-1", "")
        assert result["action"] == "respond"
        record = _record(store, "session-1")
        assert record is None or record.get("onboarding_json") is None


def test_blank_message_allowed_when_onboarding_active(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "blank-active")) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "hello")
        controller.handle("session-1", "3")
        controller.handle("session-1", "1")
        controller.handle("session-1", "My Project")
        controller.handle("session-1", "my-repo")
        # A blank message is allowed while onboarding is active (private).
        result = controller.handle("session-1", "")
        assert "Integration branch (default: main)" in result["response"]
        record = _record(store, "session-1")
        assert record is not None
        assert record["onboarding_json"] is not None


def test_persisted_onboarding_prompt_uses_the_public_renderer(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "shared-renderer")) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        controller = _controller(
            adrian_plugin_modules, store, coordinator=coordinator
        )
        controller.handle("session-1", "opening")
        choose_mode = controller.handle("session-1", "3")
        record = _record(store, "session-1")
        assert coordinator.render(record) == choose_mode

        display_name = controller.handle("session-1", "1")
        record = _record(store, "session-1")
        assert coordinator.render(record) == display_name
        assert controller.resume_notice("session-1") == display_name["response"]
        assert display_name["response"].count("[Session Startup]") == 1


def test_onboarding_renderer_reports_invalid_durable_state(
    adrian_plugin_modules, kanban_home
):
    Store = adrian_plugin_modules["store"].AdmittedStore
    with Store(database_path=_db_path(kanban_home, "invalid-renderer")) as store:
        coordinator = _coordinator(adrian_plugin_modules, store)
        rendered = coordinator.render({"onboarding_json": "not-json"})

    assert rendered["action"] == "respond"
    assert rendered["response"].startswith(
        "[Session Startup] Stored onboarding state is invalid:"
    )
