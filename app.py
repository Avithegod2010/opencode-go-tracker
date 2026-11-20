#!/usr/bin/env python3
"""
Opencode Go Tracker - Web App
Local website/app for A. CSV merger + B. Inference logger + Report.

Run:
  pip install -r requirements.txt   # now includes Flask
  python app.py                     # opens http://localhost:8000
  python app.py --port 5000 --host 127.0.0.1
  python tracker.py web             # also launches same server

Frontend: web/index.html (single-page dashboard with 4 tabs)
Backend API:
  GET  /              -> dashboard
  POST /api/merge     -> CSV merger (multipart files + plan/billable_only)
  POST /api/log       -> Inference logger (JSON -> calls opencode.ai)
  GET  /api/report    -> DB report + Go % computed
  GET  /api/go-limits -> per-model monthly limits
  GET  /api/health    -> status
"""
from __future__ import annotations
import os
import sys
import io
import csv
import json
import shutil
import tempfile
import argparse
import ipaddress
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

# Ensure src on path
sys.path.insert(0, str(Path(__file__).parent / "src"))

# Auto-install Flask if missing
try:
    import flask
except ImportError:
    print("Flask not found, installing...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "Flask>=2.3"])
    import flask

from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

from opencode_tracker.csv_merger import merge_csvs
from opencode_tracker.analytics import analytics
from opencode_tracker.forecast import forecast
from opencode_tracker.logger import OpencodeLogger, classify_error, parse_usage
from opencode_tracker.db import (
    build_where,
    current_plan,
    get_conn,
    list_profiles,
    plan_history,
    query_daily,
    query_distinct,
    query_totals,
    recent,
    set_plan,
)
from opencode_tracker.go_limits import (
    all_limits,
    load_cached_limits,
    sync_limits,
)
from opencode_tracker.importer import find_opencode_db, import_sessions
from opencode_tracker.preflight import check_key
from opencode_tracker.profiles import profile_summary, resolve_profile
from opencode_tracker.simulator import simulate_plans
from opencode_tracker.windows import compute_windows, period_start, week_start

BASE_DIR = Path(__file__).parent
WEB_DIR = BASE_DIR / "web"
DB_PATH = Path(os.getenv("OPENCODE_DB", BASE_DIR / "opencode_usage.db"))
CSV_PATH = Path(os.getenv("OPENCODE_CSV", BASE_DIR / "usage_log.csv"))
PERIOD_START_DAY = int(os.getenv("OPENCODE_PERIOD_START_DAY", "1") or 1)

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024

# --- Local-only hardening -------------------------------------------------
# The dashboard is a local tool. These checks keep it local even if someone
# binds it to 0.0.0.0 or visits it through a hostile DNS name.
def _env_set(name: str) -> set:
    raw = os.getenv(name) or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


EXTRA_TRUSTED_HOSTS = _env_set("OPENCODE_ALLOWED_HOSTS")
# Base URLs the web UI may send API keys to. CLI/env users are trusted; this
# only constrains the HTTP surface (prevents SSRF and key exfiltration).
ALLOWED_BASE_HOSTS = {"opencode.ai"} | _env_set("OPENCODE_ALLOWED_BASE_HOSTS")


def _hostname(value: str) -> str:
    try:
        return (urlsplit("//" + str(value or "")).hostname or "").lower().strip("[]")
    except ValueError:
        return ""


def _host_allowed(hostname: str) -> bool:
    if not hostname:
        return False
    if hostname in ("localhost", "127.0.0.1", "::1") or hostname in EXTRA_TRUSTED_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False  # arbitrary domain names are rejected (DNS rebinding)
    return ip.is_loopback or ip.is_private


def _port_for(value: str, scheme: str) -> int | None:
    try:
        port = urlsplit(value if "://" in value else "//" + value).port
    except ValueError:
        return None
    if port is not None:
        return port
    return {"http": 80, "https": 443}.get(scheme)


def _same_origin_ok() -> bool:
    """Reject cross-site requests and DNS-rebinding Host headers."""
    host = _hostname(request.host)
    if not _host_allowed(host):
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True  # curl / CLI
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    scheme = (parts.scheme or "").lower()
    if scheme != request.scheme.lower():
        return False
    if (parts.hostname or "").lower().strip("[]") != host:
        return False
    return _port_for(origin, scheme) == _port_for(request.host, request.scheme)


def _client_is_local() -> bool:
    addr = request.remote_addr or ""
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return addr == "localhost"


def _load_profile(name: Optional[str]) -> Optional[dict]:
    if not name:
        return None
    conn = get_conn(str(DB_PATH))
    try:
        return resolve_profile(conn, name)
    finally:
        conn.close()


def _current_plan() -> Optional[str]:
    conn = get_conn(str(DB_PATH))
    try:
        return current_plan(conn)
    finally:
        conn.close()


FILTER_KEYS = ("from", "to", "model", "member", "profile", "project", "session", "source", "status")


def _request_filters() -> dict:
    """Read report filters from the query string (repeatable values allowed)."""
    filters = {}
    for key in FILTER_KEYS:
        values = [v for v in request.args.getlist(key) if v != ""]
        if not values:
            continue
        if key == "session":
            filters["session_id"] = values if len(values) > 1 else values[0]
        else:
            filters[key] = values if len(values) > 1 else values[0]
    return filters


def _validate_base_url(url: str) -> str:
    parts = urlsplit(url or "")
    if parts.scheme != "https":
        raise ValueError("Base URL must use https")
    if parts.username or parts.password:
        raise ValueError("Base URL must not include credentials")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_BASE_HOSTS:
        raise ValueError(f"Base URL host is not allowed: {host or '(missing)'}")
    return url.rstrip("/")


@app.before_request
def _guard_local_api():
    if request.path.startswith("/api/") and not _same_origin_ok():
        return json_error("Blocked: untrusted Host/Origin for a local API request.", 403)
    return None


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'",
    )
    return response


# --- Helpers ---
def json_error(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code

# --- Routes ---
@app.route("/")
def index():
    return send_from_directory(str(WEB_DIR), "index.html")

@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "db_exists": DB_PATH.exists(),
        "csv_exists": CSV_PATH.exists(),
        "web_dir": str(WEB_DIR),
        "period_start_day": PERIOD_START_DAY,
    })

@app.route("/api/profiles")
def api_profiles():
    conn = get_conn(str(DB_PATH))
    try:
        rows = list_profiles(conn)
    finally:
        conn.close()
    return jsonify({"ok": True, "profiles": [profile_summary(row) for row in rows]})

@app.route("/api/plan", methods=["GET", "POST"])
def api_plan():
    conn = get_conn(str(DB_PATH))
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            plan = (data.get("plan") or "").strip()
            if not plan:
                return json_error("plan is required", 400)
            set_plan(
                conn,
                plan,
                effective_from=data.get("effective_from"),
                note=data.get("note"),
            )
        current = current_plan(conn)
        history = [dict(row) for row in plan_history(conn)]
    finally:
        conn.close()
    return jsonify({"ok": True, "current": current, "history": history})

@app.route("/api/filters/options")
def api_filter_options():
    conn = get_conn(str(DB_PATH))
    try:
        payload = {
            "models": query_distinct(conn, "model"),
            "members": query_distinct(conn, "member"),
            "profiles": query_distinct(conn, "profile"),
            "projects": query_distinct(conn, "project"),
            "sessions": query_distinct(conn, "session_id"),
            "sources": query_distinct(conn, "source"),
        }
    finally:
        conn.close()
    return jsonify({"ok": True, **payload})

@app.route("/api/go-limits")
def go_limits():
    limits = all_limits()
    cached = load_cached_limits()
    return jsonify({
        "limits": limits,
        "plans": ["go", "go_plus", "go_teams"],
        "note": "5h = 20% monthly (window starts on first use), weekly = 50% (Monday 00:00 UTC), monthly = 100%",
        "synced": bool(cached),
        "synced_models": len(cached),
    })

@app.route("/api/sync-limits", methods=["POST"])
def api_sync_limits():
    """Refresh the Go limit table from the documented model list API."""
    data = request.get_json(silent=True) or {}
    base_url = data.get("base_url") or os.getenv("OPENCODE_GO_URL") or "https://opencode.ai/inference/go"
    try:
        base_url = _validate_base_url(base_url)
    except ValueError as e:
        return json_error(f"Refusing base URL: {e}", 400)
    api_key = (data.get("api_key") or "").strip()
    if not api_key and _client_is_local():
        api_key = (os.getenv("OPENCODE_API_KEY") or "").strip()
    try:
        result = sync_limits(
            api_key=api_key or None,
            base_url=base_url,
            cache_path=os.getenv("OPENCODE_LIMITS_CACHE") or None,
        )
    except Exception as e:
        return json_error(f"Limit sync failed: {e}", 502)
    return jsonify({
        "ok": True,
        "source": result["source"],
        "fetched_at": result["fetched_at"],
        "models": len(result["limits"]),
        "limits": result["limits"],
    })

@app.route("/api/key-check", methods=["POST"])
def api_key_check():
    """Verify a key against the model list endpoint without calling a model."""
    data = request.get_json(silent=True) or {}
    profile_name = (data.get("profile") or "").strip() or None
    profile = None
    if profile_name:
        try:
            profile = _load_profile(profile_name)
        except KeyError as e:
            return json_error(str(e), 404)

    base_url = data.get("base_url") or (profile or {}).get("base_url") or os.getenv("OPENCODE_BASE_URL") or "https://opencode.ai/inference/go"
    if base_url == "go":
        base_url = "https://opencode.ai/inference/go"
    if base_url in ("balance", "inference"):
        base_url = "https://opencode.ai/inference"
    try:
        base_url = _validate_base_url(base_url)
    except ValueError as e:
        return json_error(f"Refusing base URL: {e}", 400)

    body_key = (data.get("api_key") or "").strip()
    profile_key = (profile or {}).get("key") or ""
    env_key = (os.getenv("OPENCODE_API_KEY") or "").strip()
    if not body_key and (profile_key or env_key) and not _client_is_local():
        return json_error("Non-local clients must send their own API key; server-side profile keys are used for loopback requests only.", 401)
    api_key = body_key or profile_key or env_key
    if not api_key or not api_key.startswith("oc_sk_"):
        return json_error("Missing or invalid API key. Create one at Console: Account > Personal API keys (needs Models > Request).", 401)

    result = check_key(api_key, base_url)
    result["profile"] = profile_name
    return jsonify(result)

@app.route("/api/merge", methods=["POST"])
def api_merge():
    # form: files (multiple), plan, billable_only, dedupe
    files = request.files.getlist("files")
    # also support single "file"
    if not files or (len(files)==1 and files[0].filename==""):
        f = request.files.get("file")
        if f:
            files = [f]
    if not files:
        return json_error("No files uploaded. Send multipart field 'files' (multiple).")
    plan = request.form.get("plan") or request.args.get("plan") or None
    if plan == "":
        plan = None
    billable_only = (request.form.get("billable_only") == "true") or (request.args.get("billable_only") == "true")
    dedupe_str = request.form.get("dedupe", request.args.get("dedupe", "true"))
    dedupe = dedupe_str != "false" and dedupe_str != "0"

    # Save to temp
    tmpdir = tempfile.mkdtemp(prefix="opencode-merge-")
    paths = []
    for f in files:
        fname = secure_filename(f.filename or "upload.csv")
        if not fname.lower().endswith(".csv"):
            fname += ".csv"
        p = Path(tmpdir) / fname
        # Ensure unique if duplicate names
        counter = 1
        orig = p
        while p.exists():
            p = orig.with_name(f"{orig.stem}_{counter}{orig.suffix}")
            counter+=1
        f.save(str(p))
        paths.append(str(p))

    try:
        result = merge_csvs(paths, output=None, billable_only=billable_only, dedupe=dedupe, plan=plan, verbose=False)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return json_error(f"Merge failed: {e}", 500)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Build CSV outputs in-memory for download
    per_model = result["per_model"]
    # Totals CSV
    out_csv = io.StringIO()
    w = csv.DictWriter(out_csv, fieldnames=["model","cost","billable_cost","list_cost","allowance_sum","rows","monthly_limit","pct_month","5h_limit","weekly_limit"])
    w.writeheader()
    for mr in per_model:
        w.writerow({
            "model": mr["model"],
            "cost": f"{mr['cost']:.6f}",
            "billable_cost": f"{mr['billable_cost']:.6f}",
            "list_cost": f"{mr['list_cost']:.6f}",
            "allowance_sum": f"{mr['allowance_sum']:.6f}",
            "rows": mr["rows"],
            "monthly_limit": mr["monthly_limit"] if mr["monthly_limit"] is not None else "",
            "pct_month": f"{mr['pct_month']:.2f}" if mr["pct_month"] is not None else "",
            "5h_limit": mr["5h_limit"] if mr["5h_limit"] is not None else "",
            "weekly_limit": mr["weekly_limit"] if mr["weekly_limit"] else "",
        })
    totals_csv_str = out_csv.getvalue()

    # Detailed (deduped + filtered) rows used for the sums, for audit/debug.
    detail_csv = io.StringIO()
    detail_fields = ["date", "model", "person", "cost", "list_cost", "allowance", "billing_source", "is_billable", "source_file"]
    dw = csv.DictWriter(detail_csv, fieldnames=detail_fields, extrasaction="ignore")
    dw.writeheader()
    for r in result["detailed_rows"]:
        dw.writerow(r)
    detailed_csv_str = detail_csv.getvalue()

    # Also include summary
    return jsonify({
        "ok": True,
        "plan": plan,
        "billable_only": billable_only,
        "dedupe": dedupe,
        "per_model": per_model,
        "per_billing": result["per_billing"],
        "total_cost": result["total_cost"],
        "total_billable": result["total_billable"],
        "total_allowance": result["total_allowance"],
        "total_cost_all": result["total_cost_all"],
        "total_billable_all": result["total_billable_all"],
        "total_allowance_all": result["total_allowance_all"],
        "rows_before": result["rows_before_dedupe"],
        "rows_after": result["rows_after_dedupe"],
        "rows_filtered": result["rows_filtered"],
        "dupes_removed": result["dupes_removed"],
        "files": result["files"],
        "totals_csv": totals_csv_str,
        "detailed_csv": detailed_csv_str,
        "detected_cols": result["detected_cols"],
    })

@app.route("/api/analytics")
def api_analytics():
    try:
        days = int(request.args.get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(120, days))
    filters = _request_filters()
    try:
        conn = get_conn(str(DB_PATH))
        try:
            result = analytics(conn, filters=filters, days=days)
        finally:
            conn.close()
    except Exception as e:
        return json_error(f"Analytics failed: {e}", 500)
    return jsonify({"ok": True, "filters": filters, "days": days, **result})

@app.route("/api/forecast")
def api_forecast():
    plan = request.args.get("plan") or "go"
    try:
        period_day = int(request.args.get("period_start_day") or PERIOD_START_DAY)
    except (TypeError, ValueError):
        period_day = PERIOD_START_DAY
    period_day = max(1, min(31, period_day))
    filters = _request_filters()
    try:
        conn = get_conn(str(DB_PATH))
        try:
            result = forecast(conn, plan, period_start_day=period_day, filters=filters)
        finally:
            conn.close()
    except Exception as e:
        return json_error(f"Forecast failed: {e}", 500)
    return jsonify({"ok": True, "plan": plan, "filters": filters, **result})

@app.route("/api/plan-sim")
def api_plan_sim():
    try:
        period_day = int(request.args.get("period_start_day") or PERIOD_START_DAY)
    except (TypeError, ValueError):
        period_day = PERIOD_START_DAY
    period_day = max(1, min(31, period_day))
    filters = _request_filters()
    try:
        conn = get_conn(str(DB_PATH))
        try:
            result = simulate_plans(conn, filters=filters, period_start_day=period_day)
        finally:
            conn.close()
    except Exception as e:
        return json_error(f"Plan simulation failed: {e}", 500)
    return jsonify({"ok": True, "filters": filters, **result})

@app.route("/api/import-sessions", methods=["POST"])
def api_import_sessions():
    """Import usage metadata from the auto-detected OpenCode session DB."""
    data = request.get_json(silent=True) or {}
    source = find_opencode_db()
    if source is None:
        return json_error(
            "OpenCode session database not found. Set OPENCODE_CLIENT_DB or use the CLI with --opencode-db.",
            404,
        )
    try:
        limit = int(data["limit"]) if data.get("limit") else None
    except (TypeError, ValueError):
        return json_error("limit must be an integer", 400)

    try:
        conn = get_conn(str(DB_PATH))
        stats = import_sessions(
            conn,
            source_db=source,
            since=data.get("since"),
            limit_sessions=limit,
            dry_run=bool(data.get("dry_run")),
            full_paths=bool(data.get("full_paths")),
            include_titles=not bool(data.get("no_titles")),
            profile=data.get("profile"),
            member=data.get("member"),
            plan=data.get("plan"),
        )
        conn.close()
    except Exception as e:
        return json_error(f"Session import failed: {e}", 500)
    return jsonify({"ok": True, **stats})

@app.route("/api/report", methods=["GET"])
def api_report():
    plan = request.args.get("plan") or None
    if plan == "":
        plan = None
    if not DB_PATH.exists():
        return jsonify({"ok": True, "empty": True, "message": "No DB yet. Log a request first.", "db_path": str(DB_PATH)})

    try:
        period_day = int(request.args.get("period_start_day") or PERIOD_START_DAY)
    except (TypeError, ValueError):
        period_day = PERIOD_START_DAY
    period_day = max(1, min(31, period_day))
    filters = _request_filters()

    try:
        conn = get_conn(str(DB_PATH))
        rows = query_totals(conn, filters)
        daily = query_daily(conn, filters)
        rec = recent(conn, 20, filters)
        p_start = period_start(start_day=period_day)
        where_nodate, params_nodate = build_where(
            {k: v for k, v in filters.items() if k not in ("from", "to")}
        )
        clauses = ["timestamp >= ?"]
        params = [p_start.isoformat()]
        if where_nodate:
            clauses.append(where_nodate.replace(" WHERE ", "", 1))
            params.extend(params_nodate)
        month_totals = conn.execute(
            "SELECT COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(allowance),0) AS allowance, "
            "COALESCE(SUM(list_cost),0) AS list_cost, COUNT(*) AS n "
            f"FROM requests WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        windows = compute_windows(
            conn, plan, period_start_day=period_day, filters=filters
        ) if plan else []
        conn.close()
    except Exception as e:
        return json_error(f"DB read failed: {e}", 500)

    # Backwards-compatible map for older clients, now scoped to the current
    # billing period instead of the lifetime of the database. A model used
    # under two plans gets a `model@plan` key to avoid collisions.
    from collections import Counter
    counts = Counter(w["model"] for w in windows)
    go_pct = {}
    for w in windows:
        key = w["model"] if counts[w["model"]] == 1 else f"{w['model']}@{w['plan']}"
        go_pct[key] = {
            "limit": w["limit"],
            "pct": w["month_pct"],
            "allowance": w["month_used"],
            "5h": w["five_hour_limit"],
            "weekly": w["week_limit"],
            "5h_used": w["five_hour_used"],
            "weekly_used": w["week_used"],
            "pct_5h": w["five_hour_pct"],
            "pct_weekly": w["week_pct"],
            "status": w["status"],
        }

    five_hour_start = next(
        (w["five_hour_start"] for w in windows if w["five_hour_start"]), None
    )

    # Serialize rows
    def row_dict(r):
        return {
            "model": r["model"],
            "billing_source": r["billing_source"],
            "n": r["n"],
            "sum_cost": r["sum_cost"],
            "sum_allowance": r["sum_allowance"],
            "sum_list": r["sum_list"],
            "sum_input": r["sum_input"],
            "sum_output": r["sum_output"],
            "sum_cache_read": r["sum_cache_read"],
        }

    return jsonify({
        "ok": True,
        "empty": len(rows)==0,
        "per_model_billing": [row_dict(r) for r in rows],
        "daily": [{"day": d["day"], "cost": d["cost"], "allowance": d["allowance"], "n": d["n"]} for d in daily],
        "recent": [dict(r) for r in rec],
        "go_pct": go_pct,
        "windows": windows,
        "filters": filters,
        "window_meta": {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "period_start": p_start.isoformat(),
            "period_start_day": period_day,
            "week_start": week_start().isoformat(),
            "five_hour_start": five_hour_start,
        },
        "plan": plan,
        "totals": {
            "cost": sum((r["sum_cost"] or 0) for r in rows),
            "allowance": sum((r["sum_allowance"] or 0) for r in rows),
            "list": sum((r["sum_list"] or 0) for r in rows),
        },
        "month_totals": {
            "cost": month_totals["cost"],
            "allowance": month_totals["allowance"],
            "list": month_totals["list_cost"],
            "n": month_totals["n"],
        },
    })

@app.route("/api/log", methods=["POST"])
def api_log():
    data = request.get_json(force=True, silent=False)
    if not data:
        return json_error("Missing JSON body")
    model = data.get("model") or "kimi-k3"
    message = data.get("message") or "Say hi in one short sentence."
    system = data.get("system")
    api = data.get("api") or "openai"
    profile_name = (data.get("profile") or "").strip() or None
    profile = None
    if profile_name:
        try:
            profile = _load_profile(profile_name)
        except KeyError as e:
            return json_error(str(e), 404)
    base_url = data.get("base_url") or (profile or {}).get("base_url") or os.getenv("OPENCODE_BASE_URL") or "https://opencode.ai/inference"
    body_key = (data.get("api_key") or "").strip()
    profile_key = (profile or {}).get("key") or ""
    env_key = (os.getenv("OPENCODE_API_KEY") or "").strip()
    max_tokens = data.get("max_tokens")
    stream = bool(data.get("stream", False))
    # allow palette: base_url may be "go" shorthand
    if base_url == "go":
        base_url = "https://opencode.ai/inference/go"
    if base_url == "balance" or base_url == "inference":
        base_url = "https://opencode.ai/inference"

    # The browser may only talk to the real Console host. This blocks SSRF
    # and stops a crafted request from sending a key to an attacker's server.
    try:
        base_url = _validate_base_url(base_url)
    except ValueError as e:
        return json_error(f"Refusing base URL: {e}", 400)

    # A server-side key (profile or .env) is for loopback use only; a LAN
    # client must bring its own key so it cannot spend the owner's credits.
    if not body_key and (profile_key or env_key) and not _client_is_local():
        return json_error(
            "Non-local clients must send their own API key; server-side keys are used for loopback requests only.",
            401,
        )
    api_key = body_key or profile_key or env_key

    if not api_key or not api_key.startswith("oc_sk_"):
        return json_error("Missing or invalid OPENCODE_API_KEY. Create one at Console: Account > Personal API keys or Settings > Service accounts (needs Models: Request). Key looks like oc_sk_...", 401)

    plan = data.get("plan") or (profile or {}).get("plan") or os.getenv("OPENCODE_PLAN") or _current_plan()
    member = data.get("member") or (profile or {}).get("member") or os.getenv("OPENCODE_MEMBER")
    project = data.get("project") or (profile or {}).get("project") or os.getenv("OPENCODE_PROJECT")

    # Create logger (uses DB_PATH/CSV_PATH)
    try:
        logger = OpencodeLogger(
            db_path=str(DB_PATH),
            csv_path=str(CSV_PATH),
            api_key=api_key,
            base_url=base_url,
            profile=profile_name,
            plan=plan,
            member=member,
            project=project,
        )
    except Exception as e:
        return json_error(f"Logger init failed: {e}")

    try:
        if api == "anthropic":
            # Anthropic needs max_tokens
            msgs = [{"role": "user", "content": message}]
            resp = logger.anthropic_messages(model=model, messages=msgs, max_tokens=max_tokens or 1024)
        elif api == "responses":
            resp = logger.responses(model=model, input_text=message)
        elif api == "gemini":
            resp = logger.gemini_generate(model=model, contents=[{"parts": [{"text": message}]}])
        else:  # openai
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": message})
            resp = logger.chat_completions(model=model, messages=msgs, max_tokens=max_tokens, stream=stream)
        parsed = parse_usage(resp) if not stream or isinstance(resp, dict) else {}
        # For stream, resp is {"chunks": [...], "usage_parsed": parsed}
        if isinstance(resp, dict) and "usage_parsed" in resp:
            parsed = resp["usage_parsed"]
            resp = {"chunks": resp.get("chunks", [])[:2], "usage_parsed": parsed, "note": "streamed"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        # Error details may include 402/429
        err_msg = str(e)
        label, hint = classify_error(None, err_msg)
        code = int(label[:3]) if label[:3].isdigit() else 400
        return jsonify({"ok": False, "error": err_msg, "hint": f"{label} - {hint}"}), code

    # Extract assistant text preview (best effort)
    preview = ""
    try:
        if isinstance(resp, dict):
            if "choices" in resp:
                preview = (resp["choices"][0].get("message", {}).get("content") or "")[:800]
                if not preview and "message" in resp["choices"][0]:
                    preview = str(resp["choices"][0]["message"])[:800]
            elif "content" in resp and isinstance(resp["content"], list):
                preview = "".join(c.get("text","") for c in resp["content"])[:800]
            elif "output" in resp:
                preview = str(resp["output"])[:800]
            elif "candidates" in resp:
                preview = resp["candidates"][0].get("content", {}).get("parts", [{}])[0].get("text","")[:800]
    except:
        pass

    return jsonify({
        "ok": True,
        "model": model,
        "api": api,
        "base_url": base_url,
        "profile": profile_name,
        "plan": plan,
        "parsed": parsed,
        "preview": preview,
        "response": resp,
        "db": str(DB_PATH),
        "csv": str(CSV_PATH),
    })

# Serve any other static file in web/
@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(str(WEB_DIR), filename)

def main():
    global DB_PATH, CSV_PATH, PERIOD_START_DAY
    parser = argparse.ArgumentParser(description="Opencode Go Tracker Web App")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default 127.0.0.1, use 0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    parser.add_argument("--debug", action="store_true", help="Flask debug mode")
    parser.add_argument("--db", help="SQLite DB path (default opencode_usage.db)")
    parser.add_argument("--csv", help="CSV log path (default usage_log.csv)")
    parser.add_argument("--period-start-day", type=int, default=PERIOD_START_DAY,
                        help="Day of month the paid Go period renews (default 1)")
    args = parser.parse_args()

    if args.db:
        DB_PATH = Path(args.db)
    if args.csv:
        CSV_PATH = Path(args.csv)
    PERIOD_START_DAY = max(1, min(31, args.period_start_day))

    # Ensure web dir exists
    if not WEB_DIR.exists():
        print(f"ERROR: web directory not found at {WEB_DIR}")
        sys.exit(1)
    if not (WEB_DIR / "index.html").exists():
        print(f"ERROR: web/index.html missing")
        sys.exit(1)

    print(f"Opencode Go Tracker Web App")
    print(f"  DB:   {DB_PATH} {'(exists)' if DB_PATH.exists() else '(will be created on first log)'}")
    print(f"  CSV:  {CSV_PATH}")
    print(f"  Web:  http://{args.host}:{args.port}")
    print(f"  Docs: Billing (Settings > Billing), Activity card, B-drawer, Go, Budgets, Inference API cost")
    print(f"  Press Ctrl+C to stop. Use --host 0.0.0.0 to expose to LAN.")
    # Open browser on Windows?
    try:
        import webbrowser
        # Don't auto-open if explicitly debugging?
        # webbrowser.open(f"http://{args.host}:{args.port}")
        pass
    except:
        pass

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)

if __name__ == "__main__":
    main()
