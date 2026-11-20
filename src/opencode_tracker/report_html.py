"""
Standalone HTML/JSON reports for scheduled or manual export.

The report bundles the same analytics, forecast, plan comparison, attribution,
and anomalies the dashboard shows, with inline CSS and no external assets.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from typing import Any, Dict, Optional

from .analytics import analytics
from .anomalies import detect_anomalies
from .attribution import attribute
from .forecast import forecast
from .simulator import simulate_plans


def report_data(
    conn,
    filters: Optional[Dict[str, Any]] = None,
    plan: str = "go",
    period_start_day: int = 1,
    days: int = 30,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    scoped = dict(filters or {})
    return {
        "generated_at": now.isoformat(),
        "plan": plan,
        "period_start_day": int(period_start_day),
        "days": int(days),
        "filters": scoped,
        "analytics": analytics(conn, filters=scoped, days=days, now=now),
        "forecast": forecast(conn, plan, period_start_day=period_start_day, filters=scoped, now=now),
        "plan_sim": simulate_plans(conn, filters=scoped, period_start_day=period_start_day, now=now),
        "anomalies": detect_anomalies(conn, filters=scoped, days=days, now=now),
        "attribution": attribute(conn, by="project", filters=scoped),
    }


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.4f}"
    except (TypeError, ValueError):
        return "-"


def _table(headers, rows, align=None):
    align = align or ["left"] * len(headers)
    out = ["<table><thead><tr>"]
    out.extend(f'<th style="text-align:{a}">{escape(str(h))}</th>' for h, a in zip(headers, align))
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for index, value in enumerate(row):
            css = "num" if align[index] == "right" else ""
            out.append(f'<td class="{css}">{escape(str(value))}</td>')
        out.append("</tr>")
    if not rows:
        out.append(f'<tr><td colspan="{len(headers)}" class="muted">No data.</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def render_html_report(
    conn,
    filters: Optional[Dict[str, Any]] = None,
    plan: str = "go",
    period_start_day: int = 1,
    days: int = 30,
    now: Optional[datetime] = None,
) -> str:
    data = report_data(conn, filters, plan, period_start_day, days, now)
    kpi = data["analytics"]["kpi"]
    filters_text = ", ".join(f"{key}={value}" for key, value in data["filters"].items()) or "none"

    kpi_cards = "".join(
        f'<div class="kpi"><div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(value)}</div><div class="sub">{escape(sub)}</div></div>'
        for label, value, sub in [
            ("Cost", _money(kpi["cost"]), f"billable {_money(kpi['billable'])}"),
            ("Requests", str(kpi["requests"]), f"{kpi['tokens']} tokens"),
            ("Cache hit", "-" if kpi["cache_hit_ratio"] is None else f"{kpi['cache_hit_ratio'] * 100:.1f}%",
             f"{kpi['cache_read']} cache-read"),
            ("Cost / request", "-" if kpi["cost_per_request"] is None else _money(kpi["cost_per_request"]),
             "billable / request"),
            ("Avg latency", "-" if kpi["avg_latency_ms"] is None else f"{kpi['avg_latency_ms']} ms", "proxy requests"),
            ("Allowance", _money(kpi["allowance"]), "Go plan consumption"),
        ]
    )

    daily_rows = [
        (row["day"], _money(row["cost"]), _money(row["allowance"]), row["n"])
        for row in data["analytics"]["daily"]
    ]
    model_rows = [
        (row["model"], _money(row["cost"]), _money(row["allowance"]), row["n"])
        for row in data["analytics"]["by_model"]
    ]
    forecast_rows = [
        (
            row["model"],
            row.get("plan") or "-",
            _money(row["month_used"]),
            _money(row["projected"]),
            "-" if row["projected_pct"] is None else f"{row['projected_pct']:.1f}%",
            "-" if row["days_to_limit"] is None else f"{row['days_to_limit']:.1f}",
            row["risk"],
        )
        for row in data["forecast"]["models"]
    ]
    plan_rows = [
        (
            row["plan"],
            _money(row["price"]),
            row["models_over"],
            _money(row["overage"]),
            "-" if row["coverage_pct"] is None else f"{row['coverage_pct']:.0f}%",
            "yes" if row["fits"] else "no",
        )
        for row in data["plan_sim"]["plans"]
    ]
    attribution_rows = [
        (row["name"] or "(none)", _money(row["cost"]),
         "-" if row["cost_share"] is None else f"{row['cost_share']:.1f}%",
         _money(row["allowance"]), row["n"])
        for row in data["attribution"]["rows"]
    ]
    anomaly_rows = [
        (row["day"], row["metric"], f"{row['value']:.4f}", f"{row['baseline_mean']:.4f}",
         "inf" if row["z"] is None else f"{row['z']:.2f}",
         row["top_models"][0]["model"] if row["top_models"] else "-")
        for row in data["anomalies"]["anomalies"]
    ]

    meta = data["forecast"]["meta"]
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>OpenCode Go Tracker report</title>
<style>
:root{{--bg:#0b0f14;--panel:#111821;--border:#1f2e44;--text:#e6eef8;--muted:#9ab0c9;--accent:#3b82f6}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 ui-sans,system-ui,"Segoe UI",Arial}}
main{{max-width:1100px;margin:0 auto;padding:28px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:15px;margin:22px 0 8px}} .muted{{color:var(--muted)}}
.meta{{color:var(--muted);font-size:12px;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
@media(max-width:800px){{.grid{{grid-template-columns:1fr 1fr}}}}
.kpi{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px}}
.kpi .label{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}}
.kpi .value{{font-size:19px;font-weight:700}} .kpi .sub{{font-size:11px;color:var(--muted)}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden}}
th,td{{padding:7px 9px;border-bottom:1px solid #17253a;font-size:12.5px}} th{{color:var(--muted);text-align:left;background:#16202e}}
td.num,th[style*="right"]{{text-align:right;font-variant-numeric:tabular-nums}}
tr:last-child td{{border-bottom:none}}
pre{{background:#070c16;border:1px solid var(--border);padding:10px;border-radius:8px;overflow:auto;font-size:11px}}
</style></head><body><main>
<h1>OpenCode Go Tracker report</h1>
<div class="meta">Generated {escape(data['generated_at'])} · plan {escape(plan)} · period {escape(meta['period_start'][:10])} → {escape(meta['period_end'][:10])} · filters: {escape(filters_text)}</div>
<div class="grid">{kpi_cards}</div>
<h2>Daily cost</h2>
{_table(["Day", "Cost", "Allowance", "Requests"], daily_rows, ["left", "right", "right", "right"])}
<h2>Models</h2>
{_table(["Model", "Cost", "Allowance", "Requests"], model_rows, ["left", "right", "right", "right"])}
<h2>Burn-rate forecast</h2>
{_table(["Model", "Plan", "Used", "Projected", "Projected %", "Days to limit", "Risk"], forecast_rows,
        ["left", "left", "right", "right", "right", "right", "left"])}
<h2>Plan comparison</h2>
{_table(["Plan", "Price/mo", "Models over", "Overage", "Coverage", "Fits"], plan_rows,
        ["left", "right", "right", "right", "right", "left"])}
<h2>Attribution by project</h2>
{_table(["Project", "Cost", "Share", "Allowance", "Requests"], attribution_rows,
        ["left", "right", "right", "right", "right"])}
<h2>Anomalies</h2>
{_table(["Day", "Metric", "Value", "Baseline", "Z", "Top model"], anomaly_rows,
        ["left", "left", "right", "right", "right", "left"])}
<p class="muted">Allowance-based Go report. Costs and allowances come from the local tracker DB; session imports carry cost/tokens but not allowance.</p>
</main></body></html>"""


def write_report(
    conn,
    path,
    fmt: str = "html",
    filters: Optional[Dict[str, Any]] = None,
    plan: str = "go",
    period_start_day: int = 1,
    days: int = 30,
) -> str:
    if fmt == "json":
        payload = report_data(conn, filters, plan, period_start_day, days)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        path.write_text(render_html_report(conn, filters, plan, period_start_day, days), encoding="utf-8")
    return str(path)
