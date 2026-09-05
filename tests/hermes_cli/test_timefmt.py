from datetime import datetime, timezone

from hermes_cli import timefmt


def test_relative_time_accepts_numeric_string(monkeypatch):
    monkeypatch.setattr(timefmt._time, "time", lambda: 10_000.0)

    assert timefmt.relative_time("2800") == "2h ago"


def test_relative_time_accepts_legacy_iso_timestamp(monkeypatch):
    current = datetime(2026, 8, 31, 18, 21, 16, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(timefmt._time, "time", lambda: current)

    assert timefmt.relative_time("2026-08-31T16:21:16Z") == "2h ago"


def test_relative_time_degrades_invalid_timestamp_without_crashing():
    assert timefmt.relative_time("not-a-timestamp") == "?"
