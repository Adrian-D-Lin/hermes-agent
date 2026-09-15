"""Workspace slice: configured integration-branch authority (non-main).

Proves materialize/freshness/FF/merge/push follow the member's trusted
registration (integration branch), not literal ``main``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_adrian_kanban_s2 import (
    _commit_workspace_feature,
    _complete_journaled_member_merge,
    _disposable_repository,
    _git,
    _journal_connection,
    _mark_workspace_member_materialized,
    _materialized_workspace_member,
)

INTEGRATION_BRANCH = "develop"

def _provider_modules() -> dict[str, Any]:
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    name = "compat_slice_workspace"
    if name in sys.modules:
        return {m: sys.modules[f"{name}.{m}"] for m in ("workspace", "schema", "journal", "phase_delivery")}
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)])
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return {m: importlib.import_module(f"{name}.{m}") for m in ("workspace", "schema", "journal", "phase_delivery")}

@pytest.fixture(scope="module")
def provider_modules():
    yield _provider_modules()

def _git_head_of_default_branch(repository):
    """Remote default-branch head.  The origin URL is the canonical GitHub
    form; insteadOf keeps real fetches on the local file."""
    _git(repository, "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
    _git(repository, "fetch", "origin", "main")
    return _git(repository, "rev-parse", "refs/remotes/origin/main")

def _repository_with_develop(provider_modules, tmp_path):
    """Disposable repo with a ``develop`` branch pushed to origin; default
    is ``main``.  Returns repo, remote path, default-branch head, develop
    head."""
    workspace_mod = provider_modules["workspace"]
    repository, base_head, remote = _disposable_repository(tmp_path)
    # _disposable_repository already sets origin to the canonical GitHub URL
    # with an insteadOf rewrite to the local file.  Push develop directly.
    _git(repository, "checkout", "-b", INTEGRATION_BRANCH)
    (repository / "develop.txt").write_text("develop\n", encoding="utf-8")
    _git(repository, "add", "develop.txt")
    _git(repository, "commit", "-m", "develop base")
    _git(repository, "push", "origin", INTEGRATION_BRANCH)
    _git(repository, "checkout", INTEGRATION_BRANCH)
    default_head = _git_head_of_default_branch(repository)
    integration_head = _git(repository, "rev-parse", INTEGRATION_BRANCH)
    return repository, remote, default_head, integration_head

def _workspace_plan(provider_modules, tmp_path):
    workspace_mod = provider_modules["workspace"]
    conn = _journal_connection(provider_modules["schema"])
    repository, remote, default_head, base_sha = _repository_with_develop(
        provider_modules, tmp_path)
    conn.execute(
        "UPDATE segment_workspace_members SET required_base_sha = ? "
        "WHERE workspace_id = ? AND repository_identity = ?",
        (base_sha, "workspace-1", "repo-1"),
    )
    registry = workspace_mod._TrustedRepositoryRegistry(
        (workspace_mod._RepositoryRegistration(
            repository_identity="repo-1",
            repository_root=str(repository),
            controlled_worktree_root=str(repository / ".segment-worktrees"),
            github_repository="Adrian-D-Lin/GRC",
            integration_branch=INTEGRATION_BRANCH,
        ),)
    )
    plan = workspace_mod._SegmentWorkspaceController(conn, registry).load(
        workspace_id="workspace-1",
        expected_initiative_id="initiative-1",
        expected_segment_id="S1",
        expected_controller_binding="controller-1",
    )
    return conn, repository, remote, default_head, base_sha, plan

def test_materialize_cached_develop_base(provider_modules, tmp_path):
    """Pinned base must equal the cached remote-tracking ref of the
    configured integration branch; missing ref or default-branch head both
    fail closed."""
    workspace_mod = provider_modules["workspace"]
    conn, repository, remote, default_head, base_sha, plan = _workspace_plan(
        provider_modules, tmp_path)
    executor = workspace_mod._GitWorkspaceExecutor()
    ok = executor.materialize(plan.members[0])
    assert ok.ready is True
    assert ok.branch_matches is True
    assert ok.observed_head == base_sha
    stale = replace(plan.members[0], required_base_sha=default_head)
    with pytest.raises(
        workspace_mod._WorkspaceRejected,
        match=f"cached origin/{INTEGRATION_BRANCH} does not match",
    ):
        executor.materialize(stale)
    _git(repository, "update-ref", "-d", f"refs/remotes/origin/{INTEGRATION_BRANCH}")
    with pytest.raises(
        workspace_mod._WorkspaceRejected,
        match=f"cached origin/{INTEGRATION_BRANCH} tracking ref is missing",
    ):
        executor.materialize(plan.members[0])
    _git(repository, "worktree", "remove", "--force", plan.members[0].target_path)
    conn.close()

def test_freshness_fast_forward_follows_develop_not_main(provider_modules, tmp_path):
    """Online freshness/FF track the configured branch; default-branch
    advancement is not surfaced."""
    workspace_mod = provider_modules["workspace"]
    conn, repository, remote, default_head, base_sha, plan = _workspace_plan(
        provider_modules, tmp_path)
    executor = workspace_mod._GitWorkspaceExecutor()
    executor.materialize(plan.members[0])
    member = _materialized_workspace_member(plan.members[0], base_sha)
    _git(repository, "checkout", "main")
    (repository / "main-advance.txt").write_text("main\n", encoding="utf-8")
    _git(repository, "add", "main-advance.txt")
    _git(repository, "commit", "-m", "advance main")
    _git(repository, "push", "origin", "main")
    unchanged = executor.inspect_freshness(member)
    assert unchanged.ready is True
    assert unchanged.remote_head == base_sha
    _git(repository, "checkout", INTEGRATION_BRANCH)
    (repository / "develop-advance.txt").write_text("develop\n", encoding="utf-8")
    _git(repository, "add", "develop-advance.txt")
    _git(repository, "commit", "-m", "advance develop")
    _git(repository, "push", "origin", INTEGRATION_BRANCH)
    develop_head = _git(repository, "rev-parse", INTEGRATION_BRANCH)
    advanced = executor.inspect_freshness(member)
    assert advanced.ready is True
    assert advanced.local_head == base_sha
    assert advanced.remote_head == develop_head
    assert advanced.local_is_ancestor_of_remote is True
    assert advanced.remote_is_ancestor_of_local is False
    fast_forwarded = executor.fast_forward_to_remote(
        member, expected_local_head=base_sha, expected_remote_head=develop_head)
    assert fast_forwarded.ready is True
    assert fast_forwarded.observed_head == develop_head
    conn.close()

def test_merge_on_develop_diagnoses_with_remote_label(provider_modules, tmp_path):
    """verify/merge run in the canonical integration checkout: merge lands
    on the configured branch, push targets its remote ref, and divergence
    is diagnosed with the registered remote label."""
    workspace_mod = provider_modules["workspace"]
    journal_mod = provider_modules["journal"]
    conn, repository, remote, default_head, base_sha, plan = _workspace_plan(
        provider_modules, tmp_path)
    executor = workspace_mod._GitWorkspaceExecutor()
    journal = journal_mod.ExternalOperationJournal(conn)
    operations = workspace_mod._JournaledWorkspaceOperations(conn, journal, executor)
    member = plan.members[0]
    executor.materialize(member)
    absent = executor.verify_merge(
        member, expected_main_sha=base_sha, expected_source_head=base_sha)
    assert absent.ready is False
    assert absent.failures == ("merge_absent",)
    source_head = _commit_workspace_feature(member)
    _mark_workspace_member_materialized(conn, member, source_head)
    conn.execute(
        "UPDATE segment_workspaces SET lifecycle_state = 'active' "
        "WHERE workspace_id = ?",
        (plan.workspace_id,),
    )
    _complete_journaled_member_merge(
        operations, journal_mod, member, base_sha, source_head,
        operation_id="merge-develop-1", at=3_000)
    merge_head = _git(repository, "rev-parse", INTEGRATION_BRANCH)
    assert _git(repository, "rev-parse", f"refs/remotes/origin/{INTEGRATION_BRANCH}") == merge_head
    assert _git(repository, "rev-parse", "main") != merge_head
    delivery_mod = provider_modules["phase_delivery"]
    assert delivery_mod._verify_remote_integration_contains(
        executor, member, source_head) == merge_head
    other = tmp_path / "remote-writer"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.name", "Remote Writer")
    _git(other, "config", "user.email", "remote@example.invalid")
    _git(other, "checkout", INTEGRATION_BRANCH)
    (other / "remote.txt").write_text("remote advance\n", encoding="utf-8")
    _git(other, "add", "remote.txt")
    _git(other, "commit", "-m", "remote advance")
    _git(other, "push", "origin", INTEGRATION_BRANCH)
    target = Path(member.target_path)
    (target / "feature2.txt").write_text("feature2\n", encoding="utf-8")
    _git(target, "add", "feature2.txt")
    _git(target, "commit", "-m", "second feature")
    second_head = _git(target, "rev-parse", "HEAD")
    with pytest.raises(
        workspace_mod._WorkspaceRejected,
        match=f"origin/{INTEGRATION_BRANCH} has diverged",
    ):
        executor.merge_to_origin_main(
            member, expected_main_sha=merge_head, expected_source_head=second_head)
    assert _git(repository, "rev-parse", INTEGRATION_BRANCH) == merge_head
    conn.close()
