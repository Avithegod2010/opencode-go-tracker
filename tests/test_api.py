import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from opencode_tracker.db import add_profile, get_conn, insert_request
from opencode_tracker.logger import HttpResult

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    import app as module

    monkeypatch.setattr(module, "DB_PATH", tmp_path / "api.db")
    monkeypatch.setattr(module, "CSV_PATH", tmp_path / "api.csv")
    monkeypatch.setattr(module, "PERIOD_START_DAY", 1)
    # /api/* is token-gated by default; these tests exercise route behaviour.
    monkeypatch.setattr(module, "API_TOKEN", "test-token")
    module.app.config.update(TESTING=True)
    return module


@pytest.fixture()
def client(app_module):
    with app_module.app.test_client() as test_client:
        test_client.environ_base["HTTP_X_TRACKER_TOKEN"] = app_module.API_TOKEN
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


def test_report_filters_and_options(app_module, client):
    conn = get_conn(str(app_module.DB_PATH))
    now = datetime.now(timezone.utc)
    insert_request(conn, {
        "timestamp": now.isoformat(),
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 1.5,
        "profile": "work",
        "member": "alice",
        "project": "tracker",
        "source": "proxy",
        "status": "ok",
    })
    insert_request(conn, {
        "timestamp": (now - timedelta(days=2)).isoformat(),
        "model": "glm-5.3",
        "billing_source": "balance",
        "cost": 0.5,
        "profile": "personal",
        "member": "bob",
        "project": "other",
        "source": "import",
        "status": "ok",
    })
    conn.close()

    res = client.get("/api/report?plan=go&profile=work&member=alice&project=tracker&source=proxy")
    body = res.get_json()
    assert body["ok"] is True
    assert body["filters"] == {
        "profile": "work", "member": "alice", "project": "tracker", "source": "proxy"
    }
    assert body["totals"]["cost"] == pytest.approx(0.0)
    assert body["month_totals"]["n"] == 1
    assert [w["model"] for w in body["windows"]] == ["kimi-k3"]
    assert [r["model"] for r in body["recent"]] == ["kimi-k3"]

    options = client.get("/api/filters/options").get_json()
    assert options["models"] == ["glm-5.3", "kimi-k3"]
    assert options["profiles"] == ["personal", "work"]
    assert options["projects"] == ["other", "tracker"]
    assert options["sources"] == ["import", "proxy"]

    injected = client.get("/api/report?plan=go&profile=x%27%20OR%201%3D1--")
    assert injected.status_code == 200
    assert injected.get_json()["month_totals"]["n"] == 0


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


# --- profiles ------------------------------------------------------------

LOG_PAYLOAD = {
    "id": "chatcmpl_profile",
    "choices": [{"message": {"content": "hi"}}],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "cost": 0.0,
        "opencode": {
            "billing_source": "go",
            "allowance": 0.05,
            "tokens": {"input": 10, "output": 2},
        },
    },
}


def test_profiles_list_never_exposes_key(app_module, client):
    conn = get_conn(str(app_module.DB_PATH))
    add_profile(
        conn,
        "work",
        "https://opencode.ai/inference/go",
        plan="go",
        key_env="OPENCODE_KEY_WORK",
        project="tracker",
        member="alice",
    )
    conn.close()

    body = client.get("/api/profiles").get_json()
    assert body["ok"] is True
    profile = body["profiles"][0]
    assert profile["name"] == "work"
    assert profile["key_env"] == "OPENCODE_KEY_WORK"
    assert "key" not in profile


def test_log_uses_profile_metadata(app_module, client, monkeypatch):
    conn = get_conn(str(app_module.DB_PATH))
    add_profile(
        conn,
        "work",
        "https://opencode.ai/inference/go",
        plan="go_plus",
        key_env="OPENCODE_KEY_WORK",
        project="tracker",
        member="alice",
    )
    conn.close()
    monkeypatch.setenv("OPENCODE_KEY_WORK", "oc_sk_profile_key")
    monkeypatch.setattr(
        app_module.OpencodeLogger,
        "_post",
        lambda self, path, payload: HttpResult(LOG_PAYLOAD, "req_profile", None, 200, 12),
    )

    res = client.post(
        "/api/log",
        json={"profile": "work", "model": "kimi-k3", "message": "hi"},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["profile"] == "work"
    assert body["plan"] == "go_plus"

    check = get_conn(str(app_module.DB_PATH))
    row = check.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()
    assert row["profile"] == "work"
    assert row["plan"] == "go_plus"
    assert row["member"] == "alice"
    assert row["project"] == "tracker"
    assert row["http_status"] == 200
    check.close()


def test_log_profile_key_is_loopback_only(app_module, client, monkeypatch):
    conn = get_conn(str(app_module.DB_PATH))
    add_profile(conn, "work", "https://opencode.ai/inference/go", key_env="OPENCODE_KEY_WORK")
    conn.close()
    monkeypatch.setenv("OPENCODE_KEY_WORK", "oc_sk_profile_key")

    res = client.post(
        "/api/log",
        json={"profile": "work", "model": "kimi-k3"},
        environ_base={"REMOTE_ADDR": "192.168.1.9"},
    )
    assert res.status_code == 401
    assert "loopback" in res.get_json()["error"]


def test_key_check_uses_profile(app_module, client, monkeypatch):
    conn = get_conn(str(app_module.DB_PATH))
    add_profile(conn, "work", "https://opencode.ai/inference", key_env="OPENCODE_KEY_WORK")
    conn.close()
    monkeypatch.setenv("OPENCODE_KEY_WORK", "oc_sk_profile_key")
    captured = {}

    def fake_check(key, base_url):
        captured["key"] = key
        captured["base_url"] = base_url
        return {"ok": True, "status_code": 200, "model_count": 1, "go_model_count": 0,
                "go_models": [], "plans_seen": [], "error": None, "hint": None,
                "key_prefix": "oc_sk_pr...", "base_url": base_url + "/v1/models"}

    monkeypatch.setattr(app_module, "check_key", fake_check)
    res = client.post("/api/key-check", json={"profile": "work"})
    assert res.status_code == 200
    assert captured["key"] == "oc_sk_profile_key"
    assert captured["base_url"] == "https://opencode.ai/inference"


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


def test_import_sessions_endpoint(app_module, client, monkeypatch, tmp_path):
    source = tmp_path / "opencode.db"
    source.write_bytes(b"")
    monkeypatch.setattr(app_module, "find_opencode_db", lambda: source)
    monkeypatch.setattr(
        app_module,
        "import_sessions",
        lambda conn, **kwargs: {
            "source_db": str(source),
            "sessions_seen": 10,
            "sessions_selected": 10,
            "sessions_imported": 10,
            "messages_seen": 50,
            "messages_imported": 48,
            "duplicates_skipped": 2,
            "adjustment_rows": 1,
            "total_cost": 1.25,
            "total_tokens": 1000,
            "dry_run": False,
        },
    )
    res = client.post("/api/import-sessions", json={})
    body = res.get_json()
    assert res.status_code == 200
    assert body["ok"] is True
    assert body["sessions_imported"] == 10
    assert body["messages_imported"] == 48


def test_import_sessions_missing_source(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "find_opencode_db", lambda: None)
    res = client.post("/api/import-sessions", json={})
    assert res.status_code == 404


def test_analytics_endpoint(app_module, client):
    conn = get_conn(str(app_module.DB_PATH))
    now = datetime.now(timezone.utc)
    insert_request(conn, {
        "timestamp": now.isoformat(),
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 1.5,
        "cache_read": 100,
        "input_tokens": 100,
        "profile": "work",
        "source": "proxy",
        "status": "ok",
    })
    insert_request(conn, {
        "timestamp": (now - timedelta(days=1)).isoformat(),
        "model": "glm-5.3",
        "billing_source": "balance",
        "cost": 0.25,
        "profile": "personal",
        "source": "import",
        "status": "ok",
    })
    conn.close()

    body = client.get("/api/analytics?days=7").get_json()
    assert body["ok"] is True
    assert body["kpi"]["requests"] == 2
    assert body["kpi"]["cost"] == pytest.approx(0.25)
    assert len(body["days"]) == 7
    assert body["series"]

    filtered = client.get("/api/analytics?days=7&profile=work").get_json()
    assert filtered["kpi"]["requests"] == 1
    assert filtered["kpi"]["cost"] == 0
    assert [row["model"] for row in filtered["by_model"]] == ["kimi-k3"]


def test_forecast_endpoint(app_module, client):
    conn = get_conn(str(app_module.DB_PATH))
    insert_request(conn, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 1.5,
        "status": "ok",
    })
    conn.close()
    body = client.get("/api/forecast?plan=go&period_start_day=1").get_json()
    assert body["ok"] is True
    assert body["models"][0]["model"] == "kimi-k3"
    assert body["models"][0]["projected"] >= 1.5
    assert body["models"][0]["projected_pct"] is not None
    assert body["meta"]["period_end"]


def test_plan_endpoint(app_module, client):
    body = client.get("/api/plan").get_json()
    assert body["ok"] is True
    assert body["current"] is None
    assert body["history"] == []

    res = client.post("/api/plan", json={
        "plan": "go",
        "effective_from": "2026-11-01T00:00:00+00:00",
        "note": "start",
    })
    assert res.status_code == 200
    body = client.get("/api/plan").get_json()
    assert body["current"] == "go"
    assert len(body["history"]) == 1

    client.post("/api/plan", json={
        "plan": "go_plus",
        "effective_from": "2026-11-15T00:00:00+00:00",
    })
    body = client.get("/api/plan").get_json()
    assert body["current"] == "go_plus"
    assert [row["plan"] for row in body["history"]] == ["go", "go_plus"]
    assert client.post("/api/plan", json={}).status_code == 400


def test_plan_sim_endpoint(app_module, client):
    conn = get_conn(str(app_module.DB_PATH))
    now = datetime.now(timezone.utc)
    for model, allowance in (("kimi-k3", 20.0), ("glm-5.3-flash", 50.0)):
        insert_request(conn, {
            "timestamp": now.isoformat(),
            "model": model,
            "billing_source": "go",
            "allowance": allowance,
            "status": "ok",
        })
    conn.close()
    body = client.get("/api/plan-sim?period_start_day=1").get_json()
    assert body["ok"] is True
    plans = {row["plan"]: row for row in body["plans"]}
    assert plans["go"]["models_over"] == 1
    assert plans["go_plus"]["fits"] is True
    assert body["models"][0]["model"] in ("kimi-k3", "glm-5.3-flash")


def test_anomalies_endpoint(app_module, client):
    conn = get_conn(str(app_module.DB_PATH))
    now = datetime.now(timezone.utc)
    for offset in range(10, 0, -1):
        insert_request(conn, {
            "timestamp": (now - timedelta(days=offset)).isoformat(),
            "model": "kimi-k3",
            "billing_source": "balance",
            "cost": 0.1,
            "status": "ok",
        })
    insert_request(conn, {
        "timestamp": now.isoformat(),
        "model": "glm-5.3",
        "billing_source": "balance",
        "cost": 3.0,
        "status": "ok",
    })
    conn.close()
    body = client.get("/api/anomalies?days=30&threshold=2").get_json()
    assert body["ok"] is True
    assert len(body["anomalies"]) == 1
    assert body["anomalies"][0]["metric"] == "cost"
    assert body["anomalies"][0]["top_models"][0]["model"] == "glm-5.3"


def test_attribution_endpoint(app_module, client):
    conn = get_conn(str(app_module.DB_PATH))
    now = datetime.now(timezone.utc)
    insert_request(conn, {
        "timestamp": now.isoformat(), "model": "kimi-k3", "billing_source": "go",
        "allowance": 2.0, "project": "tracker", "session_id": "ses_a",
        "member": "alice", "status": "ok",
    })
    insert_request(conn, {
        "timestamp": now.isoformat(), "model": "glm-5.3", "billing_source": "balance",
        "cost": 1.0, "project": "other", "session_id": "ses_b",
        "member": "bob", "status": "ok",
    })
    conn.close()
    body = client.get("/api/attribution?by=project").get_json()
    assert body["ok"] is True
    assert [row["name"] for row in body["rows"]] == ["other", "tracker"]
    assert body["total_cost"] == pytest.approx(1.0)
    assert body["rows"][0]["cost_share"] == pytest.approx(100.0)
    assert client.get("/api/attribution?by=nope").status_code == 400


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
