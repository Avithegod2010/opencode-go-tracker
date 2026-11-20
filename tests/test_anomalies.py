from datetime import datetime, timedelta, timezone

from opencode_tracker.anomalies import detect_anomalies
from opencode_tracker.db import get_conn, insert_request

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def day(offset):
    return (NOW - timedelta(days=offset)).date().isoformat()


def add(conn, when, model="kimi-k3", cost=0.0, allowance=0.0):
    insert_request(conn, {
        "timestamp": f"{when}T10:00:00+00:00",
        "model": model,
        "billing_source": "go" if allowance else "balance",
        "cost": cost,
        "allowance": allowance,
        "profile": "work",
        "status": "ok",
    })


def test_cost_spike_is_flagged(tmp_path):
    conn = get_conn(tmp_path / "anomalies.db")
    for offset in range(10, 0, -1):
        add(conn, day(offset), cost=0.1)
    add(conn, day(0), cost=2.0, model="glm-5.3")
    result = detect_anomalies(conn, days=30, threshold=2.0, now=NOW)
    assert len(result["anomalies"]) == 1
    anomaly = result["anomalies"][0]
    assert anomaly["day"] == day(0)
    assert anomaly["metric"] == "cost"
    assert anomaly["value"] == 2.0
    assert anomaly["top_models"][0]["model"] == "glm-5.3"
    conn.close()


def test_allowance_spike_is_flagged_when_cost_is_zero(tmp_path):
    conn = get_conn(tmp_path / "anomalies.db")
    for offset in range(10, 0, -1):
        add(conn, day(offset), allowance=0.1)
    add(conn, day(0), allowance=5.0)
    result = detect_anomalies(conn, days=30, threshold=2.0, now=NOW)
    assert len(result["anomalies"]) == 1
    assert result["anomalies"][0]["metric"] == "allowance"
    conn.close()


def test_needs_enough_baseline_days(tmp_path):
    conn = get_conn(tmp_path / "anomalies.db")
    for offset in range(2, -1, -1):
        add(conn, day(offset), cost=0.1 if offset else 5.0)
    result = detect_anomalies(conn, days=30, now=NOW)
    assert result["anomalies"] == []
    conn.close()


def test_anomalies_respect_filters(tmp_path):
    conn = get_conn(tmp_path / "anomalies.db")
    for offset in range(10, 0, -1):
        add(conn, day(offset), cost=0.1)
    insert_request(conn, {
        "timestamp": f"{day(0)}T10:00:00+00:00",
        "model": "kimi-k3",
        "billing_source": "balance",
        "cost": 9.0,
        "profile": "other",
        "status": "ok",
    })
    result = detect_anomalies(conn, filters={"profile": "work"}, days=30, now=NOW)
    assert result["anomalies"] == []
    conn.close()
