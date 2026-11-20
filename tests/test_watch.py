from datetime import datetime, timedelta, timezone

from opencode_tracker.db import get_conn, insert_request
from opencode_tracker.watch import render_watch, watch_loop

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def test_render_watch_empty(tmp_path):
    conn = get_conn(tmp_path / "watch.db")
    text = render_watch(conn, "go", now=NOW)
    assert "Go windows vs go" in text
    assert "No Go allowance logged yet" in text
    conn.close()


def test_render_watch_shows_windows(tmp_path):
    conn = get_conn(tmp_path / "watch.db")
    insert_request(conn, {
        "timestamp": (NOW - timedelta(hours=1)).isoformat(),
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 3.0,
        "status": "ok",
    })
    text = render_watch(conn, "go", now=NOW)
    assert "kimi-k3" in text
    assert "3.00" in text
    assert "100.0%" in text
    assert "over" in text
    conn.close()


def test_watch_once_prints(tmp_path, capsys):
    db = tmp_path / "watch.db"
    conn = get_conn(db)
    insert_request(conn, {
        "timestamp": NOW.isoformat(),
        "model": "kimi-k3",
        "billing_source": "go",
        "allowance": 1.0,
        "status": "ok",
    })
    conn.close()
    watch_loop(db, plan="go", once=True)
    out = capsys.readouterr().out
    assert "Go windows vs go" in out
    assert "kimi-k3" in out
