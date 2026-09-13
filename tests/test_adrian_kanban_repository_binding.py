"""Design-oracle tests for trusted project/repository bindings."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "repository_binding_adrian_kanban"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    try:
        yield {
            "package": package,
            "binding": __import__(
                f"{package_name}.repository_binding", fromlist=["*"]
            ),
            "workspace": __import__(f"{package_name}.workspace", fromlist=["*"]),
        }
    finally:
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                sys.modules.pop(name, None)


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:Adrian-D-Lin/GRC.git", "adrian-d-lin/grc"),
        ("ssh://git@github.com/Adrian-D-Lin/GRC.git", "adrian-d-lin/grc"),
        ("https://github.com/Adrian-D-Lin/GRC", "adrian-d-lin/grc"),
        ("https://github.com/Adrian-D-Lin/GRC.git/", "adrian-d-lin/grc"),
    ],
)
def test_normalize_github_remote_equivalence(modules, remote, expected):
    assert modules["binding"].normalize_github_repository(remote) == expected


@pytest.mark.parametrize(
    "remote",
    [
        "",
        "https://gitlab.com/owner/repo.git",
        "git@github.com:owner.git",
        "git@github.com:owner/repo/extra.git",
        "file:///srv/repo",
    ],
)
def test_normalize_github_remote_rejects_non_github_identity(modules, remote):
    with pytest.raises(ValueError, match="GitHub repository"):
        modules["binding"].normalize_github_repository(remote)


def _registration(modules, root: Path, **overrides):
    values = {
        "repository_identity": "grc",
        "repository_root": str(root),
        "controlled_worktree_root": str(root.parent / "worktrees"),
        "github_repository": "Adrian-D-Lin/GRC",
        "integration_branch": "release/production",
    }
    values.update(overrides)
    return modules["workspace"]._RepositoryRegistration(**values)


def test_registration_derives_one_remote_contract(modules, tmp_path):
    registration = _registration(modules, tmp_path / "repo")

    assert registration.github_repository == "adrian-d-lin/grc"
    assert registration.integration_branch == "release/production"
    assert registration.canonical_ssh_url == "git@github.com:adrian-d-lin/grc.git"
    assert registration.remote_branch_ref == "refs/heads/release/production"
    assert registration.remote_tracking_ref == (
        "refs/remotes/origin/release/production"
    )
    assert registration.remote_label == "origin/release/production"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("github_repository", "not-a-repository", "GitHub repository"),
        ("integration_branch", "../main", "integration_branch"),
        ("integration_branch", "main..old", "integration_branch"),
        ("integration_branch", "", "integration_branch"),
    ],
)
def test_registration_rejects_invalid_remote_contract(
    modules, tmp_path, field, value, message
):
    with pytest.raises(ValueError, match=message):
        _registration(modules, tmp_path / "repo", **{field: value})


class _Runner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, *, cwd, **_kwargs):
        self.calls.append((tuple(argv), cwd))
        if not self.responses:
            raise AssertionError(f"unexpected command: {argv}")
        expected_tail, returncode, stdout, stderr = self.responses.pop(0)
        assert tuple(argv[-len(expected_tail) :]) == tuple(expected_tail)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _identity_responses(root: Path, *, origin="git@github.com:Adrian-D-Lin/GRC.git"):
    common = str(root / ".git")
    return [
        (("rev-parse", "--git-common-dir"), 0, common + "\n", ""),
        (("config", "--get", "remote.origin.url"), 0, origin + "\n", ""),
    ]


def test_online_resolution_fetches_configured_branch_and_records_exact_sha(
    modules, tmp_path
):
    root = tmp_path / "repo"
    registration = _registration(modules, root)
    sha = "a" * 40
    runner = _Runner(
        _identity_responses(root)
        + [
            (
                (
                    "fetch",
                    "--no-tags",
                    "origin",
                    "refs/heads/release/production:refs/remotes/origin/release/production",
                ),
                0,
                "",
                "",
            ),
            (("rev-parse", registration.remote_tracking_ref), 0, sha + "\n", ""),
        ]
    )

    result = modules["binding"].RepositoryBindingResolver(
        registration, runner=runner
    ).resolve_integration_head(allow_offline=True)

    assert result.head_sha == sha
    assert result.state == "online"
    assert result.remote_ref == registration.remote_tracking_ref
    assert result.remote_error is None


def test_connectivity_failure_uses_verified_cached_ref_for_local_work(
    modules, tmp_path
):
    root = tmp_path / "repo"
    registration = _registration(modules, root)
    sha = "b" * 40
    runner = _Runner(
        _identity_responses(root)
        + [
            (
                (
                    "fetch",
                    "--no-tags",
                    "origin",
                    "refs/heads/release/production:refs/remotes/origin/release/production",
                ),
                128,
                "",
                "fatal: unable to access github.com: Could not resolve host",
            ),
            (("rev-parse", registration.remote_tracking_ref), 0, sha + "\n", ""),
            (("cat-file", "-e", f"{sha}^{{commit}}"), 0, "", ""),
        ]
    )

    result = modules["binding"].RepositoryBindingResolver(
        registration, runner=runner
    ).resolve_integration_head(allow_offline=True)

    assert result.head_sha == sha
    assert result.state == "degraded_offline"
    assert result.remote_error == "GitHub is unreachable"


def test_offline_mode_never_masks_authentication_failure(modules, tmp_path):
    root = tmp_path / "repo"
    registration = _registration(modules, root)
    runner = _Runner(
        _identity_responses(root)
        + [
            (
                (
                    "fetch",
                    "--no-tags",
                    "origin",
                    "refs/heads/release/production:refs/remotes/origin/release/production",
                ),
                128,
                "",
                "git@github.com: Permission denied (publickey).",
            )
        ]
    )

    with pytest.raises(
        modules["binding"].RepositoryBindingError,
        match="authentication",
    ):
        modules["binding"].RepositoryBindingResolver(
            registration, runner=runner
        ).resolve_integration_head(allow_offline=True)


def test_remote_dependent_operation_rejects_connectivity_fallback(modules, tmp_path):
    root = tmp_path / "repo"
    registration = _registration(modules, root)
    runner = _Runner(
        _identity_responses(root)
        + [
            (
                (
                    "fetch",
                    "--no-tags",
                    "origin",
                    "refs/heads/release/production:refs/remotes/origin/release/production",
                ),
                128,
                "",
                "ssh: connect to host github.com port 22: Network is unreachable",
            )
        ]
    )

    with pytest.raises(
        modules["binding"].RepositoryBindingError,
        match="remote evidence is required",
    ):
        modules["binding"].RepositoryBindingResolver(
            registration, runner=runner
        ).resolve_integration_head(allow_offline=False)


def test_local_origin_mismatch_blocks_before_fetch(modules, tmp_path):
    root = tmp_path / "repo"
    registration = _registration(modules, root)
    runner = _Runner(
        _identity_responses(root, origin="git@github.com:someone/else.git")
    )

    with pytest.raises(
        modules["binding"].RepositoryBindingError,
        match="origin.*does not match",
    ):
        modules["binding"].RepositoryBindingResolver(
            registration, runner=runner
        ).resolve_integration_head(allow_offline=True)

    assert len(runner.calls) == 2
