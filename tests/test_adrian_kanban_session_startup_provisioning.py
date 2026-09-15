"""Behavior tests for durable Session Startup repository provisioning."""

from __future__ import annotations

import importlib
import json
import subprocess
import urllib.error
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, ModuleType]:
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
    package = loaded.module
    result = {
        "proposal": importlib.import_module(
            f"{package.__name__}.session_startup_proposal"
        ),
        "provisioning": importlib.import_module(
            f"{package.__name__}.session_startup_provisioning"
        ),
    }
    try:
        yield result
    finally:
        manager.unload("adrian-kanban")


class Transport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, method, body, timeout):
        self.calls.append(
            {"url": url, "headers": dict(headers), "method": method,
             "body": body, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {url}")
        expected_method, path, response = self.responses.pop(0)
        assert method == expected_method
        assert path in url
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, bytes) else json.dumps(response).encode()


class Persist:
    def __init__(self, record):
        self.records = [record]

    def __call__(self, onboarding):
        previous = self.records[-1]
        record = {
            "session_id": previous["session_id"],
            "revision": previous["revision"] + 1,
            "onboarding_json": json.dumps(onboarding),
        }
        self.records.append(record)
        return record


def _repo(private=True, default_branch="main"):
    return {
        "id": 1,
        "full_name": "adrian-d-lin/proj-repo",
        "name": "proj-repo",
        "private": private,
        "default_branch": default_branch,
        "owner": {"login": "Adrian-D-Lin"},
    }


def _ref(branch="main", sha=None):
    return {
        "ref": f"refs/heads/{branch}",
        "object": {"type": "commit", "sha": sha or "a" * 40},
    }


def _proposal(modules, mode="create", branch="main", visibility="private"):
    prepare = modules["proposal"].make_proposal_preparer("/srv/worktrees")
    payload = {
        "mode": mode,
        "display_name": "Project Repository",
        "owner": "Adrian-D-Lin",
        "repository": "proj-repo",
        "branch": branch,
    }
    if mode == "create":
        payload["visibility"] = visibility
    return prepare(payload)


def _record(proposal):
    return {
        "session_id": "session-1",
        "revision": 1,
        "onboarding_json": json.dumps({
            "stage": "confirm",
            "draft": {"mode": proposal["mode"]},
            "journal": {"proposal": proposal, "events": ["proposal:prepared"]},
        }),
    }


def _runner(calls, *, failure=None):
    def run(args, timeout, extra_env):
        calls.append((list(args), timeout, dict(extra_env or {})))
        if failure:
            raise failure
        return 0, "", ""

    return run


def _block(modules, persist, index=-1):
    onboarding = json.loads(persist.records[index]["onboarding_json"])
    return onboarding["journal"][modules["provisioning"].JOURNAL_BLOCK_KEY]


def test_create_private_is_journaled_before_external_effects(
    modules, monkeypatch
):
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    transport = Transport([
        ("GET", "/user", {"login": "Adrian-D-Lin"}),
        ("GET", "/repos/adrian-d-lin/proj-repo", urllib.error.HTTPError(
            "url", 404, "missing", {}, None
        )),
        ("POST", "/user/repos", _repo()),
        ("GET", "/git/refs/heads/main", _ref()),
    ])
    git_calls = []
    record = _record(_proposal(modules))
    persist = Persist(record)
    provisioner = modules["provisioning"].make_provisioner(
        transport=transport, git_runner=_runner(git_calls)
    )

    evidence = provisioner.run_provisioning(record, persist)

    first = _block(modules, persist, 1)
    assert first["steps"] == [modules["provisioning"].STEP_OPERATION_ID]
    assert evidence["repository"] == "adrian-d-lin/proj-repo"
    assert evidence["branch_sha"] == "a" * 40
    create = next(call for call in transport.calls if call["method"] == "POST")
    assert create["url"].endswith("/user/repos")
    assert json.loads(create["body"]) == {
        "name": "proj-repo", "private": True, "auto_init": True
    }
    assert "secret-token" not in json.dumps(_block(modules, persist))
    assert git_calls[0][0][:2] == ["git", "ls-remote"]


def test_create_public_non_main_branch_is_established(modules, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "token")
    transport = Transport([
        ("GET", "/user", {"login": "Adrian-D-Lin"}),
        ("GET", "/repos/adrian-d-lin/proj-repo", urllib.error.HTTPError(
            "url", 404, "missing", {}, None
        )),
        ("POST", "/user/repos", _repo(private=False)),
        ("GET", "/git/refs/heads/release/1.0", urllib.error.HTTPError(
            "url", 404, "missing", {}, None
        )),
        ("GET", "/git/refs/heads/main", _ref("main", "b" * 40)),
        ("POST", "/git/refs", _ref("release/1.0", "c" * 40)),
    ])
    record = _record(_proposal(
        modules, branch="release/1.0", visibility="public"
    ))
    persist = Persist(record)

    evidence = modules["provisioning"].make_provisioner(
        transport=transport, git_runner=_runner([])
    ).run_provisioning(record, persist)

    assert evidence["visibility"] == "public"
    assert evidence["integration_branch"] == "release/1.0"
    assert evidence["branch_sha"] == "c" * 40
    branch_create = transport.calls[-1]
    assert json.loads(branch_create["body"])["ref"] == "refs/heads/release/1.0"


def test_bind_public_repository_needs_no_token_or_mutation(modules, monkeypatch):
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    transport = Transport([
        ("GET", "/repos/adrian-d-lin/proj-repo", _repo(private=False)),
        ("GET", "/git/refs/heads/main", _ref()),
    ])
    record = _record(_proposal(modules, mode="bind"))
    persist = Persist(record)

    evidence = modules["provisioning"].make_provisioner(
        transport=transport, git_runner=_runner([])
    ).run_provisioning(record, persist)

    assert evidence["mode"] == "bind"
    assert evidence["visibility"] is None
    assert all(call["method"] == "GET" for call in transport.calls)
    assert all("Authorization" not in call["headers"] for call in transport.calls)
    assert _block(modules, persist)["repository"]["visibility"] == "public"


@pytest.mark.parametrize(
    "responses,match",
    [
        ([("GET", "/repos/", urllib.error.HTTPError(
            "url", 404, "missing", {}, None
        ))], "already exist"),
        ([("GET", "/repos/", _repo()),
          ("GET", "/git/refs/heads/main", urllib.error.HTTPError(
              "url", 404, "missing", {}, None
          ))], "does not exist"),
    ],
)
def test_bind_requires_existing_repository_and_branch(
    modules, monkeypatch, responses, match
):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    record = _record(_proposal(modules, mode="bind"))
    with pytest.raises(modules["provisioning"].ProvisioningError, match=match):
        modules["provisioning"].make_provisioner(
            transport=Transport(responses), git_runner=_runner([])
        ).run_provisioning(record, Persist(record))


@pytest.mark.parametrize(
    "login,private,visibility,match",
    [
        ("Other-User", True, "private", "different account"),
        ("Adrian-D-Lin", True, "public", "visibility"),
    ],
)
def test_create_rejects_wrong_account_or_visibility(
    modules, monkeypatch, login, private, visibility, match
):
    monkeypatch.setenv("GH_TOKEN", "token")
    responses = [("GET", "/user", {"login": login})]
    if login == "Adrian-D-Lin":
        responses.append(("GET", "/repos/", _repo(private=private)))
    record = _record(_proposal(modules, visibility=visibility))
    with pytest.raises(modules["provisioning"].ProvisioningError, match=match):
        modules["provisioning"].make_provisioner(
            transport=Transport(responses), git_runner=_runner([])
        ).run_provisioning(record, Persist(record))


def test_retry_reuses_operation_id_and_refreshes_moved_branch(modules, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    record = _record(_proposal(modules, mode="bind"))
    persist = Persist(record)
    first_transport = Transport([
        ("GET", "/repos/", _repo()),
        ("GET", "/git/refs/heads/main", _ref(sha="a" * 40)),
    ])
    first = modules["provisioning"].make_provisioner(
        transport=first_transport, git_runner=_runner([])
    ).run_provisioning(record, persist)
    second_transport = Transport([
        ("GET", "/repos/", _repo()),
        ("GET", "/git/refs/heads/main", _ref(sha="b" * 40)),
    ])

    second = modules["provisioning"].make_provisioner(
        transport=second_transport, git_runner=_runner([])
    ).run_provisioning(persist.records[-1], persist)

    assert second["operation_id"] == first["operation_id"]
    assert second["branch_sha"] == "b" * 40
    assert _block(modules, persist)["step_evidence"][
        modules["provisioning"].STEP_BRANCH
    ]["branch_sha"] == "b" * 40


@pytest.mark.parametrize(
    "failure,match",
    [
        (urllib.error.HTTPError("url", 401, "bad", {}, None), "authentication"),
        (urllib.error.HTTPError("url", 403, "bad", {}, None), "forbidden"),
        (urllib.error.HTTPError("url", 500, "bad", {}, None), "500"),
        (urllib.error.URLError("offline"), "connectivity"),
        (TimeoutError("slow"), "timed out"),
        (OSError("network"), "connectivity"),
    ],
)
def test_transport_failures_are_actionable(modules, monkeypatch, failure, match):
    monkeypatch.setenv("GH_TOKEN", "token")
    record = _record(_proposal(modules))
    with pytest.raises(modules["provisioning"].ProvisioningError, match=match):
        modules["provisioning"].make_provisioner(
            transport=Transport([("GET", "/user", failure)]),
            git_runner=_runner([]),
        ).run_provisioning(record, Persist(record))


@pytest.mark.parametrize("body", [b"bad json", b"[]", b"{}"])
def test_malformed_user_responses_are_rejected(modules, monkeypatch, body):
    monkeypatch.setenv("GH_TOKEN", "token")
    record = _record(_proposal(modules))
    with pytest.raises(modules["provisioning"].ProvisioningError, match="malformed"):
        modules["provisioning"].make_provisioner(
            transport=Transport([("GET", "/user", body)]),
            git_runner=_runner([]),
        ).run_provisioning(record, Persist(record))


@pytest.mark.parametrize(
    "runner,match",
    [
        (lambda calls: (lambda *_args: (128, "", "Permission denied")), "ssh git access"),
        (lambda calls: _runner(calls, failure=subprocess.TimeoutExpired("git", 1)),
         "timed out"),
    ],
)
def test_ssh_failures_are_recoverable(modules, monkeypatch, runner, match):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    transport = Transport([("GET", "/repos/", _repo())])
    record = _record(_proposal(modules, mode="bind"))
    with pytest.raises(modules["provisioning"].ProvisioningError, match=match):
        modules["provisioning"].make_provisioner(
            transport=transport, git_runner=runner([])
        ).run_provisioning(record, Persist(record))


@pytest.mark.parametrize(
    "proposal_update,match",
    [
        ({"repository": "someone/repo"}, "owner"),
        ({"integration_branch": "bad..branch"}, "invalid integration branch"),
    ],
)
def test_invalid_identity_or_branch_has_no_external_effect(
    modules, proposal_update, match
):
    proposal = _proposal(modules, mode="bind")
    proposal.update(proposal_update)
    record = _record(proposal)
    transport = Transport()
    with pytest.raises(modules["provisioning"].ProvisioningError, match=match):
        modules["provisioning"].make_provisioner(
            transport=transport, git_runner=_runner([])
        ).run_provisioning(record, Persist(record))
    assert transport.calls == []


def test_operation_id_persistence_failure_precedes_external_effect(modules):
    record = _record(_proposal(modules, mode="bind"))
    transport = Transport()

    def fail_persist(_onboarding):
        raise RuntimeError("persistence unavailable")

    with pytest.raises(RuntimeError, match="persistence unavailable"):
        modules["provisioning"].make_provisioner(
            transport=transport, git_runner=_runner([])
        ).run_provisioning(record, fail_persist)
    assert transport.calls == []
