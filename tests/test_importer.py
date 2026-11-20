import json
import sqlite3
from pathlib import Path

import pytest

from opencode_tracker.db import get_conn, count_requests
from opencode_tracker.importer import import_sessions

NOW_MS = 1795078400000
SECRET = "SECRET_PROMPT_SHOULD_NOT_BE_IMPORTED"


def make_source(path: Path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT);
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT,
            cost REAL, tokens_input INTEGER, tokens_output INTEGER,
            tokens_reasoning INTEGER, tokens_cache_read INTEGER,
            tokens_cache_write INTEGER, model TEXT,
            time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT,
            cost REAL, tokens_input INTEGER, tokens_output INTEGER,
            tokens_reasoning INTEGER, tokens_cache_read INTEGER,
            tokens_cache_write INTEGER, model TEXT,
            time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT, type TEXT,
            time_created INTEGER, data TEXT
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, data TEXT
        );
        CREATE TABLE credential (id TEXT, value TEXT);
        """
    )
    conn.execute("INSERT INTO project VALUES ('p1', 'E:/Projects/tracker', 'Tracker')")
    conn.execute("INSERT INTO credential VALUES ('cred1', ?)", (SECRET,))
    conn.execute(
        "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ses_v2", "p1", "E:/Projects/tracker", "Session A", 0.5, 100, 20, 5, 50, 0,
         json.dumps({"id": "kimi-k3", "providerID": "opencode-go"}), NOW_MS - 5000, NOW_MS),
    )
    msg1 = {
        "role": "assistant", "cost": 0.2,
        "tokens": {"input": 40, "output": 10, "reasoning": 0, "cache": {"read": 20, "write": 0}},
        "model": {"id": "kimi-k3", "providerID": "opencode-go"},
        "time": {"created": NOW_MS - 4000, "completed": NOW_MS - 3000},
        "finish": "stop", "content": SECRET,
    }
    msg2 = {
        "role": "assistant", "cost": 0.1,
        "tokens": {"input": 30, "output": 5, "reasoning": 0, "cache": {"read": 10, "write": 0}},
        "model": {"id": "kimi-k3", "providerID": "opencode-go"},
        "time": {"created": NOW_MS - 2000, "completed": NOW_MS - 1000},
        "finish": "stop", "content": SECRET,
    }
    conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?)",
                 ("msg_v2_1", "ses_v2", "assistant", NOW_MS - 4000, json.dumps(msg1)))
    conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?)",
                 ("msg_v2_2", "ses_v2", "assistant", NOW_MS - 2000, json.dumps(msg2)))
    # A non-assistant row must be ignored.
    conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?)",
                 ("msg_user", "ses_v2", "user", NOW_MS - 4500, json.dumps({"text": SECRET})))

    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ses_v1", None, "E:/Projects/older", "Older session", 0.05, 10, 2, 0, 0, 0,
         json.dumps({"id": "glm-5.3", "providerID": "opencode"}), NOW_MS - 9000, NOW_MS - 8000),
    )
    v1 = {
        "role": "assistant", "cost": 0.05,
        "tokens": {"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "modelID": "glm-5.3", "providerID": "opencode",
        "time": {"created": NOW_MS - 8500, "completed": NOW_MS - 8000},
        "content": SECRET,
    }
    conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                 ("msg_v1_1", "ses_v1", NOW_MS - 8500, json.dumps(v1)))
    conn.commit()
    conn.close()
    return path


def test_imports_v2_and_v1_with_adjustment(tmp_path):
    source = make_source(tmp_path / "opencode.db")
    target = get_conn(tmp_path / "tracker.db")

    stats = import_sessions(target, source_db=str(source), member="alice", profile="work")

    assert stats["sessions_seen"] == 2
    assert stats["sessions_imported"] == 2
    assert stats["messages_seen"] == 3
    assert stats["messages_imported"] == 3
    assert stats["adjustment_rows"] == 1
    assert stats["total_cost"] == pytest.approx(0.55)
    assert stats["total_tokens"] == 187
    assert count_requests(target) == 4

    rows = target.execute(
        "SELECT * FROM requests WHERE source='import' ORDER BY id"
    ).fetchall()
    kinds = [row["request_kind"] for row in rows]
    assert kinds == ["session", "session", "session", "session-adjust"]
    adjustment = rows[3]
    assert adjustment["cost"] == pytest.approx(0.2)
    assert adjustment["input_tokens"] == 30
    assert adjustment["output_tokens"] == 5
    assert adjustment["reasoning_tokens"] == 5
    assert adjustment["cache_read"] == 20
    assert adjustment["external_id"] == "session:ses_v2:adjust"

    v2 = [row for row in rows if row["session_id"] == "ses_v2" and row["request_kind"] == "session"]
    assert all(row["project"] == "tracker" for row in v2)
    assert all(row["billing_source"] == "go" for row in v2)
    assert all(row["allowance"] == 0 for row in v2)
    v1 = [row for row in rows if row["session_id"] == "ses_v1"]
    assert v1[0]["model"] == "glm-5.3"
    assert v1[0]["project"] == "older"
    assert v1[0]["billing_source"] == "balance"

    sessions = target.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    assert [s["id"] for s in sessions] == ["ses_v1", "ses_v2"]
    assert sessions[1]["total_cost"] == pytest.approx(0.5)

    # Prompts/content and credentials are never copied into the tracker DB.
    dump = "\n".join(str(dict(row)) for row in target.execute("SELECT * FROM requests"))
    assert SECRET not in dump
    target.close()


def test_reimport_is_idempotent(tmp_path):
    source = make_source(tmp_path / "opencode.db")
    target = get_conn(tmp_path / "tracker.db")
    import_sessions(target, source_db=str(source))
    second = import_sessions(target, source_db=str(source))

    assert second["messages_imported"] == 0
    assert second["duplicates_skipped"] == 3
    assert second["adjustment_rows"] == 0
    assert count_requests(target) == 4
    target.close()


def test_dry_run_writes_nothing(tmp_path):
    source = make_source(tmp_path / "opencode.db")
    target = get_conn(tmp_path / "tracker.db")
    stats = import_sessions(target, source_db=str(source), dry_run=True)
    assert stats["messages_imported"] == 3
    assert count_requests(target) == 0
    target.close()


def test_since_filters_sessions(tmp_path):
    source = make_source(tmp_path / "opencode.db")
    target = get_conn(tmp_path / "tracker.db")
    stats = import_sessions(target, source_db=str(source), since="2026-11-19T00:00:00+00:00")
    assert stats["sessions_seen"] == 2  # both rows are around the same timestamp
    target.close()


def test_limit_selects_most_recent_session(tmp_path):
    source = make_source(tmp_path / "opencode.db")
    target = get_conn(tmp_path / "tracker.db")
    stats = import_sessions(target, source_db=str(source), limit_sessions=1)
    assert stats["sessions_seen"] == 2
    assert stats["sessions_selected"] == 1
    assert [row[0] for row in target.execute("SELECT DISTINCT session_id FROM requests")] == ["ses_v2"]
    target.close()


def test_missing_source_raises(tmp_path):
    target = get_conn(tmp_path / "tracker.db")
    with pytest.raises(FileNotFoundError):
        import_sessions(target, source_db=str(tmp_path / "missing.db"))
    target.close()
