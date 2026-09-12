"""Verified lifecycle-input and bundled-skill tests from Kanban v0.28 §8.1."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_task_inputs"
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
        "contracts": importlib.import_module(f"{package_name}.contracts"),
        "skill_bundle": importlib.import_module(f"{package_name}.skill_bundle"),
        "task_inputs": importlib.import_module(f"{package_name}.task_inputs"),
    }
    yield loaded
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


_EXPECTED_PHASE_SKILLS = {
    "D1": ("d1-design-concept", "adrian-kanban.lifecycle.d1"),
    "D2": ("d2-iterative-review", "adrian-kanban.lifecycle.d2"),
    "D3": ("d3-human-ratification", "adrian-kanban.lifecycle.d3"),
    "D4": ("d4-write-gated-integration", "adrian-kanban.lifecycle.d4"),
    "DEV1": ("dev1-scoping", "adrian-kanban.lifecycle.dev1"),
    "DEV2": ("dev2-implementation-brief", "adrian-kanban.lifecycle.dev2"),
    "DEV3": ("dev3-orchestration", "adrian-kanban.lifecycle.dev3"),
    "DEV4": ("dev4-closure", "adrian-kanban.lifecycle.dev4"),
    "PC1": ("pc1-parity-review", "adrian-kanban.lifecycle.pc1"),
}


@pytest.mark.parametrize("phase", tuple(_EXPECTED_PHASE_SKILLS))
def test_pinned_skill_bundle_resolves_exact_phase_contract_and_content_hash(
    modules, phase
):
    skill_bundle = modules["skill_bundle"]
    skill_id, contract_id = _EXPECTED_PHASE_SKILLS[phase]

    binding = skill_bundle.resolve_skill_binding(phase)
    skill_path = Path(skill_bundle.__file__).parent / "skills" / skill_id / "SKILL.md"
    content = skill_path.read_bytes()

    assert binding.skill_id == skill_id
    assert binding.skill_version == "0.2.0"
    assert binding.skill_hash == hashlib.sha256(content).hexdigest()
    assert skill_bundle.skill_contract(phase) == (contract_id, "1")
    text = content.decode("utf-8")
    assert f"kanban_phase: {phase}" in text
    assert f"kanban_contract_id: {contract_id}" in text
    assert 'kanban_contract_version: "1"' in text


def test_skill_bundle_is_complete_deterministic_and_rejects_unknown_phase(modules):
    skill_bundle = modules["skill_bundle"]

    assert tuple(skill_bundle.PHASE_SKILLS) == tuple(_EXPECTED_PHASE_SKILLS)
    assert skill_bundle.validate_skill_bundle() is True
    assert skill_bundle.skill_bundle_hash() == skill_bundle.skill_bundle_hash()
    assert len(skill_bundle.skill_bundle_hash()) == 64
    with pytest.raises(ValueError, match="unknown phase"):
        skill_bundle.resolve_skill_binding("DEV5")


def _git_manifest(*, content=b"canonical source", guidance="Use as oracle"):
    path = "Canon/design-lifecycle.md"
    digest = hashlib.sha256(content).hexdigest()
    commit = "a" * 40
    return {
        "version": 1,
        "entries": [
            {
                "workspace_path": path,
                "sha256": digest,
                "source_kind": "git_commit",
                "source_locator": commit,
            }
        ],
        "context_ref": {path: guidance},
        "snapshots": {},
    }, commit, path, content


def test_git_manifest_is_verified_and_canonicalized_without_copying_bytes(modules):
    task_inputs = modules["task_inputs"]
    manifest, commit, path, content = _git_manifest()
    reads = []

    def read_git_blob(observed_commit, observed_path):
        reads.append((observed_commit, observed_path))
        return content

    prepared = task_inputs.prepare_task_input_manifest(manifest, read_git_blob)
    finalized = task_inputs.finalize_task_input_manifest(prepared, {})

    assert reads == [(commit, path)]
    assert prepared.declared_inputs_accessible is True
    assert prepared.entries[0].snapshot_bytes is None
    assert finalized.entries[0].source_locator == commit
    assert finalized.entries[0].context_guidance == "Use as oracle"
    assert json.loads(finalized.canonical_payload) == {
        "context_ref": {path: "Use as oracle"},
        "entries": [
            {
                "sha256": hashlib.sha256(content).hexdigest(),
                "source_kind": "git_commit",
                "source_locator": commit,
                "workspace_path": path,
            }
        ],
        "version": 1,
    }


def test_snapshot_manifest_verifies_named_bytes_then_binds_attachment_id(modules):
    task_inputs = modules["task_inputs"]
    path = "2-design/S1/review.md"
    content = b"immutable review snapshot"
    manifest = {
        "version": 1,
        "entries": [
            {
                "workspace_path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "source_kind": "snapshot_attachment",
            }
        ],
        "context_ref": {path: "Use as the prior review record"},
        "snapshots": {
            path: {
                "filename": "review.md",
                "content_type": "text/markdown",
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        },
    }

    prepared = task_inputs.prepare_task_input_manifest(
        manifest,
        lambda _commit, _path: pytest.fail("snapshot must not read Git"),
    )
    assert prepared.entries[0].snapshot_bytes == content
    assert prepared.entries[0].filename == "review.md"
    assert prepared.entries[0].content_type == "text/markdown"
    finalized = task_inputs.finalize_task_input_manifest(prepared, {path: 17})
    assert finalized.entries[0].source_locator == "17"
    assert "content_base64" not in finalized.canonical_payload


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda m: m.update(version=True), "version"),
        (lambda m: m.update(version=2), "version"),
        (
            lambda m: m["entries"][0].update(workspace_path="../secret"),
            "workspace_path",
        ),
        (
            lambda m: m["entries"].append(dict(m["entries"][0])),
            "duplicate",
        ),
        (lambda m: m.update(context_ref={}), "context_ref"),
        (
            lambda m: m["entries"][0].update(sha256="A" * 64),
            "sha256",
        ),
        (
            lambda m: m["entries"][0].update(source_kind="url"),
            "source_kind",
        ),
    ),
)
def test_manifest_rejects_noncanonical_structure(modules, mutate, message):
    task_inputs = modules["task_inputs"]
    manifest, _commit, _path, content = _git_manifest()
    mutate(manifest)
    with pytest.raises(ValueError, match=message):
        task_inputs.prepare_task_input_manifest(
            manifest,
            lambda _commit, _path: content,
        )


def test_manifest_rejects_digest_mismatch_and_mixed_git_commits(modules):
    task_inputs = modules["task_inputs"]
    manifest, _commit, _path, _content = _git_manifest()
    with pytest.raises(ValueError, match="digest"):
        task_inputs.prepare_task_input_manifest(
            manifest,
            lambda _commit, _path: b"different bytes",
        )


def test_manifest_requires_an_entry_but_allows_repeated_safe_path_components(modules):
    task_inputs = modules["task_inputs"]
    with pytest.raises(ValueError, match="entries"):
        task_inputs.prepare_task_input_manifest(
            {"version": 1, "entries": [], "context_ref": {}, "snapshots": {}},
            lambda _commit, _path: b"",
        )

    manifest, _commit, old_path, content = _git_manifest()
    repeated_path = "folder/folder/source.md"
    manifest["entries"][0]["workspace_path"] = repeated_path
    manifest["context_ref"] = {
        repeated_path: manifest["context_ref"].pop(old_path)
    }
    prepared = task_inputs.prepare_task_input_manifest(
        manifest,
        lambda _commit, _path: content,
    )
    assert prepared.entries[0].workspace_path == repeated_path

    second = dict(manifest["entries"][0])
    second.update(
        workspace_path="Canon/write-gate-policy.md",
        source_locator="b" * 40,
    )
    manifest["entries"].append(second)
    manifest["context_ref"][second["workspace_path"]] = "Use as policy"
    with pytest.raises(ValueError, match="same commit"):
        task_inputs.prepare_task_input_manifest(
            manifest,
            lambda _commit, _path: b"canonical source",
        )


def test_snapshot_manifest_rejects_missing_extra_or_premature_locator(modules):
    task_inputs = modules["task_inputs"]
    content = b"snapshot"
    path = "record.md"
    base = {
        "version": 1,
        "entries": [
            {
                "workspace_path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "source_kind": "snapshot_attachment",
            }
        ],
        "context_ref": {path: "Use this record"},
        "snapshots": {
            path: {
                "filename": "record.md",
                "content_type": "text/markdown",
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        },
    }
    for mutation in ("missing", "extra", "locator"):
        candidate = json.loads(json.dumps(base))
        if mutation == "missing":
            candidate["snapshots"] = {}
        elif mutation == "extra":
            candidate["snapshots"]["extra.md"] = candidate["snapshots"][path]
        else:
            candidate["entries"][0]["source_locator"] = "17"
        with pytest.raises(ValueError):
            task_inputs.prepare_task_input_manifest(
                candidate,
                lambda _commit, _path: b"",
            )


def test_manifest_finalization_requires_exact_positive_attachment_mapping(modules):
    task_inputs = modules["task_inputs"]
    content = b"snapshot"
    path = "record.md"
    manifest = {
        "version": 1,
        "entries": [
            {
                "workspace_path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "source_kind": "snapshot_attachment",
            }
        ],
        "context_ref": {path: "Use this record"},
        "snapshots": {
            path: {
                "filename": "record.md",
                "content_type": "text/markdown",
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        },
    }
    prepared = task_inputs.prepare_task_input_manifest(
        manifest,
        lambda _commit, _path: b"",
    )
    for mapping in ({}, {path: 0}, {path: 1, "extra.md": 2}):
        with pytest.raises(ValueError, match="attachment"):
            task_inputs.finalize_task_input_manifest(prepared, mapping)


def test_lifecycle_contract_references_match_manifest_paths_exactly(modules):
    contracts = modules["contracts"]
    task_inputs = modules["task_inputs"]
    manifest, _commit, path, content = _git_manifest()
    prepared = task_inputs.prepare_task_input_manifest(
        manifest,
        lambda _commit, _path: content,
    )
    snapshot = contracts.expand_contract(
        step="D2",
        initiative_id="initiative-1",
        baseline_refs=(path,),
        governing_source_refs=("Canon/other.md",),
    )
    with pytest.raises(ValueError, match="coverage"):
        task_inputs.validate_contract_input_coverage(snapshot, prepared)

    snapshot = contracts.expand_contract(
        step="D2",
        initiative_id="initiative-1",
        baseline_refs=(path,),
        governing_source_refs=(path,),
    )
    assert task_inputs.validate_contract_input_coverage(snapshot, prepared) is True
