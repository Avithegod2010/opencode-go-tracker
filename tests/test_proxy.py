import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from opencode_tracker.db import get_conn
from opencode_tracker.proxy import ProxyConfig, start_proxy

USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 20,
    "cost": 0.0,
    "opencode": {
        "billing_source": "go",
        "allowance": 0.42,
        "tokens": {"input": 100, "output": 20, "reasoning": 3, "cache_read": 50},
    },
}

PAYLOAD = {
    "id": "chatcmpl_proxy",
    "choices": [{"message": {"content": "hi"}}],
    "usage": USAGE,
}


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if "/v1/models" in self.path:
            data = json.dumps({"object": "list", "data": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if "sse" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
            self.wfile.write(("data: " + json.dumps({"choices": [], "usage": USAGE}) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        if "error" in self.path:
            data = json.dumps({"error": "Policy budget exceeded"}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        data = json.dumps(PAYLOAD).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def wait_for_rows(db_path, count, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = get_conn(db_path)
            n = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            conn.close()
            if n >= count:
                return n
        except Exception:
            pass
        time.sleep(0.05)
    conn = get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    conn.close()
    return n


@pytest.fixture()
def servers(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream.daemon_threads = True
    up_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    up_thread.start()

    db_path = tmp_path / "proxy.db"
    config = ProxyConfig(
        upstream=f"http://127.0.0.1:{upstream.server_address[1]}",
        db_path=db_path,
        csv_path=tmp_path / "proxy.csv",
        profile="work",
        project="tracker",
        member="alice",
        plan="go",
        timeout=10,
    )
    proxy = start_proxy(config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{proxy.server_address[1]}"
    yield url, db_path
    proxy.shutdown()
    proxy.server_close()
    upstream.shutdown()
    upstream.server_close()


def test_proxy_logs_json_request(servers):
    url, db_path = servers
    resp = requests.post(
        url + "/inference/go/openai/v1/chat/completions",
        json={"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-opencode-session": "ses_test", "x-opencode-project": "tracker-ui"},
        timeout=10,
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "chatcmpl_proxy"
    assert wait_for_rows(db_path, 1) == 1

    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM requests").fetchone()
    assert row["model"] == "kimi-k3"
    assert row["source"] == "proxy"
    assert row["request_kind"] == "proxy"
    assert row["profile"] == "work"
    assert row["member"] == "alice"
    assert row["project"] == "tracker-ui"  # header wins over config
    assert row["session_id"] == "ses_test"
    assert row["billing_source"] == "go"
    assert row["allowance"] == pytest.approx(0.42)
    assert row["http_status"] == 200
    assert row["latency_ms"] is not None
    conn.close()


def test_proxy_logs_stream(servers):
    url, db_path = servers
    with requests.post(
        url + "/inference/go/openai/v1/chat/completions/sse",
        json={"model": "kimi-k3", "stream": True},
        stream=True,
        timeout=10,
    ) as resp:
        body = b"".join(resp.iter_content(1024))
    assert b"[DONE]" in body
    assert wait_for_rows(db_path, 1) == 1
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM requests").fetchone()
    assert row["allowance"] == pytest.approx(0.42)
    assert row["status"] in ("ok", "ok_stream_no_usage")
    assert row["model"] == "kimi-k3"
    conn.close()


def test_proxy_logs_error_status(servers):
    url, db_path = servers
    resp = requests.post(
        url + "/inference/go/openai/v1/chat/completions/error",
        json={"model": "kimi-k3"},
        timeout=10,
    )
    assert resp.status_code == 429
    assert wait_for_rows(db_path, 1) == 1
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM requests").fetchone()
    assert row["status"] == "429_budget_exceeded"
    assert row["http_status"] == 429
    conn.close()


def test_proxy_does_not_log_model_list(servers):
    url, db_path = servers
    resp = requests.get(url + "/inference/go/v1/models", timeout=10)
    assert resp.status_code == 200
    time.sleep(0.2)
    conn = get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    conn.close()
