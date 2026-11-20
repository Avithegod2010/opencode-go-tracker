"""
Local reverse proxy for the OpenCode Inference API.

Point OpenCode's provider base URL at this proxy:

    Go:      http://127.0.0.1:8787/inference/go
    Balance: http://127.0.0.1:8787/inference

The proxy forwards requests and streams responses unchanged, then parses the
final usage payload and records cost, allowance, tokens, session id, project,
and latency. It forwards the client's Authorization header; it does not need
or store an API key of its own.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .logger import (
    OpencodeLogger,
    classify_error,
    parse_stream_chunks,
    parse_usage,
)

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "content-encoding", "host", "accept-encoding",
}
INFERENCE_MARKERS = ("/chat/completions", "/responses", "/messages", ":generateContent")
_LOG_LOCK = threading.Lock()


@dataclass
class ProxyConfig:
    upstream: str = "https://opencode.ai"
    db_path: Path = Path("opencode_usage.db")
    csv_path: Path = Path("usage_log.csv")
    profile: Optional[str] = None
    project: Optional[str] = None
    member: Optional[str] = None
    plan: Optional[str] = None
    max_capture_bytes: int = 8 * 1024 * 1024
    timeout: int = 300


def is_inference_path(path: str) -> bool:
    return any(marker in (path or "") for marker in INFERENCE_MARKERS)


def sse_chunks(raw: bytes) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        data = stripped[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            value = json.loads(data)
        except ValueError:
            continue
        if isinstance(value, dict):
            chunks.append(value)
    return chunks


def _safe_model(payload: Any, path: str) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("model"), str):
        return payload["model"]
    marker = "/models/"
    if marker in path:
        tail = path.split(marker, 1)[1]
        return tail.split(":", 1)[0] or "unknown"
    return "unknown"


def proxy_log(
    config: ProxyConfig,
    model: str,
    path: str,
    parsed: Dict[str, Any],
    status: str,
    error: Optional[str],
    status_code: Optional[int],
    latency_ms: int,
    session_id: Optional[str],
    project: Optional[str],
) -> None:
    with _LOG_LOCK:
        logger = OpencodeLogger(
            db_path=str(config.db_path),
            csv_path=str(config.csv_path),
            api_key="proxy",
            base_url=config.upstream,
            provider_label="go" if "/inference/go" in path else None,
            profile=config.profile,
            plan=config.plan,
            member=config.member,
            project=project,
            source="proxy",
            request_kind="proxy",
        )
        try:
            logger._log_entry(
                model=model,
                endpoint=path,
                parsed=parsed,
                status=status,
                error=error,
                http_status=status_code,
                latency_ms=latency_ms,
                session_id=session_id,
                raw_json=parsed.get("raw_usage") or {},
            )
        finally:
            if logger.conn is not None:
                logger.conn.close()


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, config: ProxyConfig):
        super().__init__(address, handler)
        self.config = config


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenCodeTrackerProxy/1.0"

    def log_message(self, fmt, *args):  # keep stdout usable
        return

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle()

    # ---- helpers ----
    def _request_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _upstream_headers(self) -> Dict[str, str]:
        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_HEADERS:
                continue
            headers[key] = value
        headers["Accept-Encoding"] = "identity"
        return headers

    def _send_simple(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log(
        self,
        model: str,
        parsed: Dict[str, Any],
        status: str,
        error: Optional[str],
        status_code: Optional[int],
        started: float,
        session_id: Optional[str],
        project: Optional[str],
    ) -> None:
        if not is_inference_path(self.path):
            return
        proxy_log(
            self.server.config,
            model=model,
            path=self.path,
            parsed=parsed,
            status=status,
            error=error,
            status_code=status_code,
            latency_ms=int((time.monotonic() - started) * 1000),
            session_id=session_id,
            project=project,
        )

    # ---- main relay ----
    def _handle(self):
        config: ProxyConfig = self.server.config
        started = time.monotonic()
        body = self._request_body()
        payload: Any = {}
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
        model = _safe_model(payload, self.path)
        session_id = self.headers.get("x-opencode-session")
        project = self.headers.get("x-opencode-project") or config.project

        url = config.upstream.rstrip("/") + self.path
        try:
            upstream = requests.request(
                self.command,
                url,
                headers=self._upstream_headers(),
                data=body,
                stream=True,
                timeout=(10, config.timeout),
                allow_redirects=False,
            )
        except Exception as exc:
            error = str(exc)
            self._send_simple(502, {"error": {"message": f"Proxy upstream error: {error}"}})
            self._log(model, {}, "5xx_upstream", error, None, started, session_id, project)
            return

        status_code = upstream.status_code
        content_type = upstream.headers.get("Content-Type", "")
        streaming = "text/event-stream" in content_type.lower()

        try:
            self.send_response(status_code)
            for key, value in upstream.headers.items():
                if key.lower() in HOP_HEADERS:
                    continue
                self.send_header(key, value)
            if streaming:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                captured = bytearray()
                try:
                    for chunk in upstream.iter_content(8192):
                        if not chunk:
                            continue
                        if len(captured) < config.max_capture_bytes:
                            room = config.max_capture_bytes - len(captured)
                            captured.extend(chunk[:room])
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    upstream.close()
                parsed = parse_stream_chunks(sse_chunks(bytes(captured)))
                status = "ok" if parsed.get("cost") is not None else "ok_stream_no_usage"
                self._log(model, parsed, status, None, status_code, started, session_id, project)
                return

            content = upstream.content
            upstream.close()
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

            parsed: Dict[str, Any] = {}
            if content and "application/json" in content_type.lower():
                try:
                    parsed = parse_usage(json.loads(content))
                except ValueError:
                    parsed = {}
            if status_code >= 400:
                label, _hint = classify_error(status_code, content.decode("utf-8", "replace")[:500])
                self._log(
                    model, parsed, label, f"HTTP {status_code}", status_code, started, session_id, project
                )
            else:
                self._log(
                    model,
                    parsed,
                    "ok" if parsed.get("cost") is not None else "ok_no_usage",
                    None,
                    status_code,
                    started,
                    session_id,
                    project,
                )
        except (BrokenPipeError, ConnectionResetError):
            upstream.close()


def start_proxy(config: ProxyConfig, host: str = "127.0.0.1", port: int = 8787) -> ProxyServer:
    return ProxyServer((host, port), ProxyHandler, config)


def run_proxy(config: ProxyConfig, host: str = "127.0.0.1", port: int = 8787) -> None:
    server = start_proxy(config, host, port)
    print(f"OpenCode tracker proxy listening on http://{host}:{server.server_address[1]}")
    print(f"  Upstream: {config.upstream}")
    print(f"  DB:       {config.db_path}")
    print(f"  CSV:      {config.csv_path}")
    print("  Point OpenCode at:")
    print(f"    Go:      http://{host}:{server.server_address[1]}/inference/go")
    print(f"    Balance: http://{host}:{server.server_address[1]}/inference")
    print("  Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
