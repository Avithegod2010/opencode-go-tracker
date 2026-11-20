"""
Compare the current period's usage against Go / Go Plus / Go Teams limits.

This is a coverage simulator, not a token-price quote: it uses the model's
monthly allowance (the same `allowance` field the Go plan reports) and each
plan's per-model limit. Imported rows have allowance=0, so the simulator needs
proxy/logger rows to be meaningful.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .db import build_where, normalize_bound
from .go_limits import PLAN_PRICES, monthly_limit
from .windows import period_start

DEFAULT_PLANS = ("go", "go_plus", "go_teams")


def simulate_plans(
    conn,
    filters: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    period_start_day: int = 1,
    plans: Iterable[str] = DEFAULT_PLANS,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    p_start = period_start(now, period_start_day)
    scoped = dict(filters or {})
    existing_from = normalize_bound(scoped.get("from"))
    scoped["from"] = max(existing_from or p_start.isoformat(), p_start.isoformat())
    where, params = build_where(scoped)

    rows = conn.execute(
        f"""
        SELECT model,
               COALESCE(SUM(allowance), 0) AS used,
               COALESCE(SUM(cost), 0) AS cost,
               COUNT(*) AS n
        FROM requests{where}
        GROUP BY model ORDER BY used DESC
        """,
        params,
    ).fetchall()
    usage = [
        {"model": row["model"], "used": float(row["used"] or 0.0),
         "cost": float(row["cost"] or 0.0), "n": row["n"]}
        for row in rows
        if (row["used"] or 0) > 0
    ]

    plan_rows: List[Dict[str, Any]] = []
    models_out: List[Dict[str, Any]] = []
    for plan in plans:
        overage = 0.0
        over_count = 0
        limited = 0
        for item in usage:
            limit = monthly_limit(item["model"], plan)
            if limit is not None:
                limited += 1
                over = max(0.0, item["used"] - limit)
                if over > 0:
                    over_count += 1
                    overage += over
                pct = item["used"] / limit * 100.0 if limit else None
            else:
                over = 0.0
                pct = None
            item.setdefault("plan_views", {})[plan] = {
                "limit": limit,
                "pct": pct,
                "over": over,
                "fits": over <= 0,
            }
        plan_rows.append({
            "plan": plan,
            "price": PLAN_PRICES.get(plan),
            "models": limited,
            "models_over": over_count,
            "overage": round(overage, 6),
            "coverage_pct": ((limited - over_count) / limited * 100.0) if limited else None,
            "fits": over_count == 0,
            "total_used": round(sum(item["used"] for item in usage), 6),
        })

    for item in usage:
        models_out.append({
            "model": item["model"],
            "used": round(item["used"], 6),
            "cost": round(item["cost"], 6),
            "n": item["n"],
            "plans": item.get("plan_views", {}),
        })

    return {
        "period_start": p_start.isoformat(),
        "as_of": now.isoformat(),
        "models": models_out,
        "plans": plan_rows,
        "note": "Allowance-based coverage. Extra usage is charged at list price; this table shows allowance overage, not a token quote.",
    }
