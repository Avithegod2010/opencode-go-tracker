# Security Policy

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** (repository → **Security** → **Report a vulnerability**). Please do not open a public issue for an exploitable bug. No email contact is used for this project.

## Design and scope

This is a **local** developer tool. It is not designed to be exposed to the internet.

- The server binds `127.0.0.1` by default. `--host 0.0.0.0` is for trusted LANs only.
- Every `/api/*` route except `/api/health` requires a token (`X-Tracker-Token` header or `?token=`). Loopback is **not** authentication: any local process can reach `127.0.0.1`. When `TRACKER_API_TOKEN` is unset the server generates a 32-byte token on first start, saves it as `api_token` (mode 0600) under the user config dir (`%APPDATA%\opencode-go-tracker`, `$XDG_CONFIG_HOME/opencode-go-tracker`), and prints it once. Set `TRACKER_API_TOKEN` to override it for headless/CI use.
- The dashboard page is handed that token in a `<meta name="tracker-api-token">` tag, injected only into a same-origin render requested from loopback; a foreign `Host` or a non-local peer gets the page without it. The page sends the header on every `/api/*` call.
- The reverse proxy binds `127.0.0.1` by default, forwards the client's `Authorization` header, and never stores or logs it. Do not expose the proxy port.
- Profiles store only base URL/plan/project/member and an environment variable **name**; keys stay in the environment and are never written to SQLite.
- The session importer opens OpenCode's DB read-only and selects only session/message metadata (ids, projects, titles, models, cost, tokens, timestamps). It never reads prompts, message content, the `credential` table, or account tables.
- The dashboard's API enforces same-origin `Host`/`Origin` checks, which blocks DNS rebinding and cross-site POSTs. Additional hostnames can be allowed explicitly with `OPENCODE_ALLOWED_HOSTS`.
- A server-side `OPENCODE_API_KEY` or profile key is used **only for loopback requests**, and only behind the token gate. Non-local clients must supply their own key, so a LAN client cannot spend the owner's credits.
- Web requests may only send keys to `opencode.ai` base URLs. Override with `OPENCODE_ALLOWED_BASE_HOSTS` if you use a trusted proxy; the scheme must be HTTPS.
- Imported CSV values, session metadata, and API response fields are HTML-escaped before entering the DOM or an exported report. A Content-Security-Policy restricts scripts and connections to the local origin.
- `.env`, SQLite databases, CSV logs, reports, the limit cache, and test caches are gitignored. No telemetry is collected.

## Accepted risks

- The local SQLite DB and CSV log contain model names, token counts, cost, and billing source. Protect the machine and backups accordingly.
- Saving the API key in the browser stores it in `localStorage` for that origin. Prefer `.env` or a service-account key for automation.
- CSV exports are untrusted spreadsheets. A crafted CSV may contain formula cells that execute when opened in Excel or similar; the tracker does not rewrite cell values. Open untrusted files in a safe viewer.
- `--debug` enables Flask's debugger and must not be used on an untrusted network.

## Dependencies

Runtime dependencies are `requests` and `Flask`. Keep them updated; the project has no other runtime services.
