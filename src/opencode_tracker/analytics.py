"""
Aggregations for the Insights tab: KPIs, daily stacked series, model mix, and
dimension breakdowns. All queries are parameterized through db.build_where.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .db import build_where


def _day_range(filters: Dict[str, Any], days: int, now: datetime):
    start = filters.get("from")
    end = filters.get("to")
    try:
        if start and len(str(start)) == 10:
            start_day = datetime.fromisoformat(str(start)).date()
        else:
            start_day = (now - timedelta(days=max(1, int(days)) - 1)).date()
    except ValueError:
        start_day = (now - timedelta(days=max(1, int(days)) - 1)).date()
    try:
        if end and len(str(end)) == 10:
            end_day = datetime.fromisoformat(str(end)).date()
        else:
            end_day = now.date()
    except ValueError:
        end_day = now.date()
    if end_day < start_day:
        start_day, end_day = end_day, start_day
    span = min(120, (end_day - start_day).days + 1)
    return [start_day + timedelta(days=i) for i in range(max(1, span))]


def _group(conn, column: str, where: str, params: List[Any]) -> List[Dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT {column} AS name, SUM(cost) AS cost, SUM(allowance) AS allowance,
               COUNT(*) AS n
        FROM requests{where}
        GROUP BY {column}
        ORDER BY cost DESC
        """,
        params,
    ).fetchall()
    return [
        {"name": row["name"], "cost": row["cost"] or 0.0,
         "allowance": row["allowance"] or 0.0, "n": row["n"]}
        for row in rows
        if row["name"]
    ]


def analytics(
    conn,
    filters: Optional[Dict[str, Any]] = None,
    days: int = 30,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    scoped = dict(filters or {})
    if not scoped.get("from"):
        scoped["from"] = (now.date() - timedelta(days=max(1, int(days)) - 1)).isoformat()
    where, params = build_where(scoped)

    kpi = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(cost), 0) AS cost,
            COALESCE(SUM(CASE WHEN billing_source IN ('balance','byok') THEN cost ELSE 0 END), 0) AS billable,
            COALESCE(SUM(allowance), 0) AS allowance,
            COUNT(*) AS n,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
            COALESCE(SUM(cache_read), 0) AS cache_read,
            AVG(latency_ms) AS avg_latency
        FROM requests{where}
        """,
        params,
    ).fetchone()

    by_model = conn.execute(
        f"""
        SELECT model,
               COALESCE(SUM(cost), 0) AS cost,
               COALESCE(SUM(allowance), 0) AS allowance,
               COUNT(*) AS n,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cache_read), 0) AS cache_read
        FROM requests{where}
        GROUP BY model ORDER BY cost DESC
        """,
        params,
    ).fetchall()

    daily_rows = conn.execute(
        f"""
        SELECT substr(timestamp,1,10) AS day, model,
               COALESCE(SUM(cost), 0) AS cost,
               COALESCE(SUM(allowance), 0) AS allowance,
               COUNT(*) AS n
        FROM requests{where}
        GROUP BY day, model ORDER BY day
        """,
        params,
    ).fetchall()

    daily_total = conn.execute(
        f"""
        SELECT substr(timestamp,1,10) AS day,
               COALESCE(SUM(cost), 0) AS cost,
               COALESCE(SUM(allowance), 0) AS allowance,
               COUNT(*) AS n
        FROM requests{where}
        GROUP BY day ORDER BY day
        """,
        params,
    ).fetchall()

    model_rows = [
        {
            "model": row["model"],
            "cost": row["cost"] or 0.0,
            "allowance": row["allowance"] or 0.0,
            "n": row["n"],
            "input_tokens": row["input_tokens"] or 0,
            "output_tokens": row["output_tokens"] or 0,
            "cache_read": row["cache_read"] or 0,
        }
        for row in by_model
    ]
    day_list = [d.isoformat() for d in _day_range(scoped, days, now)]
    top = [row["model"] for row in model_rows[:8]]
    index = {(row["day"], row["model"]): row for row in daily_rows}
    series = []
    for model in top:
        series.append({
            "model": model,
            "values": [float(index.get((day, model), {"cost": 0})["cost"]) for day in day_list],
        })
    if len(model_rows) > len(top):
        other = [row["model"] for row in model_rows[len(top):]]
        series.append({
            "model": "other",
            "values": [
                sum(float(index.get((day, model), {"cost": 0})["cost"]) for model in other)
                for day in day_list
            ],
        })

    billed = kpi["billable"] or 0.0
    requests_count = kpi["n"] or 0
    cache_read = kpi["cache_read"] or 0
    input_tokens = kpi["input_tokens"] or 0
    cache_denominator = cache_read + input_tokens
    total_tokens = (
        input_tokens + (kpi["output_tokens"] or 0) + (kpi["reasoning_tokens"] or 0) + cache_read
    )

    return {
        "days": day_list,
        "series": series,
        "kpi": {
            "cost": round(kpi["cost"] or 0.0, 6),
            "billable": round(billed, 6),
            "allowance": round(kpi["allowance"] or 0.0, 6),
            "requests": requests_count,
            "input_tokens": input_tokens,
            "output_tokens": kpi["output_tokens"] or 0,
            "reasoning_tokens": kpi["reasoning_tokens"] or 0,
            "cache_read": cache_read,
            "tokens": total_tokens,
            "cache_hit_ratio": (cache_read / cache_denominator) if cache_denominator else None,
            "cost_per_request": (billed / requests_count) if requests_count else None,
            "avg_latency_ms": round(kpi["avg_latency"], 1) if kpi["avg_latency"] is not None else None,
        },
        "by_model": model_rows,
        "by_billing": _group(conn, "billing_source", where, params),
        "by_source": _group(conn, "source", where, params),
        "by_project": _group(conn, "project", where, params),
        "by_member": _group(conn, "member", where, params),
        "daily": [
            {"day": row["day"], "cost": row["cost"] or 0.0,
             "allowance": row["allowance"] or 0.0, "n": row["n"]}
            for row in daily_total
        ],
    }
