import pytest

from opencode_tracker import preflight


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else str(payload)

    def json(self):
        return self._payload


def test_check_key_ok_with_go_models(monkeypatch):
    payload = {
        "object": "list",
        "data": [
            {"id": "kimi-k3", "go": {"monthly_usage": {"go": 15, "go_plus": 60, "go_teams": 20}}},
            {"id": "glm-5.3", "go": {"monthly_usage": {"go": 500}}},
            {"id": "some-paid-model"},
        ],
    }
    monkeypatch.setattr(preflight.requests, "get", lambda *a, **kw: FakeResponse(payload))

    result = preflight.check_key("oc_sk_1234567890", "https://opencode.ai/inference/go")

    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["model_count"] == 3
    assert result["go_model_count"] == 2
    assert result["go_models"] == ["glm-5.3", "kimi-k3"]
    assert result["plans_seen"] == ["go", "go_plus", "go_teams"]
    assert result["key_prefix"] == "oc_sk_12..."
    assert "oc_sk_1234567890" not in str(result)


def test_check_key_401_is_explained(monkeypatch):
    monkeypatch.setattr(
        preflight.requests,
        "get",
        lambda *a, **kw: FakeResponse({"error": "unauthorized"}, status_code=401, text="unauthorized"),
    )
    result = preflight.check_key("oc_sk_bad", "https://opencode.ai/inference")
    assert result["ok"] is False
    assert result["status_code"] == 401
    assert "401_unauthorized" in result["hint"]


def test_check_key_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(preflight.requests, "get", boom)
    result = preflight.check_key("oc_sk_bad", "https://opencode.ai/inference")
    assert result["ok"] is False
    assert "connection refused" in result["error"]
    assert result["status_code"] is None


def test_check_key_403_is_explained(monkeypatch):
    monkeypatch.setattr(
        preflight.requests,
        "get",
        lambda *a, **kw: FakeResponse({"error": "forbidden"}, status_code=403, text="forbidden"),
    )
    result = preflight.check_key("oc_sk_bad", "https://opencode.ai/inference")
    assert result["ok"] is False
    assert "403_forbidden" in result["hint"]
