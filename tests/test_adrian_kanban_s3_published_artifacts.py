"""Immutable baseline evidence tests from Kanban v0.28 sections 8.3.3-8.3.4."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    name = "s3_published_artifacts"
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    yield {
        key: importlib.import_module(f"{name}.{key}")
        for key in ("published_artifact", "segment_manifest")
    }
    for key in tuple(sys.modules):
        if key == name or key.startswith(name + "."):
            sys.modules.pop(key, None)


def _reference():
    return {
        "path": "2-design/draft.md",
        "commit": "a" * 40,
        "sha256": hashlib.sha256(b"draft").hexdigest(),
    }


@pytest.mark.parametrize("commit_length", [40, 64])
def test_verified_artifact_is_frozen_and_pinned_without_retaining_content(
    modules, commit_length
):
    reference = _reference()
    reference["commit"] = "a" * commit_length
    calls = []
    proof = modules["published_artifact"].verify_published_artifact(
        reference, lambda commit, path: calls.append((commit, path)) or b"draft"
    )
    assert calls == [(reference["commit"], reference["path"])]
    assert dataclasses.asdict(proof) == reference
    with pytest.raises(dataclasses.FrozenInstanceError):
        proof.commit = "b" * 40
    reference["path"] = "mutated.md"
    assert proof.path == "2-design/draft.md"


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "../escape.md"),
        ("path", "/absolute.md"),
        ("path", "C:\\escape.md"),
        ("path", "with/./dot.md"),
        ("path", "trailing.md\n"),
        ("commit", "abc"),
        ("commit", "a" * 41),
        ("commit", "a" * 40 + "\n"),
        ("commit", "A" * 40),
        ("sha256", "b" * 64 + "\n"),
        ("sha256", None),
    ],
)
def test_invalid_artifact_reference_rejects_before_git_read(modules, field, value):
    reference = _reference()
    reference[field] = value
    calls = []
    with pytest.raises(ValueError):
        modules["published_artifact"].verify_published_artifact(
            reference, lambda *args: calls.append(args) or b"draft"
        )
    assert calls == []


@pytest.mark.parametrize("reference", [None, [], {}, {**_reference(), "unknown": True}])
def test_artifact_shape_is_fixed(modules, reference):
    with pytest.raises(ValueError):
        modules["published_artifact"].verify_published_artifact(
            reference, lambda *_: b"draft"
        )


def test_digest_mismatch_never_returns_proof_or_echoes_document(modules):
    with pytest.raises(ValueError) as failure:
        modules["published_artifact"].verify_published_artifact(
            _reference(), lambda *_: b"private document bytes"
        )
    assert "private" not in str(failure.value)
    assert _reference()["path"] not in str(failure.value)
    assert _reference()["commit"] not in str(failure.value)
    assert "retry" in str(failure.value)
    assert any(
        word in str(failure.value).lower() for word in ("digest", "sha256", "hash")
    )


@pytest.mark.parametrize("content", ["draft", None, bytearray(b"draft")])
def test_nonbytes_reader_cannot_produce_verified_artifact(modules, content):
    with pytest.raises((TypeError, ValueError)):
        modules["published_artifact"].verify_published_artifact(
            _reference(), lambda *_: content
        )


def test_reader_contract_and_safe_failure_propagation(modules):
    with pytest.raises(TypeError):
        modules["published_artifact"].verify_published_artifact(_reference(), None)
    original = ValueError("origin/main unavailable; verify connection and retry")

    def fail(*_):
        raise original

    with pytest.raises(ValueError) as raised:
        modules["published_artifact"].verify_published_artifact(_reference(), fail)
    assert raised.value is original


@pytest.mark.parametrize(
    "validator,length",
    [("_validate_sha", 40), ("_validate_commit", 40), ("_validate_sha256", 64)],
)
def test_shared_hash_validators_reject_trailing_newline(modules, validator, length):
    with pytest.raises(ValueError):
        getattr(modules["segment_manifest"], validator)(
            "a" * length + "\n", "reference"
        )
