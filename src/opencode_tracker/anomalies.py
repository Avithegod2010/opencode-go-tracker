"""
Simple day-level anomaly detection.

For each day, compare cost and Go allowance against the previous `baseline_days`
days. A day is flagged when it exceeds mean + threshold * stddev and clears a
minimum absolute value. This is intentionally transparent (no ML), and returns
the top models for the flagged day.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .db import build_where


def detect_anomalies(
    conn,
    filters: Optional[Dict[str, Any]] = None,
    days: int = 30,
    baseline_days: int = 14,
    threshold: float = 2.0,
    min_cost: float = 0.01,
    min_allowance: float = 0.05,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    scoped = dict(filters or {})
    if not scoped.get("from"):
        scoped["from"] = (now.date() - timedelta(days=max(1, int(days)) - 1)).isoformat()
    where, params = build_where(scoped)

    rows = conn.execute(
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
    series = [
        {"day": row["day"], "cost": row["cost"] or 0.0,
         "allowance": row["allowance"] or 0.0, "n": row["n"]}
        for row in rows
    ]

    anomalies: List[Dict[str, Any]] = []
    for index, row in enumerate(series):
        prior = series[max(0, index - int(baseline_days)):index]
        if len(prior) < 5:
            continue
        for metric, minimum in (("cost", min_cost), ("allowance", min_allowance)):
            values = [float(item[metric] or 0.0) for item in prior]
            value = float(row[metric] or 0.0)
            if value < minimum:
                continue
            mean = statistics.fmean(values)
            std = statistics.pstdev(values)
            if std == 0:
                if value <= mean:
                    continue
                z = float("inf")
            else:
                z = (value - mean) / std
            if z < threshold:
                continue
            top = conn.execute(
                """
                SELECT model, COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(allowance),0) AS allowance,
                       COUNT(*) AS n
                FROM requests WHERE substr(timestamp,1,10)=?
                GROUP BY model
                ORDER BY (CASE WHEN ?='cost' THEN SUM(cost) ELSE SUM(allowance) END) DESC
                LIMIT 3
                """,
                (row["day"], metric),
            ).fetchall()
            anomalies.append({
                "day": row["day"],
                "metric": metric,
                "value": round(value, 6),
                "baseline_mean": round(mean, 6),
                "baseline_std": round(std, 6),
                "z": None if z == float("inf") else round(z, 2),
                "n": row["n"],
                "top_models": [
                    {"model": item["model"], "cost": item["cost"] or 0.0,
                     "allowance": item["allowance"] or 0.0, "n": item["n"]}
                    for item in top
                ],
            })
            break

    return {
        "anomalies": anomalies,
        "meta": {
            "as_of": now.isoformat(),
            "days": int(days),
            "baseline_days": int(baseline_days),
            "threshold": float(threshold),
            "series_days": len(series),
        },
    }
