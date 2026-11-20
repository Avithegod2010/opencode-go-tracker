"""
SQLite storage for per-request Inference API logs plus analytics metadata.

Schema v2 adds:
  epoch, profile, plan, member, session_id, project, source, request_kind,
  external_id, http_status, latency_ms
and the profiles / plan_history / sessions tables.

The v1 schema is migrated in place; existing rows keep their data and get an
`epoch` backfilled from `timestamp`.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    epoch REAL,
    model TEXT NOT NULL,
    provider TEXT,
    endpoint TEXT,
    billing_source TEXT,
    cost REAL,
    list_cost REAL,
    allowance REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_read INTEGER,
    cache_write_5m INTEGER,
    cache_write_1h INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    estimated INTEGER,
    status TEXT,
    error TEXT,
    request_id TEXT,
    profile TEXT,
    plan TEXT,
    member TEXT,
    session_id TEXT,
    project TEXT,
    source TEXT,
    request_kind TEXT,
    external_id TEXT,
    http_status INTEGER,
    latency_ms INTEGER,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
CREATE INDEX IF NOT EXISTS idx_requests_billing ON requests(billing_source);

CREATE TABLE IF NOT EXISTS profiles (
    name TEXT PRIMARY KEY,
    base_url TEXT NOT NULL,
    plan TEXT,
    key_env TEXT,
    project TEXT,
    member TEXT,
    created_at TEXT,
    last_checked_at TEXT,
    last_status TEXT
);

CREATE TABLE IF NOT EXISTS plan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_from TEXT NOT NULL,
    plan TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_plan_history_from ON plan_history(effective_from);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project TEXT,
    title TEXT,
    source TEXT,
    model TEXT,
    started_at TEXT,
    updated_at TEXT,
    total_cost REAL DEFAULT 0,
    total_allowance REAL DEFAULT 0,
    total_tokens INTEGER DEFAULT 0
);
"""

# Columns added by v2 for databases created with v1.
ADDED_COLUMNS: Sequence[Tuple[str, str]] = (
    ("epoch", "REAL"),
    ("profile", "TEXT"),
    ("plan", "TEXT"),
    ("member", "TEXT"),
    ("session_id", "TEXT"),
    ("project", "TEXT"),
    ("source", "TEXT"),
    ("request_kind", "TEXT"),
    ("external_id", "TEXT"),
    ("http_status", "INTEGER"),
    ("latency_ms", "INTEGER"),
)

EXTRA_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_external
    ON requests(external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_requests_epoch ON requests(epoch);
CREATE INDEX IF NOT EXISTS idx_requests_profile ON requests(profile);
CREATE INDEX IF NOT EXISTS idx_requests_member ON requests(member);
CREATE INDEX IF NOT EXISTS idx_requests_project ON requests(project);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);
CREATE INDEX IF NOT EXISTS idx_requests_source ON requests(source);
CREATE INDEX IF NOT EXISTS idx_requests_plan ON requests(plan);
"""

INSERT_COLUMNS = [
    "timestamp", "epoch", "model", "provider", "endpoint", "billing_source",
    "cost", "list_cost", "allowance", "input_tokens", "output_tokens",
    "reasoning_tokens", "cache_read", "cache_write_5m", "cache_write_1h",
    "prompt_tokens", "completion_tokens", "estimated", "status", "error",
    "request_id", "profile", "plan", "member", "session_id", "project",
    "source", "request_kind", "external_id", "http_status", "latency_ms",
    "raw_json",
]

FILTER_COLUMNS = {
    "model": "model",
    "billing_source": "billing_source",
    "profile": "profile",
    "member": "member",
    "project": "project",
    "session_id": "session_id",
    "source": "source",
    "request_kind": "request_kind",
    "plan": "plan",
    "status": "status",
}


# ---------- time helpers ----------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_epoch(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Heuristic: milliseconds vs seconds.
        return float(value) / 1000.0 if value > 10_000_000_000 else float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso_from_epoch(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def normalize_bound(value: Any, end: bool = False) -> Optional[str]:
    """Normalize a date/datetime filter bound to an ISO UTC string.

    A bare date means the start of that day, or the very end of that day for
    an upper bound.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        dt = datetime.combine(value, time(23, 59, 59, 999999) if end else time(0, 0), tzinfo=timezone.utc)
        return dt.isoformat()
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            d = date.fromisoformat(text)
        except ValueError:
            return text
        dt = datetime.combine(d, time(23, 59, 59, 999999) if end else time(0, 0), tzinfo=timezone.utc)
        return dt.isoformat()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return text


def build_where(filters: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
    """Build a parameterized WHERE clause from a filter dict."""
    filters = filters or {}
    clauses: List[str] = []
    params: List[Any] = []
    start = normalize_bound(filters.get("from"))
    if start:
        clauses.append("timestamp >= ?")
        params.append(start)
    end = normalize_bound(filters.get("to"), end=True)
    if end:
        clauses.append("timestamp <= ?")
        params.append(end)
    for key, column in FILTER_COLUMNS.items():
        value = filters.get(key)
        if value is None or value == "":
            continue
        values = value if isinstance(value, (list, tuple, set)) else [value]
        values = [str(v) for v in values if v is not None and str(v) != ""]
        if not values:
            continue
        clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
        params.extend(values)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# ---------- connection / schema ----------
def _columns(conn: sqlite3.Connection, table: str = "requests") -> List[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _ensure_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.executescript(SCHEMA)
    existing = set(_columns(conn))
    for column, ddl in ADDED_COLUMNS:
        if column not in existing:
            conn.execute(f'ALTER TABLE requests ADD COLUMN "{column}" {ddl}')
    conn.executescript(EXTRA_INDEXES)
    if version < SCHEMA_VERSION:
        # Backfill epoch for pre-v2 rows. SQLite understands the ISO strings we store.
        conn.execute(
            "UPDATE requests SET epoch = CAST(strftime('%s', timestamp) AS REAL) "
            "WHERE epoch IS NULL AND timestamp IS NOT NULL"
        )
        # Fallback for formats SQLite could not parse.
        for row in conn.execute("SELECT id, timestamp FROM requests WHERE epoch IS NULL"):
            epoch = to_epoch(row["timestamp"])
            if epoch is not None:
                conn.execute("UPDATE requests SET epoch = ? WHERE id = ?", (epoch, row["id"]))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def get_conn(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.execute("PRAGMA busy_timeout=10000;")
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except sqlite3.OperationalError:
        # Another connection is switching/using the journal; WAL is already
        # enabled for the database in that case.
        pass
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


# ---------- writes ----------
def insert_request(
    conn: sqlite3.Connection,
    entry: Dict[str, Any],
    ignore_duplicate: bool = False,
) -> Optional[int]:
    values = []
    for column in INSERT_COLUMNS:
        value = entry.get(column)
        if column == "epoch" and value is None:
            value = to_epoch(entry.get("timestamp"))
        if column == "timestamp" and not value:
            value = utc_now().isoformat()
        values.append(value)
    verb = "INSERT OR IGNORE" if ignore_duplicate else "INSERT"
    cur = conn.execute(
        f"{verb} INTO requests ({','.join(INSERT_COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in INSERT_COLUMNS)})",
        values,
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else 0


def upsert_session(
    conn: sqlite3.Connection,
    session_id: str,
    project: Optional[str] = None,
    title: Optional[str] = None,
    source: Optional[str] = None,
    model: Optional[str] = None,
    started_at: Optional[str] = None,
    updated_at: Optional[str] = None,
    total_cost: Optional[float] = None,
    total_allowance: Optional[float] = None,
    total_tokens: Optional[int] = None,
) -> None:
    if not session_id:
        return
    conn.execute(
        """
        INSERT INTO sessions
            (id, project, title, source, model, started_at, updated_at,
             total_cost, total_allowance, total_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            project=COALESCE(excluded.project, sessions.project),
            title=COALESCE(excluded.title, sessions.title),
            source=COALESCE(excluded.source, sessions.source),
            model=COALESCE(excluded.model, sessions.model),
            started_at=COALESCE(sessions.started_at, excluded.started_at),
            updated_at=COALESCE(excluded.updated_at, sessions.updated_at),
            total_cost=COALESCE(excluded.total_cost, sessions.total_cost),
            total_allowance=COALESCE(excluded.total_allowance, sessions.total_allowance),
            total_tokens=COALESCE(excluded.total_tokens, sessions.total_tokens)
        """,
        (session_id, project, title, source, model, started_at, updated_at,
         total_cost, total_allowance, total_tokens),
    )
    conn.commit()


# ---------- profiles ----------
def add_profile(
    conn: sqlite3.Connection,
    name: str,
    base_url: str,
    plan: Optional[str] = None,
    key_env: Optional[str] = None,
    project: Optional[str] = None,
    member: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO profiles (name, base_url, plan, key_env, project, member, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            base_url=excluded.base_url,
            plan=excluded.plan,
            key_env=excluded.key_env,
            project=excluded.project,
            member=excluded.member
        """,
        (name, base_url, plan, key_env, project, member, utc_now().isoformat()),
    )
    conn.commit()


def list_profiles(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()


def get_profile(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM profiles WHERE name = ?", (name,)).fetchone()


def remove_profile(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM profiles WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def update_profile_check(conn: sqlite3.Connection, name: str, status: str) -> None:
    conn.execute(
        "UPDATE profiles SET last_checked_at = ?, last_status = ? WHERE name = ?",
        (utc_now().isoformat(), status, name),
    )
    conn.commit()


# ---------- plan history ----------
def set_plan(
    conn: sqlite3.Connection,
    plan: str,
    effective_from: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO plan_history (effective_from, plan, note) VALUES (?, ?, ?)",
        (normalize_bound(effective_from) or utc_now().isoformat(), plan, note),
    )
    conn.commit()


def plan_history(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM plan_history ORDER BY effective_from").fetchall()


def current_plan(conn: sqlite3.Connection, at: Optional[str] = None) -> Optional[str]:
    when = normalize_bound(at) or utc_now().isoformat()
    row = conn.execute(
        "SELECT plan FROM plan_history WHERE effective_from <= ? "
        "ORDER BY effective_from DESC LIMIT 1",
        (when,),
    ).fetchone()
    return row["plan"] if row else None


# ---------- reads ----------
def query_totals(conn: sqlite3.Connection, filters: Optional[Dict[str, Any]] = None):
    where, params = build_where(filters)
    return conn.execute(
        f"""
        SELECT model, billing_source, plan, COUNT(*) as n,
               SUM(cost) as sum_cost, SUM(list_cost) as sum_list,
               SUM(allowance) as sum_allowance,
               SUM(input_tokens) as sum_input, SUM(output_tokens) as sum_output,
               SUM(cache_read) as sum_cache_read
        FROM requests{where}
        GROUP BY model, billing_source, plan
        ORDER BY sum_cost DESC
        """,
        params,
    ).fetchall()


def query_daily(conn: sqlite3.Connection, filters: Optional[Dict[str, Any]] = None):
    where, params = build_where(filters)
    return conn.execute(
        f"""
        SELECT substr(timestamp,1,10) as day, SUM(cost) as cost,
               SUM(allowance) as allowance, COUNT(*) as n
        FROM requests{where}
        GROUP BY day ORDER BY day
        """,
        params,
    ).fetchall()


def recent(
    conn: sqlite3.Connection,
    limit: int = 20,
    filters: Optional[Dict[str, Any]] = None,
):
    where, params = build_where(filters)
    return conn.execute(
        f"SELECT * FROM requests{where} ORDER BY id DESC LIMIT ?",
        [*params, int(limit)],
    ).fetchall()


def query_distinct(
    conn: sqlite3.Connection,
    column: str,
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 500,
) -> List[str]:
    if column not in FILTER_COLUMNS:
        raise ValueError(f"Unsupported filter column: {column}")
    where, params = build_where(filters)
    clause = f"{where} AND" if where else " WHERE"
    rows = conn.execute(
        f"SELECT DISTINCT {FILTER_COLUMNS[column]} AS value FROM requests"
        f"{clause} value IS NOT NULL AND value != '' "
        "ORDER BY value LIMIT ?",
        [*params, int(limit)],
    ).fetchall()
    return [row["value"] for row in rows]


def count_requests(conn: sqlite3.Connection, filters: Optional[Dict[str, Any]] = None) -> int:
    where, params = build_where(filters)
    return conn.execute(f"SELECT COUNT(*) FROM requests{where}", params).fetchone()[0]
