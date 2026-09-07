"""Trusted Git-input and pre-tool tests derived from Kanban v0.28 §8.1."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_pre_tool_inputs"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    loaded = {
        "pre_tool_hook": importlib.import_module(f"{package_name}.pre_tool_hook"),
        "task_inputs": importlib.import_module(f"{package_name}.task_inputs"),
    }
    yield loaded
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


def _git_repository(tmp_path):
    repository = tmp_path / "project"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repository, check=True
    )
    path = repository / "Canon" / "policy.md"
    path.parent.mkdir()
    original = b"immutable policy\n"
    path.write_bytes(original)
    subprocess.run(["git", "add", "--", "Canon/policy.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repository, commit, original


def _manifest(commit, content):
    return {
        "version": 1,
        "entries": [
            {
                "workspace_path": "Canon/policy.md",
                "sha256": hashlib.sha256(content).hexdigest(),
                "source_kind": "git_commit",
                "source_locator": commit,
            }
        ],
        "context_ref": {"Canon/policy.md": "Apply as the policy oracle"},
        "snapshots": {},
    }


class _Binding:
    def __init__(self, worktree_path):
        self.worktree_path = str(worktree_path)


class _Registry:
    def __init__(self, binding):
        self.binding = binding

    def get_active_binding(self, session_id):
        return self.binding if session_id == "session-1" else None


def _context(task_inputs):
    return task_inputs.TaskInputPreparationContext(
        session_id="session-1",
        execution_context="model-tool",
        workspace_id=None,
        actor_profile="independent-reviewer",
    )


def test_git_preparer_reads_immutable_commit_not_mutable_worktree(
    modules, tmp_path
):
    repository, commit, original = _git_repository(tmp_path)
    (repository / "Canon" / "policy.md").write_bytes(b"uncommitted change\n")
    pre_tool = modules["pre_tool_hook"]
    preparer = pre_tool.GitTaskInputPreparer(
        registry_getter=lambda: _Registry(_Binding(repository))
    )

    prepared = preparer(
        {"task_input_manifest_v1": _manifest(commit, original)},
        _context(modules["task_inputs"]),
    )

    assert prepared.declared_inputs_accessible is True
    assert prepared.entries[0].source_locator == commit
    assert prepared.entries[0].snapshot_bytes is None


def test_snapshot_only_preparation_never_resolves_git_binding(modules):
    content = b"external evidence\n"
    path = "evidence/vendor-note.txt"
    manifest = {
        "version": 1,
        "entries": [
            {
                "workspace_path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "source_kind": "snapshot_attachment",
            }
        ],
        "context_ref": {path: "Treat as immutable supplied evidence"},
        "snapshots": {
            path: {
                "filename": "vendor-note.txt",
                "content_type": "text/plain",
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        },
    }
    preparer = modules["pre_tool_hook"].GitTaskInputPreparer(
        registry_getter=lambda: pytest.fail("must not resolve Git")
    )

    prepared = preparer(
        {"task_input_manifest_v1": manifest},
        _context(modules["task_inputs"]),
    )

    assert prepared.declared_inputs_accessible is True
    assert prepared.entries[0].snapshot_bytes == content


@pytest.mark.parametrize("defect", ("missing_commit", "missing_path", "wrong_digest"))
def test_git_preparer_rejects_unavailable_or_mismatched_object(
    modules, tmp_path, defect
):
    repository, commit, original = _git_repository(tmp_path)
    manifest = _manifest(commit, original)
    if defect == "missing_commit":
        manifest["entries"][0]["source_locator"] = "f" * 40
    elif defect == "missing_path":
        manifest["entries"][0]["workspace_path"] = "Canon/missing.md"
        manifest["context_ref"] = {
            "Canon/missing.md": manifest["context_ref"].pop("Canon/policy.md")
        }
    else:
        manifest["entries"][0]["sha256"] = "0" * 64
    preparer = modules["pre_tool_hook"].GitTaskInputPreparer(
        registry_getter=lambda: _Registry(_Binding(repository))
    )

    with pytest.raises(ValueError):
        preparer(
            {"task_input_manifest_v1": manifest},
            _context(modules["task_inputs"]),
        )


def test_git_preparer_rejects_missing_trusted_session_binding(modules, tmp_path):
    repository, commit, original = _git_repository(tmp_path)
    preparer = modules["pre_tool_hook"].GitTaskInputPreparer(
        registry_getter=lambda: _Registry(None)
    )

    with pytest.raises(ValueError, match="binding"):
        preparer(
            {"task_input_manifest_v1": _manifest(commit, original)},
            _context(modules["task_inputs"]),
        )


def test_pre_tool_hook_is_early_advice_without_argument_mutation(modules, tmp_path):
    repository, commit, original = _git_repository(tmp_path)
    pre_tool = modules["pre_tool_hook"]
    preparer = pre_tool.GitTaskInputPreparer(
        registry_getter=lambda: _Registry(_Binding(repository))
    )
    hook = pre_tool.build_pre_tool_hook(preparer)
    args = {
        "task_id": "task-1",
        "task_input_manifest_v1": _manifest(commit, original),
    }
    before = repr(args)

    assert hook(tool_name="kanban_create", args=args, session_id="session-1") is None
    assert repr(args) == before
    assert hook(tool_name="kanban_show", args=args, session_id="session-1") is None


def test_pre_tool_hook_returns_actionable_manifest_rejection(modules, tmp_path):
    repository, commit, original = _git_repository(tmp_path)
    manifest = _manifest(commit, original)
    manifest["entries"][0]["sha256"] = "0" * 64
    pre_tool = modules["pre_tool_hook"]
    hook = pre_tool.build_pre_tool_hook(
        pre_tool.GitTaskInputPreparer(
            registry_getter=lambda: _Registry(_Binding(repository))
        )
    )

    result = hook(
        tool_name="kanban_create",
        args={"task_input_manifest_v1": manifest},
        session_id="session-1",
    )

    assert result["action"] == "block"
    assert "declared_inputs_accessible = no" in result["message"]
    assert "Canon/policy.md" in result["message"]
    assert "digest mismatch" in result["message"]


def test_pre_tool_hook_blocks_lifecycle_create_without_manifest(modules):
    hook = modules["pre_tool_hook"].build_pre_tool_hook(
        modules["pre_tool_hook"].GitTaskInputPreparer(
            registry_getter=lambda: pytest.fail("must not resolve Git")
        )
    )

    result = hook(
        tool_name="kanban_create",
        args={"lifecycle_contract_v1": {"version": 1}},
        session_id="session-1",
    )

    assert result["action"] == "block"
    assert "task_input_manifest_v1" in result["message"]
