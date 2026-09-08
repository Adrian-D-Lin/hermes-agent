"""Fixed lifecycle-output validators for Adrian Kanban.

Implements the product-owned structural validators required by Kanban v0.28
section 8.3.1. These validators check phase-defined structure only; they never
decide whether evidence is persuasive or a conclusion is substantively correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple


@dataclass(frozen=True)
class OutputFinding:
    """A single structural finding produced by a lifecycle-output validator."""

    field: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or not self.field.strip():
            raise ValueError("field must be a nonblank string")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a nonblank string")


class OutputValidationRejected(ValueError):
    """Raised when a lifecycle output fails structural validation."""

    def __init__(self, findings: Tuple[OutputFinding, ...]) -> None:
        if not findings:
            raise ValueError("findings must be a nonempty tuple")
        self.findings = findings
        super().__init__("; ".join(f"{item.field}: {item.reason}" for item in findings))


class _FieldSpec:
    __slots__ = (
        "type",
        "nonblank",
        "non_negative",
        "exact_set",
        "exact_value",
        "nested",
    )

    def __init__(
        self,
        type_: type,
        nonblank: bool = False,
        non_negative: bool = False,
        exact_set: Optional[FrozenSet[str]] = None,
        exact_value: Optional[str] = None,
        nested: Optional[Dict[str, _FieldSpec]] = None,
    ) -> None:
        self.type = type_
        self.nonblank = nonblank
        self.non_negative = non_negative
        self.exact_set = exact_set
        self.exact_value = exact_value
        self.nested = nested


def _text() -> _FieldSpec:
    return _FieldSpec(str, nonblank=True)


def _int_nonneg() -> _FieldSpec:
    return _FieldSpec(int, non_negative=True)


def _list_of_records(nested: Dict[str, _FieldSpec]) -> _FieldSpec:
    return _FieldSpec(list, nested=nested)


def _list_of_text() -> _FieldSpec:
    return _FieldSpec(list)


_D2_ANGLES = frozenset(
    {
        "principle_alignment",
        "design_integration",
        "documentation_silence",
        "contradiction",
        "completeness_internal_coherence",
        "dependencies_downstream_impact",
        "new_principle_candidate",
        "alternative_design",
    }
)

_VALIDATORS: Dict[str, Dict[str, _FieldSpec]] = {
    "d2_review_v1": {
        "review_pass_log": _list_of_text(),
        "angle_coverage": _FieldSpec(list, exact_set=_D2_ANGLES),
        "findings": _list_of_records(
            {
                "citation": _text(),
                "materiality": _text(),
                "impact": _text(),
                "route": _text(),
            }
        ),
        "conclusion": _FieldSpec(str),
    },
    "d4_1_edit_set_v1": {
        "edit_set": _list_of_records(
            {
                "document_ref": _text(),
                "current_state_ref": _text(),
                "edit": _text(),
            }
        ),
    },
    "d4_2_verification_v1": {
        "edit_findings": _list_of_records(
            {
                "edit_ref": _text(),
                "consistency": _text(),
                "collateral_changes": _list_of_text(),
            }
        ),
        "conclusion": _text(),
        "item_count": _int_nonneg(),
    },
    "d4_5_post_write_v1": {
        "document_verifications": _list_of_records(
            {"path": _text(), "sha": _text(), "result": _text()}
        ),
        "source_item_count": _int_nonneg(),
        "determination_count": _int_nonneg(),
        "development_baseline_ref": _text(),
    },
    "dev1_angle_v1": {
        "angle": _text(),
        "findings": _list_of_records(
            {"citation": _text(), "finding": _text()}
        ),
        "uncertainties": _list_of_text(),
        "assumptions": _list_of_text(),
        "probes": _list_of_text(),
    },
    "dev1_4_design_to_scope_v1": {
        "conclusion": _text(),
        "findings": _list_of_records({}),
        "return_route": _text(),
    },
    "dev1_5_segmentation_v1": {
        "segments": _list_of_records(
            {
                "segment_id": _text(),
                "boundary": _text(),
                "dependency_ids": _list_of_text(),
                "ordering_rationale": _text(),
                "progressive_verification": _text(),
            }
        ),
    },
    "dev1_6_segment_review_v1": {
        "conclusion": _text(),
        "findings": _list_of_records({}),
        "required_revisions": _list_of_text(),
    },
    "dev2_1_brief_v1": {
        "brief_ref": _text(),
        "source_inventory": _list_of_text(),
        "file_change_map": _list_of_records({"path": _text(), "action": _text()}),
        "test_plan": _list_of_text(),
        "exit_criteria": _list_of_text(),
        "risks": _list_of_text(),
    },
    "dev2_2_design_to_brief_v1": {
        "findings": _list_of_records(
            {"citation": _text(), "impact": _text(), "route": _text()}
        ),
        "conclusion": _text(),
    },
    "dev3_1_build_v1": {
        "build_ref": _text(),
        "changed_files": _list_of_text(),
        "implementation_evidence": _list_of_text(),
    },
    "dev3_2_candidate_tests_v1": {
        "tests_ref": _text(),
        "suites": _list_of_text(),
        "fixtures": _list_of_text(),
    },
    "dev3_3_design_to_tests_v1": {
        "findings": _list_of_records({}),
        "conclusion": _text(),
    },
    "dev3_4_test_execution_v1": {
        "execution_ref": _text(),
        "suite_results": _list_of_records(
            {"suite": _text(), "passed": _int_nonneg(), "failed": _int_nonneg()}
        ),
        "failures": _list_of_text(),
    },
    "dev3_5_code_review_v1": {
        "findings": _list_of_records(
            {
                "file": _text(),
                "line": _int_nonneg(),
                "severity": _text(),
                "issue": _text(),
                "expected": _text(),
            }
        ),
        "conclusion": _text(),
    },
    "dev3_6_test_results_review_v1": {
        "findings": _list_of_records({}),
        "conclusion": _text(),
    },
    "dev3_7_independent_code_review_v1": {
        "findings": _list_of_records({}),
        "conclusion": _text(),
    },
    "dev3_8_review_synthesis_v1": {
        "dispositions": _list_of_records(
            {
                "finding_ref": _text(),
                "disposition": _text(),
                "route": _text(),
            }
        ),
        "unresolved_count": _int_nonneg(),
    },
    "dev3_10_revision_brief_v1": {
        "revision_brief_ref": _text(),
        "finding_changes": _list_of_records(
            {"finding_ref": _text(), "required_change": _text()}
        ),
    },
    "dev3_11_revision_build_v1": {
        "build_ref": _text(),
        "changed_files": _list_of_text(),
        "revision_evidence": _list_of_text(),
    },
    "dev4_1a_archive_scan_v1": {
        "candidates": _list_of_records(
            {"path": _text(), "evidence": _text(), "disposition": _text()}
        ),
    },
    "dev4_1b_archive_verification_v1": {
        "overreach_findings": _list_of_records({}),
        "underreach_findings": _list_of_records({}),
        "conclusion": _text(),
    },
    "dev4_1c_archive_execution_v1": {
        "executed_dispositions": _list_of_records(
            {"path": _text(), "disposition": _text(), "result": _text()}
        ),
    },
    "dev4_2a_ratification_package_v1": {
        "observations": _list_of_records(
            {"observation": _text(), "evidence": _text(), "recommendation": _text()}
        ),
    },
    "pc1_1a_parity_matrix_v1": {
        "rows": _list_of_records(
            {
                "requirement": _text(),
                "design_ref": _text(),
                "implementation_ref": _text(),
                "evidence": _text(),
                "conclusion": _text(),
            }
        ),
    },
    "pc1_1b_matrix_verification_v1": {
        "verified_rows": _list_of_text(),
        "corrected_rows": _list_of_text(),
        "gaps": _list_of_text(),
        "conclusion": _text(),
    },
}


def validator_names() -> FrozenSet[str]:
    return frozenset(_VALIDATORS)


def _check_text(value: Any, path: str, findings: List[OutputFinding]) -> None:
    if not isinstance(value, str) or not value.strip():
        findings.append(OutputFinding(path, "expected nonblank string"))


def _check_nonnegative_int(
    value: Any, path: str, findings: List[OutputFinding]
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        findings.append(OutputFinding(path, "expected non-negative integer"))


def _validate_value(
    value: Any,
    spec: _FieldSpec,
    path: str,
    findings: List[OutputFinding],
) -> None:
    if spec.type is str:
        if spec.nonblank:
            _check_text(value, path, findings)
        elif not isinstance(value, str):
            findings.append(OutputFinding(path, "expected string"))
        return
    if spec.type is int:
        if spec.non_negative:
            _check_nonnegative_int(value, path, findings)
        elif isinstance(value, bool) or not isinstance(value, int):
            findings.append(OutputFinding(path, "expected integer"))
        return
    if spec.type is not list:
        findings.append(OutputFinding(path, "unknown field type"))
        return
    if not isinstance(value, list):
        findings.append(OutputFinding(path, "expected list"))
        return
    if spec.nested is not None:
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            if not isinstance(item, dict):
                findings.append(OutputFinding(item_path, "expected dict"))
                continue
            for field, nested_spec in spec.nested.items():
                nested_path = f"{item_path}.{field}"
                if field not in item:
                    findings.append(OutputFinding(nested_path, "missing required field"))
                    continue
                _validate_value(item[field], nested_spec, nested_path, findings)
    elif spec.exact_set is None:
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            if not isinstance(item, str) or not item.strip():
                findings.append(OutputFinding(item_path, "expected nonblank string"))
    elif spec.exact_set is not None:
        if len(value) != len(set(value)):
            findings.append(OutputFinding(path, "duplicate entries"))
        if any(not isinstance(item, str) for item in value):
            findings.append(OutputFinding(path, "expected string entries"))
        elif set(value) != spec.exact_set:
            findings.append(OutputFinding(path, "must contain exact required entries"))


def validate_lifecycle_output(name: str, metadata: Any) -> Dict[str, Any]:
    """Validate and return a shallow copy of a fixed lifecycle output."""
    if name not in _VALIDATORS:
        raise OutputValidationRejected((OutputFinding("validator", "unknown validator"),))
    if not isinstance(metadata, dict):
        raise OutputValidationRejected((OutputFinding("output", "expected dict"),))

    findings: List[OutputFinding] = []
    for field, spec in _VALIDATORS[name].items():
        if field not in metadata:
            findings.append(OutputFinding(field, "missing required field"))
            continue
        value = metadata[field]
        if name == "d2_review_v1" and field == "conclusion":
            if not isinstance(value, str) or value not in {"DRY", "NOT_DRY"}:
                findings.append(OutputFinding(field, "expected DRY or NOT_DRY"))
            continue
        _validate_value(value, spec, field, findings)

    if findings:
        raise OutputValidationRejected(tuple(findings))
    return dict(metadata)
