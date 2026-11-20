"""
Burn-rate forecast for the current Go period, week, and 5-hour window.

Given current usage and elapsed time, project the period total and estimate
when each model's limit will be reached. Forecasts are intentionally simple:
linear projection from the current window, with low confidence before a full
day of data.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .windows import compute_windows, period_start, parse_ts, week_start


def next_period_start(now: Optional[datetime] = None, start_day: int = 1) -> datetime:
    now = now or datetime.now(timezone.utc)
    current = period_start(now, start_day)
    year, month = current.year, current.month + 1
    if month > 12:
        year += 1
        month = 1
    day = max(1, min(int(start_day), calendar.monthrange(year, month)[1]))
    return datetime(year, month, day, tzinfo=timezone.utc)


def _pct(value: Optional[float], limit: Optional[float]) -> Optional[float]:
    if value is None or not limit:
        return None
    return value / limit * 100.0


def _risk(pcts: List[Optional[float]]) -> str:
    values = [p for p in pcts if p is not None]
    if not values:
        return "no-limit"
    worst = max(values)
    if worst >= 100:
        return "over"
    if worst >= 80:
        return "risk"
    if worst >= 50:
        return "warm"
    return "ok"


def forecast(
    conn,
    plan: str = "go",
    period_start_day: int = 1,
    now: Optional[datetime] = None,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    p_start = period_start(now, period_start_day)
    p_end = next_period_start(now, period_start_day)
    week_start_at = week_start(now)
    period_days = max(1.0, (p_end - p_start).total_seconds() / 86400.0)
    elapsed_days = max(1 / 24, (now - p_start).total_seconds() / 86400.0)
    week_elapsed = max(1 / 24, (now - week_start_at).total_seconds() / 86400.0)

    windows = compute_windows(conn, plan, now=now, period_start_day=period_start_day, filters=filters)
    models = []
    for window in windows:
        limit = window.get("month_limit")
        month_used = window.get("month_used") or 0.0
        burn = month_used / elapsed_days
        projected = burn * period_days
        projected_pct = _pct(projected, limit)
        days_left = None
        if limit is not None and burn > 0:
            days_left = max(0.0, (limit - month_used) / burn)

        week_limit = window.get("week_limit")
        week_used = window.get("week_used") or 0.0
        week_projected = week_used / week_elapsed * 7.0
        week_projected_pct = _pct(week_projected, week_limit)

        five_limit = window.get("five_hour_limit")
        five_used = window.get("five_hour_used") or 0.0
        five_projected = None
        five_projected_pct = None
        if window.get("five_hour_start"):
            started = parse_ts(window["five_hour_start"])
            if started is not None:
                elapsed_hours = max(0.25, (now - started).total_seconds() / 3600.0)
                five_projected = five_used / elapsed_hours * 5.0
                five_projected_pct = _pct(five_projected, five_limit)

        confidence = "low" if elapsed_days < 1 else ("medium" if elapsed_days < 3 else "high")
        models.append({
            "model": window["model"],
            "plan": plan,
            "limit": limit,
            "month_used": round(month_used, 6),
            "burn_per_day": round(burn, 6),
            "projected": round(projected, 6),
            "projected_pct": round(projected_pct, 2) if projected_pct is not None else None,
            "days_to_limit": round(days_left, 2) if days_left is not None else None,
            "week_used": round(week_used, 6),
            "week_projected": round(week_projected, 6),
            "week_projected_pct": round(week_projected_pct, 2) if week_projected_pct is not None else None,
            "five_hour_used": round(five_used, 6),
            "five_hour_projected": round(five_projected, 6) if five_projected is not None else None,
            "five_hour_projected_pct": round(five_projected_pct, 2) if five_projected_pct is not None else None,
            "risk": _risk([projected_pct, week_projected_pct, five_projected_pct]),
            "confidence": confidence,
        })

    models.sort(key=lambda item: (item["projected_pct"] if item["projected_pct"] is not None else -1), reverse=True)
    return {
        "meta": {
            "as_of": now.isoformat(),
            "period_start": p_start.isoformat(),
            "period_end": p_end.isoformat(),
            "period_days": round(period_days, 2),
            "elapsed_days": round(elapsed_days, 2),
            "days_left_in_period": round(max(0.0, (p_end - now).total_seconds() / 86400.0), 2),
            "week_start": week_start_at.isoformat(),
            "week_elapsed_days": round(week_elapsed, 2),
        },
        "models": models,
    }
