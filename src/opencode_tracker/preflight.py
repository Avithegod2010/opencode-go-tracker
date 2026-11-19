"""
Key preflight: verify an API key against the documented model list endpoint.

GET <base_url>/v1/models is the cheapest documented call. It confirms the key
is accepted and that the Models permission is present, and it reports whether
the catalogue contains Go models. The key itself is never returned or logged.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from .logger import classify_error


def _redact(key: str) -> Optional[str]:
    if not key:
        return None
    return key[:8] + "..." if len(key) > 8 else "***"


def check_key(
    api_key: str,
    base_url: str = "https://opencode.ai/inference/go",
    timeout: int = 20,
) -> Dict[str, Any]:
    url = base_url.rstrip("/") + "/v1/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    result: Dict[str, Any] = {
        "ok": False,
        "status_code": None,
        "model_count": 0,
        "go_model_count": 0,
        "go_models": [],
        "plans_seen": [],
        "error": None,
        "hint": None,
        "key_prefix": _redact(api_key),
        "base_url": url,
    }

    try:
        if requests is not None:
            resp = requests.get(url, headers=headers, timeout=timeout)
            status = resp.status_code
            text = resp.text[:1000] if status >= 400 else ""
            payload = resp.json() if status < 400 else None
        else:  # pragma: no cover
            import urllib.error
            import urllib.request

            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = resp.status
                    payload = json.loads(resp.read().decode("utf-8"))
                    text = ""
            except urllib.error.HTTPError as exc:
                status = exc.code
                text = exc.read().decode("utf-8", "replace")[:1000]
                payload = None
    except Exception as exc:
        result["error"] = str(exc)
        result["hint"] = "Could not reach the model list endpoint. Check the base URL and network."
        return result

    result["status_code"] = status
    if status >= 400:
        label, hint = classify_error(status, text)
        result["error"] = f"HTTP {status}: {text[:300]}" if text else f"HTTP {status}"
        result["hint"] = f"{label} - {hint}"
        return result

    items = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = [i for i in payload["data"] if isinstance(i, dict)]
        elif isinstance(payload.get("models"), list):
            items = [i for i in payload["models"] if isinstance(i, dict)]

    go_models = []
    plans = set()
    for item in items:
        go = item.get("go")
        if isinstance(go, dict):
            model_id = item.get("id") or item.get("name")
            if model_id:
                go_models.append(str(model_id))
            monthly = go.get("monthly_usage")
            if isinstance(monthly, dict):
                plans.update(str(k) for k in monthly.keys())

    result["ok"] = True
    result["model_count"] = len(items)
    result["go_model_count"] = len(go_models)
    result["go_models"] = sorted(go_models)[:20]
    result["plans_seen"] = sorted(plans)
    return result
