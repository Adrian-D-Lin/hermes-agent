"""Fixed lifecycle-output validator tests from Kanban v0.28 section 8.3.

The registry names product-owned validators.  These tests deliberately verify
structure only: persuasiveness and substantive correctness remain reviewer
judgements.
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_output_validators"
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
        "validators": importlib.import_module(
            f"{package_name}.output_validators"
        ),
    }
    yield loaded
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


SAMPLES = {
    "d2_review_v1": {
        "review_pass_log": ["pass-1"],
        "angle_coverage": [
            "principle_alignment",
            "design_integration",
            "documentation_silence",
            "contradiction",
            "completeness_internal_coherence",
            "dependencies_downstream_impact",
            "new_principle_candidate",
            "alternative_design",
        ],
        "findings": [
            {
                "citation": "design.md@sha#L1",
                "materiality": "material",
                "impact": "incorrect route",
                "route": "D1",
            }
        ],
        "conclusion": "NOT_DRY",
    },
    "d4_1_edit_set_v1": {
        "edit_set": [
            {
                "document_ref": "Canon/a.md@sha",
                "current_state_ref": "sha256:abc",
                "edit": "replace rule",
            }
        ]
    },
    "d4_2_verification_v1": {
        "edit_findings": [
            {
                "edit_ref": "edit-1",
                "consistency": "consistent",
                "collateral_changes": [],
            }
        ],
        "conclusion": "ACCEPTED",
        "item_count": 1,
    },
    "d4_5_post_write_v1": {
        "document_verifications": [
            {"path": "Canon/a.md", "sha": "abc", "result": "MATCH"}
        ],
        "source_item_count": 1,
        "determination_count": 1,
        "development_baseline_ref": "design.md@sha",
    },
    "dev1_angle_v1": {
        "angle": "design_source",
        "findings": [{"citation": "design.md@sha", "finding": "gap"}],
        "uncertainties": [],
        "assumptions": [],
        "probes": [],
    },
    "dev1_4_design_to_scope_v1": {
        "conclusion": "PASSED",
        "findings": [],
        "return_route": "DEV1.3",
    },
    "dev1_5_segmentation_v1": {
        "segments": [
            {
                "segment_id": "S1",
                "boundary": "plugin core",
                "dependency_ids": [],
                "ordering_rationale": "foundation first",
                "progressive_verification": "unit and integration tests",
            }
        ]
    },
    "dev1_6_segment_review_v1": {
        "conclusion": "RATIFIED",
        "findings": [],
        "required_revisions": [],
    },
    "dev2_1_brief_v1": {
        "brief_ref": "brief.md@sha",
        "source_inventory": ["design.md@sha"],
        "file_change_map": [{"path": "module.py", "action": "create"}],
        "test_plan": ["happy", "unhappy", "fringe"],
        "exit_criteria": ["all tests pass"],
        "risks": [],
    },
    "dev2_2_design_to_brief_v1": {
        "findings": [
            {
                "citation": "design.md@sha",
                "impact": "missing behavior",
                "route": "DEV2.1",
            }
        ],
        "conclusion": "FINDINGS",
    },
    "dev3_1_build_v1": {
        "build_ref": "commit:abc",
        "changed_files": ["module.py"],
        "implementation_evidence": ["test-output.txt@sha"],
    },
    "dev3_2_candidate_tests_v1": {
        "tests_ref": "tests@sha",
        "suites": ["happy", "unhappy", "fringe"],
        "fixtures": ["valid", "invalid"],
    },
    "dev3_3_design_to_tests_v1": {
        "findings": [],
        "conclusion": "PASSED",
    },
    "dev3_4_test_execution_v1": {
        "execution_ref": "run-1",
        "suite_results": [
            {"suite": "happy", "passed": 2, "failed": 0},
            {"suite": "unhappy", "passed": 2, "failed": 0},
            {"suite": "fringe", "passed": 2, "failed": 0},
        ],
        "failures": [],
    },
    "dev3_5_code_review_v1": {
        "findings": [
            {
                "file": "module.py",
                "line": 4,
                "severity": "material",
                "issue": "gate omitted",
                "expected": "fail closed",
            }
        ],
        "conclusion": "CHANGES_REQUIRED",
    },
    "dev3_6_test_results_review_v1": {
        "findings": [],
        "conclusion": "PASSED",
    },
    "dev3_7_independent_code_review_v1": {
        "findings": [],
        "conclusion": "PASSED",
    },
    "dev3_8_review_synthesis_v1": {
        "dispositions": [
            {"finding_ref": "finding-1", "disposition": "remediate", "route": "DEV3.10"}
        ],
        "unresolved_count": 1,
    },
    "dev3_10_revision_brief_v1": {
        "revision_brief_ref": "revision.md@sha",
        "finding_changes": [
            {"finding_ref": "finding-1", "required_change": "add gate"}
        ],
    },
    "dev3_11_revision_build_v1": {
        "build_ref": "commit:def",
        "changed_files": ["module.py"],
        "revision_evidence": ["test-output-2.txt@sha"],
    },
    "dev4_1a_archive_scan_v1": {
        "candidates": [
            {"path": "old.py", "evidence": "unused", "disposition": "archive"}
        ]
    },
    "dev4_1b_archive_verification_v1": {
        "overreach_findings": [],
        "underreach_findings": [],
        "conclusion": "VERIFIED",
    },
    "dev4_1c_archive_execution_v1": {
        "executed_dispositions": [
            {"path": "old.py", "disposition": "archive", "result": "completed"}
        ]
    },
    "dev4_2a_ratification_package_v1": {
        "observations": [
            {
                "observation": "design rule should be explicit",
                "evidence": "module.py@sha#L4",
                "recommendation": "reflect",
            }
        ]
    },
    "pc1_1a_parity_matrix_v1": {
        "rows": [
            {
                "requirement": "fail closed",
                "design_ref": "design.md@sha",
                "implementation_ref": "module.py@sha#L4",
                "evidence": "test.py@sha",
                "conclusion": "PARITY",
            }
        ]
    },
    "pc1_1b_matrix_verification_v1": {
        "verified_rows": ["row-1"],
        "corrected_rows": [],
        "gaps": [],
        "conclusion": "VERIFIED",
    },
}


def test_every_registry_validator_is_implemented(modules):
    contracts = modules["contracts"]
    names = {contracts.template_for(step).output_validator for step in (
        "D2", "D4.1", "D4.2", "D4.5",
        "DEV1.1a", "DEV1.1b", "DEV1.1c", "DEV1.1d", "DEV1.1e",
        "DEV1.4", "DEV1.5", "DEV1.6", "DEV2.1", "DEV2.2",
        "DEV3.1", "DEV3.2", "DEV3.3", "DEV3.4", "DEV3.5", "DEV3.6",
        "DEV3.7", "DEV3.8", "DEV3.10", "DEV3.11", "DEV4.1a",
        "DEV4.1b", "DEV4.1c", "DEV4.2a", "PC1.1a", "PC1.1b",
    )}
    assert names == set(SAMPLES)
    assert modules["validators"].validator_names() == frozenset(SAMPLES)


@pytest.mark.parametrize("validator_name", sorted(SAMPLES))
def test_fixed_validator_accepts_a_structurally_complete_output(
    modules, validator_name
):
    assert modules["validators"].validate_lifecycle_output(
        validator_name, copy.deepcopy(SAMPLES[validator_name])
    ) == SAMPLES[validator_name]


@pytest.mark.parametrize("validator_name", sorted(SAMPLES))
def test_fixed_validator_rejects_each_missing_top_level_field(
    modules, validator_name
):
    sample = copy.deepcopy(SAMPLES[validator_name])
    removed = next(iter(sample))
    sample.pop(removed)
    with pytest.raises(modules["validators"].OutputValidationRejected) as raised:
        modules["validators"].validate_lifecycle_output(validator_name, sample)
    assert any(finding.field == removed for finding in raised.value.findings)


@pytest.mark.parametrize("validator_name", sorted(SAMPLES))
def test_fixed_validator_rejects_wrong_top_level_types(modules, validator_name):
    sample = copy.deepcopy(SAMPLES[validator_name])
    first = next(iter(sample))
    sample[first] = object()
    with pytest.raises(modules["validators"].OutputValidationRejected):
        modules["validators"].validate_lifecycle_output(validator_name, sample)


def test_d2_enforces_exact_angle_coverage_and_nested_finding_fields(modules):
    validators = modules["validators"]
    duplicate = copy.deepcopy(SAMPLES["d2_review_v1"])
    duplicate["angle_coverage"][-1] = duplicate["angle_coverage"][0]
    with pytest.raises(validators.OutputValidationRejected) as raised:
        validators.validate_lifecycle_output("d2_review_v1", duplicate)
    assert any(finding.field == "angle_coverage" for finding in raised.value.findings)

    incomplete = copy.deepcopy(SAMPLES["d2_review_v1"])
    incomplete["findings"][0].pop("citation")
    with pytest.raises(validators.OutputValidationRejected) as raised:
        validators.validate_lifecycle_output("d2_review_v1", incomplete)
    assert any(finding.field == "findings[0].citation" for finding in raised.value.findings)


def test_nested_records_are_checked_but_empty_lists_remain_valid(modules):
    validators = modules["validators"]
    invalid = copy.deepcopy(SAMPLES["dev3_5_code_review_v1"])
    invalid["findings"] = ["not-a-record"]
    with pytest.raises(validators.OutputValidationRejected) as raised:
        validators.validate_lifecycle_output("dev3_5_code_review_v1", invalid)
    assert any(finding.field == "findings[0]" for finding in raised.value.findings)

    no_findings = copy.deepcopy(SAMPLES["dev3_5_code_review_v1"])
    no_findings["findings"] = []
    no_findings["conclusion"] = "PASSED"
    assert validators.validate_lifecycle_output(
        "dev3_5_code_review_v1", no_findings
    ) == no_findings


def test_text_lists_reject_non_text_and_blank_members(modules):
    validators = modules["validators"]
    for invalid_members in ([1], ["   "]):
        sample = copy.deepcopy(SAMPLES["dev3_1_build_v1"])
        sample["changed_files"] = invalid_members
        with pytest.raises(validators.OutputValidationRejected) as raised:
            validators.validate_lifecycle_output("dev3_1_build_v1", sample)
        assert any(
            finding.field == "changed_files[0]"
            for finding in raised.value.findings
        )


def test_unknown_validator_and_non_object_fail_without_echoing_values(modules):
    validators = modules["validators"]
    with pytest.raises(validators.OutputValidationRejected) as unknown:
        validators.validate_lifecycle_output("caller_defined", {"secret": "value"})
    assert "caller_defined" not in str(unknown.value)
    assert "value" not in str(unknown.value)

    with pytest.raises(validators.OutputValidationRejected) as non_object:
        validators.validate_lifecycle_output("d2_review_v1", ["secret-value"])
    assert "secret-value" not in str(non_object.value)


def test_extra_fields_are_preserved_for_substantive_reviewer_context(modules):
    validators = modules["validators"]
    sample = copy.deepcopy(SAMPLES["dev3_1_build_v1"])
    sample["reviewer_note"] = "substantive context"
    validated = validators.validate_lifecycle_output("dev3_1_build_v1", sample)
    assert validated["reviewer_note"] == "substantive context"
