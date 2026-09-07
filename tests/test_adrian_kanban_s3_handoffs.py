from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def handoffs_module():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_handoffs"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    module = importlib.import_module(f"{package_name}.handoffs")
    yield module
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


def _valid_requirements():
    return {
        "version": 1,
        "reviewer": " default ",
        "fields": {
            " narrative ": {"type": "text"},
            "items": {"type": "list", "min_items": 1},
            "confirmed": {"type": "boolean"},
            "recommendation": {
                "type": "enum",
                "values": [" DRY ", "NOT DRY"],
            },
        },
    }


def test_normalizes_the_complete_fixed_handoff_language(handoffs_module):
    normalized = handoffs_module.normalize_handoff_requirements(
        _valid_requirements(),
        {"default", "independent-reviewer"},
    )
    assert normalized == {
        "version": 1,
        "reviewer": "default",
        "fields": {
            "narrative": {"type": "text"},
            "items": {"type": "list", "min_items": 1},
            "confirmed": {"type": "boolean"},
            "recommendation": {
                "type": "enum",
                "values": ["DRY", "NOT DRY"],
            },
        },
    }
    assert json.loads(
        handoffs_module.canonical_handoff_requirements(normalized)
    ) == normalized


def test_rejects_all_independent_declaration_defects_without_values(
    handoffs_module,
):
    malformed = {
        "version": True,
        "reviewer": "unknown-profile",
        "fields": {
            "text": {"type": "text", "extra": "secret-value"},
            "list": {"type": "list", "min_items": True},
            "enum": {"type": "enum", "values": ["same", " same "]},
            "other": {"type": "object"},
        },
        "extra": "secret-value",
    }
    with pytest.raises(handoffs_module.HandoffValidationRejected) as raised:
        handoffs_module.normalize_handoff_requirements(
            malformed,
            ("default",),
        )
    fields = [finding.field for finding in raised.value.findings]
    assert fields == [
        "requirements",
        "version",
        "reviewer",
        "text",
        "list",
        "enum",
        "other",
    ]
    assert "secret-value" not in str(raised.value)
    assert "unknown-profile" not in str(raised.value)


@pytest.mark.parametrize(
    "known_profiles",
    (
        ["default"],
        ("default", " default "),
        ("default", ""),
        ("default", 1),
    ),
)
def test_invalid_host_profile_configuration_raises_type_error(
    handoffs_module,
    known_profiles,
):
    with pytest.raises(TypeError):
        handoffs_module.normalize_handoff_requirements(
            _valid_requirements(),
            known_profiles,
        )


def test_candidate_validation_accepts_extra_fields_and_returns_a_copy(
    handoffs_module,
):
    requirements = handoffs_module.normalize_handoff_requirements(
        _valid_requirements(),
        ("default",),
    )
    metadata = {
        "narrative": "complete",
        "items": ["one"],
        "confirmed": False,
        "recommendation": "DRY",
        "extra_evidence": {"permitted": True},
    }
    accepted = handoffs_module.validate_candidate_metadata(
        requirements,
        metadata,
    )
    assert accepted == metadata
    assert accepted is not metadata


def test_candidate_validation_reports_every_missing_or_malformed_field(
    handoffs_module,
):
    requirements = handoffs_module.normalize_handoff_requirements(
        _valid_requirements(),
        ("default",),
    )
    with pytest.raises(handoffs_module.HandoffValidationRejected) as raised:
        handoffs_module.validate_candidate_metadata(
            requirements,
            {
                "narrative": " ",
                "items": [],
                "confirmed": 1,
            },
        )
    assert [finding.field for finding in raised.value.findings] == [
        "narrative",
        "items",
        "confirmed",
        "recommendation",
    ]


class _StringSubclass(str):
    pass


class _DictSubclass(dict):
    pass


def test_exact_type_rules_reject_subclasses(handoffs_module):
    malformed = _valid_requirements()
    malformed["reviewer"] = _StringSubclass("default")
    with pytest.raises(handoffs_module.HandoffValidationRejected):
        handoffs_module.normalize_handoff_requirements(
            malformed,
            ("default",),
        )

    requirements = handoffs_module.normalize_handoff_requirements(
        _valid_requirements(),
        ("default",),
    )
    with pytest.raises(handoffs_module.HandoffValidationRejected):
        handoffs_module.validate_candidate_metadata(
            requirements,
            _DictSubclass(),
        )
