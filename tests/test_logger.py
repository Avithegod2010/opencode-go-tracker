import pytest

from opencode_tracker.db import get_conn
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
