"""
All-in-one local daemon: proxy + dashboard + periodic sync/import.

`opencode-tracker daemon` keeps the proxy and the web dashboard running, and
optionally refreshes Go limits and imports OpenCode sessions on an interval.
"""
from __future__ import annotations

import importlib
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .db import get_conn
from .go_limits import sync_limits
from .importer import find_opencode_db, import_sessions
from .proxy import ProxyConfig, start_proxy
from .report_html import write_report


def load_web_app(db_path: Path, csv_path: Path, period_start_day: int):
    """Load app.py from the repository checkout and point it at this DB."""
    root = Path(__file__).resolve().parents[2]
    if not (root / "app.py").is_file():
        raise FileNotFoundError(f"app.py not found in {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    module = importlib.import_module("app")
    module.DB_PATH = Path(db_path)
    module.CSV_PATH = Path(csv_path)
    module.PERIOD_START_DAY = max(1, min(31, int(period_start_day)))
    return module.app


def _periodic(stop: threading.Event, interval_seconds: float, fn: Callable[[], object], label: str) -> None:
    while not stop.is_set():
        try:
            result = fn()
            if result is not None:
                print(f"[{label}] {result}", flush=True)
        except Exception as exc:
            print(f"[{label}] failed: {exc}", flush=True)
        stop.wait(max(5.0, float(interval_seconds)))


def run_daemon(
    *,
    db_path: Path,
    csv_path: Path,
    period_start_day: int = 1,
    host: str = "127.0.0.1",
    web_port: int = 8000,
    proxy_port: int = 8787,
    upstream: str = "https://opencode.ai",
    profile: Optional[str] = None,
    project: Optional[str] = None,
    member: Optional[str] = None,
    plan: Optional[str] = None,
    sync_interval_minutes: float = 60,
    import_interval_minutes: float = 0,
    report_every_hours: float = 0,
    report_dir: Path = Path("reports"),
    report_format: str = "html",
    limit_key: Optional[str] = None,
    web: bool = True,
) -> None:
    stop = threading.Event()
    proxy_config = ProxyConfig(
        upstream=upstream,
        db_path=db_path,
        csv_path=csv_path,
        profile=profile,
        project=project,
        member=member,
        plan=plan,
    )
    proxy = start_proxy(proxy_config, host=host, port=proxy_port)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    print(f"Proxy:      http://{host}:{proxy.server_address[1]}", flush=True)
    print(f"  Go base:  http://{host}:{proxy.server_address[1]}/inference/go", flush=True)
    print(f"  Upstream: {upstream}", flush=True)

    if sync_interval_minutes and sync_interval_minutes > 0:
        def do_sync():
            result = sync_limits(api_key=limit_key, base_url=upstream)
            return f"limits synced: {len(result['limits'])} models"

        threading.Thread(
            target=_periodic,
            args=(stop, sync_interval_minutes * 60, do_sync, "sync"),
            daemon=True,
        ).start()

    if import_interval_minutes and import_interval_minutes > 0:
        source = find_opencode_db()

        def do_import():
            if source is None:
                return "OpenCode session DB not found"
            conn = get_conn(db_path)
            try:
                stats = import_sessions(conn, source_db=source, profile=profile, member=member, plan=plan)
            finally:
                conn.close()
            return (
                f"imported {stats['messages_imported']} messages, "
                f"{stats['duplicates_skipped']} duplicates"
            )

        threading.Thread(
            target=_periodic,
            args=(stop, import_interval_minutes * 60, do_import, "import"),
            daemon=True,
        ).start()

    if report_every_hours and report_every_hours > 0:
        def do_report():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            path = Path(report_dir) / f"report-{stamp}.{report_format}"
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = get_conn(db_path)
            try:
                write_report(
                    conn,
                    path,
                    fmt=report_format,
                    plan=plan or "go",
                    period_start_day=period_start_day,
                )
            finally:
                conn.close()
            return f"wrote {path}"

        threading.Thread(
            target=_periodic,
            args=(stop, report_every_hours * 3600, do_report, "report"),
            daemon=True,
        ).start()

    web_server = None
    if web:
        from werkzeug.serving import make_server

        app = load_web_app(db_path, csv_path, period_start_day)
        web_server = make_server(host, web_port, app, threaded=True)
        threading.Thread(target=web_server.serve_forever, daemon=True).start()
        print(f"Dashboard:  http://{host}:{web_port}", flush=True)

    print("Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        proxy.shutdown()
        proxy.server_close()
        if web_server is not None:
            web_server.shutdown()
