import sqlite3
from datetime import datetime, timezone

import pytest

from opencode_tracker import db
from opencode_tracker.db import (
    add_profile,
    count_requests,
    current_plan,
    get_conn,
    get_profile,
    insert_request,
    list_profiles,
    normalize_bound,
    plan_history,
    query_distinct,
    query_totals,
    remove_profile,
    set_plan,
    to_epoch,
    upsert_session,
)

OLD_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT,
    endpoint TEXT,
    billing_source TEXT,
    cost REAL,
    list_cost REAL,
    allowance REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_read INTEGER,
    cache_write_5m INTEGER,
    cache_write_1h INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    estimated INTEGER,
    status TEXT,
    error TEXT,
    request_id TEXT,
    raw_json TEXT
);
"""


def test_new_database_has_v2_schema(tmp_path):
    conn = get_conn(tmp_path / "new.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    cols = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    for column in ("epoch", "profile", "plan", "member", "session_id", "project",
                   "source", "external_id", "http_status", "latency_ms"):
        assert column in cols
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"profiles", "plan_history", "sessions"} <= tables
    conn.close()


def test_v1_database_migrates_in_place(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    ts = "2026-11-01T10:00:00+00:00"
    raw.execute(
        "INSERT INTO requests (timestamp, model, billing_source, cost, status) VALUES (?,?,?,?,?)",
        (ts, "kimi-k3", "go", 0.0, "ok"),
    )
    raw.commit()
    raw.close()

    conn = get_conn(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    row = conn.execute("SELECT * FROM requests").fetchone()
    assert row["model"] == "kimi-k3"
    assert row["epoch"] == pytest.approx(to_epoch(ts))
    assert row["source"] is None
    conn.close()


def test_insert_and_filter_requests(tmp_path):
    conn = get_conn(tmp_path / "filters.db")
    insert_request(conn, {
        "timestamp": "2026-11-01T10:00:00+00:00",
        "model": "kimi-k3",
        "billing_source": "go",
        "cost": 0.0,
        "allowance": 1.0,
        "profile": "work",
        "member": "alice",
        "project": "tracker",
        "session_id": "ses_1",
        "source": "proxy",
    })
    insert_request(conn, {
        "timestamp": "2026-11-02T10:00:00+00:00",
        "model": "glm-5.3",
        "billing_source": "balance",
        "cost": 0.5,
        "profile": "personal",
        "member": "bob",
        "project": "other",
        "source": "log",
    })

    assert count_requests(conn) == 2
    assert count_requests(conn, {"profile": "work"}) == 1
    assert count_requests(conn, {"from": "2026-11-02", "to": "2026-11-02"}) == 1
    assert count_requests(conn, {"model": "kimi-k3"}) == 1
    assert count_requests(conn, {"model": ["kimi-k3", "glm-5.3"]}) == 2

    totals = query_totals(conn, {"member": "alice"})
    assert len(totals) == 1
    assert totals[0]["model"] == "kimi-k3"
    assert totals[0]["sum_allowance"] == pytest.approx(1.0)

    assert query_distinct(conn, "model") == ["glm-5.3", "kimi-k3"]
    assert query_distinct(conn, "source", {"profile": "work"}) == ["proxy"]
    with pytest.raises(ValueError):
        query_distinct(conn, "raw_json")
    conn.close()


def test_duplicate_external_id_is_ignored(tmp_path):
    conn = get_conn(tmp_path / "dupes.db")
    base = {
        "timestamp": "2026-11-01T10:00:00+00:00",
        "model": "kimi-k3",
        "external_id": "msg_123",
        "source": "import",
    }
    assert insert_request(conn, base, ignore_duplicate=True) > 0
    assert insert_request(conn, base, ignore_duplicate=True) == 0
    assert count_requests(conn) == 1
    conn.close()


def test_profiles_crud(tmp_path):
    conn = get_conn(tmp_path / "profiles.db")
    add_profile(conn, "work", "https://opencode.ai/inference/go", plan="go",
                key_env="OPENCODE_KEY_WORK", project="tracker", member="alice")
    profiles = list_profiles(conn)
    assert [p["name"] for p in profiles] == ["work"]
    assert get_profile(conn, "work")["key_env"] == "OPENCODE_KEY_WORK"

    add_profile(conn, "work", "https://opencode.ai/inference", plan="go_plus")
    assert get_profile(conn, "work")["plan"] == "go_plus"
    assert get_profile(conn, "work")["key_env"] is None

    assert remove_profile(conn, "work") is True
    assert remove_profile(conn, "work") is False
    conn.close()


def test_plan_history_resolves_current_plan(tmp_path):
    conn = get_conn(tmp_path / "plans.db")
    assert current_plan(conn) is None
    set_plan(conn, "go", effective_from="2026-10-01T00:00:00+00:00")
    set_plan(conn, "go_plus", effective_from="2026-11-15T00:00:00+00:00")
    assert current_plan(conn, at="2026-11-01T00:00:00+00:00") == "go"
    assert current_plan(conn, at="2026-11-20T00:00:00+00:00") == "go_plus"
    assert current_plan(conn, at="2026-09-01T00:00:00+00:00") is None
    assert len(plan_history(conn)) == 2
    conn.close()


def test_upsert_session_merges_fields(tmp_path):
    conn = get_conn(tmp_path / "sessions.db")
    upsert_session(conn, "ses_1", project="tracker", title="First")
    upsert_session(conn, "ses_1", total_cost=1.25, total_tokens=100)
    row = conn.execute("SELECT * FROM sessions WHERE id='ses_1'").fetchone()
    assert row["project"] == "tracker"
    assert row["title"] == "First"
    assert row["total_cost"] == pytest.approx(1.25)
    assert row["total_tokens"] == 100
    conn.close()


def test_normalize_bound_date_only():
    assert normalize_bound("2026-11-02").startswith("2026-11-02T00:00:00")
    assert normalize_bound("2026-11-02", end=True).startswith("2026-11-02T23:59:59")
    assert normalize_bound("2026-11-02T10:00:00+00:00") == "2026-11-02T10:00:00+00:00"
    assert normalize_bound(None) is None


def test_to_epoch_accepts_ms_and_iso():
    assert to_epoch(1795078408194) == pytest.approx(1795078408.194)
    assert to_epoch("2026-11-19T08:00:00Z") == pytest.approx(
        datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc).timestamp()
    )
    assert to_epoch("not-a-time") is None
