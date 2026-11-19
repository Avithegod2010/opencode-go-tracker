"""
Named API key profiles without storing secrets.

A profile stores only non-secret metadata (base URL, plan, project, member)
and the *name* of an environment variable that holds the key:

    OPENCODE_KEY_WORK, or OPENCODE_PROFILE_WORK_KEY, or OPENCODE_API_KEY

This keeps keys out of SQLite, exports, and the public repo while letting
personal, work, and CI/service-account keys keep separate usage labels.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from .db import get_profile

DEFAULT_BASE_URL = "https://opencode.ai/inference/go"


def profile_env_key(name: str) -> Optional[str]:
    safe = re.sub(r"[^A-Za-z0-9]", "_", name or "").upper()
    return os.getenv(f"OPENCODE_PROFILE_{safe}_KEY")


def resolve_profile(conn, name: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a profile name to settings plus a key (never persisted).

    Raises KeyError for an unknown named profile.
    """
    chosen = (name or os.getenv("OPENCODE_PROFILE") or "").strip() or None
    env_base = (os.getenv("OPENCODE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    env_key = os.getenv("OPENCODE_API_KEY") or os.getenv("OPENCODE_API_TOKEN")

    if not chosen:
        return {
            "name": "default",
            "base_url": env_base,
            "plan": os.getenv("OPENCODE_PLAN") or None,
            "key": env_key,
            "key_env": None,
            "project": os.getenv("OPENCODE_PROJECT") or None,
            "member": os.getenv("OPENCODE_MEMBER") or None,
        }

    row = get_profile(conn, chosen)
    if row is None:
        raise KeyError(f"Unknown profile: {chosen}")

    key = None
    if row["key_env"]:
        key = os.getenv(row["key_env"])
    key = key or profile_env_key(chosen) or env_key
    return {
        "name": chosen,
        "base_url": (row["base_url"] or env_base).rstrip("/"),
        "plan": row["plan"],
        "key": key,
        "key_env": row["key_env"],
        "project": row["project"],
        "member": row["member"],
    }


def profile_summary(row) -> Dict[str, Any]:
    """Public profile metadata; never includes a key."""
    return {
        "name": row["name"],
        "base_url": row["base_url"],
        "plan": row["plan"],
        "key_env": row["key_env"],
        "project": row["project"],
        "member": row["member"],
        "created_at": row["created_at"],
        "last_checked_at": row["last_checked_at"],
        "last_status": row["last_status"],
    }
