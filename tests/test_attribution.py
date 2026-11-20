from datetime import datetime, timezone

import pytest

from opencode_tracker.attribution import attribute
from opencode_tracker.db import get_conn, insert_request, upsert_session

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def seed(conn):
    insert_request(conn, {
        "timestamp": NOW.isoformat(), "model": "kimi-k3", "billing_source": "go",
        "allowance": 4.0, "cost": 0.0, "project": "tracker", "session_id": "ses_a",
        "member": "alice", "profile": "work", "source": "proxy", "status": "ok",
    })
    insert_request(conn, {
        "timestamp": NOW.isoformat(), "model": "glm-5.3", "billing_source": "balance",
        "allowance": 0.0, "cost": 2.0, "project": "other", "session_id": "ses_b",
        "member": "bob", "profile": "personal", "source": "import", "status": "ok",
    })
    upsert_session(conn, "ses_a", project="tracker", title="Session A")
    upsert_session(conn, "ses_b", project="other", title="Session B")


def test_attribute_by_project(tmp_path):
    conn = get_conn(tmp_path / "attr.db")
    seed(conn)
    result = attribute(conn, "project")
    rows = {row["name"]: row for row in result["rows"]}
    assert rows["other"]["cost"] == pytest.approx(2.0)
    assert rows["other"]["cost_share"] == pytest.approx(100.0)
    assert rows["tracker"]["allowance"] == pytest.approx(4.0)
    assert rows["tracker"]["allowance_share"] == pytest.approx(100.0)
    assert result["total_cost"] == pytest.approx(2.0)
    conn.close()


def test_attribute_by_session_includes_title(tmp_path):
    conn = get_conn(tmp_path / "attr.db")
    seed(conn)
    result = attribute(conn, "session")
    rows = {row["name"]: row for row in result["rows"]}
    assert rows["ses_a"]["title"] == "Session A"
    assert rows["ses_b"]["project"] == "other"
    conn.close()


def test_attribute_respects_filters(tmp_path):
    conn = get_conn(tmp_path / "attr.db")
    seed(conn)
    result = attribute(conn, "member", filters={"profile": "work"})
    assert [row["name"] for row in result["rows"]] == ["alice"]
    assert result["total_cost"] == 0
    conn.close()


def test_attribute_rejects_unknown(tmp_path):
    conn = get_conn(tmp_path / "attr.db")
    with pytest.raises(ValueError):
        attribute(conn, "nope")
    conn.close()
