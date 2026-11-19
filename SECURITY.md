# Security Policy

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** (repository → **Security** → **Report a vulnerability**). Please do not open a public issue for an exploitable bug. No email contact is used for this project.

## Design and scope

This is a **local** developer tool. It is not designed to be exposed to the internet.

- The server binds `127.0.0.1` by default. `--host 0.0.0.0` is for trusted LANs only.
- There is no user authentication. Run it on a machine and network you control.
- The dashboard's API enforces same-origin `Host`/`Origin` checks, which blocks DNS rebinding and cross-site POSTs. Additional hostnames can be allowed explicitly with `OPENCODE_ALLOWED_HOSTS`.
- A server-side `OPENCODE_API_KEY` from `.env` is used **only for loopback requests**. Non-local clients must supply their own key, so a LAN client cannot spend the owner's credits.
- Web requests may only send keys to `opencode.ai` base URLs. Override with `OPENCODE_ALLOWED_BASE_HOSTS` if you use a trusted proxy; the scheme must be HTTPS.
- Imported CSV values and API response fields are HTML-escaped before entering the DOM. A Content-Security-Policy restricts scripts and connections to the local origin.
- `.env`, SQLite databases, CSV logs, the limit cache, and test caches are gitignored. No telemetry is collected.

## Accepted risks

- The local SQLite DB and CSV log contain model names, token counts, cost, and billing source. Protect the machine and backups accordingly.
- Saving the API key in the browser stores it in `localStorage` for that origin. Prefer `.env` or a service-account key for automation.
- CSV exports are untrusted spreadsheets. A crafted CSV may contain formula cells that execute when opened in Excel or similar; the tracker does not rewrite cell values. Open untrusted files in a safe viewer.
- `--debug` enables Flask's debugger and must not be used on an untrusted network.

## Dependencies

Runtime dependencies are `requests` and `Flask`. Keep them updated; the project has no other runtime services.
