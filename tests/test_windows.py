from datetime import datetime, timedelta, timezone

import pytest

from opencode_tracker.db import get_conn, insert_request
from opencode_tracker.windows import (
    FIVE_HOURS,
    compute_windows,
    five_hour_window_start,
    parse_ts,
    period_start,
    week_start,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def make_conn(tmp_path):
    return get_conn(tmp_path / "windows.db")


def add(conn, when, model, allowance=0.0, cost=0.0, billing="go", **extra):
    entry = {
        "timestamp": when.isoformat() if isinstance(when, datetime) else when,
        "model": model,
        "billing_source": billing,
        "cost": cost,
        "allowance": allowance,
        "status": "ok",
    }
    entry.update(extra)
    insert_request(conn, entry)


def test_period_start_calendar_and_anniversary():
    assert period_start(NOW, 1) == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert period_start(NOW, 15) == datetime(2026, 11, 15, tzinfo=timezone.utc)
    # Before the anniversary day -> previous month.
    assert period_start(NOW, 25) == datetime(2026, 10, 25, tzinfo=timezone.utc)
    # Clamp 31 to the last day of a short month.
    assert period_start(datetime(2026, 2, 28, 12, tzinfo=timezone.utc), 31) == datetime(2026, 2, 28, tzinfo=timezone.utc)


def test_week_start_is_monday_utc():
    assert week_start(NOW) == datetime(2026, 11, 16, tzinfo=timezone.utc)


def test_parse_ts_accepts_z_and_offset():
    assert parse_ts("2026-11-19T12:00:00Z") == NOW
    assert parse_ts("2026-11-19T07:00:00-05:00") == NOW
    assert parse_ts("not-a-date") is None


def test_five_hour_window_anchors_on_first_use():
    t0 = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
    assert five_hour_window_start([t0, t0 + timedelta(hours=4)], now=t0 + timedelta(hours=4, minutes=30)) == t0
    # Window ended and nothing followed: no active window.
    assert five_hour_window_start([t0, t0 + timedelta(hours=4)], now=t0 + timedelta(hours=5, minutes=30)) is None
    # A request after the window ends starts a new one.
    t1 = t0 + timedelta(hours=5, minutes=30)
    assert five_hour_window_start([t0, t1], now=t1 + timedelta(minutes=10)) == t1


def test_compute_windows_splits_5h_week_month(tmp_path):
    conn = make_conn(tmp_path)
    add(conn, NOW - timedelta(hours=1), "kimi-k3", 2.0)
    add(conn, NOW - timedelta(hours=4, minutes=30), "kimi-k3", 1.0)
    add(conn, NOW - timedelta(days=25), "kimi-k3", 4.0)  # previous period
    add(conn, datetime(2026, 11, 17, 9, tzinfo=timezone.utc), "glm-5.3-flash", 5.0)
    add(conn, NOW - timedelta(hours=2), "gpt-6-luna", 0.0, cost=3.0, billing="balance")

    windows = {w["model"]: w for w in compute_windows(conn, "go", now=NOW, period_start_day=1)}

    kimi = windows["kimi-k3"]
    assert kimi["month_used"] == pytest.approx(3.0)
    assert kimi["week_used"] == pytest.approx(3.0)
    assert kimi["five_hour_used"] == pytest.approx(3.0)
    assert kimi["month_pct"] == pytest.approx(20.0)
    assert kimi["week_pct"] == pytest.approx(40.0)
    assert kimi["five_hour_pct"] == pytest.approx(100.0)
    assert kimi["status"] == "over"

    flash = windows["glm-5.3-flash"]
    assert flash["month_used"] == pytest.approx(5.0)
    assert flash["five_hour_used"] == 0.0

    # Balance-only usage never counts toward Go windows.
    assert "gpt-6-luna" not in windows

    # Sorted by monthly percentage, highest first.
    assert [w["model"] for w in compute_windows(conn, "go", now=NOW, period_start_day=1)][0] == "kimi-k3"
    conn.close()


def test_weekly_reset_and_period_anniversary(tmp_path):
    conn = make_conn(tmp_path)
    add(conn, datetime(2026, 11, 15, 23, 0, tzinfo=timezone.utc), "kimi-k3", 9.0)  # Sunday
    add(conn, datetime(2026, 11, 16, 0, 30, tzinfo=timezone.utc), "kimi-k3", 1.0)  # Monday
    add(conn, datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc), "kimi-k3", 2.0)  # before the 15th

    by_week = compute_windows(conn, "go", now=NOW, period_start_day=1)[0]
    assert by_week["week_used"] == pytest.approx(1.0)
    assert by_week["month_used"] == pytest.approx(12.0)

    by_anniversary = compute_windows(conn, "go", now=NOW, period_start_day=15)[0]
    assert by_anniversary["month_used"] == pytest.approx(10.0)  # excludes Nov 10
    conn.close()


def test_five_hour_usage_expires(tmp_path):
    conn = make_conn(tmp_path)
    add(conn, NOW - timedelta(hours=6), "kimi-k3", 2.0)
    windows = compute_windows(conn, "go", now=NOW, period_start_day=1)
    kimi = windows[0]
    assert kimi["five_hour_used"] == 0.0
    assert kimi["five_hour_start"] is None
    conn.close()


def test_future_rows_are_ignored(tmp_path):
    conn = make_conn(tmp_path)
    add(conn, NOW + timedelta(hours=1), "kimi-k3", 2.0)
    assert compute_windows(conn, "go", now=NOW, period_start_day=1) == []
    conn.close()


def test_compute_windows_respects_filters(tmp_path):
    conn = make_conn(tmp_path)
    add(conn, NOW - timedelta(hours=1), "kimi-k3", 1.0, profile="work", project="tracker")
    add(conn, NOW - timedelta(hours=1), "kimi-k3", 2.0, profile="personal", project="other")

    filtered = compute_windows(conn, "go", now=NOW, period_start_day=1, filters={"profile": "work"})
    assert len(filtered) == 1
    assert filtered[0]["month_used"] == pytest.approx(1.0)
    assert filtered[0]["five_hour_used"] == pytest.approx(1.0)

    assert compute_windows(conn, "go", now=NOW, period_start_day=1, filters={"project": "missing"}) == []
    conn.close()
