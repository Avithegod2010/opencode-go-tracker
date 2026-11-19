"""
A. CSV Merger: merge Console Export usage CSVs into per-model totals

Docs context:
- Activity > Usage breaks down cost by day, model, person, Export usage CSV for last 7/30/90 days
- What counts toward budgets (Budgets page):
    Paid=yes, Free=no, Go within plan=no, Go extra usage=yes
    -> budgets What counts table: Paid Console models Yes, Free No, Go within plan No, Go extra usage Yes
- Inference Cost: usage.cost, billing_source (free|balance|go|go-plus|byok), list_cost, allowance, tokens{}

This merger:
- Globs multiple CSVs (e.g. 7d + 30d + 90d), dedupes overlapping days, sums per model.
- Auto-detects columns (case-insensitive, normalized) for date/model/person/cost/billing_source.
- Reports both billable (balance + go extra usage + byok estimated) and total (including free/go).
- Can filter --billable-only to mirror budget spend.
- Computes Go plan consumption % if you pass --plan go|go_plus|go_teams.
- Outputs CSV + pretty console table.

Expected CSV headers (Console Export) are typically daily totals:
  date, model, member/person/email, cost, (optional) tokens, billing_source, requests
But we handle any header variant gracefully.
"""
from __future__ import annotations
import csv
import glob
import re
from collections import defaultdict, Counter
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from .go_limits import normalize_model, monthly_limit

# Column alias sets (normalized lowercase, alphanumeric only)
COL_ALIASES = {
    "date": {"date", "day", "timestamp", "time", "period", "start", "created_at"},
    "model": {"model", "modelid", "modelname", "model_id", "model_name"},
    "person": {"person", "member", "user", "email", "account", "owner", "serviceaccount", "service_account"},
    "cost": {"cost", "amount", "dollars", "usd", "price", "total", "charged", "spend"},
    "list_cost": {"listcost", "list_cost", "totalcost", "retail", "listprice"},
    "billing_source": {"billingsource", "billing_source", "source", "billing", "billedvia"},
    "allowance": {"allowance", "goallowance", "go_allowance"},
    "requests": {"requests", "count", "calls", "numrequests"},
    "input_tokens": {"input", "inputtokens", "prompttokens", "prompt_tokens", "input_tokens"},
    "output_tokens": {"output", "outputtokens", "completiontokens", "completion_tokens", "output_tokens"},
}

# For budget counting: which billing_source counts
BUDGET_COUNTS = {
    "balance": True,   # Paid via balance
    "byok": True,      # Your own provider key at configured price (if price set)
    "free": False,
    "go": False,       # within plan
    "go-plus": False,
    "go_plus": False,
    # go extra usage is still balance - it shows as balance after limit, so counted
}

def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.strip().lower())

def detect_columns(headers: List[str]) -> Dict[str, Optional[str]]:
    """Map logical column -> actual header name (or None)"""
    norm_map = {_norm_header(h): h for h in headers}
    # also map lower stripped
    out: Dict[str, Optional[str]] = {}
    for logical, aliases in COL_ALIASES.items():
        found = None
        for alias in aliases:
            na = _norm_header(alias)
            if na in norm_map:
                found = norm_map[na]
                break
        out[logical] = found
    return out

def _parse_float(v) -> float:
    if v is None or v == "":
        return 0.0
    # strip $, commas
    s = str(v).strip().replace("$", "").replace(",", "")
    # handle parentheses negative
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        # maybe empty or dash
        return 0.0

def _normalize_billing_source(v: Optional[str]) -> str:
    if not v:
        return "unknown"
    s = str(v).strip().lower().replace(" ", "_").replace("-", "_")
    # map go_plus variants
    if s in ("go_plus", "goplus", "go_plus_subscription"):
        return "go-plus"
    if s == "go":
        return "go"
    if s in ("balance", "paid", "credits"):
        return "balance"
    if s == "free":
        return "free"
    if s in ("byok", "custom", "provider"):
        return "byok"
    return s

def merge_csvs(
    patterns: List[str],
    output: Optional[Path] = None,
    billable_only: bool = False,
    dedupe: bool = True,
    plan: Optional[str] = None,
    verbose: bool = True,
) -> Dict:
    """
    patterns: glob patterns like ["exports/*.csv", "usage_7d.csv"]
    Returns dict with totals and writes output CSV if requested.
    """
    files: List[Path] = []
    for pat in patterns:
        # support direct file path without glob chars
        if Path(pat).is_file():
            files.append(Path(pat))
        else:
            for m in glob.glob(pat, recursive=False):
                p = Path(m)
                if p.is_file():
                    files.append(p)
            # also try expanded glob with ** if needed
            if not files and ("*" not in pat and "?" not in pat):
                # treat as literal? already handled
                pass

    if not files:
        raise FileNotFoundError(f"No CSVs matched patterns: {patterns}")

    if verbose:
        print(f"Found {len(files)} CSV(s):")
        for f in files:
            print(f"  - {f} ({f.stat().st_size} bytes)")

    # Collect rows
    all_rows: List[Dict] = []
    column_warnings = Counter()
    per_file_rows = {}
    detected_cols_per_file = {}

    for fp in files:
        try:
            with open(fp, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames is None:
                    print(f"WARN: {fp} has no header row, skipping")
                    continue
                headers = [h for h in reader.fieldnames if h is not None]
                colmap = detect_columns(headers)
                detected_cols_per_file[str(fp)] = colmap

                # require at least model or cost
                if colmap["model"] is None:
                    column_warnings["missing_model"] += 1
                    # try fallback: first column maybe model?
                if colmap["cost"] is None:
                    # try to find any numeric column that could be cost
                    # search header containing cost/amount/usd
                    # already attempted, so warn
                    column_warnings["missing_cost"] += 1
                    print(f"WARN: {fp} headers={headers} -> colmap={colmap} (no cost col detected)")
                    # skip? but we can still try to parse; assume no cost = 0
                if colmap["date"] is None:
                    column_warnings["missing_date"] += 1

                rows = []
                for i, raw in enumerate(reader, start=2):
                    # raw keys may be None for extra columns
                    model_raw = raw.get(colmap["model"]) if colmap["model"] else None
                    # if no model col, try to infer from first value
                    if not model_raw and colmap["model"] is None:
                        # pick first non-empty value that looks like model id
                        for k, v in raw.items():
                            if v and isinstance(v, str) and re.search(r"[a-z]+-[a-z0-9]", v.lower()):
                                model_raw = v
                                break
                    cost_raw = raw.get(colmap["cost"]) if colmap["cost"] else None
                    # fallback: try to find cost-like value as float in any column
                    if cost_raw is None and colmap["cost"] is None:
                        for k, v in raw.items():
                            try:
                                fv = _parse_float(v)
                                if fv != 0 and k and "token" not in k.lower():
                                    cost_raw = v
                                    break
                            except: pass

                    date_raw = raw.get(colmap["date"]) if colmap["date"] else ""
                    person_raw = raw.get(colmap["person"]) if colmap["person"] else ""
                    billing_raw = raw.get(colmap["billing_source"]) if colmap["billing_source"] else ""
                    allowance_raw = raw.get(colmap["allowance"]) if colmap["allowance"] else ""
                    list_cost_raw = raw.get(colmap["list_cost"]) if colmap["list_cost"] else ""

                    # if cost missing but list_cost present use it
                    if (cost_raw is None or str(cost_raw).strip() == "") and list_cost_raw:
                        cost_raw = list_cost_raw

                    cost = _parse_float(cost_raw)
                    list_cost = _parse_float(list_cost_raw) if list_cost_raw else cost
                    allowance = _parse_float(allowance_raw)
                    billing_source = _normalize_billing_source(billing_raw)

                    # Determine billable: per Budgets What counts table
                    # Free -> No, Go within plan -> No, Paid (balance) Yes, Go extra usage Yes (which appears as balance)
                    # byok -> Yes if price set (we count it as billable)
                    is_billable = BUDGET_COUNTS.get(billing_source, True) if billing_source != "unknown" else True
                    # If billing_source unknown, we can't filter; treat as billable unless billable_only and we want conservative?
                    # For unknown, keep as billable.

                    # Normalize model
                    model = normalize_model(str(model_raw).strip()) if model_raw else "unknown"
                    if model == "unknown" and colmap["model"] is None:
                        model = f"unknown:{fp.stem}"

                    rows.append({
                        "date": str(date_raw).strip() if date_raw else "",
                        "model": model,
                        "model_raw": str(model_raw).strip() if model_raw else "",
                        "person": str(person_raw).strip() if person_raw else "",
                        "cost": cost,
                        "list_cost": list_cost,
                        "billing_source": billing_source,
                        "allowance": allowance,
                        "is_billable": is_billable,
                        "source_file": str(fp),
                        "raw": raw,
                    })
                per_file_rows[str(fp)] = len(rows)
                all_rows.extend(rows)
                if verbose:
                    print(f"  {fp}: {len(rows)} rows | columns {headers} -> detected {colmap}")
        except Exception as e:
            print(f"ERROR reading {fp}: {e}")
            raise

    if not all_rows:
        raise ValueError("No rows parsed from CSVs")

    # Dedupe: key by the daily aggregate. Overlapping 7/30/90 exports repeat
    # the same day; drop only repeats that come from a *different* file, so
    # legitimately distinct rows inside one export survive.
    before = len(all_rows)
    deduped = all_rows
    dupes_removed = 0
    if dedupe:
        seen: Dict[tuple, set] = {}
        uniq = []
        for r in all_rows:
            key = (
                r["date"], r["model"], r["person"], r["billing_source"],
                f"{r['cost']:.6f}", f"{r['allowance']:.6f}",
            )
            files_for_key = seen.get(key)
            if files_for_key is None:
                seen[key] = {r["source_file"]}
                uniq.append(r)
            elif r["source_file"] in files_for_key:
                # Same file, same aggregate: this is a distinct row, keep it.
                uniq.append(r)
            else:
                files_for_key.add(r["source_file"])
                dupes_removed += 1
        deduped = uniq
        if verbose:
            print(f"Deduped: {before} -> {len(deduped)} rows (removed {dupes_removed} overlapping daily totals)")

    # Optionally filter billable only
    filtered = deduped
    if billable_only:
        filtered = [r for r in deduped if r["is_billable"]]
        if verbose:
            print(f"Billable-only filter: {len(deduped)} -> {len(filtered)} rows (free/go-within-plan excluded)")

    # Aggregate per model
    per_model = defaultdict(lambda: {"cost": 0.0, "list_cost": 0.0, "allowance": 0.0, "requests": 0, "billable_cost": 0.0, "rows": 0})
    per_day = defaultdict(lambda: {"cost": 0.0, "billable_cost": 0.0})
    per_person = defaultdict(lambda: {"cost": 0.0, "rows": 0})
    per_billing = Counter()

    for r in filtered:
        m = r["model"]
        per_model[m]["cost"] += r["cost"]
        per_model[m]["list_cost"] += r["list_cost"]
        per_model[m]["allowance"] += r["allowance"]
        per_model[m]["rows"] += 1
        if r["is_billable"]:
            per_model[m]["billable_cost"] += r["cost"]
        per_day[r["date"]]["cost"] += r["cost"]
        if r["is_billable"]:
            per_day[r["date"]]["billable_cost"] += r["cost"]
        if r["person"]:
            per_person[r["person"]]["cost"] += r["cost"]
            per_person[r["person"]]["rows"] += 1
        per_billing[r["billing_source"]] += r["cost"]

    # Totals shown to the user always describe the rows they are looking at.
    # The unfiltered figures are returned separately for context.
    total_cost_all = sum(r["cost"] for r in deduped)
    total_billable_all = sum(r["cost"] for r in deduped if r["is_billable"])
    total_allowance_all = sum(r["allowance"] for r in deduped)
    total_cost = sum(r["cost"] for r in filtered)
    total_billable = sum(r["cost"] for r in filtered if r["is_billable"])
    total_allowance = sum(r["allowance"] for r in filtered)

    # Prepare per-model sorted list
    model_rows = []
    for model, agg in per_model.items():
        ml = monthly_limit(model, plan) if plan else None
        # allowance may be present per row (Go); sum it; else cost is proxy?
        used_allowance = agg["allowance"] if agg["allowance"] != 0 else agg["cost"]
        # but for non-Go billing_source, allowance is 0
        pct_month = (used_allowance / ml * 100) if (ml and ml != 0 and used_allowance) else None
        # window calcs
        if ml:
            m20, m50, m100 = ml*0.2, ml*0.5, ml
        else:
            m20=m50=m100=None
        model_rows.append({
            "model": model,
            "cost": agg["cost"],
            "billable_cost": agg["billable_cost"],
            "list_cost": agg["list_cost"],
            "allowance_sum": agg["allowance"],
            "rows": agg["rows"],
            "monthly_limit": ml,
            "pct_month": pct_month,
            "5h_limit": m20,
            "weekly_limit": m50,
        })
    model_rows.sort(key=lambda x: x["cost"], reverse=True)

    # Print report
    if verbose:
        print("\n" + "="*78)
        print("PER-MODEL TOTALS" + (" (billable only)" if billable_only else " (all)"))
        if plan:
            print(f"Plan: {plan} | Monthly limit % uses allowance (if present) else cost")
        print("="*78)
        hdr = f"{'Model':<28} {'Cost $':>10} {'Billable $':>11} {'Rows':>5}"
        if plan:
            hdr += f" {'Limit':>8} {'% used':>8} {'5h $':>7} {'Wk $':>7}"
        print(hdr)
        print("-"*len(hdr))
        for mr in model_rows:
            line = f"{mr['model']:<28} {mr['cost']:10.4f} {mr['billable_cost']:11.4f} {mr['rows']:5d}"
            if plan:
                lim = f"${mr['monthly_limit']:.0f}" if mr['monthly_limit'] else "Unl."
                pct = f"{mr['pct_month']:.1f}%" if mr['pct_month'] is not None else "-"
                h5 = f"${mr['5h_limit']:.0f}" if mr['5h_limit'] else "-"
                wk = f"${mr['weekly_limit']:.0f}" if mr['weekly_limit'] else "-"
                line += f" {lim:>8} {pct:>8} {h5:>7} {wk:>7}"
            print(line)
        print("-"*len(hdr))
        print(f"TOTAL (shown rows): ${total_cost:.4f} | Billable (budgets What counts): ${total_billable:.4f} | Allowance sum: ${total_allowance:.4f}")
        if billable_only:
            print(f"All sources (before --billable-only): ${total_cost_all:.4f} | Billable: ${total_billable_all:.4f} | Allowance: ${total_allowance_all:.4f}")
        if per_billing:
            print("By billing_source:")
            for src, amt in per_billing.most_common():
                counts = "YES" if BUDGET_COUNTS.get(src, True) else "NO "
                # unknown treated as YES for budget but note
                if src == "unknown":
                    counts = "YES?"
                print(f"  {src:<12} ${amt:8.4f}  (counts toward budgets: {counts})")
        if per_person:
            print("Top persons:")
            for person, agg in sorted(per_person.items(), key=lambda x: x[1]["cost"], reverse=True)[:10]:
                print(f"  {person:<24} ${agg['cost']:.4f} ({agg['rows']} rows)")
        if per_day:
            print(f"Date range: {min(k for k in per_day if k) or '?'} .. {max(k for k in per_day if k) or '?'} ({len(per_day)} days)")

    # Write output CSV if requested
    if output:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", newline="", encoding="utf-8") as out:
            fieldnames = ["model","cost","billable_cost","list_cost","allowance_sum","rows","monthly_limit","pct_month","5h_limit","weekly_limit"]
            w = csv.DictWriter(out, fieldnames=fieldnames)
            w.writeheader()
            for mr in model_rows:
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
        if verbose:
            print(f"\nWrote per-model totals to {output}")

        # Also write merged deduped detailed CSV beside it for audit?
        detailed = output.with_name(output.stem + "_detailed.csv")
        with open(detailed, "w", newline="", encoding="utf-8") as out:
            fn = ["date","model","person","cost","list_cost","allowance","billing_source","is_billable","source_file"]
            w = csv.DictWriter(out, fieldnames=fn)
            w.writeheader()
            for r in filtered:
                w.writerow({k: r[k] for k in fn})
        if verbose:
            print(f"Wrote detailed merged rows to {detailed}")

    return {
        "files": [str(f) for f in files],
        "rows_before_dedupe": before,
        "rows_after_dedupe": len(deduped),
        "rows_filtered": len(filtered),
        "dupes_removed": dupes_removed,
        "per_model": model_rows,
        "per_billing": dict(per_billing),
        "total_cost": total_cost,
        "total_billable": total_billable,
        "total_allowance": total_allowance,
        "total_cost_all": total_cost_all,
        "total_billable_all": total_billable_all,
        "total_allowance_all": total_allowance_all,
        "detailed_rows": filtered,
        "detected_cols": detected_cols_per_file,
    }

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Merge OpenCode Export usage CSVs into per-model totals")
    p.add_argument("patterns", nargs="+", help="CSV files or globs (e.g. exports/*.csv)")
    p.add_argument("-o","--output", help="Output totals CSV path")
    p.add_argument("--billable-only", action="store_true", help="Filter to budgets What counts (paid/balance/go extra usage only; exclude free/go)")
    p.add_argument("--no-dedupe", action="store_true", help="Disable overlap dedupe")
    p.add_argument("--plan", choices=["go","go_plus","go-plus","go_teams","go-teams"], help="Show Go limit %")
    args = p.parse_args()
    merge_csvs(args.patterns, Path(args.output) if args.output else None, billable_only=args.billable_only, dedupe=not args.no_dedupe, plan=args.plan)
