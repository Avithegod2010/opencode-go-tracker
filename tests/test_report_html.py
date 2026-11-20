import json
from datetime import datetime, timedelta, timezone

from opencode_tracker.db import get_conn, insert_request
from opencode_tracker.report_html import render_html_report, report_data, write_report

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def seed(conn):
    for offset in range(6, 0, -1):
        insert_request(conn, {
            "timestamp": (NOW - timedelta(days=offset)).isoformat(),
            "model": "kimi-k3",
            "billing_source": "go",
            "allowance": 1.0,
            "cost": 0.0,
            "project": "tracker",
            "status": "ok",
        })
    insert_request(conn, {
        "timestamp": NOW.isoformat(),
        "model": "<script>alert(1)</script>",
        "billing_source": "balance",
        "cost": 2.0,
        "project": "other",
        "status": "ok",
    })


def test_report_html_sections_and_escaping(tmp_path):
    conn = get_conn(tmp_path / "report.db")
    seed(conn)
    html = render_html_report(conn, plan="go", now=NOW)
    assert "<html" in html
    assert "OpenCode Go Tracker report" in html
    assert "Burn-rate forecast" in html
    assert "Plan comparison" in html
    assert "Attribution by project" in html
    assert "Anomalies" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    conn.close()


def test_report_data_is_json_serializable(tmp_path):
    conn = get_conn(tmp_path / "report.db")
    seed(conn)
    data = report_data(conn, plan="go", now=NOW)
    assert set(["analytics", "forecast", "plan_sim", "anomalies", "attribution"]) <= set(data)
    json.dumps(data)
    conn.close()


def test_write_report_html_and_json(tmp_path):
    conn = get_conn(tmp_path / "report.db")
    seed(conn)
    html_path = tmp_path / "report.html"
    json_path = tmp_path / "report.json"
    write_report(conn, html_path, fmt="html", plan="go")
    write_report(conn, json_path, fmt="json", plan="go")
    assert html_path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert "analytics" in payload
    conn.close()
