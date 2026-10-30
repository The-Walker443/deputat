"""The 00:05 scheduler job calls the real timetable.generate_today_suggestions (keyword-only signature)."""

from __future__ import annotations

from app.services import backup, timetable


def test_scheduler_job_resolves_and_calls_timetable_function(monkeypatch):
    calls = []
    real = timetable.generate_today_suggestions
    assert backup._timetable_job() is real

    def fake(*, today=None, now=None):
        calls.append((today, now))

    monkeypatch.setattr(timetable, "generate_today_suggestions", fake)
    backup.run_timetable_suggestions()
    assert calls == [(None, None)]
