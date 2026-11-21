"""The optional shared-secret gate on /api/* (TRACKER_API_TOKEN)."""

import pytest


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    import app as module

    monkeypatch.setattr(module, "DB_PATH", tmp_path / "token.db")
    monkeypatch.setattr(module, "CSV_PATH", tmp_path / "token.csv")
    monkeypatch.setattr(module, "PERIOD_START_DAY", 1)
    module.app.config.update(TESTING=True)
    return module


@pytest.fixture()
def client(app_module):
    with app_module.app.test_client() as test_client:
        yield test_client


def test_unset_token_keeps_local_behaviour(app_module, client):
    app_module.API_TOKEN = ""
    assert client.get("/api/profiles").status_code == 200


def test_health_is_always_reachable(app_module, client):
    app_module.API_TOKEN = "s3cret-token"
    # Probes and the watchdog must not need a credential.
    assert client.get("/api/health").status_code == 200


def test_missing_token_is_rejected(app_module, client):
    app_module.API_TOKEN = "s3cret-token"
    assert client.get("/api/profiles").status_code == 401


def test_wrong_token_is_rejected(app_module, client):
    app_module.API_TOKEN = "s3cret-token"
    res = client.get("/api/profiles", headers={"X-Tracker-Token": "nope"})
    assert res.status_code == 401


def test_header_token_is_accepted(app_module, client):
    app_module.API_TOKEN = "s3cret-token"
    res = client.get("/api/profiles", headers={"X-Tracker-Token": "s3cret-token"})
    assert res.status_code == 200


def test_query_token_is_accepted(app_module, client):
    app_module.API_TOKEN = "s3cret-token"
    assert client.get("/api/profiles?token=s3cret-token").status_code == 200


def test_state_changing_route_is_gated_too(app_module, client):
    app_module.API_TOKEN = "s3cret-token"
    res = client.post("/api/plan", json={"plan": "go"})
    assert res.status_code == 401
    res = client.post(
        "/api/plan",
        json={"plan": "go"},
        headers={"X-Tracker-Token": "s3cret-token"},
    )
    assert res.status_code != 401
