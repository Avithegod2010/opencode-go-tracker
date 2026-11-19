import pytest

from opencode_tracker.db import add_profile, get_conn
from opencode_tracker.profiles import profile_summary, resolve_profile


def test_default_profile_uses_environment(tmp_path, monkeypatch):
    conn = get_conn(tmp_path / "p.db")
    monkeypatch.setenv("OPENCODE_API_KEY", "oc_sk_env")
    monkeypatch.setenv("OPENCODE_BASE_URL", "https://opencode.ai/inference/go")
    monkeypatch.setenv("OPENCODE_PLAN", "go")
    result = resolve_profile(conn)
    assert result["name"] == "default"
    assert result["base_url"] == "https://opencode.ai/inference/go"
    assert result["key"] == "oc_sk_env"
    assert result["plan"] == "go"
    conn.close()


def test_named_profile_resolves_key_env(tmp_path, monkeypatch):
    conn = get_conn(tmp_path / "p.db")
    add_profile(conn, "work", "https://opencode.ai/inference", plan="go_plus",
                key_env="OPENCODE_KEY_WORK", project="tracker", member="alice")
    monkeypatch.setenv("OPENCODE_KEY_WORK", "oc_sk_work")
    monkeypatch.setenv("OPENCODE_API_KEY", "oc_sk_fallback")
    result = resolve_profile(conn, "work")
    assert result["base_url"] == "https://opencode.ai/inference"
    assert result["plan"] == "go_plus"
    assert result["key"] == "oc_sk_work"
    assert result["member"] == "alice"
    conn.close()


def test_named_profile_falls_back_to_profile_key_env(tmp_path, monkeypatch):
    conn = get_conn(tmp_path / "p.db")
    add_profile(conn, "ci", "https://opencode.ai/inference/go")
    monkeypatch.setenv("OPENCODE_PROFILE_CI_KEY", "oc_sk_ci")
    result = resolve_profile(conn, "ci")
    assert result["key"] == "oc_sk_ci"
    conn.close()


def test_unknown_profile_raises(tmp_path):
    conn = get_conn(tmp_path / "p.db")
    with pytest.raises(KeyError, match="Unknown profile"):
        resolve_profile(conn, "missing")
    conn.close()


def test_summary_never_contains_key(tmp_path, monkeypatch):
    conn = get_conn(tmp_path / "p.db")
    add_profile(conn, "work", "https://opencode.ai/inference/go", key_env="OPENCODE_KEY_WORK")
    row = conn.execute("SELECT * FROM profiles WHERE name='work'").fetchone()
    summary = profile_summary(row)
    assert "key" not in summary
    assert summary["key_env"] == "OPENCODE_KEY_WORK"
    conn.close()
