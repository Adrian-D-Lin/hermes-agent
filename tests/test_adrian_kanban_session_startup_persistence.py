"""Behavior tests for durable Session Startup repository and Project binding."""

from __future__ import annotations

import copy
import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def persistence_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import yaml
    from hermes_cli import kanban_db as kb
    from hermes_cli.plugins import PluginManager

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["adrian-kanban"]}}),
        encoding="utf-8",
    )
    kb.init_db()
    manager = PluginManager(scope_key=str(home.resolve()))
    manager.discover_and_load()
    loaded = manager._plugins["adrian-kanban"]
    assert loaded.enabled is True, loaded.error
    assert loaded.module is not None
    module = importlib.import_module(
        f"{loaded.module.__name__}.session_startup_persistence"
    )
    try:
        yield module
    finally:
        manager.unload("adrian-kanban")


def _proposal(**updates):
    value = {
        "project_slug": "repo",
        "display_name": "Repository",
        "canonical_checkout": "/home/progenitor/AI-main/repo",
        "repository": "adrian-d-lin/repo",
        "board_slug": "repo-board",
        "integration_branch": "main",
        "controlled_worktree_root": "/home/progenitor/AI-worktrees",
    }
    value.update(updates)
    return value


def _evidence(**updates):
    value = {
        "canonical_checkout": "/home/progenitor/AI-main/repo",
        "normalized_origin": "adrian-d-lin/repo",
        "integration_branch": "main",
    }
    value.update(updates)
    return value


def _merge(target, update):
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class ConfigStore:
    def __init__(self):
        self.data = {
            "kanban": {
                "controlled_worktree_root": "/home/progenitor/AI-worktrees",
                "repository_registry": {},
            },
            "unrelated": {"preserve": True},
        }
        self.writes = []

    def get(self):
        return self.data

    def write(self, update):
        self.writes.append(copy.deepcopy(update))
        _merge(self.data, update)


def _project(
    *,
    project_id="p1",
    slug="repo",
    name="Repository",
    primary_path="/home/progenitor/AI-main/repo",
    board_slug="repo-board",
    archived=False,
):
    return SimpleNamespace(
        id=project_id,
        slug=slug,
        name=name,
        primary_path=primary_path,
        board_slug=board_slug,
        archived=archived,
    )


class ProjectsDB:
    def __init__(self, projects=()):
        self.projects = list(projects)
        self.create_calls = []
        self.suffix_created_slug = False
        self.fail_create = None

    def get_project(self, _conn, identity):
        return next(
            (
                project
                for project in self.projects
                if identity in (project.id, project.slug)
            ),
            None,
        )

    def list_projects(self, _conn, *, include_archived=False):
        return [
            project
            for project in self.projects
            if include_archived or not project.archived
        ]

    def find_by_primary_path(self, _conn, path, *, include_archived=False):
        return next(
            (
                project
                for project in self.projects
                if project.primary_path == path
                and (include_archived or not project.archived)
            ),
            None,
        )

    def create_project(self, _conn, **fields):
        self.create_calls.append(fields)
        if self.fail_create:
            raise self.fail_create
        slug = fields["slug"] + ("-1" if self.suffix_created_slug else "")
        created = _project(
            project_id="created",
            slug=slug,
            name=fields["name"],
            primary_path=fields["primary_path"],
            board_slug=fields["board_slug"],
        )
        self.projects.append(created)
        return created.id


@contextmanager
def _connection():
    yield object()


def _persist_project(module, database, proposal=None, connector=_connection):
    return module.persist_project_binding(
        proposal or _proposal(),
        projects_db_getter=lambda: database,
        projects_db_connector=connector,
    )


def test_new_registry_write_is_minimal_and_preserves_siblings(persistence_module):
    config = ConfigStore()
    config.data["kanban"]["repository_registry"]["other"] = {
        "repository_root": "/home/progenitor/AI-main/other",
        "github_repository": "adrian-d-lin/other",
        "integration_branch": "develop",
    }

    result = persistence_module.persist_repository_registry(
        _proposal(), _evidence(), config_getter=config.get, config_writer=config.write
    )

    assert result.as_dict()["registry_entry"]["github_repository"] == (
        "adrian-d-lin/repo"
    )
    assert config.writes == [{
        "kanban": {
            "repository_registry": {
                "repo": {
                    "repository_root": "/home/progenitor/AI-main/repo",
                    "github_repository": "adrian-d-lin/repo",
                    "integration_branch": "main",
                }
            }
        }
    }]
    assert "other" in config.data["kanban"]["repository_registry"]
    assert config.data["unrelated"] == {"preserve": True}


def test_exact_registry_retry_does_not_rewrite(persistence_module):
    config = ConfigStore()
    for _ in range(2):
        persistence_module.persist_repository_registry(
            _proposal(),
            _evidence(),
            config_getter=config.get,
            config_writer=config.write,
        )
    assert len(config.writes) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository_root", "/home/progenitor/AI-main/different"),
        ("github_repository", "adrian-d-lin/different"),
        ("integration_branch", "develop"),
    ],
)
def test_registry_conflicts_are_actionable(persistence_module, field, value):
    config = ConfigStore()
    config.data["kanban"]["repository_registry"]["repo"] = {
        "repository_root": "/home/progenitor/AI-main/repo",
        "github_repository": "adrian-d-lin/repo",
        "integration_branch": "main",
    }
    config.data["kanban"]["repository_registry"]["repo"][field] = value
    with pytest.raises(persistence_module.PersistenceError, match=field):
        persistence_module.persist_repository_registry(
            _proposal(),
            _evidence(),
            config_getter=config.get,
            config_writer=config.write,
        )
    assert config.writes == []


@pytest.mark.parametrize(
    "proposal,evidence,match",
    [
        (_proposal(repository="adrian-d-lin/other"), _evidence(), "repository"),
        (_proposal(integration_branch="develop"), _evidence(), "integration_branch"),
        (
            _proposal(canonical_checkout="/home/progenitor/AI-main/other"),
            _evidence(),
            "canonical_checkout",
        ),
    ],
)
def test_proposal_and_checkout_identity_must_agree(
    persistence_module, proposal, evidence, match
):
    config = ConfigStore()
    with pytest.raises(persistence_module.PersistenceError, match=match):
        persistence_module.persist_repository_registry(
            proposal,
            evidence,
            config_getter=config.get,
            config_writer=config.write,
        )
    assert config.writes == []


@pytest.mark.parametrize(
    "proposal,match",
    [
        ({}, "missing required"),
        (_proposal(project_slug="Bad Slug"), "canonical form"),
        (_proposal(project_slug=" repo"), "whitespace"),
        (_proposal(repository=" adrian-d-lin/repo"), "whitespace"),
        (_proposal(controlled_worktree_root="relative"), "absolute"),
        (
            _proposal(canonical_checkout="/home/progenitor/AI-main"),
            "exact AI-main child",
        ),
        (
            _proposal(canonical_checkout="/home/progenitor/AI-main/repo/nested"),
            "exact AI-main child",
        ),
        (
            _proposal(canonical_checkout="/home/progenitor/AI-main/other"),
            "exact AI-main child",
        ),
    ],
)
def test_invalid_project_binding_input_fails_before_database(
    persistence_module, proposal, match
):
    reached = False

    def connector():
        nonlocal reached
        reached = True
        return _connection()

    with pytest.raises(persistence_module.PersistenceError, match=match):
        persistence_module.persist_project_binding(
            proposal,
            projects_db_getter=lambda: ProjectsDB(),
            projects_db_connector=connector,
        )
    assert reached is False


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda cfg: cfg.update({"kanban": []}), "kanban config"),
        (
            lambda cfg: cfg["kanban"].update({"repository_registry": []}),
            "repository_registry",
        ),
        (
            lambda cfg: cfg["kanban"].update(
                {"controlled_worktree_root": "/different"}
            ),
            "controlled_worktree_root",
        ),
    ],
)
def test_malformed_or_mismatched_config_fails_before_write(
    persistence_module, mutation, match
):
    config = ConfigStore()
    mutation(config.data)
    with pytest.raises(persistence_module.PersistenceError, match=match):
        persistence_module.persist_repository_registry(
            _proposal(),
            _evidence(),
            config_getter=config.get,
            config_writer=config.write,
        )
    assert config.writes == []


def test_registry_writer_failure_is_recoverable(persistence_module):
    config = ConfigStore()

    def fail(_update):
        raise OSError("disk unavailable")

    with pytest.raises(persistence_module.PersistenceError, match="persistence failed"):
        persistence_module.persist_repository_registry(
            _proposal(), _evidence(), config_getter=config.get, config_writer=fail
        )


def test_new_project_uses_exact_fields_and_retry_is_idempotent(persistence_module):
    database = ProjectsDB()
    first = _persist_project(persistence_module, database)
    second = _persist_project(persistence_module, database)

    assert first == second
    assert first.as_dict() == {
        "project_id": "created",
        "project_slug": "repo",
        "display_name": "Repository",
        "primary_path": "/home/progenitor/AI-main/repo",
        "board_slug": "repo-board",
    }
    assert database.create_calls == [{
        "name": "Repository",
        "slug": "repo",
        "primary_path": "/home/progenitor/AI-main/repo",
        "board_slug": "repo-board",
    }]


def test_display_name_is_the_only_trimmed_identity(persistence_module):
    database = ProjectsDB()
    result = _persist_project(
        persistence_module, database, _proposal(display_name="  Repository  ")
    )
    assert result.display_name == "Repository"
    assert database.create_calls[0]["name"] == "Repository"


@pytest.mark.parametrize(
    "existing,match",
    [
        (_project(name="Other"), "different fields"),
        (_project(archived=True), "archived project"),
        (
            _project(
                project_id="path-owner",
                slug="other",
                primary_path="/home/progenitor/AI-main/repo",
                board_slug="other-board",
            ),
            "already belongs",
        ),
        (
            _project(
                project_id="board-owner",
                slug="other",
                primary_path="/home/progenitor/AI-main/other",
                board_slug="repo-board",
            ),
            "already bound",
        ),
    ],
)
def test_existing_project_conflicts_fail_without_creation(
    persistence_module, existing, match
):
    database = ProjectsDB([existing])
    with pytest.raises(persistence_module.PersistenceError, match=match):
        _persist_project(persistence_module, database)
    assert database.create_calls == []


def test_auto_suffixed_project_slug_is_rejected(persistence_module):
    database = ProjectsDB()
    database.suffix_created_slug = True
    with pytest.raises(persistence_module.PersistenceError, match="auto-suffixed"):
        _persist_project(persistence_module, database)


@pytest.mark.parametrize(
    "failure",
    [OSError("write failed"), RuntimeError("write failed")],
)
def test_project_operational_failures_are_recoverable(
    persistence_module, failure
):
    database = ProjectsDB()
    database.fail_create = failure
    with pytest.raises(
        persistence_module.PersistenceError, match="Project binding persistence failed"
    ):
        _persist_project(persistence_module, database)


def test_programmer_errors_from_injected_database_are_not_hidden(persistence_module):
    database = ProjectsDB()

    def connector():
        raise AssertionError("bad fake contract")

    with pytest.raises(AssertionError, match="bad fake contract"):
        _persist_project(persistence_module, database, connector=connector)


def test_real_projects_database_contract_and_retry(
    persistence_module, tmp_path: Path
):
    from hermes_cli import projects_db

    database_path = tmp_path / "projects.db"
    connector = lambda: projects_db.connect_closing(db_path=database_path)
    first = persistence_module.persist_project_binding(
        _proposal(),
        projects_db_getter=lambda: projects_db,
        projects_db_connector=connector,
    )
    second = persistence_module.persist_project_binding(
        _proposal(),
        projects_db_getter=lambda: projects_db,
        projects_db_connector=connector,
    )

    assert first.project_id == second.project_id
    with projects_db.connect_closing(db_path=database_path) as connection:
        project = projects_db.get_project(connection, "repo")
        assert project is not None
        assert project.name == "Repository"
        assert project.primary_path == "/home/progenitor/AI-main/repo"
        assert project.board_slug == "repo-board"
        assert len(projects_db.list_projects(connection)) == 1
