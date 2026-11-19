import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from opencode_tracker.db import get_conn, insert_request

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    import app as module

    monkeypatch.setattr(module, "DB_PATH", tmp_path / "api.db")
    monkeypatch.setattr(module, "CSV_PATH", tmp_path / "api.csv")
    monkeypatch.setattr(module, "PERIOD_START_DAY", 1)
    module.app.config.update(TESTING=True)
    return module


@pytest.fixture()
def client(app_module):
    with app_module.app.test_client() as test_client:
        yield test_client


def upload(name):
    return (io.BytesIO((EXAMPLES / name).read_bytes()), name)


def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["period_start_day"] == 1


def test_index_serves_dashboard(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Opencode Go Tracker" in res.data


def test_merge_examples_returns_totals_and_detailed_csv(client):
    res = client.post(
        "/api/merge",
        data={
            "files": [upload("usage_7d_example.csv"), upload("usage_30d_example.csv")],
            "plan": "go",
            "dedupe": "true",
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["rows_before"] == 29
    assert body["rows_after"] == 18
    assert body["dupes_removed"] == 11
    assert body["total_cost"] == pytest.approx(8.45)
    assert body["total_allowance"] == pytest.approx(4.42)
    assert len(body["per_model"]) == 12
    assert body["totals_csv"].startswith("model,cost,billable_cost")
    assert body["detailed_csv"].startswith("date,model,person")
    assert "kimi-k3" in body["detailed_csv"]


def test_merge_billable_only_keeps_context_totals(client):
    res = client.post(
        "/api/merge",
        data={
            "files": [upload("usage_30d_example.csv")],
            "billable_only": "true",
            "dedupe": "true",
        },
        content_type="multipart/form-data",
    )
    body = res.get_json()
    assert body["ok"] is True
    assert body["rows_filtered"] < body["rows_after"]
    assert body["total_cost"] == pytest.approx(body["total_cost_all"])
    assert body["total_allowance"] == pytest.approx(0.0)
    assert body["total_allowance_all"] > 0


def test_merge_rejects_empty_request(client):
    res = client.post("/api/merge", data={}, content_type="multipart/form-data")
    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_report_empty_when_no_db(client):
    res = client.get("/api/report?plan=go")
    body = res.get_json()
    assert body["ok"] is True
    assert body["empty"] is True


def test_report_windows_use_current_period(app_module, client):
    conn = get_conn(str(app_module.DB_PATH))
    now = datetime.now(timezone.utc)
    insert_request(conn, {
        "timestamp": now.isoformat(),
        "model": "kimi-k3",
        "billing_source": "go",
        "cost": 0.0,
        "allowance": 1.5,
        "status": "ok",
    })
    insert_request(conn, {
        "timestamp": now.isoformat(),
        "model": "claude-haiku-5-5",
        "billing_source": "balance",
        "cost": 0.25,
        "allowance": 0.0,
        "status": "ok",
    })
    conn.close()

    res = client.get("/api/report?plan=go&period_start_day=1")
    body = res.get_json()

    assert body["ok"] is True
    assert body["empty"] is False
    assert body["month_totals"]["n"] == 2
    assert body["month_totals"]["cost"] == pytest.approx(0.25)

    windows = {w["model"]: w for w in body["windows"]}
    assert windows["kimi-k3"]["month_used"] == pytest.approx(1.5)
    assert windows["kimi-k3"]["month_pct"] == pytest.approx(10.0)
    assert windows["kimi-k3"]["five_hour_used"] == pytest.approx(1.5)
    assert "claude-haiku-5-5" not in windows

    assert body["go_pct"]["kimi-k3"]["limit"] == 15
    assert body["go_pct"]["kimi-k3"]["pct"] == pytest.approx(10.0)
    assert body["window_meta"]["period_start_day"] == 1


def test_report_rejects_bad_period_day(app_module, client):
    conn = get_conn(str(app_module.DB_PATH))
    insert_request(conn, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 1.0,
        "status": "ok",
    })
    conn.close()

    res = client.get("/api/report?plan=go&period_start_day=nope")
    assert res.status_code == 200
    assert res.get_json()["window_meta"]["period_start_day"] == 1


def test_log_requires_a_valid_key(client):
    res = client.post("/api/log", json={"api_key": "nope"})
    assert res.status_code == 401
    assert "OPENCODE_API_KEY" in res.get_json()["error"]


def test_key_check_requires_a_key(client, monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    res = client.post("/api/key-check", json={})
    assert res.status_code == 401


def test_key_check_rejects_bad_base_url(client):
    res = client.post("/api/key-check", json={"api_key": "oc_sk_fake", "base_url": "https://evil.example"})
    assert res.status_code == 400


def test_key_check_returns_model_summary(app_module, client, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "check_key",
        lambda key, base_url: {
            "ok": True,
            "status_code": 200,
            "model_count": 27,
            "go_model_count": 27,
            "go_models": ["kimi-k3"],
            "plans_seen": ["go"],
            "error": None,
            "hint": None,
            "key_prefix": "oc_sk_12...",
            "base_url": base_url + "/v1/models",
        },
    )
    res = client.post(
        "/api/key-check",
        json={"api_key": "oc_sk_1234567890", "base_url": "https://opencode.ai/inference/go"},
    )
    body = res.get_json()
    assert res.status_code == 200
    assert body["ok"] is True
    assert body["model_count"] == 27
    assert body["go_model_count"] == 27


def test_go_limits_lists_known_models(client):
    body = client.get("/api/go-limits").get_json()
    assert body["plans"] == ["go", "go_plus", "go_teams"]
    assert body["limits"]["kimi-k3"]["go"] == 15
    assert "synced" in body


def test_sync_limits_endpoint(app_module, client, monkeypatch):
    def fake_sync(**kwargs):
        return {
            "source": "https://example.test/v1/models",
            "fetched_at": "2026-11-19T00:00:00+00:00",
            "limits": {"kimi-k3": {"go": 15, "go_plus": 60, "go_teams": 20}},
        }

    monkeypatch.setattr(app_module, "sync_limits", fake_sync)
    res = client.post("/api/sync-limits", json={})
    body = res.get_json()
    assert res.status_code == 200
    assert body["models"] == 1
    assert body["limits"]["kimi-k3"]["go"] == 15


# --- security hardening -------------------------------------------------

def test_rejects_cross_site_origin(client):
    res = client.post("/api/log", json={"model": "kimi-k3"}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    assert "Blocked" in res.get_json()["error"]


def test_rejects_dns_rebinding_host(client):
    res = client.get("/api/health", headers={"Host": "evil.example:8000"})
    assert res.status_code == 403


def test_allows_same_origin(client):
    res = client.get("/api/health", headers={"Origin": "http://localhost"})
    assert res.status_code == 200


def test_log_rejects_non_opencode_base_url(client):
    res = client.post(
        "/api/log",
        json={"api_key": "oc_sk_fake", "base_url": "https://evil.example/collect"},
    )
    assert res.status_code == 400
    assert "not allowed" in res.get_json()["error"]


def test_log_rejects_plain_http_base_url(client):
    res = client.post(
        "/api/log",
        json={"api_key": "oc_sk_fake", "base_url": "http://opencode.ai/inference"},
    )
    assert res.status_code == 400
    assert "https" in res.get_json()["error"]


def test_non_local_client_cannot_use_server_key(client, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "oc_sk_server_key_not_real")
    res = client.post(
        "/api/log",
        json={"model": "kimi-k3"},
        environ_base={"REMOTE_ADDR": "192.168.1.9"},
    )
    assert res.status_code == 401
    assert "loopback" in res.get_json()["error"]


def test_sync_limits_rejects_ssrf(client):
    res = client.post(
        "/api/sync-limits",
        json={"base_url": "https://evil.example/v1/models"},
    )
    assert res.status_code == 400
    assert "not allowed" in res.get_json()["error"]


def test_security_headers(client):
    res = client.get("/")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in res.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]
