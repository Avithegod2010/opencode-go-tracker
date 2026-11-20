"""
Import usage metadata from OpenCode's local session database.

The importer reads only usage metadata: session ids, projects, titles, model
ids, cost, token counts, timestamps, and finish/error state. It never reads
prompts, message content, credentials, accounts, or API keys.

OpenCode's session store does not include `opencode.allowance`, so imported
rows carry cost/tokens but allowance=0. The reverse proxy is the source for
allowance and therefore for live Go limit percentages.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import (
    current_plan,
    get_conn,
    insert_request,
    normalize_bound,
    upsert_session,
    utc_now,
)


def find_opencode_db(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    env = os.getenv("OPENCODE_CLIENT_DB")
    if env:
        path = Path(env).expanduser()
        if path.is_file():
            return path
    home = Path.home()
    candidates = [home / ".local" / "share" / "opencode" / "opencode.db"]
    if os.name == "nt":
        for var in ("LOCALAPPDATA", "APPDATA"):
            base = os.getenv(var)
            if base:
                candidates.append(Path(base) / "opencode" / "opencode.db")
    for path in candidates:
        if path.is_file():
            return path
    return None


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ms_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _since_ms(value: Any) -> Optional[int]:
    iso = normalize_bound(value)
    if not iso:
        return None
    try:
        text = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return None


def _project_label(path: Any, full_paths: bool = False) -> Optional[str]:
    if not path:
        return None
    text = str(path).replace("\\", "/").rstrip("/")
    if not text:
        return None
    return text if full_paths else text.split("/")[-1]


def _session_model(value: Any) -> Optional[str]:
    obj = _json_obj(value)
    return obj.get("id") or obj.get("modelID") or obj.get("modelId") or None


def _tokens(data: Dict[str, Any]) -> Dict[str, int]:
    tokens = data.get("tokens") or {}
    if not isinstance(tokens, dict):
        return {}
    cache = tokens.get("cache") or {}
    if not isinstance(cache, dict):
        cache = {}
    def number(key: str) -> int:
        try:
            return int(tokens.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    try:
        cache_read = int(cache.get("read") or 0)
        cache_write = int(cache.get("write") or 0)
    except (TypeError, ValueError):
        cache_read = cache_write = 0
    return {
        "input": number("input"),
        "output": number("output"),
        "reasoning": number("reasoning"),
        "cache_read": cache_read,
        "cache_write": cache_write,
    }


def _billing_source(provider: str, model_id: str, cost: float) -> str:
    provider = (provider or "").lower()
    model_id = (model_id or "").lower()
    if "opencode-go" in provider:
        return "go"
    if cost and cost > 0:
        return "balance"
    if "free" in model_id:
        return "free"
    return "unknown"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _sessions(conn: sqlite3.Connection, since_ms: Optional[int]) -> Dict[str, Dict[str, Any]]:
    sessions: Dict[str, Dict[str, Any]] = {}
    projects = {}
    if _table_exists(conn, "project"):
        for row in conn.execute("SELECT id, worktree, name FROM project"):
            projects[row["id"]] = row["worktree"] or row["name"]

    for table in ("session", "session_v2"):
        if not _table_exists(conn, table):
            continue
        where = " WHERE time_updated >= ?" if since_ms is not None else ""
        params = (since_ms,) if since_ms is not None else ()
        rows = conn.execute(
            f"""
            SELECT id, project_id, directory, title, cost,
                   tokens_input, tokens_output, tokens_reasoning,
                   tokens_cache_read, tokens_cache_write, model,
                   time_created, time_updated
            FROM {table}{where}
            """,
            params,
        ).fetchall()
        for row in rows:
            sid = row["id"]
            existing = sessions.get(sid)
            candidate = {
                "id": sid,
                "project_path": projects.get(row["project_id"]) or row["directory"],
                "title": row["title"],
                "cost": float(row["cost"] or 0.0),
                "input": int(row["tokens_input"] or 0),
                "output": int(row["tokens_output"] or 0),
                "reasoning": int(row["tokens_reasoning"] or 0),
                "cache_read": int(row["tokens_cache_read"] or 0),
                "cache_write": int(row["tokens_cache_write"] or 0),
                "model": _session_model(row["model"]),
                "created": row["time_created"],
                "updated": row["time_updated"],
                "table": table,
            }
            # Prefer session_v2 when both tables contain the same session.
            if existing is None or table == "session_v2":
                sessions[sid] = candidate
    return sessions


def _v2_messages(
    conn: sqlite3.Connection,
    since_ms: Optional[int],
    session_ids: Optional[set] = None,
) -> List[sqlite3.Row]:
    if not _table_exists(conn, "session_message"):
        return []
    clauses = ["type='assistant'"]
    params: List[Any] = []
    if since_ms is not None:
        clauses.append("time_created >= ?")
        params.append(since_ms)
    if session_ids:
        clauses.append(f"session_id IN ({','.join('?' for _ in session_ids)})")
        params.extend(session_ids)
    return conn.execute(
        f"""
        SELECT id, session_id,
               json_extract(data,'$.cost') AS cost,
               json_extract(data,'$.tokens') AS tokens,
               json_extract(data,'$.model') AS model,
               json_extract(data,'$.time') AS time,
               json_extract(data,'$.finish') AS finish,
               json_extract(data,'$.error') AS error
        FROM session_message WHERE {' AND '.join(clauses)}
        """,
        params,
    ).fetchall()


def _v1_messages(
    conn: sqlite3.Connection,
    since_ms: Optional[int],
    session_ids: Optional[set] = None,
) -> List[sqlite3.Row]:
    if not _table_exists(conn, "message"):
        return []
    clauses = ["json_extract(data,'$.role')='assistant'"]
    params: List[Any] = []
    if since_ms is not None:
        clauses.append("time_created >= ?")
        params.append(since_ms)
    if session_ids:
        clauses.append(f"session_id IN ({','.join('?' for _ in session_ids)})")
        params.extend(session_ids)
    return conn.execute(
        f"""
        SELECT id, session_id,
               json_extract(data,'$.cost') AS cost,
               json_extract(data,'$.tokens') AS tokens,
               json_extract(data,'$.modelID') AS model_id,
               json_extract(data,'$.providerID') AS provider,
               json_extract(data,'$.time') AS time,
               json_extract(data,'$.finish') AS finish,
               json_extract(data,'$.error') AS error
        FROM message WHERE {' AND '.join(clauses)}
        """,
        params,
    ).fetchall()


def _message_entry(
    row: sqlite3.Row,
    session: Dict[str, Any],
    version: str,
    project: Optional[str],
    profile: Optional[str],
    member: Optional[str],
    plan: Optional[str],
) -> Optional[Dict[str, Any]]:
    tokens = _tokens({"tokens": _json_obj(row["tokens"])})
    try:
        cost = float(row["cost"] or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    total_tokens = sum(tokens.values())
    if total_tokens <= 0 and cost <= 0:
        return None

    if version == "v2":
        model_obj = _json_obj(row["model"])
        model_id = model_obj.get("id") or session.get("model") or "unknown"
        provider = model_obj.get("providerID") or ""
    else:
        model_id = row["model_id"] or session.get("model") or "unknown"
        provider = row["provider"] or ""

    time_obj = _json_obj(row["time"])
    stamp = _ms_to_iso(time_obj.get("completed") or time_obj.get("created"))
    latency = None
    if time_obj.get("created") and time_obj.get("completed"):
        try:
            latency = int(float(time_obj["completed"]) - float(time_obj["created"]))
        except (TypeError, ValueError):
            latency = None

    status = "error" if row["error"] else "ok"
    return {
        "timestamp": stamp or _ms_to_iso(session.get("updated")) or utc_now().isoformat(),
        "model": str(model_id),
        "provider": provider or None,
        "endpoint": "opencode/session",
        "billing_source": _billing_source(provider, str(model_id), cost),
        "cost": cost,
        "list_cost": None,
        "allowance": 0.0,
        "input_tokens": tokens.get("input"),
        "output_tokens": tokens.get("output"),
        "reasoning_tokens": tokens.get("reasoning"),
        "cache_read": tokens.get("cache_read"),
        "cache_write_5m": tokens.get("cache_write"),
        "prompt_tokens": tokens.get("input"),
        "completion_tokens": tokens.get("output"),
        "status": status,
        "profile": profile,
        "plan": plan,
        "member": member,
        "session_id": session["id"],
        "project": project,
        "source": "import",
        "request_kind": "session",
        "external_id": row["id"],
        "http_status": None,
        "latency_ms": latency,
        "raw_json": json.dumps({
            "imported_from": "opencode",
            "session_id": session["id"],
            "provider": provider,
            "model": str(model_id),
            "finish": row["finish"],
        }, ensure_ascii=False)[:2000],
    }


def import_sessions(
    target_conn: sqlite3.Connection,
    source_db: Optional[str | Path] = None,
    since: Optional[str] = None,
    limit_sessions: Optional[int] = None,
    dry_run: bool = False,
    full_paths: bool = False,
    include_titles: bool = True,
    profile: Optional[str] = None,
    member: Optional[str] = None,
    plan: Optional[str] = None,
) -> Dict[str, Any]:
    source_path = find_opencode_db(str(source_db) if source_db else None)
    if source_path is None:
        raise FileNotFoundError(
            "OpenCode session database not found. Pass --opencode-db or set OPENCODE_CLIENT_DB."
        )

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=10)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA query_only=ON")
    source.execute("PRAGMA busy_timeout=5000")

    since_ms = _since_ms(since)
    all_sessions = _sessions(source, since_ms)
    seen_sessions = len(all_sessions)
    if limit_sessions:
        all_sessions = dict(
            sorted(all_sessions.items(), key=lambda item: item[1]["updated"] or 0, reverse=True)[: int(limit_sessions)]
        )
    sessions = all_sessions

    plan = plan or current_plan(target_conn)
    stats: Dict[str, Any] = {
        "source_db": str(source_path),
        "sessions_seen": seen_sessions,
        "sessions_selected": len(sessions),
        "sessions_imported": 0,
        "messages_seen": 0,
        "messages_imported": 0,
        "duplicates_skipped": 0,
        "adjustment_rows": 0,
        "total_cost": round(sum(s.get("cost", 0.0) for s in sessions.values()), 6),
        "total_tokens": sum(
            s.get("input", 0) + s.get("output", 0) + s.get("reasoning", 0)
            + s.get("cache_read", 0) + s.get("cache_write", 0)
            for s in sessions.values()
        ),
        "dry_run": bool(dry_run),
    }
    if not sessions:
        source.close()
        return stats

    session_ids = set(sessions)
    v2_rows = _v2_messages(source, since_ms, session_ids)
    v1_rows = _v1_messages(source, since_ms, session_ids)
    source.close()

    # Prefer v2 messages for sessions present in session_v2; use v1 for older sessions.
    grouped: Dict[str, List[tuple]] = {}
    for row in v2_rows:
        if row["session_id"] in session_ids:
            grouped.setdefault(row["session_id"], []).append((row, "v2"))
    for row in v1_rows:
        sid = row["session_id"]
        if sid in session_ids and sid not in grouped:
            grouped.setdefault(sid, []).append((row, "v1"))

    imported_cost: Dict[str, float] = {}
    imported_components: Dict[str, Dict[str, int]] = {}

    def components(entry: Dict[str, Any]) -> Dict[str, int]:
        return {
            "input": int(entry.get("input_tokens") or 0),
            "output": int(entry.get("output_tokens") or 0),
            "reasoning": int(entry.get("reasoning_tokens") or 0),
            "cache_read": int(entry.get("cache_read") or 0),
            "cache_write": int(entry.get("cache_write_5m") or 0),
        }

    for sid, session in sessions.items():
        project = _project_label(session.get("project_path"), full_paths)
        if not dry_run:
            upsert_session(
                target_conn,
                sid,
                project=project,
                title=session.get("title") if include_titles else None,
                source="import",
                model=session.get("model"),
                started_at=_ms_to_iso(session.get("created")),
                updated_at=_ms_to_iso(session.get("updated")),
                total_cost=session.get("cost"),
                total_allowance=0.0,
                total_tokens=(
                    session.get("input", 0) + session.get("output", 0)
                    + session.get("reasoning", 0) + session.get("cache_read", 0)
                    + session.get("cache_write", 0)
                ),
            )
        stats["sessions_imported"] += 1
        imported_cost.setdefault(sid, 0.0)
        imported_components.setdefault(
            sid, {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
        )

        for row, version in grouped.get(sid, []):
            stats["messages_seen"] += 1
            entry = _message_entry(row, session, version, project, profile, member, plan)
            if entry is None:
                continue
            imported_cost[sid] += entry["cost"]
            for key, value in components(entry).items():
                imported_components[sid][key] += value
            if dry_run:
                stats["messages_imported"] += 1
                continue
            inserted = insert_request(target_conn, entry, ignore_duplicate=True)
            if inserted:
                stats["messages_imported"] += 1
            else:
                stats["duplicates_skipped"] += 1

        # Reconcile per-message usage with the session aggregate so totals match.
        cost_delta = round(session.get("cost", 0.0) - imported_cost[sid], 10)
        token_deltas = {
            key: max(0, int(session.get(key, 0)) - imported_components[sid][key])
            for key in ("input", "output", "reasoning", "cache_read", "cache_write")
        }
        if cost_delta > 0.0000001 or any(value > 0 for value in token_deltas.values()):
            if dry_run:
                stats["adjustment_rows"] += 1
            else:
                adjustment_id = insert_request(target_conn, {
                    "timestamp": _ms_to_iso(session.get("updated")) or utc_now().isoformat(),
                    "model": session.get("model") or "unknown",
                    "provider": "opencode",
                    "endpoint": "opencode/session",
                    "billing_source": "balance" if cost_delta > 0 else "unknown",
                    "cost": max(0.0, cost_delta),
                    "list_cost": None,
                    "allowance": 0.0,
                    "input_tokens": token_deltas["input"],
                    "output_tokens": token_deltas["output"],
                    "reasoning_tokens": token_deltas["reasoning"],
                    "cache_read": token_deltas["cache_read"],
                    "cache_write_5m": token_deltas["cache_write"],
                    "prompt_tokens": token_deltas["input"],
                    "completion_tokens": token_deltas["output"],
                    "status": "ok",
                    "profile": profile,
                    "plan": plan,
                    "member": member,
                    "session_id": sid,
                    "project": project,
                    "source": "import",
                    "request_kind": "session-adjust",
                    "external_id": f"session:{sid}:adjust",
                    "raw_json": json.dumps({"imported_from": "opencode", "adjustment": True}, ensure_ascii=False),
                }, ignore_duplicate=True)
                if adjustment_id:
                    stats["adjustment_rows"] += 1

    target_conn.commit()
    return stats
