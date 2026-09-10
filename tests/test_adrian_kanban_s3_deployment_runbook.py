"""Contract checks for the Adrian Kanban stopped-cutover runbook."""

from pathlib import Path


RUNBOOK = (
    Path(__file__).parents[1] / "plugins" / "adrian-kanban" / "DEPLOYMENT.md"
)


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_covers_the_complete_stopped_cutover_sequence():
    text = _runbook()

    required_phrases = (
        "dark stage",
        "maintenance window",
        "WAL",
        "SHM",
        "integrity_check",
        "dry-run",
        "kanban.database_path",
        "kanban.mutation_authority",
        "adrian-kanban",
        "dispatch_in_gateway",
        "auto_decompose",
        "review_dispatch",
        "negative bypass",
        "ordinary task canary",
        "initiative canary",
        "staged reactivation",
        "soak",
        "authority reversal",
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
    assert "builder-tester" in text and "must not load session startup" in text
    assert "independent-reviewer" in text and "test-authority-reviewer" in text
    assert "write-gate" in text
    assert "legacy pre-tool hook" in text


def test_runbook_requires_separate_legacy_mapping_and_safe_rollback():
    text = _runbook().casefold()

    assert "separate migration exercise" in text
    assert "do not infer" in text
    assert "accepted plugin mutation" in text
    assert "reverse migration" in text
    assert "keep the system stopped" in text

