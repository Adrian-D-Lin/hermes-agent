"""Regression checks that executable phase skills describe the live S3 contract."""

from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
SKILL_ROOT = PLUGIN_ROOT / "skills"
PHASE_SKILLS = (
    "dev2-implementation-brief",
    "dev3-orchestration",
    "dev4-closure",
)


@pytest.mark.parametrize("skill_id", PHASE_SKILLS)
def test_phase_skill_has_no_stale_unimplemented_contract_markers(skill_id):
    text = (SKILL_ROOT / skill_id / "SKILL.md").read_text(encoding="utf-8")

    assert "<!-- TODO:" not in text
    assert "once the Kanban field contract is finalized" not in text
    assert "when the Kanban workspace resolver is implemented" not in text


@pytest.mark.parametrize("skill_id", PHASE_SKILLS)
def test_segment_phase_skill_names_the_runtime_workspace_and_input_contract(skill_id):
    text = (SKILL_ROOT / skill_id / "SKILL.md").read_text(encoding="utf-8")

    assert "segment_workspace_id" in text
    assert "task_input_manifest_v1" in text
    assert "predecessor_ref" in text
    assert "system-derived" in text
    assert "concurrent bindings" in text.lower()
    assert "stale diffs" in text.lower()


def test_dev2_skill_describes_immutable_accepted_handoff_to_dev3():
    text = (
        SKILL_ROOT / "dev2-implementation-brief" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "task_candidate_handoffs" in text
    assert "task_reviewer_verdicts" in text
    assert "source_locator" in text


def test_dev3_skill_describes_manifest_pinned_test_store():
    text = (SKILL_ROOT / "dev3-orchestration" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "test store" in text.lower()
    assert "sha256" in text
    assert "context_ref" in text


def test_dev4_skill_describes_coordinator_owned_merge_and_retirement():
    text = (SKILL_ROOT / "dev4-closure" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "DEV4.3" in text
    assert "DEV4.4" in text
    assert "merged" in text
    assert "retired" in text
    assert "caller" in text.lower()
