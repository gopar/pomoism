"""Behavior tests for the pure dashboard aggregation helpers."""

from __future__ import annotations

from datetime import datetime

from pomo.web import stats


def _epoch(year: int, month: int, day: int, hour: int = 9, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute).timestamp())


def _session(
    *,
    start: int,
    duration: int = 1500,
    kind: str = "pomodoro",
    state: str = "ended",
    ended_at: float | None = None,
    project: str | None = None,
    name: str | None = None,
) -> dict:
    return {
        "id": f"s-{start}-{kind}",
        "state": state,
        "start_epoch": start,
        "duration": duration,
        "origin_machine": "laptop",
        "updated_at": float(start),
        "ended_at": ended_at if ended_at is not None else start + duration,
        "name": name,
        "project": project,
        "kind": kind,
    }


class TestSessionSeconds:
    def test_uses_actual_end_when_present(self):
        session = _session(start=100, duration=60, ended_at=250)
        assert stats.session_seconds(session) == 150

    def test_falls_back_to_planned_duration(self):
        session = _session(start=100, duration=60, ended_at=None)
        assert stats.session_seconds(session) == 60

    def test_actual_end_before_start_falls_back(self):
        session = _session(start=100, duration=60, ended_at=50)
        assert stats.session_seconds(session) == 60


class TestSessionKind:
    def test_explicit_kind_wins(self):
        assert stats.session_kind({"kind": "break", "state": "ended"}) == "break"

    def test_old_break_state_is_inferred(self):
        assert stats.session_kind({"kind": None, "state": "break-overtime"}) == "break"

    def test_defaults_to_pomodoro(self):
        assert stats.session_kind({"kind": None, "state": "ended"}) == "pomodoro"


class TestAggregate:
    def test_totals_and_project_breakdown(self):
        # Given: two focus sessions and one break
        sessions = [
            _session(start=_epoch(2024, 1, 1), duration=1500, project="website"),
            _session(start=_epoch(2024, 1, 1, 10), duration=900, project="backend"),
            _session(start=_epoch(2024, 1, 1, 11), duration=300, kind="break"),
        ]

        # When: aggregating
        data = stats.aggregate_sessions(sessions)

        # Then: focus and break totals are separate
        assert data["focus_seconds"] == 2400
        assert data["focus_count"] == 2
        assert data["break_seconds"] == 300
        assert data["break_count"] == 1
        assert data["average_focus_seconds"] == 1200
        assert data["projects"]["website"] == {"seconds": 1500, "count": 1}
        assert data["top_project"] == {"name": "website", "seconds": 1500, "count": 1}

    def test_archived_sessions_count_as_completed(self):
        session = _session(start=_epoch(2024, 1, 1), state="archived")
        data = stats.aggregate_sessions([session])
        assert data["focus_count"] == 1
        assert data["focus_seconds"] == 1500

    def test_active_sessions_are_not_counted(self):
        session = _session(start=_epoch(2024, 1, 1), state="pomodoro")
        data = stats.aggregate_sessions([session])
        assert data["focus_count"] == 0
        assert data["focus_seconds"] == 0

    def test_range_fills_missing_days(self):
        sessions = [_session(start=_epoch(2024, 1, 2), duration=60)]
        data = stats.aggregate_sessions(
            sessions,
            from_date="2024-01-01",
            to_date="2024-01-03",
        )
        assert [day["date"] for day in data["daily"]] == [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
        ]
        assert data["daily"][0]["focus_seconds"] == 0
        assert data["daily"][1]["focus_seconds"] == 60
        assert data["chart_max"] == 60
        assert data["daily"][1]["focus_pct"] == 100

    def test_all_time_does_not_fill_gaps(self):
        sessions = [_session(start=_epoch(2024, 1, 2), duration=60)]
        data = stats.aggregate_sessions(
            sessions,
            from_date="2024-01-01",
            to_date="2024-01-03",
            all_time=True,
        )
        assert [day["date"] for day in data["daily"]] == ["2024-01-02"]

    def test_old_rows_without_kind_default_to_pomodoro(self):
        session = _session(start=_epoch(2024, 1, 1), state="ended", kind=None)
        data = stats.aggregate_sessions([session])
        assert data["focus_count"] == 1
        assert data["break_count"] == 0

    def test_empty_data(self):
        data = stats.aggregate_sessions([])
        assert data["focus_seconds"] == 0
        assert data["daily"] == []
        assert data["top_project"] is None
