"""Behavior tests for canonical Session Startup checkout materialization."""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def checkout_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
        f"{loaded.module.__name__}.session_startup_checkout"
    )
    try:
        yield module
    finally:
        manager.unload("adrian-kanban")


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Runner:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        if not self.responses:
            raise AssertionError(f"unexpected Git call {argv}")
        fragment, result = self.responses.pop(0)
        assert fragment in " ".join(argv)
        if isinstance(result, Exception):
            raise result
        if len(argv) > 1 and argv[1] == "clone" and result.returncode == 0:
            Path(argv[-1]).mkdir()
        return result


def _existing_runner(branch="main", remote_sha=None, status=""):
    remote_sha = remote_sha or "a" * 40
    return Runner([
        ("config", Result(stdout="git@github.com:Adrian-D-Lin/repo.git\n")),
        ("abbrev-ref", Result(stdout=f"{branch}\n")),
        ("status", Result(stdout=status)),
        ("fetch", Result()),
        (f"origin/{branch}", Result(stdout=f"{remote_sha}\n")),
    ])


def _establish(module, root, runner, **updates):
    arguments = {
        "checkout": str(root / "repo"),
        "repository": "Adrian-D-Lin/repo",
        "integration_branch": "main",
        "base_sha": "a" * 40,
        "checkout_root": str(root),
        "git_runner": runner,
    }
    arguments.update(updates)
    return module.establish_canonical_checkout(**arguments)


def test_absent_target_clones_with_safe_argv_and_verifies_evidence(
    checkout_module, tmp_path
):
    runner = Runner([
        ("clone", Result()),
        ("config", Result(stdout="git@github.com:Adrian-D-Lin/repo.git\n")),
        ("abbrev-ref", Result(stdout="main\n")),
        ("origin/main", Result(stdout="a" * 40 + "\n")),
        ("status", Result()),
    ])

    evidence = _establish(checkout_module, tmp_path, runner)

    clone = runner.calls[0]
    assert clone[:2] == ["git", "clone"]
    assert "--branch" in clone
    assert "git@github.com:adrian-d-lin/repo.git" in clone
    assert evidence.as_dict() == {
        "canonical_checkout": str(tmp_path / "repo"),
        "normalized_origin": "adrian-d-lin/repo",
        "integration_branch": "main",
        "remote_ref": "origin/main",
        "base_sha": "a" * 40,
        "created_by_operation": True,
    }


def test_retry_revalidates_existing_checkout_without_second_clone(
    checkout_module, tmp_path
):
    first = Runner([
        ("clone", Result()),
        ("config", Result(stdout="git@github.com:Adrian-D-Lin/repo.git\n")),
        ("abbrev-ref", Result(stdout="main\n")),
        ("origin/main", Result(stdout="a" * 40 + "\n")),
        ("status", Result()),
    ])
    assert _establish(checkout_module, tmp_path, first).created_by_operation
    second = _existing_runner()

    evidence = _establish(checkout_module, tmp_path, second)

    assert not any(call[1:2] == ["clone"] for call in second.calls)
    assert evidence.created_by_operation is False
    fetch_index = next(i for i, call in enumerate(second.calls) if "fetch" in call)
    ref_index = next(
        i for i, call in enumerate(second.calls) if "origin/main" in call
    )
    assert fetch_index < ref_index
    fetch = second.calls[fetch_index]
    assert "refs/heads/main:refs/remotes/origin/main" in fetch


@pytest.mark.parametrize(
    "responses,match",
    [
        ([("config", Result(stdout="git@github.com:Other/repo.git\n"))],
         "does not match"),
        ([("config", Result(stdout="git@github.com:Adrian-D-Lin/repo.git\n")),
          ("abbrev-ref", Result(stdout="develop\n"))], "not the integration branch"),
        ([("config", Result(stdout="git@github.com:Adrian-D-Lin/repo.git\n")),
          ("abbrev-ref", Result(stdout="main\n")),
          ("status", Result(stdout=" M changed.py\n"))], "uncommitted changes"),
        ([("config", Result(stdout="git@github.com:Adrian-D-Lin/repo.git\n")),
          ("abbrev-ref", Result(stdout="main\n")), ("status", Result()),
          ("fetch", Result()), ("origin/main", Result(returncode=128))], "missing"),
        ([("config", Result(stdout="git@github.com:Adrian-D-Lin/repo.git\n")),
          ("abbrev-ref", Result(stdout="main\n")), ("status", Result()),
          ("fetch", Result()),
          ("origin/main", Result(stdout="b" * 40 + "\n"))], "moved"),
    ],
)
def test_existing_checkout_rejects_identity_state_and_freshness_failures(
    checkout_module, tmp_path, responses, match
):
    (tmp_path / "repo").mkdir()
    with pytest.raises(checkout_module.CheckoutError, match=match):
        _establish(checkout_module, tmp_path, Runner(responses))


def test_non_directory_and_non_git_targets_are_not_rewritten(
    checkout_module, tmp_path
):
    target = tmp_path / "repo"
    target.write_text("occupied", encoding="utf-8")
    runner = Runner()
    with pytest.raises(checkout_module.CheckoutError, match="not a directory"):
        _establish(checkout_module, tmp_path, runner)
    assert runner.calls == []

    target.unlink()
    target.mkdir()
    with pytest.raises(checkout_module.CheckoutError, match="not a Git checkout"):
        _establish(
            checkout_module,
            tmp_path,
            Runner([("config", Result(returncode=128, stderr="not git"))]),
        )


@pytest.mark.parametrize("relative", [".", "../escape", "repo/deeper"])
def test_checkout_must_be_one_direct_child(checkout_module, tmp_path, relative):
    checkout = str((tmp_path / relative).resolve())
    with pytest.raises(checkout_module.CheckoutError, match="direct child"):
        _establish(
            checkout_module,
            tmp_path,
            Runner(),
            checkout=checkout,
        )


def test_symlink_escape_is_rejected_before_git(checkout_module, tmp_path):
    outside = tmp_path.parent / "outside-checkout"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "repo"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink unavailable")
    runner = Runner()
    with pytest.raises(checkout_module.CheckoutError, match="direct child"):
        _establish(checkout_module, tmp_path, runner)
    assert runner.calls == []


@pytest.mark.parametrize(
    "updates,match",
    [
        ({"repository": "repo"}, "owner/repo"),
        ({"repository": "Other/repo"}, "owner must be"),
        ({"base_sha": "not-a-sha"}, "valid 40- or 64-character"),
        ({"integration_branch": "bad..branch"}, "invalid integration branch"),
        ({"checkout_root": "relative"}, "absolute"),
    ],
)
def test_invalid_inputs_fail_before_git(checkout_module, tmp_path, updates, match):
    runner = Runner()
    with pytest.raises(checkout_module.CheckoutError, match=match):
        _establish(checkout_module, tmp_path, runner, **updates)
    assert runner.calls == []


@pytest.mark.parametrize(
    "failure,match",
    [
        (subprocess.TimeoutExpired(["git"], 2), "timed out"),
        (OSError("git unavailable"), "could not run"),
    ],
)
def test_runner_failures_are_recoverable(checkout_module, tmp_path, failure, match):
    def runner(*_args, **_kwargs):
        raise failure

    with pytest.raises(checkout_module.CheckoutError, match=match):
        _establish(checkout_module, tmp_path, runner)


def test_evidence_and_errors_exclude_environment_secrets(
    checkout_module, tmp_path, monkeypatch
):
    monkeypatch.setenv("GH_TOKEN", "must-not-appear")
    evidence = _establish(
        checkout_module,
        tmp_path,
        Runner([
            ("clone", Result()),
            ("config", Result(stdout="https://github.com/Adrian-D-Lin/repo.git\n")),
            ("abbrev-ref", Result(stdout="main\n")),
            ("origin/main", Result(stdout="a" * 40 + "\n")),
            ("status", Result()),
        ]),
    )
    assert "must-not-appear" not in repr(evidence.as_dict())
