"""
SQLite storage for per-request Inference API logs.

Schema captures exactly what /v2/docs/console/api/inference/ returns:
  usage.cost, opencode.billing_source, opencode.list_cost, opencode.allowance,
  opencode.tokens {input, output, reasoning, cache_read, cache_write_5m, cache_write_1h}
plus metadata for debugging.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT,                 -- inference vs go vs custom
    endpoint TEXT,                 -- full URL path used
    billing_source TEXT,           -- free|balance|go|go-plus|byok
    cost REAL,                     -- usage.cost (charged)
    list_cost REAL,                -- opencode.list_cost
    allowance REAL,                -- opencode.allowance (Go dollars toward limit)
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_read INTEGER,
    cache_write_5m INTEGER,
    cache_write_1h INTEGER,
    prompt_tokens INTEGER,         -- raw prompt_tokens if present
    completion_tokens INTEGER,     -- raw completion_tokens
    estimated INTEGER,             -- boolean 0/1 opencode.estimated
    status TEXT,                   -- ok/error/402/429
    error TEXT,
    request_id TEXT,
    raw_json TEXT                  -- full usage/opencode blob for audit
);
CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
CREATE INDEX IF NOT EXISTS idx_requests_billing ON requests(billing_source);
"""

def get_conn(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

def insert_request(conn: sqlite3.Connection, entry: Dict[str, Any]) -> int:
    cols = ["timestamp","model","provider","endpoint","billing_source","cost","list_cost","allowance",
            "input_tokens","output_tokens","reasoning_tokens","cache_read","cache_write_5m","cache_write_1h",
            "prompt_tokens","completion_tokens","estimated","status","error","request_id","raw_json"]
    vals = [entry.get(c) for c in cols]
    # default timestamp
    if not vals[0]:
        vals[0] = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(f"INSERT INTO requests ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", vals)
    conn.commit()
    return cur.lastrowid

def query_totals(conn: sqlite3.Connection, where: str = "", params=()):
    q = f"""
    SELECT
        model,
        billing_source,
        COUNT(*) as n,
        SUM(cost) as sum_cost,
        SUM(list_cost) as sum_list,
        SUM(allowance) as sum_allowance,
        SUM(input_tokens) as sum_input,
        SUM(output_tokens) as sum_output,
        SUM(cache_read) as sum_cache_read
    FROM requests
    {where}
    GROUP BY model, billing_source
    ORDER BY sum_cost DESC
    """
    return conn.execute(q, params).fetchall()

def query_daily(conn: sqlite3.Connection):
    return conn.execute("""
        SELECT substr(timestamp,1,10) as day, SUM(cost) as cost, SUM(allowance) as allowance, COUNT(*) as n
        FROM requests GROUP BY day ORDER BY day
    """).fetchall()

def recent(conn: sqlite3.Connection, limit=20):
    return conn.execute("SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
