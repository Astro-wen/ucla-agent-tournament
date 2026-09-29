"""OpenAI-compatible chat client with a hard spend limit (Nebius Token Factory by default), for LOCAL evaluation.

    python -m local.llm_client status            # key present/absent, model, budget left (never prints the key)
    python -m local.llm_client models            # GET /models
    python -m local.llm_client ping              # one short completion (a few hundred tokens, logged in the ledger)
    python -m local.llm_client ledger [--run ID] # spend so far, per run

Agent-independent: only httpx, python-dotenv and the standard library, no `local.*` / `agent.*` imports, so the
deployed agent can reuse this file as agent/llm.py later. It never raises for a missing / unusable key or an
exhausted budget at construction: `client.available` is False with a reason, and `chat` / `complete_json` raise
`LLMUnavailable` (a subclass of `LLMError`), which callers turn into their rule-based fallback. Every failure of a
request (HTTP status, timeout, transport, malformed body, a reply off the schema) surfaces as `LLMError`; a cache
that fails is a miss, a ledger that fails refuses.

Configuration: the environment first, then NEBIUS_* keys (and only those) from a .env file -- $NEBIUS_DOTENV, else
the .env of the checkout this file lives in. The key only ever goes into the Authorization header: never into repr,
logs, the ledger, the cache, exceptions or test output (error bodies and HTTP-library errors can quote the key or the
request, so only HTTP status codes and exception type names are reported, and none is chained).
  NEBIUS_API_KEY     required for real calls (absent -> available False, reason 'no NEBIUS_API_KEY'; one that is
                     not a valid header value -> unavailable too, without saying why in terms of its content)
  NEBIUS_BASE_URL    default https://api.tokenfactory.nebius.com/v1/ (any OpenAI-compatible /chat/completions)
  NEBIUS_MODEL       default Qwen/Qwen3-30B-A3B-Instruct-2507 (see DEFAULT_MODEL)
  NEBIUS_BUDGET_USD  global budget over the whole ledger, default 5.0 (not a finite number -> the default; < 0 -> 0):
                     estimated spend + the next request's worst case above it -> refused
  NEBIUS_MAX_CALLS   per-run cap on real (uncached) request attempts, retries included, default 2000
  NEBIUS_MAX_TOKENS  per-run cap on prompt + completion tokens, default 3,000,000
  NEBIUS_RUN_ID      the run the per-run caps count (shared by a run's worker processes); default: one per process
  NEBIUS_LEDGER      spend ledger, default ~/.cache/altruagent/llm_usage.jsonl (outside the repo; JSON lines with
                     time, run, model, tag, request id, tokens, estimated cost, latency, status -- no key, no prompt,
                     no answer). A ledger that cannot be read or written refuses every request (fail closed).
  NEBIUS_CACHE       sqlite response cache for identical requests, default ~/.cache/altruagent/llm_cache.sqlite;
                     0 / off disables it. A hit costs nothing and replays the stored answer (local evaluation only:
                     the same prompt always gets the same answer, even when the model samples)
  NEBIUS_TIMEOUT     seconds per request, default 60 (examples/llm/providers.py REQUEST_TIMEOUT_SECONDS)

Costs are ESTIMATES from PRICES (USD per 1M tokens) times the token counts the server reports; an unknown model is
charged the conservative UNKNOWN_PRICE. Each request attempt (retries included) first RESERVES its worst case
(estimated prompt + max_tokens) in the shared ledger: the caps are checked against everything already there and the
reservation appended under one exclusive lock, so parallel workers cannot all pass the same check. The final line
replaces the reservation with the reported usage. An attempt whose outcome is unknown -- a timeout or a connection
dropped after sending, a 502 / 504, a 200 with an unreadable body -- stays charged at the worst case, since the
provider may still bill it; a 200 without usage is charged the estimated prompt plus its text; an attempt that never
left (connection refused) or was rejected (4xx, 429, 500, 503) is charged nothing. Streaming is never requested and
max_tokens is always sent, so no reply can cost more than its reservation's completion part.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

try:
    import fcntl
except ImportError:                                   # pragma: no cover - Windows: no cross-process lock
    fcntl = None

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
# Cheapest instruct model on Token Factory that answers strict JSON schemas quickly without a thinking phase (probe
# 2026-09-28: json_schema accepted, ~1 s for a short answer). Same tier as the gpt-4o-mini that examples/llm_agent.py
# defaults to, so it stands in for "the organizers' generic LLM agent". $0.10 / $0.30 per 1M tokens.
DEFAULT_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
DEFAULT_BUDGET_USD = 5.0
DEFAULT_MAX_CALLS = 2000
DEFAULT_MAX_TOKENS = 3_000_000
DEFAULT_TIMEOUT = 60.0
CACHE_DIR = Path.home() / ".cache" / "altruagent"
DEFAULT_LEDGER = CACHE_DIR / "llm_usage.jsonl"
DEFAULT_CACHE = CACHE_DIR / "llm_cache.sqlite"
ENV_KEYS = ("NEBIUS_API_KEY", "NEBIUS_BASE_URL", "NEBIUS_MODEL", "NEBIUS_BUDGET_USD", "NEBIUS_MAX_CALLS",
            "NEBIUS_MAX_TOKENS", "NEBIUS_RUN_ID", "NEBIUS_LEDGER", "NEBIUS_CACHE", "NEBIUS_TIMEOUT")

# USD per 1M tokens (input, output), Nebius Token Factory list prices as of 2026-09-28. Nebius's own price page
# (tokenfactory.nebius.com/organization/prices) renders client-side only, so these are from the copy in the Mastra
# provider docs (mastra.ai/models/providers/nebius), which lists the same model ids as GET /models; OpenRouter's
# Nebius listing agrees for Qwen3-30B-A3B-Instruct-2507 ($0.10 / $0.30) and gpt-oss-120b ($0.15 / $0.60).
PRICES = {
    "deepseek-ai/DeepSeek-V4-Flash-0731": (0.14, 0.28),
    "deepseek-ai/DeepSeek-V4-Pro": (2.0, 4.0),
    "deepseek-ai/DeepSeek-V4-Pro-0813": (1.0, 4.0),
    "deepseek-ai/DeepSeek-V4.1-Flash": (0.30, 1.0),
    "google/gemma-3-27b-it": (0.10, 0.30),
    "MiniMaxAI/MiniMax-M3": (0.30, 1.0),
    "moonshotai/Kimi-K2.7-Code": (0.95, 4.0),
    "moonshotai/Kimi-K3": (3.0, 15.0),
    "NousResearch/Hermes-4-405B": (1.0, 3.0),
    "nvidia/Nemotron-3_5-Lightning": (0.06, 0.24),
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1.0, 3.0),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "Qwen/Qwen3-235B-A22B-Instruct-2507": (0.20, 0.60),
    "Qwen/Qwen3-30B-A3B-Instruct-2507": (0.10, 0.30),
    "Qwen/Qwen3.5-397B-A17B": (0.60, 4.0),
    "zai-org/GLM-5.2": (1.0, 4.0),
    "zai-org/GLM-5.3": (1.0, 4.0),
    "zai-org/GLM-5.3-Flash": (0.15, 0.50),
}
# Models without a known price (e.g. Kimi-K2.6, GLM-5.1, Qwen3.8-27B) are charged the most expensive listed rate.
UNKNOWN_PRICE = (3.0, 15.0)
BYTES_PER_TOKEN = 3.0          # pre-call prompt estimate from UTF-8 bytes: English / JSON is ~3.5-4 bytes a token,
MSG_OVERHEAD_TOKENS = 8        # CJK ~3 bytes for 1+ token; plus the chat template's tokens per message
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
MAYBE_BILLED_STATUS = frozenset({502, 504})     # a gateway gave up waiting: the model may still have run (and billed)
MAX_RETRY_AFTER_S = 10.0
MAX_PARSE_CHARS = 200_000
# httpx errors raised before the request left this machine (nothing can have been billed)
_UNSENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.LocalProtocolError,
           httpx.UnsupportedProtocol)


class LLMError(RuntimeError):
    """A request failed (after its retries) or the reply was unusable. Messages never contain the key."""


class LLMUnavailable(LLMError):
    """No usable key, or a spend cap would be exceeded, or the spend ledger cannot be used: the client refuses
    without calling; fall back to rules."""


def known_price(model: str) -> tuple[float, float] | None:
    """The listed price of `model` (exact id, or the same name ignoring case / the org prefix), or None."""
    if model in PRICES:
        return PRICES[model]
    tail = {k.split("/")[-1].lower(): v for k, v in PRICES.items()}
    return tail.get(str(model).split("/")[-1].lower())


def price_of(model: str) -> tuple[float, float]:
    return known_price(model) or UNKNOWN_PRICE


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = price_of(model)
    return (prompt_tokens * pin + completion_tokens * pout) / 1e6


# ------------------------------------------------------------------------------------------ ledger
def _zero() -> dict:
    return {"calls": 0, "tokens": 0, "usd": 0.0}


def _nonneg(v, cast):
    """A finite number >= 0 from a ledger / usage field, else 0 (a damaged line must never crash the budget check)."""
    try:
        x = cast(v)
    except (TypeError, ValueError, OverflowError):
        return cast(0)
    if isinstance(x, float) and not math.isfinite(x):
        return cast(0)
    return x if x >= 0 else cast(0)


class Ledger:
    """JSONL of request attempts, shared by processes (path None: kept in this process only).

    Each attempt is a 'reserved' line written before it is sent -- by `reserve`, which checks the caps against the
    whole file and appends under one exclusive lock -- then a final line with the same `rid` and the actual usage,
    which replaces the reservation. A reservation without its final line (the process died mid-request) stays
    counted at its worst case. `refresh()` reads what others appended since the last read; `totals` is the whole
    ledger, `runs[run_id]` = {'calls', 'tokens', 'usd'}, open reservations included; `pending` = open reservations."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path).expanduser() if path else None
        self._lock = threading.RLock()
        self._offset = 0
        self.totals, self.runs, self.pending = _zero(), {}, {}

    def _acc(self, run: str, tokens: int, usd: float, sign: int = 1) -> None:
        for acc in (self.totals, self.runs.setdefault(run, _zero())):
            acc["calls"] += sign
            acc["tokens"] += sign * tokens
            acc["usd"] += sign * usd

    def _add(self, rec: dict) -> None:
        run, rid = str(rec.get("run")), rec.get("rid")
        tokens = _nonneg(rec.get("prompt_tokens") or 0, int) + _nonneg(rec.get("completion_tokens") or 0, int)
        usd = _nonneg(rec.get("cost_usd") or 0.0, float)
        if rec.get("status") == "reserved":
            if isinstance(rid, str):
                self.pending[rid] = (run, tokens, usd)
            self._acc(run, tokens, usd)
            return
        if isinstance(rid, str) and rid in self.pending:
            self._acc(*self.pending.pop(rid), sign=-1)
        self._acc(run, tokens, usd)

    def refresh(self) -> None:
        """Read new complete lines. Raises OSError when the ledger exists but cannot be read."""
        if self.path is None:
            return
        with self._lock:
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size < self._offset:                  # truncated / replaced: start over
                self._offset = 0
                self.totals, self.runs, self.pending = _zero(), {}, {}
            if size == self._offset:
                return
            with open(self.path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
            end = chunk.rfind(b"\n") + 1             # a half-written last line is read next time
            for line in chunk[:end].splitlines():
                try:
                    rec = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(rec, dict):
                    self._add(rec)
            self._offset += end

    def _write(self, rec: dict, check=None) -> str | None:
        """Append `rec` under the exclusive lock; with `check`, only if check(self) -- run on the refreshed ledger,
        inside the lock -- returns None, else that refusal reason (and nothing is written). Raises OSError."""
        with self._lock:
            if self.path is None:
                why = check(self) if check is not None else None
                if why is None:
                    self._add(rec)
                return why
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            with open(self.path, "ab") as f:
                if fcntl is not None:
                    fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    why = None
                    if check is not None:
                        self.refresh()
                        why = check(self)
                    if why is None:
                        f.write(line)
                        f.flush()
                finally:
                    if fcntl is not None:
                        fcntl.flock(f, fcntl.LOCK_UN)
            self.refresh()
            return why

    def reserve(self, rec: dict, check) -> str | None:
        """Atomically check the caps and append a reservation (see the class docstring). Raises OSError."""
        return self._write({**rec, "status": "reserved"}, check)

    def append(self, rec: dict) -> None:
        self._write(rec)

    def run(self, run_id: str) -> dict:
        return dict(self.runs.get(run_id) or _zero())


# ------------------------------------------------------------------------------------------ cache
class ResponseCache:
    """sqlite: request hash -> (text, token counts). Safe across processes (sqlite's own locking). Holds answers only:
    the key is a hash of the request, so neither the prompt nor the API key is stored."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, model TEXT, text TEXT, "
                             "prompt_tokens INTEGER, completion_tokens INTEGER, created REAL)")
            self._db.commit()

    def get(self, key: str):
        """(text, prompt_tokens, completion_tokens) or None; a cache that fails (locked, damaged) is a miss."""
        try:
            with self._lock:
                row = self._db.execute("SELECT text, prompt_tokens, completion_tokens FROM cache WHERE key = ?",
                                       (key,)).fetchone()
        except Exception:                              # noqa: BLE001 - sqlite3.Error and friends: just a miss
            return None
        return row if row is not None and isinstance(row[0], str) else None

    def put(self, key: str, model: str, text: str, prompt_tokens: int, completion_tokens: int) -> None:
        try:
            with self._lock:
                self._db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?, ?, ?)",
                                 (key, model, text, prompt_tokens, completion_tokens, time.time()))
                self._db.commit()
        except Exception:                              # noqa: BLE001 - caching is best effort
            pass


# ------------------------------------------------------------------------------------------ the client
@dataclass
class Completion:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    cached: bool = False
    finish_reason: str | None = None


@dataclass
class Stats:
    calls: int = 0                 # real requests that returned 200
    cache_hits: int = 0
    errors: int = 0                # requests that failed (each attempt)
    timeouts: int = 0              # ... of which timed out (NEBIUS_TIMEOUT, default 60 s; not retried)
    refused: int = 0               # LLMUnavailable raised (no key / caps / ledger)
    json_errors: int = 0           # 200 replies that were not the JSON object asked for (or off its schema)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latencies: list = field(default_factory=list)

    def as_dict(self) -> dict:
        lat = sorted(self.latencies)
        pct = lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))], 3) if lat else None   # noqa: E731
        return {"calls": self.calls, "cache_hits": self.cache_hits, "errors": self.errors, "timeouts": self.timeouts,
                "refused": self.refused, "json_errors": self.json_errors, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "cost_usd": round(self.cost_usd, 6),
                "latency_p50_s": pct(0.5), "latency_p95_s": pct(0.95), "latency_max_s": pct(1.0)}


def _read_dotenv(path: str | Path | None) -> dict:
    """Only the NEBIUS_* keys of a .env file (never the agent's other secrets), or {}."""
    if path is None:
        return {}
    p = Path(path).expanduser()
    if not p.is_file():
        return {}
    try:
        from dotenv import dotenv_values
        values = dotenv_values(p)
    except Exception:                                  # noqa: BLE001 - an unreadable .env is just "no config"
        return {}
    return {k: v for k, v in values.items() if k in ENV_KEYS and v}


def default_dotenv() -> Path:
    return Path(os.environ.get("NEBIUS_DOTENV") or Path(__file__).resolve().parents[1] / ".env")


def _num(v, cast, default):
    try:
        return cast(v) if v not in (None, "") else default
    except (TypeError, ValueError, OverflowError):
        return default


def _finite(v, default: float, *, low: float = 0.0, positive: bool = False) -> float:
    """A finite float >= low (> 0 when positive), else `default` for NaN / inf / junk and `low` below the range."""
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(x) or (positive and x <= 0):
        return default
    return max(low, x)


def _count(v) -> int | None:
    """A token count as the server reported it (a non-negative int / integral float), else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float) and math.isfinite(v) and v >= 0:
        return int(v)
    return None


def _parse_completion(resp) -> tuple[str, tuple[int, int] | None, str | None] | None:
    """(text, (prompt_tokens, completion_tokens) or None when usage is missing / unusable, finish_reason) of a 200
    chat completion, or None when the body has no message. Never raises."""
    try:
        data = resp.json()
        choice = data["choices"][0]
        content = choice["message"].get("content")
        finish = choice.get("finish_reason")
        usage = data.get("usage")
    except Exception:                                  # noqa: BLE001 - ValueError, KeyError, TypeError, ... = no message
        return None
    text = content if isinstance(content, str) else ""
    pt = ct = None
    if isinstance(usage, dict):
        pt, ct = _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))
    counts = (pt, ct) if pt is not None and ct is not None and (pt or ct) else None
    return text, counts, finish if isinstance(finish, str) else None


class LLMClient:
    """One OpenAI-compatible endpoint + model, with retries, a response cache and hard spend caps."""

    def __init__(self, api_key: str | None, *, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = DEFAULT_TIMEOUT, retries: int = 2, backoff: float = 1.0, retry_timeouts: bool = False,
                 budget_usd: float = DEFAULT_BUDGET_USD, max_calls: int = DEFAULT_MAX_CALLS,
                 max_tokens: int = DEFAULT_MAX_TOKENS, run_id: str | None = None,
                 ledger_path: str | Path | None = DEFAULT_LEDGER, cache_path: str | Path | None = DEFAULT_CACHE,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.model, self.base_url = str(model), str(base_url).rstrip("/") + "/"
        self.timeout = _finite(timeout, DEFAULT_TIMEOUT, positive=True)
        self.retries, self.backoff, self.retry_timeouts = max(0, int(retries)), backoff, retry_timeouts
        self.budget_usd = _finite(budget_usd, DEFAULT_BUDGET_USD)
        self.max_calls, self.max_tokens = max(0, int(max_calls)), max(0, int(max_tokens))
        self.run_id = run_id or f"{os.uname().nodename if hasattr(os, 'uname') else 'host'}-{os.getpid()}-" \
                                f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.ledger = Ledger(ledger_path)
        self.cache = None
        if cache_path:
            try:
                self.cache = ResponseCache(cache_path)
            except Exception:                          # noqa: BLE001 - no cache is fine
                self.cache = None
        self.stats = Stats()
        self._sleep = sleep
        self._json_modes: dict[str, str] = {}          # schema name -> the first JSON mode the endpoint accepted
        self._lock = threading.Lock()
        self._http = None
        key = api_key.strip() if isinstance(api_key, str) else ""
        self.key_state = "present" if key else "absent"
        self._no_http = "no NEBIUS_API_KEY"
        if key:
            try:
                self._http = httpx.Client(base_url=self.base_url, headers={"Authorization": f"Bearer {key}"},
                                          timeout=self.timeout, transport=transport)
            except Exception:                          # noqa: BLE001 - its message could quote the key: dropped
                self._http = None
            if self._http is None:
                self.key_state = "unusable"
                self._no_http = "NEBIUS_API_KEY or NEBIUS_BASE_URL unusable (not a valid header value / URL)"

    @classmethod
    def from_env(cls, *, dotenv: str | Path | None | bool = True, environ: dict | None = None, **overrides):
        """Build from NEBIUS_* (environment first, then the .env; see the module docstring). Never raises."""
        env = dict(_read_dotenv(default_dotenv() if dotenv is True else (dotenv or None)))
        src = os.environ if environ is None else environ
        env.update({k: v for k, v in src.items() if k in ENV_KEYS and v})
        cache = env.get("NEBIUS_CACHE")
        kw = dict(model=env.get("NEBIUS_MODEL") or DEFAULT_MODEL,
                  base_url=env.get("NEBIUS_BASE_URL") or DEFAULT_BASE_URL,
                  timeout=_num(env.get("NEBIUS_TIMEOUT"), float, DEFAULT_TIMEOUT),
                  budget_usd=_num(env.get("NEBIUS_BUDGET_USD"), float, DEFAULT_BUDGET_USD),
                  max_calls=_num(env.get("NEBIUS_MAX_CALLS"), int, DEFAULT_MAX_CALLS),
                  max_tokens=_num(env.get("NEBIUS_MAX_TOKENS"), int, DEFAULT_MAX_TOKENS),
                  run_id=env.get("NEBIUS_RUN_ID") or None,
                  ledger_path=env.get("NEBIUS_LEDGER") or DEFAULT_LEDGER,
                  cache_path=None if (cache or "").lower() in ("0", "off", "no", "false") else (cache or DEFAULT_CACHE))
        kw.update(overrides)
        return cls(env.get("NEBIUS_API_KEY"), **kw)

    def __repr__(self) -> str:
        return f"LLMClient(model={self.model!r}, base_url={self.base_url!r}, key={self.key_state})"

    # ---- caps ------------------------------------------------------------------------------------------------
    def spent(self) -> dict:
        """{'total_usd', 'run_calls', 'run_tokens', 'run_usd'} from the shared ledger (open reservations included)."""
        try:
            self.ledger.refresh()
        except OSError:
            pass
        run = self.ledger.run(self.run_id)
        return {"total_usd": self.ledger.totals["usd"], "run_calls": run["calls"], "run_tokens": run["tokens"],
                "run_usd": run["usd"]}

    def _why(self, led: Ledger, est_usd: float, est_tokens: int) -> str | None:
        total = led.totals["usd"]
        if total + est_usd > self.budget_usd:
            return (f"budget: ${total:.4f} spent + ${est_usd:.4f} for this call > NEBIUS_BUDGET_USD "
                    f"${self.budget_usd:.2f}")
        run = led.run(self.run_id)
        if run["calls"] + 1 > self.max_calls:
            return f"run cap: {run['calls']} calls = NEBIUS_MAX_CALLS {self.max_calls}"
        if run["tokens"] + est_tokens > self.max_tokens:
            return f"run cap: {run['tokens']} + {est_tokens} tokens > NEBIUS_MAX_TOKENS {self.max_tokens}"
        return None

    def refusal(self, est_usd: float = 0.0, est_tokens: int = 0) -> str | None:
        """Why the next request (of this estimated size) would be refused, or None."""
        if self._http is None:
            return self._no_http
        try:
            self.ledger.refresh()
        except OSError as exc:
            return f"spend ledger unreadable ({type(exc).__name__}): the budget cannot be checked"
        return self._why(self.ledger, est_usd, est_tokens)

    @property
    def available(self) -> bool:
        return self.refusal() is None

    @property
    def unavailable_reason(self) -> str | None:
        return self.refusal()

    def estimate(self, messages: list[dict], max_tokens: int) -> tuple[int, int, float]:
        """Worst case of one request: (estimated prompt tokens, prompt + max_tokens, USD)."""
        size = len(json.dumps(messages, ensure_ascii=False, default=str).encode("utf-8"))
        prompt = int(size / BYTES_PER_TOKEN) + 16 + MSG_OVERHEAD_TOKENS * len(messages)
        return prompt, prompt + int(max_tokens), cost_usd(self.model, prompt, int(max_tokens))

    # ---- requests ----------------------------------------------------------------------------------------------
    def _cache_key(self, body: dict) -> str:
        return hashlib.sha256(json.dumps({"base": self.base_url, **body}, sort_keys=True, ensure_ascii=False,
                                         default=str).encode()).hexdigest()

    def _refuse(self, why: str):
        with self._lock:
            self.stats.refused += 1
        return LLMUnavailable(why)

    def chat(self, messages: list[dict], *, max_tokens: int = 512, temperature: float | None = None,
             response_format: dict | None = None, tag: str = "", use_cache: bool = True) -> Completion:
        """One chat completion. Raises LLMUnavailable (refused, nothing sent) or LLMError (failed after retries)."""
        max_tokens = max(1, int(max_tokens))
        body: dict = {"model": self.model, "messages": messages, "max_tokens": max_tokens}    # never "stream"
        if temperature is not None:
            body["temperature"] = float(temperature)
        if response_format is not None:
            body["response_format"] = response_format
        est, key = None, None
        try:
            key = self._cache_key(body) if (self.cache is not None and use_cache and self._http is not None) else None
            est = self.estimate(messages, max_tokens)
        except (TypeError, ValueError, RecursionError):
            est = None
        if est is None:
            raise LLMError("the messages cannot be sent as JSON")
        est_prompt, est_tokens, est_usd = est
        if key is not None:
            hit = self.cache.get(key)
            if hit is not None:
                with self._lock:
                    self.stats.cache_hits += 1
                return Completion(hit[0], self.model, _nonneg(hit[1] or 0, int), _nonneg(hit[2] or 0, int), 0.0, 0.0,
                                  cached=True)
        if self._http is None:
            raise self._refuse(self._no_http)
        attempt = 0
        while True:
            base = {"t": round(time.time(), 3), "run": self.run_id, "model": self.model, "tag": tag,
                    "rid": uuid.uuid4().hex}
            try:
                why = self.ledger.reserve({**base, "prompt_tokens": est_prompt, "completion_tokens": max_tokens,
                                           "cost_usd": round(est_usd, 8)},
                                          lambda led: self._why(led, est_usd, est_tokens))
            except OSError as exc:
                why = f"spend ledger not writable ({type(exc).__name__}): the budget cannot be enforced"
            if why is not None:
                raise self._refuse(why)
            t0 = time.perf_counter()
            resp, status, err, maybe_billed, retryable = None, None, None, False, False
            try:
                resp = self._http.post("chat/completions", json=body)
                status = resp.status_code
            except httpx.TimeoutException as exc:
                err, maybe_billed, retryable = f"timeout ({type(exc).__name__})", not isinstance(exc, _UNSENT), \
                    self.retry_timeouts
            except (httpx.LocalProtocolError, httpx.UnsupportedProtocol) as exc:
                err = f"client ({type(exc).__name__})"
            except httpx.TransportError as exc:
                err, maybe_billed, retryable = f"transport ({type(exc).__name__})", not isinstance(exc, _UNSENT), True
            except Exception as exc:                   # noqa: BLE001 - httpx's own errors may quote the request
                err = f"client ({type(exc).__name__})"
            latency = time.perf_counter() - t0
            if status == 200:
                parsed = _parse_completion(resp)
                if parsed is None:                     # the model ran, but nothing usable came back
                    self._settle(base, est_prompt, max_tokens, latency, "bad_body", est_usd, estimated=True)
                    with self._lock:
                        self.stats.errors += 1
                    raise LLMError("the response had no message")
                text, counts, finish = parsed
                pt, ct = counts if counts is not None else (est_prompt, len(text.encode("utf-8")) // 3 + 1)
                usd = cost_usd(self.model, pt, ct)
                self._settle(base, pt, ct, latency, "ok", usd, estimated=counts is None)
                with self._lock:
                    self.stats.calls += 1
                    self.stats.prompt_tokens += pt
                    self.stats.completion_tokens += ct
                    self.stats.cost_usd += usd
                    self.stats.latencies.append(latency)
                if key is not None:
                    self.cache.put(key, self.model, text, pt, ct)
                return Completion(text, self.model, pt, ct, usd, latency, False, finish)
            maybe_billed = maybe_billed or status in MAYBE_BILLED_STATUS
            self._settle(base, est_prompt if maybe_billed else 0, max_tokens if maybe_billed else 0, latency,
                         f"http_{status}" if status is not None else err, est_usd if maybe_billed else 0.0,
                         estimated=maybe_billed)
            with self._lock:
                self.stats.errors += 1
                self.stats.timeouts += bool(err and err.startswith("timeout"))
            retry_after = None
            if status is not None:
                retry_after, retryable, reason = _retry_after(resp), status in RETRY_STATUS, f"HTTP {status}"
            else:
                reason = err
            if not retryable or attempt >= self.retries:
                raise LLMError(f"request failed ({reason})")
            attempt += 1
            self._sleep(min(MAX_RETRY_AFTER_S, retry_after if retry_after is not None
                            else self.backoff * 2 ** (attempt - 1)))

    def _settle(self, base: dict, pt: int, ct: int, latency: float, status: str, usd: float,
                estimated: bool = False) -> None:
        """The attempt's final ledger line (replaces its reservation). If it cannot be written the reservation stays,
        i.e. the attempt stays charged at its worst case."""
        rec = {**base, "t": round(time.time(), 3), "prompt_tokens": int(pt), "completion_tokens": int(ct),
               "cost_usd": round(usd, 8), "latency_s": round(latency, 3), "status": status}
        if estimated:
            rec["estimated"] = True
        try:
            self.ledger.append(rec)
        except OSError:
            pass

    def complete_json(self, messages: list[dict], *, schema: dict | None = None, schema_name: str = "answer",
                      max_tokens: int = 512, temperature: float | None = None, tag: str = "",
                      check_schema: bool = True) -> tuple[dict, Completion]:
        """A JSON object answer. Tries strict json_schema, then json_object, then plain instructions; the first mode
        the endpoint accepts is remembered per schema name (one schema it rejects doesn't downgrade the others).
        With check_schema, a reply that does not conform to `schema` (types, enums, required / extra keys) is an
        LLMError -- what a strict json_schema endpoint guarantees, applied to the fallback modes too. Raises LLMError
        (LLMUnavailable when refused). Legality against the game's options stays the caller's job."""
        mode_key = schema_name if schema else ""
        modes = (["json_schema"] if schema else []) + ["json_object", "prompt"]
        known = self._json_modes.get(mode_key)
        if known in modes:                             # start from the mode that worked (no repeated 400s)
            modes = modes[modes.index(known):]
        last: LLMError | None = None
        for mode in modes:
            msgs, fmt = list(messages), None
            if mode == "json_schema":
                fmt = {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
            else:
                hint = "Reply with one JSON object only, no other text."
                if schema is not None:
                    hint += " It must match this JSON schema: " + json.dumps(schema, separators=(",", ":"))
                msgs = msgs + [{"role": "system", "content": hint}]
                if mode == "json_object":
                    fmt = {"type": "json_object"}
            try:
                comp = self.chat(msgs, max_tokens=max_tokens, temperature=temperature, response_format=fmt, tag=tag)
            except LLMUnavailable:
                raise
            except LLMError as exc:
                last = exc
                if "HTTP 400" in str(exc) or "HTTP 422" in str(exc):   # this response_format is not supported
                    continue
                raise
            self._json_modes[mode_key] = mode
            data = parse_json_object(comp.text)
            problem = "the reply was not a JSON object" if data is None else \
                (schema_problem(data, schema) if schema is not None and check_schema else None)
            if problem is not None:
                with self._lock:
                    self.stats.json_errors += 1
                raise LLMError(problem if data is None else f"the reply did not match the schema: {problem}")
            return data, comp
        raise last or LLMError("no JSON mode accepted")

    def list_models(self) -> list[str]:
        if self._http is None:
            raise LLMUnavailable(self._no_http)
        resp, err = None, None
        try:
            resp = self._http.get("models")
        except Exception as exc:                       # noqa: BLE001 - reported by type only, never chained
            err = type(exc).__name__
        if err is not None:
            raise LLMError(f"request failed ({err})")
        if resp.status_code != 200:
            raise LLMError(f"request failed (HTTP {resp.status_code})")
        try:
            data = resp.json()
        except ValueError:
            data = None
        items = data.get("data") if isinstance(data, dict) else data
        return [m.get("id") for m in (items if isinstance(items, list) else [])
                if isinstance(m, dict) and isinstance(m.get("id"), str)]

    def close(self) -> None:
        if self._http is not None:
            self._http.close()


def _retry_after(resp) -> float | None:
    try:
        v = resp.headers.get("retry-after")
        x = float(v) if v is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, x) if x is not None and math.isfinite(x) else None


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def parse_json_object(text: str) -> dict | None:
    """The first JSON object in a reply (tolerates <think> blocks, ``` fences and prose around it), or None. Never
    raises, whatever the model (or text it was shown) makes it write."""
    if not isinstance(text, str):
        return None
    text = _THINK.sub("", text[:MAX_PARSE_CHARS])
    low = text.lower()
    if "</think>" in low:                              # a template that opened the thinking block itself
        text = text[low.rfind("</think>") + len("</think>"):]
        low = text.lower()
    if "<think>" in low:                               # thinking cut off by max_tokens: no answer follows
        text = text[:low.find("<think>")]
    text = text.strip()
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text, i)
        except (ValueError, RecursionError):
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            return obj
        i = text.find("{", i + 1)
    return None


_JSON_TYPES = {"object": dict, "array": list, "string": str, "null": type(None)}


def _is_type(v, t) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "boolean":
        return isinstance(v, bool)
    py = _JSON_TYPES.get(t)
    return True if py is None else isinstance(v, py)   # a keyword we don't know: not ours to reject


def schema_problem(value, schema, path: str = "answer") -> str | None:
    """Why `value` does not conform to `schema`, or None. The subset strict structured output uses: type (one or a
    list), enum, properties / required / additionalProperties: false, array items."""
    if not isinstance(schema, dict):
        return None
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else list(types)
        if not any(_is_type(value, t) for t in types):
            return f"{path} must be {' or '.join(map(str, types))}, got {type(value).__name__}"
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(type(value) is type(e) and value == e for e in enum):
        return f"{path} must be one of {enum[:20]}"
    if isinstance(value, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for k in schema.get("required") or []:
            if k not in value:
                return f"{path}.{k} is missing"
        if schema.get("additionalProperties") is False:
            extra = [str(k)[:40] for k in value if k not in props]
            if extra:
                return f"{path} has keys the schema does not allow: {extra[:3]}"
        for k, sub in props.items():
            if k in value:
                p = schema_problem(value[k], sub, f"{path}.{k}")
                if p:
                    return p
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            p = schema_problem(item, schema["items"], f"{path}[{i}]")
            if p:
                return p
    return None


# ------------------------------------------------------------------------------------------ one client per process
_SHARED: dict = {}


def shared_client(**overrides) -> LLMClient:
    """The process-wide client from the environment (so one run's caps and stats are in one place). Never raises: if
    the configuration cannot be read at all, a keyless client (everything refused -> rule fallback)."""
    key = tuple(sorted(overrides.items()))
    c = _SHARED.get(key)
    if c is None:
        try:
            c = LLMClient.from_env(**overrides)
        except Exception:                              # noqa: BLE001 - never crash a game over the configuration
            c = LLMClient(None, ledger_path=None, cache_path=None)
        _SHARED[key] = c
    return c


# ------------------------------------------------------------------------------------------ CLI
def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nebius / OpenAI-compatible client: status, models, ping, ledger")
    ap.add_argument("cmd", choices=("status", "models", "ping", "ledger"))
    ap.add_argument("--run", help="ledger: only this run id")
    ap.add_argument("--model", help="override NEBIUS_MODEL for this command")
    a = ap.parse_args(argv)
    kw = {"model": a.model} if a.model else {}
    c = LLMClient.from_env(**kw)
    if a.cmd == "status":
        s = c.spent()
        print(f"key: {c.key_state} | model {c.model} | base {c.base_url}")
        print(f"ledger {c.ledger.path}: ${s['total_usd']:.4f} spent of ${c.budget_usd:.2f} budget "
              f"({len(c.ledger.pending)} open reservations) | per-run caps {c.max_calls} calls, {c.max_tokens} "
              f"tokens | cache {c.cache.path if c.cache else 'off'}")
        print("available" if c.available else f"unavailable: {c.unavailable_reason}")
        return 0
    if a.cmd == "ledger":
        c.spent()
        runs = {a.run: c.ledger.run(a.run)} if a.run else c.ledger.runs
        for rid, r in runs.items():
            print(f"{rid}: {r['calls']} requests, {r['tokens']} tokens, ${r['usd']:.4f}")
        t = c.ledger.totals
        print(f"total: {t['calls']} requests, {t['tokens']} tokens, ${t['usd']:.4f} of ${c.budget_usd:.2f} "
              f"({len(c.ledger.pending)} open reservations, counted at their worst case)")
        return 0
    try:
        if a.cmd == "models":
            for m in c.list_models():
                pin, pout = price_of(m)
                note = "" if known_price(m) else "  (price unknown: charged the conservative UNKNOWN_PRICE)"
                print(f"{m:45s} ${pin:.2f} / ${pout:.2f} per 1M in/out{note}")
            return 0
        data, comp = c.complete_json(
            [{"role": "user", "content": 'Reply with {"ok": true, "word": <one English word for a greeting>}.'}],
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
                    "required": ["ok", "word"], "additionalProperties": False},
            max_tokens=30, tag="ping")
        print(f"{c.model}: {data} | {comp.prompt_tokens}+{comp.completion_tokens} tokens, ${comp.cost_usd:.6f}, "
              f"{comp.latency_s:.2f} s, json mode {c._json_modes.get('answer')}")
        return 0
    except LLMError as e:
        print(f"{type(e).__name__}: {e}")
        return 1
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(_cli())
