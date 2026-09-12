"""Contract checks for the Adrian Kanban stopped-cutover runbook."""

from pathlib import Path


RUNBOOK = (
    Path(__file__).parents[1] / "plugins" / "adrian-kanban" / "DEPLOYMENT.md"
)


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_covers_the_complete_stopped_upgrade_sequence():
    text = _runbook()

    required_phrases = (
        "dark stage",
        "maintenance window",
        "WAL",
        "SHM",
        "integrity_check",
        "kanban.database_path",
        "kanban.mutation_authority",
        "adrian-kanban",
        "negative bypass",
        "coordination backfill",
        "maintenance_backfill",
        "--apply",
        "legacy startup hook",
        "exact-once held-prompt release",
        "close → verified archive → coordination retirement",
        "staged reactivation",
        "soak",
        "blind restore",
    )
    for phrase in required_phrases:
        assert phrase.casefold() in text.casefold(), phrase


def test_runbook_pins_one_release_and_database_for_every_runtime():
    text = _runbook()

    for profile in (
        "default",
        "builder-tester",
        "independent-reviewer",
        "test-authority-reviewer",
    ):
        assert profile in text
    for component in (
        "gateway",
        "Desktop",
        "dispatcher",
        "private adapter",
    ):
        assert component.casefold() in text.casefold()

    assert "one immutable release" in text.casefold()
    assert "one absolute" in text.casefold()
    assert "mixed-version" in text.casefold()
    assert "fail closed" in text.casefold()


def test_runbook_preserves_session_startup_and_write_gate_profile_contracts():
    text = _runbook().casefold()

    assert "session startup" in text
    assert "builder-tester" in text and "excluded" in text
    assert "independent-reviewer" in text and "test-authority-reviewer" in text
    assert "moa selected inside `default`" in text
    assert "write-gate" in text
    assert "legacy external `pre_llm_call`" in text


def test_runbook_requires_primary_only_backfill_and_safe_rollback():
    text = _runbook().casefold()

    assert "separate completed migration concern" in text
    assert "do not infer" in text
    assert "additional repositories are not backfilled automatically" in text
    assert "accepted plugin mutation" in text
    assert "reverse migration" in text
    assert "keep the system stopped" in text
