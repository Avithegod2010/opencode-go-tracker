"""
Cost attribution by project, session, member, profile, or model.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .db import build_where

DIMENSIONS = {
    "project": "project",
    "session": "session_id",
    "member": "member",
    "profile": "profile",
    "model": "model",
}


def attribute(
    conn,
    by: str = "project",
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    if by not in DIMENSIONS:
        raise ValueError(f"Unsupported attribution: {by}")
    column = DIMENSIONS[by]
    where, params = build_where(filters)
    total_row = conn.execute(
        f"SELECT COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(allowance),0) AS allowance "
        f"FROM requests{where}",
        params,
    ).fetchone()
    total_cost = total_row["cost"] or 0.0
    total_allowance = total_row["allowance"] or 0.0

    rows = conn.execute(
        f"""
        SELECT {column} AS name,
               COALESCE(SUM(cost),0) AS cost,
               COALESCE(SUM(allowance),0) AS allowance,
               COUNT(*) AS n,
               MIN(timestamp) AS first_seen,
               MAX(timestamp) AS last_seen
        FROM requests{where}
        GROUP BY {column}
        ORDER BY cost DESC, n DESC
        LIMIT ?
        """,
        [*params, int(limit)],
    ).fetchall()

    sessions = {}
    if by == "session":
        ids = [row["name"] for row in rows if row["name"]]
        if ids:
            marks = ",".join("?" for _ in ids)
            for session in conn.execute(
                f"SELECT id, title, project FROM sessions WHERE id IN ({marks})", ids
            ):
                sessions[session["id"]] = session

    out = []
    for row in rows:
        item = {
            "name": row["name"],
            "cost": row["cost"] or 0.0,
            "allowance": row["allowance"] or 0.0,
            "n": row["n"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "cost_share": (row["cost"] or 0.0) / total_cost * 100.0 if total_cost else None,
            "allowance_share": (row["allowance"] or 0.0) / total_allowance * 100.0 if total_allowance else None,
        }
        if by == "session" and row["name"] in sessions:
            item["title"] = sessions[row["name"]]["title"]
            item["project"] = sessions[row["name"]]["project"]
        out.append(item)

    return {
        "by": by,
        "rows": out,
        "total_cost": total_cost,
        "total_allowance": total_allowance,
    }
