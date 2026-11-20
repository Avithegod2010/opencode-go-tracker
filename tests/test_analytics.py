from datetime import datetime, timedelta, timezone

import pytest

from opencode_tracker.analytics import analytics
from opencode_tracker.db import get_conn, insert_request

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def seed(conn):
    insert_request(conn, {
        "timestamp": (NOW - timedelta(hours=1)).isoformat(),
        "model": "kimi-k3",
        "billing_source": "go",
        "cost": 0.0,
        "allowance": 2.0,
        "input_tokens": 100,
        "output_tokens": 20,
        "reasoning_tokens": 5,
        "cache_read": 100,
        "latency_ms": 50,
        "profile": "work",
        "member": "alice",
        "project": "tracker",
        "source": "proxy",
        "status": "ok",
    })
    insert_request(conn, {
        "timestamp": (NOW - timedelta(days=2)).isoformat(),
        "model": "glm-5.3",
        "billing_source": "balance",
        "cost": 0.5,
        "allowance": 0.0,
        "input_tokens": 50,
        "output_tokens": 10,
        "cache_read": 50,
        "profile": "personal",
        "member": "bob",
        "project": "other",
        "source": "import",
        "status": "ok",
    })


def test_analytics_kpis_and_series(tmp_path):
    conn = get_conn(tmp_path / "analytics.db")
    seed(conn)
    result = analytics(conn, days=30, now=NOW)

    assert result["kpi"]["cost"] == pytest.approx(0.5)
    assert result["kpi"]["billable"] == pytest.approx(0.5)
    assert result["kpi"]["allowance"] == pytest.approx(2.0)
    assert result["kpi"]["requests"] == 2
    assert result["kpi"]["cache_hit_ratio"] == pytest.approx(0.5)
    assert result["kpi"]["cost_per_request"] == pytest.approx(0.25)
    assert result["kpi"]["avg_latency_ms"] == pytest.approx(50.0)
    assert result["kpi"]["tokens"] == 335

    assert len(result["days"]) == 30
    assert result["series"][0]["model"] == "glm-5.3"
    assert sum(result["series"][0]["values"]) == pytest.approx(0.5)
    assert result["by_model"][0]["model"] == "glm-5.3"
    assert result["by_source"][0]["name"] in ("import", "proxy")
    assert {row["name"] for row in result["by_project"]} == {"tracker", "other"}
    conn.close()


def test_analytics_respects_filters(tmp_path):
    conn = get_conn(tmp_path / "analytics.db")
    seed(conn)
    result = analytics(conn, filters={"profile": "work"}, days=30, now=NOW)
    assert result["kpi"]["requests"] == 1
    assert result["kpi"]["cost"] == 0
    assert result["kpi"]["allowance"] == pytest.approx(2.0)
    assert [row["model"] for row in result["by_model"]] == ["kimi-k3"]
    conn.close()


def test_analytics_date_range(tmp_path):
    conn = get_conn(tmp_path / "analytics.db")
    seed(conn)
    result = analytics(conn, filters={"from": "2026-11-18", "to": "2026-11-19"}, days=30, now=NOW)
    assert result["days"] == ["2026-11-18", "2026-11-19"]
    assert result["kpi"]["requests"] == 1
    conn.close()
