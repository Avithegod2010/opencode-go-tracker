"""
Rolling Go usage windows computed from the local request log.

Semantics come from https://opencode.ai/docs/go#usage-limits:

- 5-hour limit: 20% of the model's monthly allowance. The window starts when
  you first use your allowance and lasts five hours.
- Weekly limit: 50% of the monthly allowance, resetting Monday 00:00 UTC.
- Monthly limit: 100%, resetting when a new paid period starts. The local
  tracker cannot see the subscription anniversary, so the reset day is
  configurable (`period_start_day`, default 1 = calendar month).

`allowance` is the only field that counts toward Go limits; `cost` is 0 while
usage stays inside the plan. Non-Go rows (allowance absent/0) are ignored.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .go_limits import monthly_limit, normalize_model

FIVE_HOURS = timedelta(hours=5)


def _utc(value: Optional[datetime] = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _month_start(year: int, month: int, day: int, tz=timezone.utc) -> datetime:
    day = max(1, min(int(day), calendar.monthrange(year, month)[1]))
    return datetime(year, month, day, tzinfo=tz)


def period_start(now: Optional[datetime] = None, start_day: int = 1) -> datetime:
    """Most recent start-of-billing-period at 00:00 UTC.

    `start_day` is the day of the month the paid period renews. Values beyond
    the month length are clamped (e.g. 31 in February -> 28/29).
    """
    now = _utc(now)
    start = _month_start(now.year, now.month, start_day)
    if start > now:
        year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        start = _month_start(year, month, start_day)
    return start


def week_start(now: Optional[datetime] = None) -> datetime:
    """Most recent Monday at 00:00 UTC."""
    now = _utc(now)
    start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start


def parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _utc(value)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Python 3.9's fromisoformat does not accept a trailing Z.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def five_hour_window_start(
    timestamps: Iterable[datetime],
    now: Optional[datetime] = None,
    window: timedelta = FIVE_HOURS,
) -> Optional[datetime]:
    """Return the start of the currently active 5-hour window, if any.

    The window is anchored at the first Go request after the previous window
    ended. Requests before `now - 2 * window` cannot belong to an active
    window, so callers normally only pass recent timestamps.
    """
    now = _utc(now)
    ordered = sorted(t for t in timestamps if t <= now)
    current: Optional[datetime] = None
    for ts in ordered:
        if current is None or ts >= current + window:
            current = ts
    if current is not None and now < current + window:
        return current
    return None


def _status(pcts: Iterable[Optional[float]]) -> str:
    values = [p for p in pcts if p is not None]
    if not values:
        return "no-limit"
    worst = max(values)
    if worst >= 100:
        return "over"
    if worst >= 80:
        return "near"
    if worst >= 50:
        return "warm"
    return "ok"


def _pct(used: Optional[float], limit: Optional[float]) -> Optional[float]:
    if limit is None or limit <= 0:
        return None
    return (used or 0.0) / limit * 100.0


def compute_windows(
    conn,
    plan: str = "go",
    now: Optional[datetime] = None,
    period_start_day: int = 1,
) -> List[Dict[str, Any]]:
    """Per-model Go usage for the active 5h / week / month windows.

    Returns a list sorted by monthly usage, each item:

        model, limit,
        month_used, month_pct, month_limit,
        week_used, week_pct, week_limit,
        five_hour_used, five_hour_pct, five_hour_limit,
        five_hour_start, status

    Only models with Go allowance in at least one window are returned.
    """
    now = _utc(now)
    p_start = period_start(now, period_start_day)
    w_start = week_start(now)
    # Two 5-hour windows of history is enough to find the active anchor.
    recent = now - 2 * FIVE_HOURS
    bound = min(p_start, w_start, recent)

    rows = conn.execute(
        "SELECT timestamp, model, allowance FROM requests WHERE timestamp >= ?",
        (bound.isoformat(),),
    ).fetchall()

    month_used: Dict[str, float] = defaultdict(float)
    week_used: Dict[str, float] = defaultdict(float)
    recent_ts: List[datetime] = []
    parsed_rows: List[Tuple[datetime, str, float]] = []

    for row in rows:
        ts = parse_ts(row["timestamp"])
        if ts is None or ts > now:
            continue
        model = normalize_model(str(row["model"] or ""))
        allowance = float(row["allowance"] or 0.0)
        if allowance <= 0:
            continue
        parsed_rows.append((ts, model, allowance))
        if ts >= recent:
            recent_ts.append(ts)
        if ts >= w_start:
            week_used[model] += allowance
        if ts >= p_start:
            month_used[model] += allowance

    f_start = five_hour_window_start(recent_ts, now=now)
    five_used: Dict[str, float] = defaultdict(float)
    if f_start is not None:
        for ts, model, allowance in parsed_rows:
            if ts >= f_start:
                five_used[model] += allowance

    models = set(month_used) | set(week_used) | set(five_used)
    out: List[Dict[str, Any]] = []
    for model in models:
        limit = monthly_limit(model, plan)
        f_limit = limit * 0.2 if limit else None
        w_limit = limit * 0.5 if limit else None
        m_used = month_used.get(model, 0.0)
        w_used = week_used.get(model, 0.0)
        f_used = five_used.get(model, 0.0)
        m_pct = _pct(m_used, limit)
        w_pct = _pct(w_used, w_limit)
        f_pct = _pct(f_used, f_limit)
        out.append({
            "model": model,
            "limit": limit,
            "month_used": m_used,
            "month_limit": limit,
            "month_pct": m_pct,
            "week_used": w_used,
            "week_limit": w_limit,
            "week_pct": w_pct,
            "five_hour_used": f_used,
            "five_hour_limit": f_limit,
            "five_hour_pct": f_pct,
            "five_hour_start": f_start.isoformat() if f_start else None,
            "status": _status([m_pct, w_pct, f_pct]),
        })

    out.sort(key=lambda x: (x["month_pct"] if x["month_pct"] is not None else -1), reverse=True)
    return out


def window_meta(
    now: Optional[datetime] = None,
    period_start_day: int = 1,
    five_hour_start: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = _utc(now)
    return {
        "as_of": now.isoformat(),
        "period_start": period_start(now, period_start_day).isoformat(),
        "period_start_day": int(period_start_day),
        "week_start": week_start(now).isoformat(),
        "five_hour_start": five_hour_start.isoformat() if five_hour_start else None,
    }
