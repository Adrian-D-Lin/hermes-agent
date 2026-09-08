"""Fixed lifecycle skill bundle for the Adrian Kanban verified slice."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .lifecycle import SkillBinding
from .versioning import SKILL_BUNDLE_VERSION

__all__ = [
    "PHASE_SKILLS",
    "resolve_skill_binding",
    "skill_contract",
    "validate_skill_bundle",
    "skill_bundle_hash",
]

PHASE_SKILLS: dict[str, tuple[str, str]] = {
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

_SKILL_VERSION = SKILL_BUNDLE_VERSION
_CONTRACT_VERSION = "1"


def _skill_path(skill_id: str) -> Path:
    return Path(__file__).parent / "skills" / skill_id / "SKILL.md"


def _parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing frontmatter start")
    data: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return data
        if ":" not in line:
            raise ValueError(f"invalid frontmatter line {i}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        data[key] = value
    raise ValueError("missing frontmatter end")


def _validate_phase(phase: str, skill_id: str, contract_id: str) -> None:
    path = _skill_path(skill_id)
    if not path.is_file():
        raise ValueError(f"missing skill file for {phase}")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"skill file for {phase} is not UTF-8") from None
    fm = _parse_frontmatter(text)
    if fm.get("name") != skill_id:
        raise ValueError(f"skill name mismatch for {phase}")
    if fm.get("version") != _SKILL_VERSION:
        raise ValueError(f"skill version mismatch for {phase}")
    if fm.get("kanban_phase") != phase:
        raise ValueError(f"kanban phase mismatch for {phase}")
    if fm.get("kanban_contract_id") != contract_id:
        raise ValueError(f"contract id mismatch for {phase}")
    if fm.get("kanban_contract_version") != _CONTRACT_VERSION:
        raise ValueError(f"contract version mismatch for {phase}")


def resolve_skill_binding(phase: str) -> SkillBinding:
    if phase not in PHASE_SKILLS:
        raise ValueError(f"unknown phase: {phase}")
    skill_id, _ = PHASE_SKILLS[phase]
    path = _skill_path(skill_id)
    raw = path.read_bytes()
    return SkillBinding(
        skill_id=skill_id,
        skill_version=_SKILL_VERSION,
        skill_hash=hashlib.sha256(raw).hexdigest(),
    )


def skill_contract(phase: str) -> tuple[str, str]:
    if phase not in PHASE_SKILLS:
        raise ValueError(f"unknown phase: {phase}")
    _, contract_id = PHASE_SKILLS[phase]
    return (contract_id, _CONTRACT_VERSION)


def validate_skill_bundle() -> bool:
    if set(PHASE_SKILLS.keys()) != set(_EXPECTED_PHASES):
        return False
    for phase, (skill_id, contract_id) in PHASE_SKILLS.items():
        try:
            _validate_phase(phase, skill_id, contract_id)
        except ValueError:
            return False
    return True


def skill_bundle_hash() -> str:
    h = hashlib.sha256()
    for phase, (skill_id, contract_id) in PHASE_SKILLS.items():
        h.update(phase.encode("utf-8"))
        h.update(b"\x00")
        h.update(skill_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(_SKILL_VERSION.encode("utf-8"))
        h.update(b"\x00")
        h.update(contract_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(_CONTRACT_VERSION.encode("utf-8"))
        h.update(b"\x00")
        raw = _skill_path(skill_id).read_bytes()
        h.update(hashlib.sha256(raw).digest())
        h.update(b"\x00")
    return h.hexdigest()


_EXPECTED_PHASES = frozenset(PHASE_SKILLS.keys())
