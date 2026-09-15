"""Behavior tests for Session Startup project proposal and inspection."""

from __future__ import annotations

import importlib
import json
import os
import socket
import urllib.error
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def proposal_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, ModuleType]:
    import yaml
    from hermes_cli.plugins import PluginManager

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
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
        "binding": importlib.import_module(
            f"{package.__name__}.repository_binding"
        ),
        "proposal": importlib.import_module(
            f"{package.__name__}.session_startup_proposal"
        ),
    }
    try:
        yield modules
    finally:
        manager.unload("adrian-kanban")


def _payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mode": "create",
        "display_name": "New Project",
        "owner": "Adrian-D-Lin",
        "repository": "Example_Repo",
        "branch": "release/1.0",
    }
    payload.update(updates)
    return payload


def test_proposal_create_and_bind_have_exact_fields(proposal_modules):
    proposal = proposal_modules["proposal"]
    prepare = proposal.make_proposal_preparer("/srv/worktrees")

    created = prepare(_payload())
    bound = prepare(_payload(mode="bind"))

    assert created == {
        "mode": "create",
        "display_name": "New Project",
        "project_slug": "new-project",
        "repository": "adrian-d-lin/example_repo",
        "integration_branch": "release/1.0",
        "canonical_checkout": "/home/progenitor/AI-main/new-project",
        "controlled_worktree_root": "/srv/worktrees",
        "board_slug": "new-project",
        "visibility": "private",
    }
    expected_bound = {
        key: value for key, value in created.items() if key != "visibility"
    }
    expected_bound["mode"] = "bind"
    assert bound == expected_bound


def test_proposal_preserves_display_and_accepts_explicit_public(proposal_modules):
    prepare = proposal_modules["proposal"].make_proposal_preparer("/srv/worktrees")
    result = prepare(_payload(display_name="  Display Name  ", visibility="public"))
    assert result["display_name"] == "  Display Name  "
    assert result["visibility"] == "public"
    assert result["project_slug"] == "display-name"


@pytest.mark.parametrize(
    "updates,match",
    [
        ({"mode": "other"}, "mode"),
        ({"display_name": "  "}, "display_name"),
        ({"owner": "Other"}, "owner"),
        ({"owner": "adrian-d-lin"}, "owner"),
        ({"repository": "owner/repo"}, "repository name"),
        ({"branch": "bad..branch"}, "integration branch"),
        ({"visibility": "Public"}, "visibility"),
    ],
)
def test_proposal_rejects_invalid_inputs(proposal_modules, updates, match):
    prepare = proposal_modules["proposal"].make_proposal_preparer("/srv/worktrees")
    with pytest.raises(ValueError, match=match):
        prepare(_payload(**updates))


@pytest.mark.parametrize("root", ["", "relative/path", None])
def test_proposal_requires_absolute_worktree_root(proposal_modules, root):
    with pytest.raises((TypeError, ValueError)):
        proposal_modules["proposal"].make_proposal_preparer(root)


@pytest.mark.parametrize(
    "value",
    ["/main", "main/", "main//next", "main..next", "feature lock", "@",
     "main@{1}", "main.", "release/one.lock", "main\\next", "main~next"],
)
def test_shared_branch_validator_rejects_unsafe_refs(proposal_modules, value):
    with pytest.raises(ValueError):
        proposal_modules["binding"].validate_integration_branch(value)


def test_inspector_uses_fixed_owner_and_validates_default_branch(proposal_modules):
    calls = []

    def transport(url, headers, timeout):
        calls.append((url, headers, timeout))
        return json.dumps({"default_branch": "release/1.0"}).encode()

    inspector = proposal_modules["proposal"].GitHubDefaultBranchInspector(
        timeout=3, transport=transport
    )
    assert inspector.inspect("Example_Repo") == "release/1.0"
    assert inspector.inspect("ADRIAN-D-LIN/Example_Repo") == "release/1.0"
    assert calls[0][0].endswith("/adrian-d-lin/example_repo")
    assert "Authorization" not in calls[0][1]
    assert calls[0][2] == 3.0


@pytest.mark.parametrize(
    "failure,match",
    [
        (urllib.error.HTTPError("url", 401, "no", {}, None), "authentication"),
        (urllib.error.HTTPError("url", 403, "no", {}, None), "forbidden"),
        (urllib.error.HTTPError("url", 404, "no", {}, None), "not found"),
        (urllib.error.HTTPError("url", 500, "bad", {}, None), "http error 500"),
        (urllib.error.URLError("offline"), "connectivity"),
        (socket.timeout(), "timed out"),
        (TimeoutError("slow"), "timed out"),
        (OSError("network"), "connectivity"),
    ],
)
def test_inspector_classifies_transport_failures(proposal_modules, failure, match):
    def transport(*_args):
        raise failure

    inspector = proposal_modules["proposal"].GitHubDefaultBranchInspector(
        transport=transport
    )
    with pytest.raises(proposal_modules["proposal"].GitHubInspectionError, match=match):
        inspector.inspect("repo")


@pytest.mark.parametrize(
    "body,match",
    [
        (b"not-json", "malformed"),
        (b"[]", "malformed"),
        (b"{}", "default_branch"),
        (b'{"default_branch":"bad..branch"}', "not a valid branch"),
    ],
)
def test_inspector_rejects_invalid_responses(proposal_modules, body, match):
    inspector = proposal_modules["proposal"].GitHubDefaultBranchInspector(
        transport=lambda *_args: body
    )
    with pytest.raises(proposal_modules["proposal"].GitHubInspectionError, match=match):
        inspector.inspect("repo")


def test_default_transport_requires_credentials(proposal_modules, monkeypatch):
    for variable in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    inspector = proposal_modules["proposal"].GitHubDefaultBranchInspector()
    with pytest.raises(
        proposal_modules["proposal"].GitHubInspectionError,
        match="missing github credentials",
    ):
        inspector.inspect("repo")


def test_factory_reads_current_config_once_per_call_without_mutation(
    proposal_modules, tmp_path
):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("sentinel", encoding="utf-8")
    roots = iter(("/srv/one", "/srv/two"))
    calls = []

    def loader():
        root = next(roots)
        calls.append(root)
        return {"kanban": {"controlled_worktree_root": root}}

    first = proposal_modules["proposal"].make_onboarding_callbacks(
        config_loader=loader, transport=lambda *_args: b'{"default_branch":"main"}'
    )
    first["proposal_preparer"](_payload())
    first["proposal_preparer"](_payload(repository="second"))
    second = proposal_modules["proposal"].make_onboarding_callbacks(
        config_loader=loader, transport=lambda *_args: b'{"default_branch":"main"}'
    )

    assert calls == ["/srv/one", "/srv/two"]
    assert first["proposal_preparer"](_payload())["controlled_worktree_root"] == "/srv/one"
    assert second["proposal_preparer"](_payload())["controlled_worktree_root"] == "/srv/two"
    assert first["repo_inspector"]("repo") == "main"
    assert config_file.read_text(encoding="utf-8") == "sentinel"
