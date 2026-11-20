"""
Terminal live view for Go windows.

`watch` polls the local DB and redraws the 5h / week / month table. It does not
make network requests; the proxy or logger keeps the DB current.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import get_conn
from .windows import compute_windows, period_start, week_start


def _cell(used: float, limit: Optional[float], pct: Optional[float]) -> str:
    if limit is None:
        return "unlimited"
    return f"${used:6.2f}/${limit:6.0f} {pct or 0:6.1f}%"


def render_watch(
    conn,
    plan: str = "go",
    period_start_day: int = 1,
    now: Optional[datetime] = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    windows = compute_windows(conn, plan, now=now, period_start_day=period_start_day)
    lines = [
        f"Go windows vs {plan}  |  as of {now.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"period since {period_start(now, period_start_day).strftime('%Y-%m-%d')}  "
        f"week since {week_start(now).strftime('%Y-%m-%d')}",
        "-" * 92,
        f"{'Model':<28} {'5h used/limit':>22} {'Week used/limit':>24} {'Month used/limit':>26} {'Status':>9}",
    ]
    if not windows:
        lines.append("No Go allowance logged yet. Start the proxy, log a request, or import sessions.")
    for w in windows:
        lines.append(
            f"{w['model']:<28} "
            f"{_cell(w['five_hour_used'], w['five_hour_limit'], w['five_hour_pct']):>22} "
            f"{_cell(w['week_used'], w['week_limit'], w['week_pct']):>24} "
            f"{_cell(w['month_used'], w['month_limit'], w['month_pct']):>26} "
            f"{w['status']:>9}"
        )
    return "\n".join(lines)


def watch_loop(
    db_path: Path | str,
    plan: str = "go",
    period_start_day: int = 1,
    interval: float = 5.0,
    once: bool = False,
) -> None:
    while True:
        conn = get_conn(db_path)
        try:
            text = render_watch(conn, plan, period_start_day)
        finally:
            conn.close()
        if not once:
            print("\033[2J\033[H", end="")
        print(text, flush=True)
        if once:
            return
        try:
            time.sleep(max(0.5, float(interval)))
        except KeyboardInterrupt:
            return
