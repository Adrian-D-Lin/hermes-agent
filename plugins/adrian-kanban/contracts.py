"""Immutable contract registry and expander (fixed templates only).

No database, policy, skill loading, filesystem, network, config, or clock.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "ContractRejected",
    "ContractTemplate",
    "ContractSnapshot",
    "SequenceTemplate",
    "REGISTRY_VERSION",
    "registry_hash",
    "template_for",
    "expand_contract",
    "validate_snapshot",
]

REGISTRY_VERSION = "v0.28-s2-1"
_SNAPSHOT_VERSION = 1
_CONTRACT_VERSION = 1

_RELEASE_CONDITIONS = frozenset({"accepted_completion", "initiative_checkpoint"})


class ContractRejected(ValueError):
    """Raised when a contract template or snapshot violates the fixed contract."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SequenceTemplate:
    family: str
    ordinal: int
    predecessor_step: Optional[str]
    release_condition: str

    def __post_init__(self) -> None:
        if not isinstance(self.family, str) or not self.family.strip():
            raise ContractRejected("sequence family must be a nonblank string")
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool):
            raise ContractRejected("sequence ordinal must be an integer")
        if self.ordinal <= 0:
            raise ContractRejected("sequence ordinal must be positive")
        if self.predecessor_step is not None:
            if not isinstance(self.predecessor_step, str) or not self.predecessor_step.strip():
                raise ContractRejected("sequence predecessor must be a nonblank string or null")
        if self.release_condition not in _RELEASE_CONDITIONS:
            raise ContractRejected("sequence release condition must be allowed")
        if self.ordinal == 1 and self.predecessor_step is not None:
            raise ContractRejected("ordinal 1 sequence must have no predecessor")
        if self.ordinal > 1 and self.predecessor_step is None:
            raise ContractRejected("ordinal >1 sequence requires a predecessor")


@dataclass(frozen=True)
class ContractTemplate:
    contract_id: str
    contract_version: int
    phase: str
    step: str
    execution_profile: str
    required_reference_groups: tuple[str, ...]
    constraints: tuple[str, ...]
    output_validator: str
    sequence: Optional[SequenceTemplate] = None

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or not self.contract_id.strip():
            raise ContractRejected("contract_id must be a nonblank string")
        if not isinstance(self.contract_version, int) or isinstance(self.contract_version, bool):
            raise ContractRejected("contract_version must be an integer")
        if self.contract_version <= 0:
            raise ContractRejected("contract_version must be positive")
        for name in ("phase", "step", "execution_profile", "output_validator"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractRejected(f"{name} must be a nonblank string")
        for name in ("required_reference_groups", "constraints"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise ContractRejected(f"{name} must be a tuple")
            if len(set(value)) != len(value):
                raise ContractRejected(f"{name} must be unique")
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    raise ContractRejected(f"{name} items must be nonblank strings")
        if self.sequence is not None and not isinstance(self.sequence, SequenceTemplate):
            raise ContractRejected("sequence must be a SequenceTemplate or null")


@dataclass(frozen=True)
class ContractSnapshot:
    version: int
    contract_id: str
    contract_version: int
    phase: str
    step: str
    initiative_id: str
    segment_id: Optional[str]
    segment_workspace_id: Optional[str]
    execution_profile: str
    baseline_refs: tuple[str, ...]
    governing_source_refs: tuple[str, ...]
    prior_record_refs: tuple[str, ...]
    constraints: tuple[str, ...]
    sequence_group_id: Optional[str]
    sequence_ordinal: Optional[int]
    predecessor_ref: Optional[str]
    release_condition: Optional[str]
    output_validator: str
    registry_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise ContractRejected("snapshot version must be an integer")
        if not isinstance(self.contract_id, str) or not self.contract_id.strip():
            raise ContractRejected("contract_id must be a nonblank string")
        if not isinstance(self.contract_version, int) or isinstance(self.contract_version, bool):
            raise ContractRejected("contract_version must be an integer")
        for name in ("phase", "step", "execution_profile", "output_validator", "registry_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractRejected(f"{name} must be a nonblank string")
        if not isinstance(self.initiative_id, str) or not self.initiative_id.strip():
            raise ContractRejected("initiative_id must be a nonblank string")
        for name in (
            "segment_id",
            "segment_workspace_id",
            "sequence_group_id",
            "predecessor_ref",
            "release_condition",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ContractRejected(f"{name} must be a nonblank string or null")
        if self.sequence_ordinal is not None:
            if isinstance(self.sequence_ordinal, bool) or not isinstance(self.sequence_ordinal, int):
                raise ContractRejected("sequence_ordinal must be an integer or null")
            if self.sequence_ordinal <= 0:
                raise ContractRejected("sequence_ordinal must be positive")
        for name in (
            "baseline_refs",
            "governing_source_refs",
            "prior_record_refs",
            "constraints",
        ):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise ContractRejected(f"{name} must be a tuple")
            if len(set(value)) != len(value):
                raise ContractRejected(f"{name} must be unique")
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    raise ContractRejected(f"{name} items must be nonblank strings")
        if (self.segment_id is None) != (self.segment_workspace_id is None):
            raise ContractRejected("segment_id and segment_workspace_id must both be null or both non-null")
        if self.sequence_ordinal is None:
            if (
                self.sequence_group_id is not None
                or self.predecessor_ref is not None
                or self.release_condition is not None
            ):
                raise ContractRejected("non-sequence snapshot must have all sequence fields null")
        else:
            if self.sequence_group_id is None or self.release_condition is None:
                raise ContractRejected("sequence snapshot requires non-null group and release")
            if self.sequence_ordinal == 1 and self.predecessor_ref is not None:
                raise ContractRejected("ordinal 1 snapshot must have no predecessor")
            if self.sequence_ordinal > 1 and self.predecessor_ref is None:
                raise ContractRejected("ordinal >1 snapshot requires a predecessor")
        if self.release_condition is not None and self.release_condition not in _RELEASE_CONDITIONS:
            raise ContractRejected("release_condition must be allowed or null")

    def canonical_dict(self) -> dict:
        return {
            "baseline_refs": list(self.baseline_refs),
            "constraints": list(self.constraints),
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "execution_profile": self.execution_profile,
            "governing_source_refs": list(self.governing_source_refs),
            "initiative_id": self.initiative_id,
            "output_validator": self.output_validator,
            "phase": self.phase,
            "prior_record_refs": list(self.prior_record_refs),
            "predecessor_ref": self.predecessor_ref,
            "release_condition": self.release_condition,
            "registry_hash": self.registry_hash,
            "segment_id": self.segment_id,
            "segment_workspace_id": self.segment_workspace_id,
            "sequence_group_id": self.sequence_group_id,
            "sequence_ordinal": self.sequence_ordinal,
            "step": self.step,
            "version": self.version,
        }

    def canonical_payload(self) -> str:
        return json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))


def _sequence_template(
    family: str,
    ordinal: int,
    predecessor_step: Optional[str],
    release_condition: str,
) -> SequenceTemplate:
    return SequenceTemplate(family, ordinal, predecessor_step, release_condition)


_TEMPLATES: tuple[ContractTemplate, ...] = (
    ContractTemplate(
        contract_id="design-lifecycle.d2",
        contract_version=_CONTRACT_VERSION,
        phase="D2",
        step="D2",
        execution_profile="independent-reviewer",
        required_reference_groups=("baseline_refs", "governing_source_refs"),
        constraints=("eight_angle_review", "accumulated_record_on_reentry"),
        output_validator="d2_review_v1",
    ),
    ContractTemplate(
        contract_id="design-lifecycle.d4",
        contract_version=_CONTRACT_VERSION,
        phase="D4",
        step="D4.1",
        execution_profile="independent-reviewer",
        required_reference_groups=("baseline_refs", "prior_record_refs"),
        constraints=("exact_per_document_edit_set",),
        output_validator="d4_1_edit_set_v1",
        sequence=_sequence_template("d4", 1, None, "accepted_completion"),
    ),
    ContractTemplate(
        contract_id="design-lifecycle.d4",
        contract_version=_CONTRACT_VERSION,
        phase="D4",
        step="D4.2",
        execution_profile="test-authority-reviewer",
        required_reference_groups=("baseline_refs", "prior_record_refs"),
        constraints=("d4_1_accepted",),
        output_validator="d4_2_verification_v1",
        sequence=_sequence_template("d4", 2, "D4.1", "accepted_completion"),
    ),
    ContractTemplate(
        contract_id="design-lifecycle.d4",
        contract_version=_CONTRACT_VERSION,
        phase="D4",
        step="D4.5",
        execution_profile="test-authority-reviewer",
        required_reference_groups=("baseline_refs", "prior_record_refs"),
        constraints=("d4_3_approval_checkpoint", "d4_4_execution_checkpoint"),
        output_validator="d4_5_post_write_v1",
        sequence=_sequence_template("d4", 5, "D4.4", "initiative_checkpoint"),
    ),
    ContractTemplate(
        contract_id="design-lifecycle.dev1",
        contract_version=_CONTRACT_VERSION,
        phase="DEV1",
        step="DEV1.1a",
        execution_profile="independent-reviewer",
        required_reference_groups=("baseline_refs",),
        constraints=("angle_isolation", "no_prior_angle_handoffs"),
        output_validator="dev1_angle_v1",
        sequence=_sequence_template("dev1_angles", 1, None, "accepted_completion"),
    ),
    ContractTemplate(
        contract_id="design-lifecycle.dev1",
        contract_version=_CONTRACT_VERSION,
        phase="DEV1",
        step="DEV1.1b",
        execution_profile="independent-reviewer",
        required_reference_groups=("baseline_refs",),
        constraints=("angle_isolation", "no_prior_angle_handoffs"),
        output_validator="dev1_angle_v1",
        sequence=_sequence_template("dev1_angles", 2, "DEV1.1a", "accepted_completion"),
    ),
    ContractTemplate(
        contract_id="design-lifecycle.dev1",
        contract_version=_CONTRACT_VERSION,
        phase="DEV1",
        step="DEV1.1c",
        execution_profile="independent-reviewer",
        required_reference_groups=("baseline_refs",),
        constraints=("angle_isolation", "no_prior_angle_handoffs"),
        output_validator="dev1_angle_v1",
        sequence=_sequence_template("dev1_angles", 3, "DEV1.1b", "accepted_completion"),
    ),
    ContractTemplate(
        contract_id="design-lifecycle.dev1",
        contract_version=_CONTRACT_VERSION,
        phase="DEV1",
        step="DEV1.1d",
        execution_profile="independent-reviewer",
        required_reference_groups=("baseline_refs",),
        constraints=("angle_isolation", "no_prior_angle_handoffs"),
        output_validator="dev1_angle_v1",
        sequence=_sequence_template("dev1_angles", 4, "DEV1.1c", "accepted_completion"),
    ),
    ContractTemplate(
        contract_id="design-lifecycle.dev1",
        contract_version=_CONTRACT_VERSION,
        phase="DEV1",
        step="DEV1.1e",
        execution_profile="independent-reviewer",
        required_reference_groups=("baseline_refs",),
        constraints=("angle_isolation", "no_prior_angle_handoffs"),
        output_validator="dev1_angle_v1",
        sequence=_sequence_template("dev1_angles", 5, "DEV1.1d", "accepted_completion"),
    ),
    ContractTemplate(
        contract_id="design-lifecycle.dev1",
        contract_version=_CONTRACT_VERSION,
        phase="DEV1",
        step="DEV1.4",
        execution_profile="test-authority-reviewer",
        required_reference_groups=("baseline_refs", "prior_record_refs"),
        constraints=("dry_cumulative_reconnaissance",),
        output_validator="dev1_4_design_to_scope_v1",
    ),
    ContractTemplate(
        contract_id="design-lifecycle.dev1",
        contract_version=_CONTRACT_VERSION,
        phase="DEV1",
        step="DEV1.5",
        execution_profile="independent-reviewer",
        required_reference_groups=("prior_record_refs",),
        constraints=("ratified_scope",),
        output_validator="dev1_5_segmentation_v1",
    ),
    ContractTemplate(
        contract_id="design-lifecycle.dev1",
        contract_version=_CONTRACT_VERSION,
        phase="DEV1",
        step="DEV1.6",
        execution_profile="test-authority-reviewer",
        required_reference_groups=("prior_record_refs",),
        constraints=("ratified_scope", "proposed_segment_list"),
        output_validator="dev1_6_segment_review_v1",
    ),
)

_TEMPLATES_BY_STEP: dict[str, ContractTemplate] = {t.step: t for t in _TEMPLATES}


def _normalize_refs(value: tuple) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ContractRejected("reference group must be a tuple")
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ContractRejected("reference items must be nonblank strings")
        normalized = item.strip()
        if normalized in seen:
            raise ContractRejected("reference group must be unique")
        seen.add(normalized)
        out.append(normalized)
    return tuple(out)


def _compact_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def registry_hash() -> str:
    """Return the SHA-256 hex of the compact sorted JSON registry."""
    entries: list[dict] = []
    for template in _TEMPLATES:
        entry: dict = {
            "contract_id": template.contract_id,
            "contract_version": template.contract_version,
            "phase": template.phase,
            "step": template.step,
            "execution_profile": template.execution_profile,
            "required_reference_groups": list(template.required_reference_groups),
            "constraints": list(template.constraints),
            "output_validator": template.output_validator,
        }
        if template.sequence is not None:
            entry["sequence"] = {
                "family": template.sequence.family,
                "ordinal": template.sequence.ordinal,
                "predecessor_step": template.sequence.predecessor_step,
                "release_condition": template.sequence.release_condition,
            }
        else:
            entry["sequence"] = None
        entries.append(entry)
    payload = _compact_json({"registry_version": REGISTRY_VERSION, "templates": entries})
    return hashlib.sha256(payload).hexdigest()


def template_for(step: str) -> ContractTemplate:
    """Return the template for an exact, nonblank step, or reject."""
    if not isinstance(step, str) or not step.strip():
        raise ContractRejected("step must be a nonblank string")
    template = _TEMPLATES_BY_STEP.get(step)
    if template is None:
        raise ContractRejected("unknown step")
    return template


def expand_contract(
    *,
    step: str,
    initiative_id: str,
    baseline_refs: tuple = (),
    governing_source_refs: tuple = (),
    prior_record_refs: tuple = (),
    segment_id: Optional[str] = None,
    segment_workspace_id: Optional[str] = None,
    predecessor_ref: Optional[str] = None,
) -> ContractSnapshot:
    """Expand a template into a validated snapshot for the given inputs."""
    if not isinstance(initiative_id, str) or not initiative_id.strip():
        raise ContractRejected("initiative_id must be a nonblank string")
    if segment_id is not None:
        if not isinstance(segment_id, str) or not segment_id.strip():
            raise ContractRejected("segment_id must be a nonblank string")
    if segment_workspace_id is not None:
        if not isinstance(segment_workspace_id, str) or not segment_workspace_id.strip():
            raise ContractRejected("segment_workspace_id must be a nonblank string")
    if (segment_id is None) != (segment_workspace_id is None):
        raise ContractRejected("segment_id and segment_workspace_id must both be set or both null")

    template = template_for(step)

    if segment_id is not None or segment_workspace_id is not None:
        raise ContractRejected("initial templates reject segment and segment_workspace")

    baseline_refs = _normalize_refs(baseline_refs)
    governing_source_refs = _normalize_refs(governing_source_refs)
    prior_record_refs = _normalize_refs(prior_record_refs)

    required = set(template.required_reference_groups)
    provided = {
        "baseline_refs": baseline_refs,
        "governing_source_refs": governing_source_refs,
        "prior_record_refs": prior_record_refs,
    }
    missing = [name for name in ("baseline_refs", "governing_source_refs", "prior_record_refs") if name in required and not provided[name]]
    if missing:
        raise ContractRejected("missing required reference groups: " + ",".join(missing))

    sequence_group_id: Optional[str] = None
    sequence_ordinal: Optional[int] = None
    predecessor_ref_out: Optional[str] = None
    release_condition: Optional[str] = None

    if template.sequence is not None:
        seq = template.sequence
        sequence_group_id = f"{initiative_id}:{seq.family}"
        sequence_ordinal = seq.ordinal
        if seq.predecessor_step is not None:
            if predecessor_ref is None:
                raise ContractRejected("predecessor_ref is required for this sequence")
            if not isinstance(predecessor_ref, str) or not predecessor_ref.strip():
                raise ContractRejected("predecessor_ref must be a nonblank string")
            predecessor_ref_out = predecessor_ref.strip()
        else:
            if predecessor_ref is not None:
                raise ContractRejected("no predecessor is expected for this sequence")
        release_condition = seq.release_condition

    return ContractSnapshot(
        version=_SNAPSHOT_VERSION,
        contract_id=template.contract_id,
        contract_version=template.contract_version,
        phase=template.phase,
        step=template.step,
        initiative_id=initiative_id.strip(),
        segment_id=segment_id,
        segment_workspace_id=segment_workspace_id,
        execution_profile=template.execution_profile,
        baseline_refs=baseline_refs,
        governing_source_refs=governing_source_refs,
        prior_record_refs=prior_record_refs,
        constraints=tuple(template.constraints),
        sequence_group_id=sequence_group_id,
        sequence_ordinal=sequence_ordinal,
        predecessor_ref=predecessor_ref_out,
        release_condition=release_condition,
        output_validator=template.output_validator,
        registry_hash=registry_hash(),
    )


def validate_snapshot(snapshot: ContractSnapshot) -> bool:
    """Validate a snapshot against its template and the current registry.

    Raises ContractRejected listing only mismatched field names; otherwise
    returns True.
    """
    if type(snapshot) is not ContractSnapshot:
        raise ContractRejected("snapshot must be a ContractSnapshot")

    try:
        template = template_for(snapshot.step)
    except ContractRejected as exc:
        raise ContractRejected("step") from exc

    mismatches: list[str] = []

    if snapshot.version != _SNAPSHOT_VERSION:
        mismatches.append("version")
    if snapshot.contract_id != template.contract_id:
        mismatches.append("contract_id")
    if snapshot.contract_version != template.contract_version:
        mismatches.append("contract_version")
    if snapshot.phase != template.phase:
        mismatches.append("phase")
    if snapshot.execution_profile != template.execution_profile:
        mismatches.append("execution_profile")
    if snapshot.output_validator != template.output_validator:
        mismatches.append("output_validator")
    if tuple(snapshot.constraints) != tuple(template.constraints):
        mismatches.append("constraints")

    required = set(template.required_reference_groups)
    provided = {
        "baseline_refs": snapshot.baseline_refs,
        "governing_source_refs": snapshot.governing_source_refs,
        "prior_record_refs": snapshot.prior_record_refs,
    }
    missing = [name for name in ("baseline_refs", "governing_source_refs", "prior_record_refs") if name in required and not provided[name]]
    if missing:
        mismatches.append("required_reference_groups")

    if template.sequence is not None:
        seq = template.sequence
        if snapshot.sequence_group_id != f"{snapshot.initiative_id}:{seq.family}":
            mismatches.append("sequence_group_id")
        if snapshot.sequence_ordinal != seq.ordinal:
            mismatches.append("sequence_ordinal")
        if snapshot.release_condition != seq.release_condition:
            mismatches.append("release_condition")
        if seq.predecessor_step is not None:
            if snapshot.predecessor_ref is None:
                mismatches.append("predecessor_ref")
        else:
            if snapshot.predecessor_ref is not None:
                mismatches.append("predecessor_ref")
    else:
        if snapshot.sequence_group_id is not None:
            mismatches.append("sequence_group_id")
        if snapshot.sequence_ordinal is not None:
            mismatches.append("sequence_ordinal")
        if snapshot.predecessor_ref is not None:
            mismatches.append("predecessor_ref")
        if snapshot.release_condition is not None:
            mismatches.append("release_condition")

    if snapshot.registry_hash != registry_hash():
        mismatches.append("registry_hash")

    if snapshot.segment_id is not None:
        mismatches.append("segment_id")
    if snapshot.segment_workspace_id is not None:
        mismatches.append("segment_workspace_id")

    if mismatches:
        raise ContractRejected("snapshot mismatch: " + ",".join(mismatches))

    return True
