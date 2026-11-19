"""
B. Logger: capture per-request usage.cost / allowance into SQLite/CSV via Inference API

Docs:
- Cost section: every response returns usage.cost, opencode.billing_source (free|balance|go|go-plus|byok),
  opencode.list_cost, opencode.allowance, opencode.tokens {input, output, reasoning, cache_read, cache_write_5m, cache_write_1h}
  location: in usage for OpenAI/Anthropic, in usageMetadata for Gemini.
  Streams report it on final usage chunk (Chat Completions with stream_options.include_usage,
  response.completed for Responses, message_delta for Messages, last chunk for Gemini).
- Authentication: Authorization: Bearer <token> (personal API key or service account key)
  personal keys need Models > Request; service account keys similar.
  Usage:Read is separate (for future polling, not inference).

This module:
- Provides parse_usage() that handles OpenAI Chat Completions, Responses, Anthropic Messages, Gemini.
- Provides OpencodeLogger class that wraps HTTP calls and logs to SQLite + CSV.
- CLI helper call_once() for single test request.

Usage:
  from opencode_tracker.logger import OpencodeLogger
  logger = OpencodeLogger(db_path="opencode_usage.db", csv_path="usage_log.csv", api_key="oc_sk_...", base_url="https://opencode.ai/inference")
  resp_json = logger.chat_completions(model="kimi-k3", messages=[{"role":"user","content":"hi"}])
  # resp_json is full response; logger auto-extracted usage and wrote to DB/CSV

For streaming, set stream=True and include_usage handling.

Go-specific base_url: https://opencode.ai/inference/go   (vs https://opencode.ai/inference)
Go endpoint mapping per docs: /openai/v1/chat/completions, /openai/v1/responses, /anthropic/v1/messages, /google/...
  All paths are identical under /inference/go/ prefix.

Billing_source values:
  free    -> Free models (never charged)
  balance -> Paid via Console balance (including Go Extra Usage after limit)
  go      -> Go within plan (cost=0, allowance>0)
  go-plus -> Go Plus within plan
  byok    -> Your own provider key (cost may be estimated)
"""
from __future__ import annotations
import csv
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None  # type: ignore

from .db import get_conn, insert_request

# ---------- env loading (no extra dep) ----------
def load_dotenv(path: Path = Path(".env")):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k,v=line.split("=",1)
        k=k.strip(); v=v.strip().strip('"').strip("'")
        import os
        if k not in os.environ:
            os.environ[k]=v

# ---------- usage parsing ----------
def parse_usage(response_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract normalized usage fields from any inference response.
    Returns dict with keys: cost, billing_source, list_cost, allowance, estimated,
                            tokens {input, output, reasoning, cache_read, cache_write_5m, cache_write_1h},
                            prompt_tokens, completion_tokens, raw_location
    Handles:
      OpenAI Chat Completions: usage {prompt_tokens, completion_tokens, cost, opencode{...}}
      OpenAI Responses:        usage + response.completed? but same shape
      Anthropic:               usage {input_tokens, output_tokens, cost, opencode}
      Gemini:                  usageMetadata {promptTokenCount, candidatesTokenCount, ... , cost, opencode}
    """
    # locate usage object
    usage = None
    loc = None
    if "usage" in response_json and isinstance(response_json["usage"], dict):
        usage = response_json["usage"]
        loc = "usage"
    elif "usageMetadata" in response_json and isinstance(response_json["usageMetadata"], dict):
        usage = response_json["usageMetadata"]
        loc = "usageMetadata"
    elif "usage_metadata" in response_json and isinstance(response_json["usage_metadata"], dict):
        usage = response_json["usage_metadata"]
        loc = "usage_metadata"
    else:
        # streaming or nested? Try to find in choices? For some streaming, top-level still usage
        # Also check response["usage"] for Responses API
        for k in ("usage", "usageMetadata"):
            if k in response_json:
                usage = response_json[k]
                loc = k
                break

    if usage is None:
        # no usage block - maybe error response
        return {
            "cost": None, "billing_source": None, "list_cost": None, "allowance": None,
            "estimated": None, "tokens": {}, "prompt_tokens": None, "completion_tokens": None,
            "raw_location": None, "raw_usage": None,
        }

    # opencode block may be nested inside usage or sibling
    opencode = usage.get("opencode") if isinstance(usage, dict) else None
    if opencode is None and "opencode" in response_json:
        opencode = response_json.get("opencode")
    if opencode is None:
        # Sometimes x-opencode headers? but specs says in usage
        opencode = {}

    # cost: usage.cost
    cost = usage.get("cost")
    # Gemini may have cost at top? fallback to opencode?
    if cost is None and "cost" in response_json:
        cost = response_json["cost"]

    billing_source = opencode.get("billing_source") if isinstance(opencode, dict) else None
    list_cost = opencode.get("list_cost") if isinstance(opencode, dict) else None
    allowance = opencode.get("allowance") if isinstance(opencode, dict) else None
    estimated = opencode.get("estimated") if isinstance(opencode, dict) else None
    tokens = opencode.get("tokens") if isinstance(opencode, dict) else None
    if tokens is None:
        tokens = {}

    # prompt/completion tokens fallback for reporting
    prompt_tokens = usage.get("prompt_tokens") or usage.get("promptTokenCount") or usage.get("input_tokens") or usage.get("inputTokens")
    completion_tokens = usage.get("completion_tokens") or usage.get("candidatesTokenCount") or usage.get("output_tokens") or usage.get("outputTokens")

    # Gemini has promptTokenCount / candidatesTokenCount already covered

    return {
        "cost": cost,
        "billing_source": billing_source,
        "list_cost": list_cost,
        "allowance": allowance,
        "estimated": estimated,
        "tokens": tokens or {},
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "raw_location": loc,
        "raw_usage": usage,
        "raw_opencode": opencode,
    }

def parse_stream_chunks(chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """For streaming chat completions, the last chunk carries usage with include_usage. Find it."""
    for chunk in reversed(chunks):
        if "usage" in chunk:
            return parse_usage(chunk)
        # Responses API: response.completed
        if chunk.get("type") == "response.completed" and "response" in chunk:
            return parse_usage(chunk["response"])
        # Anthropic message_delta
        if chunk.get("type") == "message_delta" and "usage" in chunk:
            return parse_usage(chunk)
        # Anthropic final?
        if "usage" in chunk.get("delta", {}) if isinstance(chunk.get("delta"), dict) else False:
            return parse_usage(chunk["delta"])
    return parse_usage({})

# ---------- logger class ----------
class OpencodeLogger:
    def __init__(
        self,
        db_path: Optional[str | Path] = "opencode_usage.db",
        csv_path: Optional[str | Path] = "usage_log.csv",
        api_key: Optional[str] = None,
        base_url: str = "https://opencode.ai/inference",
        provider_label: Optional[str] = None,
    ):
        import os
        load_dotenv()
        self.api_key = api_key or os.getenv("OPENCODE_API_KEY") or os.getenv("OPENCODE_API_TOKEN") or ""
        self.base_url = (base_url or os.getenv("OPENCODE_BASE_URL") or "https://opencode.ai/inference").rstrip("/")
        self.db_path = Path(db_path) if db_path else None
        self.csv_path = Path(csv_path) if csv_path else None
        self.provider_label = provider_label

        if self.db_path:
            self.conn = get_conn(self.db_path)
        else:
            self.conn = None  # type: ignore

        # ensure CSV header exists
        if self.csv_path:
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.csv_path.exists():
                with open(self.csv_path, "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow([
                        "timestamp","model","endpoint","billing_source","cost","list_cost","allowance",
                        "input_tokens","output_tokens","reasoning_tokens","cache_read","cache_write_5m","cache_write_1h",
                        "prompt_tokens","completion_tokens","estimated","status","request_id"
                    ])

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise ValueError("Missing OPENCODE_API_KEY (set env or pass api_key). Get from Console: Account > Personal API keys or Settings > Service accounts. Needs Models > Request permission. Service account keys default to All.")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _log_entry(self, model: str, endpoint: str, parsed: Dict[str, Any], status: str = "ok", error: Optional[str]=None, request_id: Optional[str]=None, raw_json: Optional[Dict]=None):
        ts = datetime.now(timezone.utc).isoformat()
        tokens = parsed.get("tokens") or {}
        entry = {
            "timestamp": ts,
            "model": model,
            "provider": self.provider_label or ("go" if "/inference/go" in self.base_url else "inference"),
            "endpoint": endpoint,
            "billing_source": parsed.get("billing_source"),
            "cost": parsed.get("cost"),
            "list_cost": parsed.get("list_cost"),
            "allowance": parsed.get("allowance"),
            "input_tokens": tokens.get("input"),
            "output_tokens": tokens.get("output"),
            "reasoning_tokens": tokens.get("reasoning"),
            "cache_read": tokens.get("cache_read"),
            "cache_write_5m": tokens.get("cache_write_5m"),
            "cache_write_1h": tokens.get("cache_write_1h"),
            "prompt_tokens": parsed.get("prompt_tokens"),
            "completion_tokens": parsed.get("completion_tokens"),
            "estimated": 1 if parsed.get("estimated") else 0,
            "status": status,
            "error": error,
            "request_id": request_id,
            "raw_json": json.dumps(raw_json or parsed.get("raw_usage") or {}, ensure_ascii=False)[:10000],
        }
        # DB
        if self.conn is not None:
            try:
                insert_request(self.conn, entry)
            except Exception as e:
                print(f"WARN DB insert failed: {e}")
        # CSV
        if self.csv_path:
            try:
                with open(self.csv_path, "a", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow([
                        entry["timestamp"], entry["model"], entry["endpoint"], entry["billing_source"],
                        entry["cost"], entry["list_cost"], entry["allowance"],
                        entry["input_tokens"], entry["output_tokens"], entry["reasoning_tokens"],
                        entry["cache_read"], entry["cache_write_5m"], entry["cache_write_1h"],
                        entry["prompt_tokens"], entry["completion_tokens"], entry["estimated"], entry["status"], entry["request_id"]
                    ])
            except Exception as e:
                print(f"WARN CSV append failed: {e}")

        # Human summary to stdout (key insight: cost vs allowance vs billing_source)
        # As per docs: cost==0 for free and Go within plan; allowance >0 for Go
        allowance_str = f" allowance=${parsed.get('allowance'):.5f}" if parsed.get("allowance") is not None else ""
        list_cost_str = f" list=${parsed.get('list_cost'):.5f}" if parsed.get("list_cost") is not None else ""
        estimated_str = " (estimated)" if parsed.get("estimated") else ""
        tokens_str = f" tokens in={tokens.get('input')} out={tokens.get('output')} cache_read={tokens.get('cache_read')}"
        print(f"-> logged {model} billing_source={parsed.get('billing_source')} cost=${parsed.get('cost')}{allowance_str}{list_cost_str}{estimated_str} {tokens_str} [{status}]")
        return entry

    # ----- endpoint helpers -----
    def _post(self, path: str, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        """Returns (json, request_id, error). Uses requests if available else urllib."""
        url = self.base_url + path
        headers = self._headers()
        data = json.dumps(payload).encode()
        request_id = None
        if requests is not None:
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=120)
                request_id = r.headers.get("x-request-id") or r.headers.get("request-id")
                if r.status_code >= 400:
                    # include 402 insufficient funds, 429 budget exceeded
                    err = f"HTTP {r.status_code}: {r.text[:1000]}"
                    try:
                        j = r.json()
                    except:
                        j = {"error": err, "status": r.status_code}
                    return j, request_id, err
                j = r.json()
                return j, request_id, None
            except Exception as e:
                return None, None, str(e)
        else:
            # urllib fallback
            import urllib.request, urllib.error
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = resp.read().decode()
                    request_id = resp.headers.get("x-request-id")
                    j = json.loads(body)
                    return j, request_id, None
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:2000] if hasattr(e, "read") else str(e)
                return {"error": body, "status": e.code}, None, f"HTTP {e.code}: {body[:1000]}"
            except Exception as e:
                return None, None, str(e)

    def chat_completions(
        self,
        model: str,
        messages: List[Dict[str, str]],
        stream: bool = False,
        max_tokens: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
        include_usage: bool = True,
    ) -> Dict[str, Any]:
        """
        POST /openai/v1/chat/completions (works for Go and non-Go).
        Logs usage.cost / allowance automatically.
        For streaming, set stream=True; we request stream_options.include_usage and parse final chunk.
        """
        path = "/openai/v1/chat/completions"
        # Determine base path: if user passed /inference/go we keep same path (Go routing adds /go prefix in base_url)
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if stream:
            payload["stream"] = True
            if include_usage:
                payload["stream_options"] = {"include_usage": True}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if extra:
            payload.update(extra)

        if stream:
            return self._chat_stream(path, payload, model)

        j, req_id, err = self._post(path, payload)
        if err or j is None:
            parsed = {"cost": None, "billing_source": None, "list_cost": None, "allowance": None, "tokens": {}, "prompt_tokens": None, "completion_tokens": None, "estimated": None}
            status = "error"
            # try to detect 402 vs 429 from err
            if err and "402" in err:
                status = "402_insufficient_funds"
            elif err and "429" in err:
                status = "429_budget_exceeded"
            self._log_entry(model, path, parsed, status=status, error=err, request_id=req_id, raw_json=j or {"error": err})
            raise RuntimeError(err or "unknown error")
        else:
            parsed = parse_usage(j)
            req_id = req_id or j.get("id") or j.get("request_id")
            self._log_entry(model, path, parsed, status="ok", request_id=req_id, raw_json=j)
            return j

    def _chat_stream(self, path: str, payload: Dict[str, Any], model: str) -> Dict[str, Any]:
        """Streaming via requests streaming SSE parse."""
        import os
        url = self.base_url + path
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        if requests is None:
            raise RuntimeError("Streaming requires 'requests' package. pip install requests")
        # Use requests stream
        chunks: List[Dict[str, Any]] = []
        try:
            with requests.post(url, headers=headers, json=payload, stream=True, timeout=180) as r:
                request_id = r.headers.get("x-request-id")
                if r.status_code >= 400:
                    text = r.text[:2000]
                    err = f"HTTP {r.status_code}: {text}"
                    parsed = {"cost": None, "billing_source": None, "list_cost": None, "allowance": None, "tokens": {}}
                    self._log_entry(model, path, parsed, status="error", error=err, request_id=request_id, raw_json={"error": text})
                    raise RuntimeError(err)
                # SSE: data: {...}\n
                buffer = ""
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data = line[5:]
                        if data == "[DONE]":
                            break
                        try:
                            j = json.loads(data)
                            chunks.append(j)
                        except:
                            continue
                # Last chunk should contain usage
                parsed = parse_stream_chunks(chunks) if chunks else parse_usage({})
                status = "ok" if parsed.get("cost") is not None else "ok_stream_no_usage"
                self._log_entry(model, path, parsed, status=status, request_id=request_id, raw_json={"chunks": chunks[-1] if chunks else {}})
                # Return aggregated fake response for caller convenience
                return {"chunks": chunks, "usage_parsed": parsed}
        except Exception as e:
            parsed = {"cost": None, "billing_source": None, "list_cost": None, "allowance": None, "tokens": {}}
            self._log_entry(model, path, parsed, status="error", error=str(e), raw_json={})
            raise

    def anthropic_messages(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 1024,
        system: Optional[str] = None,
        stream: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        path = "/anthropic/v1/messages"
        payload: Dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            payload["system"] = system
        if stream:
            payload["stream"] = True
        if extra:
            payload.update(extra)
        j, req_id, err = self._post(path, payload)
        if err or j is None:
            parsed = {"cost": None, "billing_source": None, "list_cost": None, "allowance": None, "tokens": {}}
            status = "402_insufficient_funds" if err and "402" in err else "429_budget_exceeded" if err and "429" in err else "error"
            self._log_entry(model, path, parsed, status=status, error=err, request_id=req_id, raw_json=j or {})
            raise RuntimeError(err or "unknown error")
        parsed = parse_usage(j)
        self._log_entry(model, path, parsed, status="ok", request_id=req_id or j.get("id"), raw_json=j)
        return j

    def responses(
        self,
        model: str,
        input_text: str | List[Dict[str, Any]],
        stream: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        path = "/openai/v1/responses"
        payload: Dict[str, Any] = {"model": model, "input": input_text}
        if stream:
            payload["stream"] = True
        if extra:
            payload.update(extra)
        j, req_id, err = self._post(path, payload)
        if err or j is None:
            parsed = {"cost": None, "billing_source": None, "list_cost": None, "allowance": None, "tokens": {}}
            self._log_entry(model, path, parsed, status="error", error=err, request_id=req_id, raw_json=j or {})
            raise RuntimeError(err or "unknown error")
        parsed = parse_usage(j)
        self._log_entry(model, path, parsed, status="ok", request_id=req_id or j.get("id"), raw_json=j)
        return j

    def gemini_generate(
        self,
        model: str,
        contents: List[Dict[str, Any]],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Gemini path is /google/v1beta/models/<model>:generateContent  or :streamGenerateContent
        path = f"/google/v1beta/models/{model}:generateContent"
        payload: Dict[str, Any] = {"contents": contents}
        if extra:
            payload.update(extra)
        j, req_id, err = self._post(path, payload)
        if err or j is None:
            parsed = {"cost": None, "billing_source": None, "list_cost": None, "allowance": None, "tokens": {}}
            self._log_entry(model, path, parsed, status="error", error=err, request_id=req_id, raw_json=j or {})
            raise RuntimeError(err or "unknown error")
        parsed = parse_usage(j)
        self._log_entry(model, path, parsed, status="ok", request_id=req_id, raw_json=j)
        return j

# CLI single-call helper
def call_once(args):
    import os
    load_dotenv()
    api_key = args.api_key or os.getenv("OPENCODE_API_KEY")
    base_url = args.base_url or os.getenv("OPENCODE_BASE_URL") or "https://opencode.ai/inference"
    logger = OpencodeLogger(db_path=args.db, csv_path=args.csv, api_key=api_key, base_url=base_url)
    # Dispatch by API family
    if args.api == "anthropic":
        msgs = [{"role": "user", "content": args.message}]
        return logger.anthropic_messages(model=args.model, messages=msgs, max_tokens=args.max_tokens or 1024)
    elif args.api == "responses":
        return logger.responses(model=args.model, input_text=args.message)
    elif args.api == "gemini":
        return logger.gemini_generate(model=args.model, contents=[{"parts":[{"text": args.message}]}])
    else:  # openai chat
        msgs = [{"role": "user", "content": args.message}]
        if args.system:
            msgs.insert(0, {"role": "system", "content": args.system})
        return logger.chat_completions(model=args.model, messages=msgs, max_tokens=args.max_tokens, stream=args.stream)
