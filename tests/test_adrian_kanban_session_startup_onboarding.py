"""Behavior tests for durable Session Startup project onboarding."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def onboarding_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, ModuleType], Path]:
    import yaml
    from hermes_cli.plugins import PluginManager

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["adrian-kanban"]}}),
        encoding="utf-8",
    )
    manager = PluginManager(scope_key=str(home.resolve()))
    manager.discover_and_load()
    loaded = manager._plugins["adrian-kanban"]
    assert loaded.enabled is True, loaded.error
    assert loaded.module is not None
    package = loaded.module
    modules = {
        "store": importlib.import_module(f"{package.__name__}.store"),
        "onboarding": importlib.import_module(
            f"{package.__name__}.session_startup_onboarding"
        ),
    }
    try:
        yield modules, home
    finally:
        manager.unload("adrian-kanban")


def _proposal(payload: dict) -> dict:
    result = {
        "mode": payload["mode"],
        "display_name": payload["display_name"],
        "project_slug": payload["display_name"].strip().lower().replace(" ", "-"),
        "repository": f"adrian-d-lin/{payload['repository'].lower()}",
        "integration_branch": payload["branch"],
        "canonical_checkout": f"/home/progenitor/AI-main/{payload['repository']}",
        "controlled_worktree_root": "/home/progenitor/AI-worktrees",
        "board_slug": payload["repository"].lower(),
    }
    if payload["mode"] == "create":
        result["visibility"] = payload["visibility"]
    return result


def _new_record(store, session_id="session-1"):
    return store.create_or_read_session_startup(
        session_id=session_id,
        opening_prompt="held opening prompt",
        protocol_version="v0.29",
    )


def _current(store, session_id="session-1"):
    return store.read_session_startup(session_id=session_id)


def _coordinator(modules, store, **callbacks):
    return modules["onboarding"].OnboardingCoordinator(
        store,
        repo_inspector=callbacks.get("inspector"),
        proposal_preparer=callbacks.get("preparer", _proposal),
        operation_executor=callbacks.get("executor"),
    )


def _advance_create_to_confirm(coordinator, store):
    coordinator.start_onboarding(_new_record(store))
    for message in ("1", "Project One", "new-repo", "", ""):
        result = coordinator.handle(_current(store), message)
        assert result["action"] == "respond"
    return _current(store)


def test_create_flow_persists_defaults_and_hands_back(onboarding_modules):
    modules, home = onboarding_modules
    captured = []

    def executor(record, proposal, persist):
        captured.append((record, proposal, persist))
        return {"status": "created", "project": {"id": "project-new"}}

    Store = modules["store"].AdmittedStore
    with Store(database_path=str(home / "create.db")) as store:
        coordinator = _coordinator(modules, store, executor=executor)
        record = _advance_create_to_confirm(coordinator, store)
        state = json.loads(record["onboarding_json"])
        assert state["stage"] == "confirm"
        assert state["draft"]["visibility"] == "private"
        assert state["draft"]["branch_create"] == "main"
        assert state["journal"]["events"] == [
            "field:display_name",
            "field:repository",
            "field:visibility",
            "field:branch",
            "proposal:prepared",
        ]

        not_confirmed = coordinator.handle(record, "CONFIRM")
        assert "Enter confirm" in not_confirmed["response"]
        assert captured == []

        result = coordinator.handle(_current(store), "confirm")
        final = _current(store)

    assert result == {"action": "handback", "project_id": "project-new", "revision": 7}
    assert captured[0][1]["visibility"] == "private"
    assert captured[0][1]["integration_branch"] == "main"
    assert final["onboarding_json"] is None
    assert final["held_opening_prompt"] == "held opening prompt"


def test_create_public_requires_exact_lowercase_selection(onboarding_modules):
    modules, home = onboarding_modules
    Store = modules["store"].AdmittedStore
    with Store(database_path=str(home / "public.db")) as store:
        coordinator = _coordinator(modules, store)
        coordinator.start_onboarding(_new_record(store))
        for message in ("1", "Project", "repo"):
            coordinator.handle(_current(store), message)
        revision = _current(store)["revision"]
        rejected = coordinator.handle(_current(store), "Public")
        assert "visibility must be" in rejected["response"]
        assert _current(store)["revision"] == revision
        coordinator.handle(_current(store), "public")
        assert json.loads(_current(store)["onboarding_json"])["draft"]["visibility"] == "public"


def test_bind_flow_uses_inspector_default_or_typed_override(onboarding_modules):
    modules, home = onboarding_modules
    inspected = []
    Store = modules["store"].AdmittedStore
    with Store(database_path=str(home / "bind.db")) as store:
        coordinator = _coordinator(
            modules,
            store,
            inspector=lambda repository: inspected.append(repository) or "develop",
        )
        coordinator.start_onboarding(_new_record(store))
        for message in ("2", "existing-repo", "Existing Project", ""):
            coordinator.handle(_current(store), message)
        default_state = json.loads(_current(store)["onboarding_json"])

    assert inspected == ["existing-repo"]
    assert default_state["stage"] == "confirm"
    assert default_state["draft"]["branch_bind"] == "develop"
    assert "visibility" not in default_state["journal"]["proposal"]


def test_bind_invalid_repository_never_calls_inspector(onboarding_modules):
    modules, home = onboarding_modules
    inspected = []
    Store = modules["store"].AdmittedStore
    with Store(database_path=str(home / "invalid-repo.db")) as store:
        coordinator = _coordinator(
            modules, store, inspector=lambda repository: inspected.append(repository)
        )
        coordinator.start_onboarding(_new_record(store))
        coordinator.handle(_current(store), "2")
        before = _current(store)
        result = coordinator.handle(before, "owner/repo")
        after = _current(store)

    assert "Invalid repository" in result["response"]
    assert inspected == []
    assert after["revision"] == before["revision"]


@pytest.mark.parametrize(
    "stage,draft,journal",
    [
        ("choose_mode", {}, {}),
        ("create_details", {"mode": "create", "next": "display_name"}, {}),
        ("confirm", {"mode": "create"}, {"proposal": _proposal({
            "mode": "create", "display_name": "P", "repository": "r",
            "branch": "main", "visibility": "private",
        })}),
        ("blocked_recoverable", {"mode": "create"}, {
            "recovery_error": "failed", "proposal": _proposal({
                "mode": "create", "display_name": "P", "repository": "r",
                "branch": "main", "visibility": "private",
            }),
        }),
    ],
)
def test_cancel_clears_every_onboarding_stage(
    onboarding_modules, stage, draft, journal
):
    modules, home = onboarding_modules
    Store = modules["store"].AdmittedStore
    with Store(database_path=str(home / f"cancel-{stage}.db")) as store:
        record = _new_record(store)
        record = store.set_session_startup_onboarding(
            session_id=record["session_id"],
            expected_revision=record["revision"],
            onboarding={"stage": stage, "draft": draft, "journal": journal},
        )
        result = _coordinator(modules, store).handle(record, "cancel")
        final = _current(store)

    assert result["action"] == "return_to_projects"
    assert final["onboarding_json"] is None


def test_executor_failure_blocks_and_retry_uses_persisted_proposal(onboarding_modules):
    modules, home = onboarding_modules
    outcomes = iter((RuntimeError("temporary failure"), {
        "status": "created", "project": {"id": "project-retried"}
    }))

    def executor(record, proposal, persist):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    Store = modules["store"].AdmittedStore
    with Store(database_path=str(home / "retry.db")) as store:
        coordinator = _coordinator(modules, store, executor=executor)
        record = _advance_create_to_confirm(coordinator, store)
        blocked = coordinator.handle(record, "confirm")
        blocked_record = _current(store)
        blocked_state = json.loads(blocked_record["onboarding_json"])
        result = coordinator.handle(blocked_record, "retry")

    assert "temporary failure" in blocked["response"]
    assert blocked_state["stage"] == "blocked_recoverable"
    assert blocked_state["journal"]["proposal"]["repository"].endswith("/new-repo")
    assert result["action"] == "handback"
    assert result["project_id"] == "project-retried"


def test_executor_checkpoints_use_newest_revision_when_failure_is_blocked(
    onboarding_modules,
):
    modules, home = onboarding_modules

    def executor(record, proposal, persist):
        state = json.loads(record["onboarding_json"])
        state["journal"]["events"].append("checkpoint-one")
        record = persist(state)
        state = json.loads(record["onboarding_json"])
        state["journal"]["events"].append("checkpoint-two")
        persist(state)
        raise RuntimeError("after checkpoints")

    Store = modules["store"].AdmittedStore
    with Store(database_path=str(home / "checkpoint.db")) as store:
        coordinator = _coordinator(modules, store, executor=executor)
        record = _advance_create_to_confirm(coordinator, store)
        result = coordinator.handle(record, "confirm")
        final = json.loads(_current(store)["onboarding_json"])

    assert "after checkpoints" in result["response"]
    assert final["stage"] == "blocked_recoverable"
    assert final["journal"]["events"][-3:] == [
        "checkpoint-one", "checkpoint-two", "blocked"
    ]


def test_stale_record_fails_closed_without_overwriting_newer_state(onboarding_modules):
    modules, home = onboarding_modules
    Store = modules["store"].AdmittedStore
    with Store(database_path=str(home / "stale.db")) as store:
        coordinator = _coordinator(modules, store)
        coordinator.start_onboarding(_new_record(store))
        stale = _current(store)
        coordinator.handle(stale, "1")
        current = _current(store)
        result = coordinator.handle(stale, "2")
        after = _current(store)

    assert "Failed to persist project onboarding" in result["response"]
    assert after["revision"] == current["revision"]
    assert after["onboarding_json"] == current["onboarding_json"]


@pytest.mark.parametrize(
    "executor_result",
    [None, {"status": "failed"}, {"status": "created"},
     {"status": "created", "project": {"id": " "}}],
)
def test_unexpected_executor_results_become_recoverable_blocks(
    onboarding_modules, executor_result
):
    modules, home = onboarding_modules
    Store = modules["store"].AdmittedStore
    with Store(database_path=str(home / f"bad-result-{id(executor_result)}.db")) as store:
        coordinator = _coordinator(
            modules, store, executor=lambda *_args: executor_result
        )
        record = _advance_create_to_confirm(coordinator, store)
        result = coordinator.handle(record, "confirm")
        final = json.loads(_current(store)["onboarding_json"])

    assert result["action"] == "respond"
    assert final["stage"] == "blocked_recoverable"
    assert final["journal"]["recovery_error"]
