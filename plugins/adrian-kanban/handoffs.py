from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable

__all__ = [
    "HandoffFinding",
    "HandoffValidationRejected",
    "normalize_handoff_requirements",
    "canonical_handoff_requirements",
    "validate_candidate_metadata",
]


@dataclass(frozen=True)
class HandoffFinding:
    field: str
    reason: str

    def __post_init__(self) -> None:
        if type(self.field) is not str or not self.field.strip():
            raise ValueError("field must be a nonblank string")
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError("reason must be a nonblank string")


class HandoffValidationRejected(ValueError):
    def __init__(self, findings: Iterable[HandoffFinding]) -> None:
        findings_tuple = tuple(findings)
        if not findings_tuple:
            raise ValueError("at least one finding is required")
        if any(type(finding) is not HandoffFinding for finding in findings_tuple):
            raise TypeError("findings must be HandoffFinding instances")
        self.findings = findings_tuple
        super().__init__(
            "; ".join(
                f"{finding.field}: {finding.reason}"
                for finding in findings_tuple
            )
        )


def _is_nonblank_str(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _validate_known_profiles(known_profiles: Any) -> FrozenSet[str]:
    if type(known_profiles) not in {tuple, frozenset, set}:
        raise TypeError("known_profiles must be a tuple, frozenset, or set")
    profiles = []
    for profile in known_profiles:
        if not _is_nonblank_str(profile):
            raise TypeError("known_profiles must contain only nonblank strings")
        profiles.append(profile.strip())
    if len(profiles) != len(set(profiles)):
        raise TypeError("known_profiles must contain unique values")
    return frozenset(profiles)


def normalize_handoff_requirements(
    raw: Any,
    known_profiles: Any,
) -> dict[str, Any]:
    profiles = _validate_known_profiles(known_profiles)
    findings: list[HandoffFinding] = []
    if type(raw) is not dict:
        raise HandoffValidationRejected(
            (HandoffFinding("requirements", "must be an object"),)
        )
    if set(raw) != {"version", "reviewer", "fields"}:
        findings.append(
            HandoffFinding(
                "requirements",
                "must have exactly keys version, reviewer, and fields",
            )
        )

    if type(raw.get("version")) is not int or raw.get("version") != 1:
        findings.append(HandoffFinding("version", "must be the integer 1"))

    reviewer_raw = raw.get("reviewer")
    reviewer = ""
    if not _is_nonblank_str(reviewer_raw):
        findings.append(HandoffFinding("reviewer", "must be a nonblank string"))
    else:
        reviewer = reviewer_raw.strip()
        if reviewer not in profiles:
            findings.append(HandoffFinding("reviewer", "is not a known profile"))

    normalized_fields: dict[str, dict[str, Any]] = {}
    fields_raw = raw.get("fields")
    if type(fields_raw) is not dict or not fields_raw:
        findings.append(HandoffFinding("fields", "must be a non-empty object"))
    else:
        seen_names: set[str] = set()
        for raw_name, declaration in fields_raw.items():
            if not _is_nonblank_str(raw_name):
                findings.append(
                    HandoffFinding("fields", "field names must be nonblank strings")
                )
                continue
            name = raw_name.strip()
            if name in seen_names:
                findings.append(
                    HandoffFinding("fields", "field names must be unique after trimming")
                )
                continue
            seen_names.add(name)
            if type(declaration) is not dict:
                findings.append(
                    HandoffFinding(name, "field declaration must be an object")
                )
                continue
            field_type = declaration.get("type")
            if type(field_type) is not str:
                findings.append(HandoffFinding(name, "type must be a string"))
                continue
            if field_type in {"text", "boolean"}:
                if set(declaration) != {"type"}:
                    findings.append(
                        HandoffFinding(
                            name,
                            f"{field_type} declaration permits only type",
                        )
                    )
                    continue
                normalized_fields[name] = {"type": field_type}
            elif field_type == "list":
                if set(declaration) not in ({"type"}, {"type", "min_items"}):
                    findings.append(
                        HandoffFinding(
                            name,
                            "list declaration permits only type and min_items",
                        )
                    )
                    continue
                min_items = declaration.get("min_items", 0)
                if type(min_items) is not int or min_items < 0:
                    findings.append(
                        HandoffFinding(
                            name,
                            "min_items must be a non-negative integer",
                        )
                    )
                    continue
                normalized_fields[name] = {
                    "type": "list",
                    "min_items": min_items,
                }
            elif field_type == "enum":
                if set(declaration) != {"type", "values"}:
                    findings.append(
                        HandoffFinding(
                            name,
                            "enum declaration requires only type and values",
                        )
                    )
                    continue
                values = declaration.get("values")
                if type(values) is not list or not values:
                    findings.append(
                        HandoffFinding(name, "enum values must be a non-empty list")
                    )
                    continue
                normalized_values = []
                invalid_value = False
                for value in values:
                    if not _is_nonblank_str(value):
                        findings.append(
                            HandoffFinding(
                                name,
                                "enum values must be nonblank strings",
                            )
                        )
                        invalid_value = True
                        break
                    normalized_values.append(value.strip())
                if invalid_value:
                    continue
                if len(normalized_values) != len(set(normalized_values)):
                    findings.append(
                        HandoffFinding(
                            name,
                            "enum values must be unique after trimming",
                        )
                    )
                    continue
                normalized_fields[name] = {
                    "type": "enum",
                    "values": normalized_values,
                }
            else:
                findings.append(
                    HandoffFinding(
                        name,
                        "type must be text, list, boolean, or enum",
                    )
                )

    if findings:
        raise HandoffValidationRejected(findings)
    return {"version": 1, "reviewer": reviewer, "fields": normalized_fields}


def canonical_handoff_requirements(requirements: Any) -> str:
    if type(requirements) is not dict:
        raise HandoffValidationRejected(
            (HandoffFinding("requirements", "must be an object"),)
        )
    reviewer = requirements.get("reviewer")
    if not _is_nonblank_str(reviewer):
        raise HandoffValidationRejected(
            (HandoffFinding("reviewer", "must be a nonblank string"),)
        )
    normalized = normalize_handoff_requirements(
        requirements,
        (reviewer.strip(),),
    )
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def validate_candidate_metadata(
    requirements: Any,
    metadata: Any,
) -> dict[str, Any]:
    if type(requirements) is not dict:
        raise HandoffValidationRejected(
            (HandoffFinding("requirements", "must be an object"),)
        )
    reviewer = requirements.get("reviewer")
    if not _is_nonblank_str(reviewer):
        raise HandoffValidationRejected(
            (HandoffFinding("reviewer", "must be a nonblank string"),)
        )
    normalized = normalize_handoff_requirements(
        requirements,
        (reviewer.strip(),),
    )
    if type(metadata) is not dict:
        raise HandoffValidationRejected(
            (HandoffFinding("metadata", "must be an object"),)
        )

    findings: list[HandoffFinding] = []
    for name, declaration in normalized["fields"].items():
        if name not in metadata:
            findings.append(HandoffFinding(name, "is required"))
            continue
        value = metadata[name]
        field_type = declaration["type"]
        if field_type == "text" and not _is_nonblank_str(value):
            findings.append(HandoffFinding(name, "must be a nonblank string"))
        elif field_type == "boolean" and type(value) is not bool:
            findings.append(HandoffFinding(name, "must be a boolean"))
        elif field_type == "list":
            if type(value) is not list:
                findings.append(HandoffFinding(name, "must be a list"))
            elif len(value) < declaration["min_items"]:
                findings.append(
                    HandoffFinding(name, "does not meet the minimum item count")
                )
        elif field_type == "enum" and (
            type(value) is not str or value not in declaration["values"]
        ):
            findings.append(HandoffFinding(name, "must be an allowed enum value"))
    if findings:
        raise HandoffValidationRejected(findings)
    return dict(metadata)
