import pytest

from opencode_tracker.db import get_conn, set_plan
from opencode_tracker.logger import OpencodeLogger


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, text=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {"x-request-id": "req_123"}
        self.text = text if text is not None else str(payload)

    def json(self):
        return self._payload


OK_PAYLOAD = {
    "id": "chatcmpl_1",
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


def test_chat_completions_persists_to_db_and_csv(tmp_path, monkeypatch):
    import opencode_tracker.logger as logger_module

    calls = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        calls["json"] = json
        return FakeResponse(OK_PAYLOAD)

    monkeypatch.setattr(logger_module.requests, "post", fake_post)

    db = tmp_path / "usage.db"
    csv_path = tmp_path / "usage.csv"
    logger = OpencodeLogger(
        db_path=db,
        csv_path=csv_path,
        api_key="test-key-not-real",
        base_url="https://opencode.ai/inference/go",
    )

    result = logger.chat_completions("kimi-k3", [{"role": "user", "content": "hi"}])

    assert result["id"] == "chatcmpl_1"
    assert calls["url"] == "https://opencode.ai/inference/go/openai/v1/chat/completions"
    assert calls["headers"]["Authorization"] == "Bearer test-key-not-real"
    assert calls["json"]["messages"][0]["content"] == "hi"

    conn = get_conn(db)
    row = conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()
    assert row["model"] == "kimi-k3"
    assert row["provider"] == "go"
    assert row["billing_source"] == "go"
    assert row["allowance"] == pytest.approx(0.05)
    assert row["input_tokens"] == 10
    assert row["status"] == "ok"
    conn.close()

    text = csv_path.read_text(encoding="utf-8")
    assert "kimi-k3" in text
    assert "go" in text.splitlines()[1]


def test_429_is_logged_and_raised(tmp_path, monkeypatch):
    import opencode_tracker.logger as logger_module

    monkeypatch.setattr(
        logger_module.requests,
        "post",
        lambda *a, **kw: FakeResponse(
            {"error": "Policy budget exceeded"}, status_code=429, text="Policy budget exceeded"
        ),
    )

    db = tmp_path / "usage.db"
    logger = OpencodeLogger(db_path=db, csv_path=None, api_key="test-key-not-real")

    with pytest.raises(RuntimeError, match="429"):
        logger.chat_completions("kimi-k3", [{"role": "user", "content": "hi"}])

    conn = get_conn(db)
    row = conn.execute("SELECT status, error FROM requests ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "429_budget_exceeded"
    assert "429" in row["error"]
    conn.close()


def test_missing_key_raises():
    logger = OpencodeLogger(db_path=None, csv_path=None, api_key="", base_url="https://x")
    with pytest.raises(ValueError, match="OPENCODE_API_KEY"):
        logger.chat_completions("kimi-k3", [{"role": "user", "content": "hi"}])


def test_503_retries_then_succeeds(tmp_path, monkeypatch):
    import opencode_tracker.logger as logger_module

    responses = [
        FakeResponse({"error": "busy"}, status_code=503, text="busy"),
        FakeResponse(OK_PAYLOAD, status_code=200),
    ]
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return responses.pop(0)

    monkeypatch.setattr(logger_module.requests, "post", fake_post)
    monkeypatch.setattr(logger_module.time, "sleep", lambda _seconds: None)

    db = tmp_path / "retry.db"
    logger = OpencodeLogger(db_path=db, csv_path=None, api_key="test-key-not-real", retries=2)
    result = logger.chat_completions("kimi-k3", [{"role": "user", "content": "hi"}])

    assert result["id"] == "chatcmpl_1"
    assert len(calls) == 2
    conn = get_conn(db)
    row = conn.execute("SELECT status, http_status, latency_ms FROM requests").fetchone()
    assert row["status"] == "ok"
    assert row["http_status"] == 200
    assert row["latency_ms"] is not None
    conn.close()


def test_5xx_retries_exhausted_and_logged(tmp_path, monkeypatch):
    import opencode_tracker.logger as logger_module

    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse({"error": "down"}, status_code=503, text="down")

    monkeypatch.setattr(logger_module.requests, "post", fake_post)
    monkeypatch.setattr(logger_module.time, "sleep", lambda _seconds: None)

    db = tmp_path / "down.db"
    logger = OpencodeLogger(db_path=db, csv_path=None, api_key="test-key-not-real", retries=1)
    with pytest.raises(RuntimeError, match="503"):
        logger.chat_completions("kimi-k3", [{"role": "user", "content": "hi"}])

    assert len(calls) == 2
    conn = get_conn(db)
    row = conn.execute("SELECT status, http_status FROM requests").fetchone()
    assert row["status"] == "5xx_upstream"
    assert row["http_status"] == 503
    conn.close()


def test_429_is_not_retried(tmp_path, monkeypatch):
    import opencode_tracker.logger as logger_module

    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return FakeResponse({"error": "budget"}, status_code=429, text="budget")

    monkeypatch.setattr(logger_module.requests, "post", fake_post)
    logger = OpencodeLogger(db_path=tmp_path / "429.db", csv_path=None, api_key="test-key-not-real", retries=3)
    with pytest.raises(RuntimeError, match="429"):
        logger.chat_completions("kimi-k3", [{"role": "user", "content": "hi"}])
    assert len(calls) == 1


def test_classify_error():
    from opencode_tracker.logger import classify_error

    assert classify_error(401, "")[0] == "401_unauthorized"
    assert classify_error(None, "HTTP 402: Insufficient account funds")[0] == "402_insufficient_funds"
    assert classify_error(503, "")[0] == "5xx_upstream"
    assert classify_error(418, "")[0] == "error"


def test_logger_uses_current_plan(tmp_path, monkeypatch):
    import opencode_tracker.logger as logger_module

    db = tmp_path / "plan.db"
    conn = get_conn(db)
    set_plan(conn, "go_plus", effective_from="2026-11-01T00:00:00+00:00")
    conn.close()
    monkeypatch.setattr(
        logger_module.requests,
        "post",
        lambda *a, **kw: FakeResponse(OK_PAYLOAD),
    )
    logger = OpencodeLogger(db_path=db, csv_path=None, api_key="test-key-not-real", retries=0)
    logger.chat_completions("kimi-k3", [{"role": "user", "content": "hi"}])

    check = get_conn(db)
    row = check.execute("SELECT plan FROM requests ORDER BY id DESC LIMIT 1").fetchone()
    assert row["plan"] == "go_plus"
    check.close()


def test_existing_csv_header_is_preserved(tmp_path):
    csv_path = tmp_path / "old.csv"
    old_header = ["timestamp", "model", "endpoint", "billing_source", "cost"]
    import csv as csv_module

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        csv_module.writer(fh).writerow(old_header)

    logger = OpencodeLogger(db_path=None, csv_path=csv_path, api_key="test-key-not-real")
    logger._log_entry("kimi-k3", "/x", {"cost": 0.1, "billing_source": "go"})

    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == old_header
    assert len(lines[1].split(",")) == len(old_header)
