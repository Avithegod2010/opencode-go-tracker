from datetime import datetime, timedelta, timezone

import pytest

from opencode_tracker.db import get_conn, insert_request
from opencode_tracker.forecast import forecast, next_period_start

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def seed(conn):
    # Month: 9.25 / 18.5 elapsed days -> 0.5/day -> 15 projected.
    # The 1.5 row is also inside the active 5h window (started 11:00).
    insert_request(conn, {
        "timestamp": "2026-11-03T12:00:00+00:00",
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 4.0,
        "status": "ok",
    })
    insert_request(conn, {
        "timestamp": "2026-11-17T12:00:00+00:00",
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 2.0,
        "status": "ok",
    })
    insert_request(conn, {
        "timestamp": "2026-11-19T11:00:00+00:00",
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 1.5,
        "status": "ok",
    })
    insert_request(conn, {
        "timestamp": "2026-11-19T10:00:00+00:00",
        "model": "glm-5.3-flash",
        "billing_source": "go",
        "allowance": 1.0,
        "status": "ok",
    })


def test_next_period_start():
    assert next_period_start(NOW, 1) == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert next_period_start(NOW, 15) == datetime(2026, 12, 15, tzinfo=timezone.utc)


def test_forecast_projects_month_and_windows(tmp_path):
    conn = get_conn(tmp_path / "forecast.db")
    seed(conn)
    result = forecast(conn, "go", period_start_day=1, now=NOW)
    meta = result["meta"]
    assert meta["period_start"].startswith("2026-11-01")
    assert meta["period_end"].startswith("2026-12-01")
    assert meta["days_left_in_period"] == pytest.approx(11.5)

    kimi = next(m for m in result["models"] if m["model"] == "kimi-k3")
    assert kimi["month_used"] == pytest.approx(7.5)
    assert kimi["burn_per_day"] == pytest.approx(7.5 / 18.5, rel=1e-3)
    assert kimi["projected"] == pytest.approx(7.5 / 18.5 * 30, rel=1e-3)
    assert kimi["projected_pct"] == pytest.approx(81.08, rel=1e-3)
    assert kimi["week_projected_pct"] == pytest.approx((3.5 / 3.5 * 7) / 7.5 * 100, rel=1e-3)
    assert kimi["five_hour_used"] == pytest.approx(1.5)
    assert kimi["five_hour_projected"] == pytest.approx(3.75)
    assert kimi["risk"] == "over"
    assert kimi["confidence"] == "high"
    conn.close()


def test_forecast_empty(tmp_path):
    conn = get_conn(tmp_path / "forecast.db")
    result = forecast(conn, "go", now=NOW)
    assert result["models"] == []
    assert result["meta"]["period_days"] == 30
    conn.close()


def test_forecast_respects_filters(tmp_path):
    conn = get_conn(tmp_path / "forecast.db")
    seed(conn)
    result = forecast(conn, "go", now=NOW, filters={"model": "glm-5.3-flash"})
    assert [m["model"] for m in result["models"]] == ["glm-5.3-flash"]
    conn.close()
