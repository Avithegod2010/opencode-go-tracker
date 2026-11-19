"""
Go / Go Plus / Go Teams monthly limits per model.
Source: https://opencode.ai/docs/go
Token prices are per 1M tokens.

Each model has: 5-hour = 20% monthly, weekly = 50% monthly, monthly = 100%.

Window semantics (from https://opencode.ai/docs/go#usage-limits):
- The 5-hour window starts when you first use your allowance and lasts 5 hours.
- The weekly window resets Monday at 00:00 UTC.
- The monthly allowance resets when a new paid period starts. Since the local
  tracker cannot see the subscription anniversary, the reset day is
  configurable (`--period-start-day`, default 1 = calendar month).

The hard-coded table below is a fallback. `sync_limits()` refreshes it from the
documented Go model list API (``GET <base>/v1/models``) and caches the result
next to the DB. Cached values win over the static table.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Monthly limits in USD. None = Unlimited (free models)
GO_LIMITS = {
    # Model ID -> {go, go_plus, go_teams} monthly $
    "glm-5.3-flash":        {"go": 60,  "go_plus": 180, "go_teams": 65},
    "glm-5.3":              {"go": 15,  "go_plus": 120, "go_teams": 20},
    "glm-5.2":              {"go": 60,  "go_plus": 180, "go_teams": 65},
    "kimi-k3.1-pro":        {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "kimi-k3":              {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "kimi-k2.7-code":       {"go": 60,  "go_plus": 180, "go_teams": 65},
    "longcat-2.0":          {"go": 60,  "go_plus": 240, "go_teams": 65},
    "step-5-preview":       {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "beam-free":            {"go": None, "go_plus": None, "go_teams": None},
    "iceman-contributor-free": {"go": None, "go_plus": None, "go_teams": None},
    "mimo-v2.6-flash":      {"go": 60,  "go_plus": 120, "go_teams": 65},
    "mimo-v2.6-pro":        {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "mimo-v2.5":            {"go": 60,  "go_plus": 120, "go_teams": 65},
    "mimo-v2.5-pro":        {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "minimax-m3.1-flash":   {"go": 20,  "go_plus": 80,  "go_teams": 25},
    "minimax-m3":           {"go": 60,  "go_plus": 180, "go_teams": 65},  # both <=512K and >512K share limit
    "muse-spark-1.3-contributor": {"go": 60, "go_plus": 120, "go_teams": 65},
    "muse-spark-1.2-contributor": {"go": 60, "go_plus": 120, "go_teams": 65},
    "qwen3.8-max":          {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "qwen3.8-flash":        {"go": 30,  "go_plus": 90,  "go_teams": 35},
    "qwen3.7-plus":         {"go": 60,  "go_plus": 180, "go_teams": 65},
    "deepseek-v4.1-flash":  {"go": 100, "go_plus": 200, "go_teams": 105},
    "deepseek-v4-pro":      {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "hy4-preview":          {"go": 30,  "go_plus": 120, "go_teams": 35},
    "hy3":                  {"go": 60,  "go_plus": 240, "go_teams": 65},
    "grok-4.7":             {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "gpt-6-luna":           {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "gpt-5.6-luna":         {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "claude-haiku-5-5":     {"go": 15,  "go_plus": 60,  "go_teams": 20},
    "gemini-4-argon":       {"go": 30,  "go_plus": 120, "go_teams": 35},
}

# Normalized aliases (in case CSV uses display names)
ALIASES = {
    "glm-5.3-flash": "glm-5.3-flash",
    "glm 5.3 flash": "glm-5.3-flash",
    "glm-5.3": "glm-5.3",
    "glm-5.2": "glm-5.2",
    "kimi k3.1 pro": "kimi-k3.1-pro",
    "kimi k3": "kimi-k3",
    "kimi-k2.7-code": "kimi-k2.7-code",
    "longcat 2.0": "longcat-2.0",
    "mimo-v2.6-flash": "mimo-v2.6-flash",
    "mimo-v2.6-pro": "mimo-v2.6-pro",
    "mimo-v2.5": "mimo-v2.5",
    "mimo-v2.5-pro": "mimo-v2.5-pro",
    "minimax m3.1 flash": "minimax-m3.1-flash",
    "minimax m3": "minimax-m3",
    "muse spark 1.3 contributor": "muse-spark-1.3-contributor",
    "muse spark 1.2 contributor": "muse-spark-1.2-contributor",
    "qwen3.8 max": "qwen3.8-max",
    "qwen3.8 flash": "qwen3.8-flash",
    "qwen3.7 plus": "qwen3.7-plus",
    "deepseek v4.1 flash": "deepseek-v4.1-flash",
    "deepseek v4 pro": "deepseek-v4-pro",
    "grok 4.7": "grok-4.7",
    "gpt 6 luna": "gpt-6-luna",
    "gpt 5.6 luna": "gpt-5.6-luna",
    "claude haiku 5.5": "claude-haiku-5-5",
    "gemini 4 argon": "gemini-4-argon",
}

DEFAULT_CACHE_PATH = Path(os.getenv("OPENCODE_LIMITS_CACHE", "go_limits_cache.json"))

_cached: Dict[str, Dict[str, Optional[float]]] = {}
_cache_loaded = False
_cache_path: Optional[Path] = None


def normalize_model(name: str) -> str:
    if not name:
        return ""
    n = name.strip().lower()
    # direct match
    if n in GO_LIMITS or n in _cached:
        return n
    if n in ALIASES:
        return ALIASES[n]
    # try replacing spaces/underscores with dashes
    n2 = n.replace(" ", "-").replace("_", "-")
    if n2 in GO_LIMITS or n2 in _cached:
        return n2
    if n2 in ALIASES:
        return ALIASES[n2]
    # opencode-go/ prefix
    if n2.startswith("opencode-go/"):
        cand = n2[len("opencode-go/"):]
        if cand in GO_LIMITS or cand in _cached:
            return cand
    return n  # fallback as-is


def _read_cache(path: Path) -> Dict[str, Dict[str, Optional[float]]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        limits = data.get("limits", data)
        out: Dict[str, Dict[str, Optional[float]]] = {}
        for model, entry in limits.items():
            if not isinstance(entry, dict):
                continue
            out[model.lower()] = {
                "go": entry.get("go"),
                "go_plus": entry.get("go_plus"),
                "go_teams": entry.get("go_teams"),
            }
        return out
    except (OSError, ValueError):
        return {}


def load_cached_limits(path: Optional[str | Path] = None, force: bool = False) -> Dict[str, Dict[str, Optional[float]]]:
    """Load synced limits from disk (once per process unless force=True)."""
    global _cached, _cache_loaded, _cache_path
    if _cache_loaded and not force and path is None:
        return _cached
    p = Path(path) if path else DEFAULT_CACHE_PATH
    _cache_path = p
    _cached = _read_cache(p)
    _cache_loaded = True
    return _cached


def all_limits() -> Dict[str, Dict[str, Optional[float]]]:
    """Merged view: synced cache first, static table as fallback."""
    load_cached_limits()
    merged = dict(GO_LIMITS)
    for model, entry in _cached.items():
        merged[model] = entry
    return merged


def monthly_limit(model: str, plan: str = "go") -> Optional[float]:
    """plan in {go, go_plus, go_teams, go-plus, go-teams}"""
    p = plan.lower().replace("-", "_") if plan else "go"
    if p not in ("go", "go_plus", "go_teams"):
        p = "go"
    norm = normalize_model(model)
    entry = all_limits().get(norm)
    if not entry:
        return None
    value = entry.get(p)
    return float(value) if value is not None else None


def window_limits(model: str, plan: str = "go"):
    m = monthly_limit(model, plan)
    if m is None:
        return (None, None, None)
    return (m * 0.2, m * 0.5, m)


PLAN_PRICES = {"go": 10, "go_plus": 40, "go_teams": 20}  # per month / per member


# ---------- live sync from the documented model list API ----------
def _plan_value(entry: Dict[str, Any], *names: str) -> Optional[float]:
    """Pull a number out of a plan entry that may be a number or a small object."""
    for name in names:
        value = entry.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            for key in ("amount", "usd", "limit", "monthly", "value"):
                inner = value.get(key)
                if isinstance(inner, (int, float)) and not isinstance(inner, bool):
                    return float(inner)
    return None


def _limits_from_item(item: Dict[str, Any]) -> Optional[Dict[str, Optional[float]]]:
    """Extract {go, go_plus, go_teams} from one /v1/models item.

    Documented shape: each Go model has a `go` object whose `monthly_usage`
    holds the monthly limit on each plan. Accepts a few tolerant variants.
    """
    go = item.get("go")
    if not isinstance(go, dict):
        return None

    monthly = go.get("monthly_usage")
    out: Dict[str, Optional[float]] = {"go": None, "go_plus": None, "go_teams": None}

    if isinstance(monthly, dict):
        out["go"] = _plan_value(monthly, "go", "monthly", "base")
        out["go_plus"] = _plan_value(monthly, "go_plus", "go-plus", "goplus", "plus")
        out["go_teams"] = _plan_value(monthly, "go_teams", "go-teams", "goteams", "teams")
    elif isinstance(monthly, (int, float)) and not isinstance(monthly, bool):
        out["go"] = float(monthly)
        out["go_plus"] = float(monthly)
        out["go_teams"] = float(monthly)
    else:
        # Some payloads may put the plans directly under `go`.
        out["go"] = _plan_value(go, "go", "monthly", "base")
        out["go_plus"] = _plan_value(go, "go_plus", "go-plus", "goplus", "plus")
        out["go_teams"] = _plan_value(go, "go_teams", "go-teams", "goteams", "teams")

    if all(v is None for v in out.values()):
        return None
    return out


def sync_limits(
    api_key: Optional[str] = None,
    base_url: str = "https://opencode.ai/inference/go",
    cache_path: Optional[str | Path] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Fetch Go model limits from the model list API and cache them.

    GET <base_url>/v1/models is documented at
    https://opencode.ai/v2/docs/console/api/models. The key is optional for the
    public Go catalog, but a Go key may return the workspace's own catalog.
    """
    url = base_url.rstrip("/") + "/v1/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a declared dependency
        requests = None  # type: ignore

    if requests is not None:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} from {url}: {resp.text[:500]}")
        payload = resp.json()
    else:  # pragma: no cover - fallback when requests is unavailable
        import urllib.request
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))

    items: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = [i for i in payload["data"] if isinstance(i, dict)]
        elif isinstance(payload.get("models"), list):
            items = [i for i in payload["models"] if isinstance(i, dict)]

    limits: Dict[str, Dict[str, Optional[float]]] = {}
    for item in items:
        model_id = item.get("id") or item.get("model") or item.get("name")
        if not model_id:
            continue
        parsed = _limits_from_item(item)
        if parsed:
            limits[str(model_id).lower()] = parsed

    if not limits:
        raise RuntimeError(
            f"No Go limits found in {url}. "
            "The response did not contain data[].go.monthly_usage."
        )

    fetched_at = datetime.now(timezone.utc).isoformat()
    result = {
        "fetched_at": fetched_at,
        "source": url,
        "limits": limits,
    }
    target = Path(cache_path) if cache_path else DEFAULT_CACHE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    load_cached_limits(target, force=True)
    return result
