#!/usr/bin/env python3
"""
Opencode Go Tracker - Local tracker CLI

Implements:
  A. CSV merger: merge Console Export usage CSVs into per-model totals
     Activity > Usage > Export usage (7/30/90 days).
  B. Logger: capture per-request usage.cost / allowance into SQLite/CSV via Inference API
  C. Report: per-model totals plus live Go 5h / weekly / monthly windows
  D. Limit sync: refresh monthly limits from the documented Go model list API

Billing context (https://opencode.ai/v2/docs/console/billing):
  Settings > Billing, Activity billing card, B-key drawer, Invoices,
  Go subscription/renewal entries, Extra Usage toggle on the Go page.

Go context (https://opencode.ai/docs/go):
  Go $10/mo, Go Plus $40/mo, Go Teams $20/member/mo
  Per-model monthly $ limits (Go, Go Plus, Go Teams).
  Windows: 5h = 20% (starts on first use), weekly = 50% (Monday 00:00 UTC),
  monthly = 100% (resets on the paid period anniversary).

Quick start:
  python tracker.py                  # interactive menu
  python tracker.py csv-merge "exports/*.csv" -o totals.csv --plan go
  python tracker.py log --model kimi-k3 --message "Say hi"
  python tracker.py report --plan go
  python tracker.py sync-limits
  python tracker.py web --port 8000  # or python app.py

Env: copy .env.example to .env with OPENCODE_API_KEY, OPENCODE_BASE_URL etc.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .csv_merger import merge_csvs
from .db import (
    add_profile,
    current_plan,
    get_conn,
    get_profile,
    list_profiles,
    plan_history,
    query_totals,
    query_daily,
    recent,
    remove_profile,
    set_plan,
    update_profile_check,
)
from .go_limits import monthly_limit, sync_limits
from .importer import find_opencode_db, import_sessions
from .logger import OpencodeLogger, load_dotenv
from .preflight import check_key
from .profiles import resolve_profile
from .windows import compute_windows, period_start, week_start


def project_root() -> Path:
    """Locate the repository checkout (for app.py / web assets)."""
    here = Path(__file__).resolve()
    candidates = []
    if len(here.parents) > 2:
        candidates.append(here.parents[2])  # src/opencode_tracker/cli.py -> repo root
    candidates.append(Path.cwd())
    for candidate in candidates:
        if (candidate / "app.py").is_file() and (candidate / "web" / "index.html").is_file():
            return candidate
    return candidates[0] if candidates else Path.cwd()


def _period_day(value: int | None) -> int:
    if value is None:
        value = int(os.getenv("OPENCODE_PERIOD_START_DAY", "1") or 1)
    return max(1, min(31, int(value)))


def cmd_csv_merge(args):
    patterns = []
    if args.glob:
        patterns.append(args.glob)
    if args.files:
        patterns.extend(args.files)
    if not patterns:
        print("Provide CSVs: tracker.py csv-merge exports/*.csv  OR  --glob 'exports/*.csv'")
        sys.exit(2)
    merge_csvs(
        patterns,
        output=Path(args.output) if args.output else None,
        billable_only=args.billable_only,
        dedupe=not args.no_dedupe,
        plan=args.plan,
        verbose=True,
    )


def cmd_init_db(args):
    conn = get_conn(args.db)
    print(f"Initialized SQLite DB at {args.db} (WAL mode). Tables: requests")
    conn.close()


def cmd_report(args):
    db = Path(args.db)
    if not db.exists():
        print(f"DB not found: {db}. Run: python tracker.py init-db --db {db}  or first log a request.")
        sys.exit(1)
    conn = get_conn(db)
    print(f"Report from {db}\n" + "=" * 78)
    rows = query_totals(conn)
    if not rows:
        print("No logged requests yet. Use: python tracker.py log --model <model> --message 'hi'")
        conn.close()
        return
    # Header
    print(f"{'Model':<28} {'Src':<10} {'N':>4} {'Cost $':>10} {'Allowance $':>12} {'List $':>10}")
    print("-" * 78)
    total_cost = 0
    total_allow = 0
    for r in rows:
        total_cost += r["sum_cost"] or 0
        total_allow += r["sum_allowance"] or 0
        print(f"{r['model']:<28} {(r['billing_source'] or '-'):<10} {r['n']:4d} {(r['sum_cost'] or 0):10.4f} {(r['sum_allowance'] or 0):12.4f} {(r['sum_list'] or 0):10.4f}")
    print("-" * 78)
    print(f"TOTAL cost=${total_cost:.4f} allowance=${total_allow:.4f} (allowance>0 means Go plan consumption)")

    # Daily spark
    print("\nDaily cost:")
    for d in query_daily(conn):
        print(f"  {d['day']}  ${d['cost']:.4f}  allowance=${d['allowance'] or 0:.4f}  n={d['n']}")

    # Live Go windows (5h / week / month)
    if args.plan:
        day = _period_day(args.period_start_day)
        ws = compute_windows(conn, args.plan, period_start_day=day)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        print(f"\nGo windows vs {args.plan} (5h=20%, week=50%, month=100%)")
        print(f"  period start {period_start(now, day).isoformat()} | week start {week_start(now).isoformat()} | as of {now.isoformat()}")
        if not ws:
            print("  no Go allowance logged yet")
        else:
            print(f"  {'Model':<28} {'5h used/limit':>22} {'Week used/limit':>24} {'Month used/limit':>26} {'Status':>9}")
            for w in ws:
                def cell(used, limit, pct):
                    if limit is None:
                        return "unlimited"
                    return f"${used:.2f}/${limit:.0f} ({pct:.1f}%)"
                print(
                    f"  {w['model']:<28} "
                    f"{cell(w['five_hour_used'], w['five_hour_limit'], w['five_hour_pct'] or 0):>22} "
                    f"{cell(w['week_used'], w['week_limit'], w['week_pct'] or 0):>24} "
                    f"{cell(w['month_used'], w['month_limit'], w['month_pct'] or 0):>26} "
                    f"{w['status']:>9}"
                )

    # Recent
    print("\nRecent 10 requests:")
    for r in recent(conn, 10):
        print(f"  [{r['timestamp'][:19]}] {r['model']} {r['billing_source']} cost={r['cost']} allowance={r['allowance']} tokens={r['input_tokens']}/{r['output_tokens']} cache_read={r['cache_read']} status={r['status']}")
    conn.close()

    # Also show CSV if exists
    if args.csv and Path(args.csv).exists():
        print(f"\nCSV log at {args.csv} ({Path(args.csv).stat().st_size} bytes)")


def cmd_import_sessions(args):
    load_dotenv()
    source = find_opencode_db(args.opencode_db)
    if source is None:
        print("OpenCode session database not found.")
        print("Pass --opencode-db PATH or set OPENCODE_CLIENT_DB.")
        sys.exit(1)
    conn = get_conn(args.db)
    try:
        stats = import_sessions(
            conn,
            source_db=source,
            since=args.since,
            limit_sessions=args.limit,
            dry_run=args.dry_run,
            full_paths=args.full_paths,
            include_titles=not args.no_titles,
            profile=args.profile,
            member=args.member,
            plan=args.plan,
        )
    except Exception as e:
        print(f"Import failed: {e}")
        sys.exit(1)
    finally:
        conn.close()

    print(f"Source: {stats['source_db']}")
    print(f"Sessions: {stats['sessions_imported']} imported of {stats['sessions_seen']} seen")
    print(f"Messages: {stats['messages_imported']} imported, {stats['duplicates_skipped']} duplicates skipped")
    print(f"Session adjustments: {stats['adjustment_rows']}")
    print(f"Source totals: cost ${stats['total_cost']:.4f} | tokens {stats['total_tokens']}")
    if stats["dry_run"]:
        print("Dry run: nothing written.")
    else:
        print(f"Wrote to {args.db}")


def cmd_profile(args):
    conn = get_conn(args.db)
    try:
        action = args.profile_cmd or "list"
        if action == "add":
            add_profile(
                conn,
                args.name,
                args.base_url,
                plan=args.plan,
                key_env=args.key_env,
                project=args.project,
                member=args.member,
            )
            print(f"Profile '{args.name}' saved.")
            print(f"  base_url: {args.base_url}")
            print(f"  plan: {args.plan or '-'}  key_env: {args.key_env or '-'}")
            print("Keys are never stored; put the key in that environment variable or use OPENCODE_API_KEY.")
        elif action == "list":
            rows = list_profiles(conn)
            if not rows:
                print("No profiles. Add one: opencode-tracker profile add work --base-url https://opencode.ai/inference/go --plan go --key-env OPENCODE_KEY_WORK")
            else:
                print(f"{'Name':<16} {'Plan':<10} {'Key env':<24} {'Last':<8} Base URL")
                print("-" * 100)
                for row in rows:
                    print(f"{row['name']:<16} {(row['plan'] or '-'):<10} {(row['key_env'] or '-'):<24} {(row['last_status'] or '-'):<8} {row['base_url']}")
        elif action == "remove":
            if remove_profile(conn, args.name):
                print(f"Removed profile '{args.name}'.")
            else:
                print(f"No profile named '{args.name}'.")
                sys.exit(1)
        elif action == "check":
            try:
                profile = resolve_profile(conn, args.name)
            except KeyError as e:
                print(str(e))
                sys.exit(1)
            if not profile["key"]:
                print(f"No key found for '{args.name}'. Set ${args.key_env or 'OPENCODE_PROFILE_' + args.name.upper() + '_KEY'}.")
                sys.exit(1)
            base_url = args.base_url or profile["base_url"]
            result = check_key(profile["key"], base_url)
            update_profile_check(conn, args.name, "ok" if result["ok"] else (str(result["status_code"]) if result["status_code"] else "error"))
            print(f"{args.name}: HTTP {result['status_code']}  models={result['model_count']}  Go models={result['go_model_count']}")
            print(f"  key: {result['key_prefix']}  base: {result['base_url']}")
            if not result["ok"]:
                print(f"  {result['error']}")
                if result["hint"]:
                    print(f"  {result['hint']}")
                sys.exit(1)
    finally:
        conn.close()


def cmd_log(args):
    # Interactive single inference + log
    load_dotenv()
    profile_name = args.profile or os.getenv("OPENCODE_PROFILE")
    profile = None
    if profile_name:
        conn = get_conn(args.db)
        try:
            profile = resolve_profile(conn, profile_name)
        except KeyError as e:
            print(str(e))
            sys.exit(1)
        finally:
            conn.close()

    api_key = args.api_key or (profile or {}).get("key") or os.getenv("OPENCODE_API_KEY")
    if not api_key:
        print("Missing OPENCODE_API_KEY. Set in .env or env var.")
        print("Create one in Console:")
        print("  Personal: Account > Personal API keys > Add API key (needs workspace)")
        print("  Service account: Settings > Service accounts > Add Service Account > Add API Key")
        print("  Permissions: Restricted -> Models: Request")
        print("Then: export OPENCODE_API_KEY=oc_sk_...  or put in .env")
        sys.exit(1)
    base_url = (
        args.base_url
        or (profile or {}).get("base_url")
        or os.getenv("OPENCODE_BASE_URL")
        or "https://opencode.ai/inference"
    )
    plan = args.plan or (profile or {}).get("plan") or os.getenv("OPENCODE_PLAN")
    member = (profile or {}).get("member") or os.getenv("OPENCODE_MEMBER")
    project = (profile or {}).get("project") or os.getenv("OPENCODE_PROJECT")
    logger = OpencodeLogger(
        db_path=args.db,
        csv_path=args.csv,
        api_key=api_key,
        base_url=base_url,
        profile=(profile or {}).get("name"),
        plan=plan,
        member=member,
        project=project,
    )
    # Dispatch
    try:
        if args.api == "anthropic":
            res = logger.anthropic_messages(model=args.model, messages=[{"role": "user", "content": args.message}], max_tokens=args.max_tokens or 1024)
        elif args.api == "responses":
            res = logger.responses(model=args.model, input_text=args.message)
        elif args.api == "gemini":
            res = logger.gemini_generate(model=args.model, contents=[{"parts": [{"text": args.message}]}])
        else:
            msgs = []
            if args.system:
                msgs.append({"role": "system", "content": args.system})
            msgs.append({"role": "user", "content": args.message})
            res = logger.chat_completions(model=args.model, messages=msgs, max_tokens=args.max_tokens, stream=args.stream)
        # Print assistant text snippet
        try:
            if "choices" in res:
                txt = res["choices"][0].get("message", {}).get("content", "")[:500]
                print(f"\nAssistant: {txt}")
            elif "content" in res:
                # anthropic
                txt = "".join(c.get("text", "") for c in res.get("content", []))[:500]
                print(f"\nAssistant: {txt}")
            elif "output" in res:
                print(f"\nResponse output: {str(res['output'])[:500]}")
            elif "candidates" in res:
                txt = res["candidates"][0].get("content", {}).get("parts", [{}])[0].get("text", "")[:500]
                print(f"\nAssistant: {txt}")
        except Exception:
            pass
        print(f"\nFull response logged to {args.db} + {args.csv}")
        if args.json_out:
            import json
            Path(args.json_out).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Saved JSON to {args.json_out}")
    except Exception as e:
        print(f"Request failed: {e}")
        sys.exit(1)


def cmd_sync_limits(args):
    load_dotenv()
    profile = None
    if getattr(args, "profile", None):
        conn = get_conn(getattr(args, "db", "opencode_usage.db"))
        try:
            profile = resolve_profile(conn, args.profile)
        except KeyError as e:
            print(str(e))
            sys.exit(1)
        finally:
            conn.close()
    api_key = args.api_key or (profile or {}).get("key") or os.getenv("OPENCODE_API_KEY")
    base_url = args.base_url or (profile or {}).get("base_url") or "https://opencode.ai/inference/go"
    try:
        result = sync_limits(
            api_key=api_key,
            base_url=base_url,
            cache_path=args.cache,
        )
    except Exception as e:
        print(f"Limit sync failed: {e}")
        sys.exit(1)
    print(f"Synced {len(result['limits'])} model limits from {result['source']}")
    print(f"Fetched at {result['fetched_at']}")
    cache = args.cache or os.getenv("OPENCODE_LIMITS_CACHE", "go_limits_cache.json")
    print(f"Cache: {cache}")
    for model, entry in sorted(result["limits"].items()):
        print(f"  {model:<30} go ${entry['go']}  plus ${entry['go_plus']}  teams ${entry['go_teams']}")


def cmd_key_check(args):
    load_dotenv()
    profile = None
    if getattr(args, "profile", None):
        conn = get_conn(getattr(args, "db", "opencode_usage.db"))
        try:
            profile = resolve_profile(conn, args.profile)
        except KeyError as e:
            print(str(e))
            sys.exit(1)
        finally:
            conn.close()
    api_key = args.api_key or (profile or {}).get("key") or os.getenv("OPENCODE_API_KEY")
    if not api_key:
        print("Missing OPENCODE_API_KEY. Put it in .env or pass --api-key.")
        print("Create one at Console: Account > Personal API keys (needs Models > Request).")
        sys.exit(1)
    base_url = args.base_url or (profile or {}).get("base_url") or os.getenv("OPENCODE_BASE_URL") or "https://opencode.ai/inference/go"
    if base_url == "go":
        base_url = "https://opencode.ai/inference/go"
    if base_url in ("balance", "inference"):
        base_url = "https://opencode.ai/inference"

    result = check_key(api_key, base_url)
    print(f"Key: {result['key_prefix']}  Base URL: {result['base_url']}")
    print(
        f"HTTP {result['status_code']}  models={result['model_count']}  "
        f"Go models={result['go_model_count']}"
    )
    if result["ok"]:
        print("Key OK. Models: Read/Request works.")
        if result["go_models"]:
            print("Go sample: " + ", ".join(result["go_models"][:8]))
        if result["plans_seen"]:
            print("Plans seen: " + ", ".join(result["plans_seen"]))
    else:
        print(f"Key check failed: {result['error']}")
        if result["hint"]:
            print(f"Hint: {result['hint']}")
        sys.exit(1)


def cmd_web(args):
    # launch Flask dashboard (same as app.py)
    import subprocess
    app_path = project_root() / "app.py"
    if not app_path.is_file():
        print(f"app.py not found next to this checkout (looked in {app_path}).")
        print("The web dashboard needs the repository checkout; the CLI still works when installed.")
        sys.exit(1)
    cmd = [sys.executable, str(app_path)]
    if getattr(args, "host", None):
        cmd += ["--host", args.host]
    if getattr(args, "port", None):
        cmd += ["--port", str(args.port)]
    if getattr(args, "db", None):
        cmd += ["--db", args.db]
    if getattr(args, "period_start_day", None):
        cmd += ["--period-start-day", str(args.period_start_day)]
    if getattr(args, "debug", False):
        cmd += ["--debug"]
    print(f"Launching web app: {' '.join(cmd)}")
    subprocess.run(cmd)


def interactive():
    print(__doc__)
    print("\nChoose:")
    print("  [A] CSV merger - merge Console Export usage CSVs into per-model totals")
    print("      Export in Console: Activity > Usage > Export usage (7 / 30 / 90 days)")
    print("      Then: python tracker.py csv-merge \"exports/*.csv\" -o totals.csv")
    print("  [B] Logger - capture per-request cost/allowance via Inference API -> SQLite/CSV")
    print("      Needs: OPENCODE_API_KEY with Models: Request (Personal or Service account key)")
    print("  [R] Report - DB totals plus live 5h / weekly / monthly Go windows")
    print("  [K] Key check - verify a key against the model list endpoint")
    print("  [S] Sync limits - refresh monthly limits from the Go model list API")
    print("  [W] Web dashboard - local website at http://localhost:8000")
    print("  [H] Help - docs links for Billing / Go / Budgets / Inference API")
    choice = input("\nSelect [A/B/R/K/S/W/H] or Enter to see help: ").strip().lower()
    if choice == "a":
        print("\nExample A:")
        print('  python tracker.py csv-merge "exports/*.csv" -o totals.csv --plan go')
        print('  python tracker.py csv-merge exports/usage_7d.csv exports/usage_30d.csv --billable-only')
        print("\nWhat counts (Budgets#what-counts): Paid=yes, Free=no, Go within plan=no, Go extra usage=yes")
        print("CSV merger dedupes overlapping days and shows billable vs total; --billable-only filters like budgets.")
    elif choice == "b":
        print("\nExample B:")
        print('  copy .env.example to .env and set OPENCODE_API_KEY=oc_sk_...')
        print('  # Go base URL: https://opencode.ai/inference/go')
        print('  # Paid balance: https://opencode.ai/inference')
        print('  python tracker.py log --model kimi-k3 --message "Hello" --db opencode_usage.db --csv usage_log.csv')
        print('  python tracker.py log --model gpt-6-luna --api responses --message "Hi"')
        print('  python tracker.py report --db opencode_usage.db --plan go')
        print("\nEvery response logs: usage.cost, opencode.billing_source, list_cost, allowance, tokens{...}")
        print("Billing_source: free | balance | go | go-plus | byok")
        print("Cost=0 for free & Go within plan; allowance>0 counts toward Go monthly limit.")
    elif choice == "r":
        print("\nReport: python tracker.py report --db opencode_usage.db --plan go_plus")
        print("Billing period renews on the 1st by default; use --period-start-day N for your anniversary.")
    elif choice == "k":
        print("\nKey check: python tracker.py key-check")
        print("Calls /v1/models and reports model count + Go catalogue access.")
    elif choice == "s":
        print("\nSync limits: python tracker.py sync-limits")
        print("Fetches https://opencode.ai/inference/go/v1/models and caches go.monthly_usage.")

    elif choice == "w":
        print("\nWeb dashboard - local website (no data leaves device except Inference API calls):")
        print("  pip install -r requirements.txt  # Flask + requests")
        print("  python app.py --port 8000        # or python tracker.py web --port 8000")
        print("  Open http://localhost:8000  -> tabs: A. CSV Merger, B. Logger, Report")
    elif choice == "h":
        print("\nDocs:")
        print("  Billing:  https://opencode.ai/v2/docs/console/billing")
        print("  Go:       https://opencode.ai/docs/go")
        print("  Budgets:  https://opencode.ai/v2/docs/console/budgets/")
        print("  Inference API Cost: https://opencode.ai/v2/docs/console/api/inference/")
        print("  API Keys: https://opencode.ai/v2/docs/console/api/")
        print("  Models:   https://opencode.ai/v2/docs/console/api/models")
        print("\nWeb app: python app.py  -> http://localhost:8000")
    else:
        print("\nRun with --help for full CLI.")


def build_parser():
    p = argparse.ArgumentParser(description="Opencode Go Tracker - CSV merger + Inference logger")
    sub = p.add_subparsers(dest="cmd")

    # csv-merge
    pc = sub.add_parser("csv-merge", help="A. Merge Export usage CSVs into per-model totals")
    pc.add_argument("files", nargs="*", help="CSV files (also accepts globs expanded by shell)")
    pc.add_argument("--glob", help="Glob pattern e.g. 'exports/*.csv' (alternative to files)")
    pc.add_argument("-o", "--output", help="Output totals CSV path (e.g. totals.csv)")
    pc.add_argument("--billable-only", action="store_true", help="Only count budgets What counts (Paid & Go extra usage; exclude free/go-within-plan)")
    pc.add_argument("--no-dedupe", action="store_true", help="Disable dedupe of overlapping 7/30/90d exports")
    pc.add_argument("--plan", choices=["go", "go_plus", "go-plus", "go_teams", "go-teams"], help="Show %% of Go monthly limit per model")

    # log
    pl = sub.add_parser("log", help="B. Capture one inference request (cost/allowance -> SQLite/CSV)")
    pl.add_argument("--model", default="kimi-k3", help="Model ID (e.g. kimi-k3, gpt-6-luna, claude-haiku-5-5, gemini-4-argon, glm-5.3-flash)")
    pl.add_argument("--message", default="Say hi in one short sentence.", help="User message")
    pl.add_argument("--system", help="System prompt (OpenAI chat only)")
    pl.add_argument("--api", choices=["openai", "anthropic", "responses", "gemini"], default="openai", help="API family (default openai chat)")
    pl.add_argument("--max-tokens", type=int, help="max_tokens / max_output_tokens")
    pl.add_argument("--stream", action="store_true", help="Stream and capture final usage (needs stream_options.include_usage)")
    pl.add_argument("--api-key", help="Override OPENCODE_API_KEY")
    pl.add_argument("--base-url", help="Override OPENCODE_BASE_URL (default https://opencode.ai/inference; Go is https://opencode.ai/inference/go)")
    pl.add_argument("--profile", help="Named profile (base URL, plan, key env)")
    pl.add_argument("--plan", choices=["go", "go_plus", "go-plus", "go_teams", "go-teams"], help="Plan label stored with the request")
    pl.add_argument("--db", default="opencode_usage.db", help="SQLite DB path")
    pl.add_argument("--csv", default="usage_log.csv", help="CSV log path (also written)")
    pl.add_argument("--json-out", help="Save full response JSON to file")

    # report
    pr = sub.add_parser("report", help="Show DB totals and live Go windows")
    pr.add_argument("--db", default="opencode_usage.db", help="SQLite DB path")
    pr.add_argument("--csv", default="usage_log.csv", help="CSV path (info only)")
    pr.add_argument("--plan", choices=["go", "go_plus", "go-plus", "go_teams", "go-teams"], help="Show Go window %%")
    pr.add_argument("--period-start-day", type=int, help="Day of month the paid period renews (default 1 or OPENCODE_PERIOD_START_DAY)")

    # init-db
    pi = sub.add_parser("init-db", help="Create SQLite DB")
    pi.add_argument("--db", default="opencode_usage.db", help="SQLite DB path")

    # import-sessions
    pim = sub.add_parser("import-sessions", help="Import usage metadata from OpenCode's local session DB")
    pim.add_argument("--opencode-db", help="Path to OpenCode's opencode.db (auto-detected if omitted)")
    pim.add_argument("--db", default="opencode_usage.db", help="Tracker SQLite DB")
    pim.add_argument("--since", help="Only sessions updated on/after this date/ISO timestamp")
    pim.add_argument("--limit", type=int, help="Import at most N recent sessions")
    pim.add_argument("--dry-run", action="store_true", help="Count without writing")
    pim.add_argument("--full-paths", action="store_true", help="Store full project paths instead of folder names")
    pim.add_argument("--no-titles", action="store_true", help="Do not store session titles")
    pim.add_argument("--profile", help="Profile label for imported rows")
    pim.add_argument("--member", help="Member label for imported rows")
    pim.add_argument("--plan", choices=["go", "go_plus", "go-plus", "go_teams", "go-teams"], help="Plan label for imported rows")

    # sync-limits
    ps = sub.add_parser("sync-limits", help="Fetch Go monthly limits from the model list API")
    ps.add_argument("--api-key", help="Override OPENCODE_API_KEY")
    ps.add_argument("--base-url", default="https://opencode.ai/inference/go", help="Go base URL")
    ps.add_argument("--cache", help="Cache JSON path (default go_limits_cache.json)")
    ps.add_argument("--profile", help="Named profile")
    ps.add_argument("--db", default="opencode_usage.db", help="SQLite DB path for profile lookup")

    # key-check
    pk = sub.add_parser("key-check", help="Verify a key against the model list endpoint")
    pk.add_argument("--api-key", help="Override OPENCODE_API_KEY")
    pk.add_argument("--base-url", help="Base URL (default OPENCODE_BASE_URL or Go)")
    pk.add_argument("--profile", help="Named profile")
    pk.add_argument("--db", default="opencode_usage.db", help="SQLite DB path for profile lookup")

    # profile
    pp = sub.add_parser("profile", help="Manage named API key profiles (keys stay in env)")
    psub = pp.add_subparsers(dest="profile_cmd")
    pa = psub.add_parser("add", help="Create or update a profile")
    pa.add_argument("name")
    pa.add_argument("--base-url", default="https://opencode.ai/inference/go")
    pa.add_argument("--plan", choices=["go", "go_plus", "go-plus", "go_teams", "go-teams"])
    pa.add_argument("--key-env", help="Environment variable holding the key")
    pa.add_argument("--project", help="Default project label")
    pa.add_argument("--member", help="Default member label")
    pa.add_argument("--db", default="opencode_usage.db")
    psub.add_parser("list", help="List profiles").add_argument("--db", default="opencode_usage.db")
    prm = psub.add_parser("remove", help="Remove a profile")
    prm.add_argument("name")
    prm.add_argument("--db", default="opencode_usage.db")
    pc = psub.add_parser("check", help="Check a profile's key")
    pc.add_argument("name")
    pc.add_argument("--base-url", help="Override the profile base URL")
    pc.add_argument("--db", default="opencode_usage.db")

    # web
    pw = sub.add_parser("web", help="Launch local website dashboard (same as python app.py)")
    pw.add_argument("--host", default="127.0.0.1", help="Host (default 127.0.0.1, use 0.0.0.0 for LAN)")
    pw.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    pw.add_argument("--db", help="SQLite DB path")
    pw.add_argument("--period-start-day", type=int, help="Day of month the paid period renews")
    pw.add_argument("--debug", action="store_true", help="Flask debug")

    return p


def main():
    parser = build_parser()
    if len(sys.argv) == 1:
        interactive()
        return
    args = parser.parse_args()
    if args.cmd == "csv-merge":
        cmd_csv_merge(args)
    elif args.cmd == "log":
        cmd_log(args)
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "init-db":
        cmd_init_db(args)
    elif args.cmd == "import-sessions":
        cmd_import_sessions(args)
    elif args.cmd == "sync-limits":
        cmd_sync_limits(args)
    elif args.cmd == "key-check":
        cmd_key_check(args)
    elif args.cmd == "profile":
        cmd_profile(args)
    elif args.cmd == "web":
        cmd_web(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
