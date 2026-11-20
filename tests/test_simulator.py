from datetime import datetime, timezone

import pytest

from opencode_tracker.db import get_conn, insert_request
from opencode_tracker.simulator import simulate_plans

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def seed(conn):
    insert_request(conn, {
        "timestamp": NOW.isoformat(),
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 20.0,
        "status": "ok",
    })
    insert_request(conn, {
        "timestamp": NOW.isoformat(),
        "model": "glm-5.3-flash",
        "billing_source": "go",
        "allowance": 50.0,
        "status": "ok",
    })


def test_plan_simulator(tmp_path):
    conn = get_conn(tmp_path / "sim.db")
    seed(conn)
    result = simulate_plans(conn, now=NOW)
    plans = {row["plan"]: row for row in result["plans"]}

    assert plans["go"]["models_over"] == 1
    assert plans["go"]["overage"] == pytest.approx(5.0)
    assert plans["go"]["fits"] is False
    assert plans["go"]["coverage_pct"] == pytest.approx(50.0)
    assert plans["go"]["price"] == 10

    assert plans["go_plus"]["models_over"] == 0
    assert plans["go_plus"]["fits"] is True
    assert plans["go_plus"]["coverage_pct"] == pytest.approx(100.0)

    assert plans["go_teams"]["models_over"] == 0
    assert plans["go_teams"]["overage"] == 0

    kimi = next(m for m in result["models"] if m["model"] == "kimi-k3")
    assert kimi["plans"]["go"]["limit"] == 15
    assert kimi["plans"]["go_plus"]["limit"] == 60
    assert kimi["plans"]["go"]["fits"] is False
    conn.close()


def test_plan_simulator_ignores_old_and_cost_only_rows(tmp_path):
    conn = get_conn(tmp_path / "sim.db")
    insert_request(conn, {
        "timestamp": "2026-10-01T12:00:00+00:00",
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 99.0,
        "status": "ok",
    })
    insert_request(conn, {
        "timestamp": NOW.isoformat(),
        "model": "kimi-k3",
        "billing_source": "balance",
        "cost": 2.0,
        "allowance": 0.0,
        "status": "ok",
    })
    result = simulate_plans(conn, now=NOW)
    assert result["models"] == []
    conn.close()


def test_plan_simulator_filters(tmp_path):
    conn = get_conn(tmp_path / "sim.db")
    seed(conn)
    result = simulate_plans(conn, now=NOW, filters={"model": "glm-5.3-flash"})
    assert [m["model"] for m in result["models"]] == ["glm-5.3-flash"]
    assert all(row["fits"] for row in result["plans"])
    conn.close()
