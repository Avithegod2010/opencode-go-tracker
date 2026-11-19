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
from .db import get_conn, query_totals, query_daily, recent
from .go_limits import monthly_limit, sync_limits
from .logger import OpencodeLogger, load_dotenv
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


def cmd_log(args):
    # Interactive single inference + log
    load_dotenv()
    api_key = args.api_key or os.getenv("OPENCODE_API_KEY")
    if not api_key:
        print("Missing OPENCODE_API_KEY. Set in .env or env var.")
        print("Create one in Console:")
        print("  Personal: Account > Personal API keys > Add API key (needs workspace)")
        print("  Service account: Settings > Service accounts > Add Service Account > Add API Key")
        print("  Permissions: Restricted -> Models: Request")
        print("Then: export OPENCODE_API_KEY=oc_sk_...  or put in .env")
        sys.exit(1)
    logger = OpencodeLogger(
        db_path=args.db,
        csv_path=args.csv,
        api_key=api_key,
        base_url=args.base_url or os.getenv("OPENCODE_BASE_URL") or "https://opencode.ai/inference",
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
    api_key = args.api_key or os.getenv("OPENCODE_API_KEY")
    try:
        result = sync_limits(
            api_key=api_key,
            base_url=args.base_url,
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
    print("  [S] Sync limits - refresh monthly limits from the Go model list API")
    print("  [W] Web dashboard - local website at http://localhost:8000")
    print("  [H] Help - docs links for Billing / Go / Budgets / Inference API")
    choice = input("\nSelect [A/B/R/S/W/H] or Enter to see help: ").strip().lower()
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

    # sync-limits
    ps = sub.add_parser("sync-limits", help="Fetch Go monthly limits from the model list API")
    ps.add_argument("--api-key", help="Override OPENCODE_API_KEY")
    ps.add_argument("--base-url", default="https://opencode.ai/inference/go", help="Go base URL")
    ps.add_argument("--cache", help="Cache JSON path (default go_limits_cache.json)")

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
    elif args.cmd == "sync-limits":
        cmd_sync_limits(args)
    elif args.cmd == "web":
        cmd_web(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
