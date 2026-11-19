# Opencode Go Tracker — Local CSV + Inference Logger + Web App

Local tracker for **OpenCode Go / Console**. Three ways to use it, one shared library:

- **CLI** (`tracker.py` / `opencode-tracker`) — CSV merge, per-request logger, reports, limit sync.
- **Web dashboard** (`app.py` + `web/index.html`) — drag-drop merger, live logger, report with computed 5h/week/month windows.
- **Standalone page** (`Go-Tracker.html`) — no server, no install: open the file and drop CSVs.

No data leaves your device except the Inference API calls you make and the public model-list request used by `sync-limits`.

## What it does

| Console area | What it shows | This tracker |
|---|---|---|
| **Activity → Usage** | Cost by day, model, person; **Export usage** CSV for 7/30/90 days | **CSV merger** consumes those exports |
| **Inference API → Cost** | Every response returns `usage.cost`, `opencode.billing_source`, `list_cost`, `allowance`, `tokens{...}` | **Logger** parses and stores every field |
| **Go** | Per-model monthly limits; 5h = 20%, weekly = 50%, monthly = 100% | **Windows report** computes live 5h/week/month usage |
| **Go model list API** | `GET /inference/go/v1/models` with `go.monthly_usage` | **`sync-limits`** refreshes the local limit table |
| **Budgets → What counts** | Paid & Go extra usage count; free & Go-within-plan don't | Merger's `--billable-only`, logger's `billing_source` |

Go window semantics come from <https://opencode.ai/docs/go#usage-limits>:

- **5-hour limit = 20%** of the model's monthly allowance. The window starts when you first use your allowance and lasts five hours.
- **Weekly limit = 50%**, resetting **Monday 00:00 UTC**.
- **Monthly limit = 100%**, resetting when a new paid period starts. The local tracker cannot see your subscription anniversary, so the reset day is configurable (`--period-start-day`, default 1).

`allowance` is what counts toward Go limits. `cost` is 0 while usage stays inside the plan; it becomes non-zero (and `billing_source=balance`) after Extra Usage kicks in.

---

## Quick start

```powershell
git clone https://github.com/Avithegod2010/opencode-go-tracker.git
cd opencode-go-tracker

# Editable install (CLI + library). Flask and requests come with it.
pip install -e ".[dev]"

# Optional: configure a key for live logging
copy .env.example .env
# edit .env: OPENCODE_API_KEY=oc_sk_... and OPENCODE_BASE_URL
#   Personal key: Console → Account → Personal API keys → Add API key
#   Service account: Console → Settings → Service accounts → Add API Key
#   Permission needed: Restricted → Models: Request
#   Base URL: https://opencode.ai/inference/go  (Go)  or  https://opencode.ai/inference (paid balance)

# Dashboard
python app.py                 # http://localhost:8000
# or
opencode-tracker web --port 8000
```

### Dashboard tabs

- **A. CSV Merger** — drop `usage_7d.csv` + `usage_30d.csv` (+ `90d`), pick a plan, toggle billable-only/dedupe, **Merge** → per-model table plus **totals.csv** and **detailed.csv** downloads.
- **B. Logger** — paste an `oc_sk_...` key, choose Base URL, model and API family, send a message → shows `billing_source`, `cost`, `allowance`, tokens, and writes SQLite + CSV.
- **Report** — per-model/billing totals, current-period totals, daily trend, recent requests, and the live **5h / week / month** window table. Set the **period start day** to your subscription anniversary.
- **Help** — docs links and billing/Go/budget notes.

Keys typed in the browser persist in `localStorage` only on your device. Prefer `.env` for CLI use.

### Standalone page

Open `Go-Tracker.html` directly (double-click). Drop the same export CSVs; everything runs in the browser. The root `index.html` is a small landing page linking both surfaces.

---

## A. CSV merger

**Where to get CSVs:** Console → **Activity → Usage** → **Export usage** → **7 / 30 / 90 days**. The exports overlap, so the merger deduplicates.

```powershell
# Merge everything in a folder
opencode-tracker csv-merge "exports/*.csv" -o totals.csv --plan go

# Explicit files
opencode-tracker csv-merge exports/usage_7d.csv exports/usage_30d.csv exports/usage_90d.csv -o totals.csv

# Mirror budget spend: only Paid + Go extra usage (exclude Free & Go within plan)
opencode-tracker csv-merge "exports/*.csv" -o totals_billable.csv --billable-only

# Other plans / raw sum
opencode-tracker csv-merge "exports/*.csv" --plan go_plus
opencode-tracker csv-merge "exports/*.csv" --plan go_teams
opencode-tracker csv-merge "exports/*.csv" -o raw.csv --no-dedupe
```

`python tracker.py csv-merge ...` is equivalent.

**Output**

- Console table sorted by cost, with monthly limit, `% used`, and the 5h/week limit values when `--plan` is given.
- `totals.csv` — `model, cost, billable_cost, list_cost, allowance_sum, rows, monthly_limit, pct_month, 5h_limit, weekly_limit`.
- `totals_detailed.csv` — the deduped rows used for the sums (audit trail).
- Footer: shown totals, billable totals, allowance, billing-source breakdown, top persons, date range.
- With `--billable-only`, the totals describe the **filtered rows** you are looking at; the unfiltered totals are printed underneath for context.

**Column auto-detection** is case/spacing-insensitive. Recognized: `date/day/timestamp`, `model/model_id`, `person/member/user/email`, `cost/amount/usd`, `billing_source/source`, `allowance`, `list_cost`, and token columns.

**Dedupe** uses `(date, model, person, billing_source, cost, allowance)`. Repeats are dropped only when they come from a **different** file, so overlapping 7/30/90 exports collapse while genuinely distinct rows inside one export survive. Disable with `--no-dedupe`.

**Billable logic (Budgets → What counts):**

| Spend | Counts? | `billing_source` |
|---|---|---|
| Paid Console models | **Yes** | `balance` |
| Your own provider (BYOK) with a price | **Yes** | `byok` |
| Free models | **No** | `free` |
| Go within plan | **No** | `go` / `go-plus` |
| Go extra usage from credits | **Yes** | `balance` (after the limit) |

Sample exports live in `examples/`:

```powershell
opencode-tracker csv-merge "examples/*.csv" -o examples_totals.csv --plan go
```

---

## B. Logger (Inference API → SQLite/CSV)

Every inference response reports cost fields (see <https://opencode.ai/v2/docs/console/api/inference/>):

```json
"usage": {
  "prompt_tokens": 1200,
  "completion_tokens": 300,
  "cost": 0.00412,
  "opencode": {
    "billing_source": "balance",
    "list_cost": 0.00412,
    "allowance": 0.003,
    "estimated": true,
    "tokens": {"input":1200,"output":300,"reasoning":120,"cache_read":8000,"cache_write_5m":0,"cache_write_1h":0}
  }
}
```

The logger supports all four API families:

- OpenAI Chat Completions `/openai/v1/chat/completions`
- OpenAI Responses `/openai/v1/responses`
- Anthropic Messages `/anthropic/v1/messages`
- Gemini `/google/v1beta/models/<model>:generateContent`

Streaming is parsed from the final usage event (`stream_options.include_usage` for Chat Completions, `response.completed` for Responses, `message_delta` for Messages, last chunk for Gemini).

### Single call → DB + CSV

```powershell
opencode-tracker log --model kimi-k3 --message "Say hi in one sentence."
opencode-tracker log --model glm-5.3-flash --message "Hi" --base-url https://opencode.ai/inference/go
opencode-tracker log --model claude-haiku-5-5 --api anthropic --message "Hi"
opencode-tracker log --model gpt-6-luna --api responses --message "Hi"
opencode-tracker log --model gemini-4-argon --api gemini --message "Hi"
opencode-tracker log --model kimi-k3 --message "Stream hello" --stream
opencode-tracker log --model kimi-k3 --message "Hi" --json-out response.json
```

**SQLite** (`opencode_usage.db`) table `requests`:

| Column | Meaning |
|---|---|
| `timestamp` | UTC ISO |
| `model`, `provider`, `endpoint` | request metadata |
| `billing_source` | `free \| balance \| go \| go-plus \| byok` |
| `cost` | charged amount (0 for free / Go within plan) |
| `list_cost` | retail price |
| `allowance` | Go dollars consumed |
| `input_tokens`, `output_tokens`, `reasoning_tokens`, `cache_read`, `cache_write_5m/1h` | from `opencode.tokens` |
| `prompt_tokens`, `completion_tokens` | raw `usage.*` |
| `estimated` | BYOK estimate flag |
| `status` | `ok \| error \| 401_unauthorized \| 402_insufficient_funds \| 403_forbidden \| 429_budget_exceeded` |
| `raw_json` | usage blob for audit |

**CSV** (`usage_log.csv`) gets the same columns, appended per request.

**Keys & errors:** send `Authorization: Bearer oc_sk_...`. A key needs **Models: Request**. HTTP `401` = invalid/expired/revoked key; `402` = balance zero; `403` = key lacks permission or the model is blocked; `429` = budget policy hit.

### Programmatic use

```python
from opencode_tracker.logger import OpencodeLogger, parse_usage

logger = OpencodeLogger(
    db_path="opencode_usage.db",
    csv_path="usage_log.csv",
    api_key="oc_sk_...",
    base_url="https://opencode.ai/inference/go",
)

resp = logger.chat_completions(model="kimi-k3", messages=[{"role": "user", "content": "hi"}])
parsed = parse_usage(resp)  # billing_source, cost, allowance, tokens...
```

For your own HTTP calls, wrap them with `parse_usage(response_json)` and insert via `opencode_tracker.db.insert_request`.

---

## Go windows (5h / week / month)

```powershell
opencode-tracker report --plan go
opencode-tracker report --plan go_plus --period-start-day 15
opencode-tracker report --plan go_teams --db opencode_usage.db
```

The report prints lifetime per-model totals, then a live window table:

```
Model                        5h used/limit          Week used/limit         Month used/limit  Status
kimi-k3                   $2.00/$3 (66.7%)      $3.00/$8 (40.0%)      $9.00/$15 (60.0%)    warm
```

- **5h** is anchored at the first Go request after the previous window ended — not a naive "last 5 hours".
- **Week** starts Monday 00:00 UTC.
- **Month** starts on `--period-start-day` (or `OPENCODE_PERIOD_START_DAY`), default 1. Set it to your subscription anniversary for accurate monthly percentages.
- Only rows with `allowance > 0` count. Balance/free rows are ignored.
- `status` is `ok` (<50%), `warm` (50–80%), `near` (80–100%), `over` (≥100%), or `no-limit`.

The web Report tab shows the same table and lets you change the period start day without restarting.

> CSV exports are daily totals, so they cannot resolve the 5-hour window. The standalone page therefore shows the **current month** and **last 7 days** relative to the newest date in the file and labels 5h as unavailable.

---

## Limit sync (`sync-limits`)

The live Go model list is documented at <https://opencode.ai/v2/docs/console/api/models>:

```powershell
opencode-tracker sync-limits
# GET https://opencode.ai/inference/go/v1/models
# caches data[].go.monthly_usage -> go_limits_cache.json
```

- Works without a key (public Go catalog). With a Go key it returns the workspace's own catalog.
- Cached values take precedence over the built-in table, so limits stay current as plans change.
- Example: the live list currently reports **GLM-5.3 at $500/$1000/$505** (a temporary higher allowance), while the static fallback still has the normal $15/$120/$20.
- The cache file is gitignored; delete it to fall back to the built-in table.
- Web equivalent: `POST /api/sync-limits` with `{}` (or `{"api_key": "..."}`).

**About usage polling:** Console documents a **Usage: Read** permission, but the public API overview lists only Models, Inference, and Providers — there is no documented usage endpoint to poll. This tracker therefore records usage from inference responses (the logger) and imports the official CSV exports. The `sync-limits` command uses the one documented endpoint that carries usage limits.

---

## Project layout

```
.
├── app.py                      # Flask server (serves web/ + /api/*)
├── web/index.html              # Dashboard (Merger, Logger, Report, Help)
├── index.html                  # Landing page linking the two UIs
├── Go-Tracker.html             # Standalone, no-backend analysis page
├── tracker.py                  # Thin CLI shim (kept for `python tracker.py ...`)
├── src/opencode_tracker/
│   ├── cli.py                  # CLI implementation + `opencode-tracker` entry point
│   ├── csv_merger.py           # A. merger
│   ├── logger.py               # B. inference logger + parse_usage
│   ├── db.py                   # SQLite schema/queries
│   ├── windows.py              # 5h / week / month computation
│   └── go_limits.py            # limits table + sync cache
├── tests/                      # pytest suite (42 tests)
├── examples/                   # sample usage CSVs
├── .env.example
├── pyproject.toml
├── SECURITY.md
└── README.md
```

## Tests

```powershell
pip install -e ".[dev]"
python -m pytest -q
```

Coverage: CSV dedupe/totals/billable filtering, window math (5h anchoring, Monday reset, period anniversary, future rows), usage parsing for all four API families, logger persistence and error statuses, the HTTP API through Flask's test client, and the security guards (Host/Origin checks, base-URL allowlist, loopback-only server key, security headers).

---

## Troubleshooting

- **No CSVs matched** → quote the glob: `"exports/*.csv"` (Windows shell expansion differs).
- **Missing model/cost column** → check the Console export header row; the merger prints detected columns.
- **Go % looks too high** → the CSV's `allowance` may cover more than the current month, or your subscription renews mid-month. Use the live report with `--period-start-day`.
- **Windows show 0** → allowance is only present on live Inference responses, not in daily CSV exports. Log a Go request first, or import a CSV only for cost totals.
- **No allowance in CSV** → exports may only carry `cost`; the merger falls back to cost for the summary percentage.
- **`sync-limits` fails** → check network access to `https://opencode.ai/inference/go/v1/models`; clear `go_limits_cache.json` and retry.
- **HTTP 401** → key invalid/expired/revoked.
- **HTTP 402** → balance zero; add credits or enable Auto-recharge.
- **HTTP 403** → key lacks **Models: Request**, or the model is blocked by workspace privacy settings.
- **HTTP 429** → budget policy hit; raise it in Controls or check Activity → Logs.
- **`web` says app.py not found** → the installed CLI can run the library, but the dashboard needs the repository checkout (`python app.py`).

## Privacy

`.env`, `*.db`, `*.csv`, `go_limits_cache.json`, and `.pytest_cache/` are local and gitignored. No telemetry. Requests go only to `https://opencode.ai` and the base URL you configure.

## Security

See [SECURITY.md](SECURITY.md) for the full policy. Defaults are deliberately local:

- Binds `127.0.0.1`; do not expose the dev server to the internet.
- The server-side `OPENCODE_API_KEY` from `.env` is only used for **loopback** requests. LAN clients must send their own key.
- The web logger only forwards to `opencode.ai` (override hosts with `OPENCODE_ALLOWED_BASE_HOSTS`), blocking SSRF and key exfiltration.
- `Host`/`Origin` validation blocks DNS rebinding and cross-site POSTs. Extra hostnames can be allowed with `OPENCODE_ALLOWED_HOSTS`.
- CSV/API values are HTML-escaped in the dashboard and a strict Content-Security-Policy limits connections to the same origin.
- Browser-saved keys live in `localStorage` only when you tick “save key”.

## License

MIT — see [LICENSE](LICENSE).
