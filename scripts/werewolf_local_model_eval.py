from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shlex
import sys
import threading
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["ARENA_RECORD"] = "0"
os.environ["ARENA_STATE"] = "0"
os.environ["ARENA_SUMMARY"] = "0"

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, LegalAction, SendMessage, WithReasoning
from altruagent.runner import _validate_decision
from examples.llm.providers import ProviderError
from examples.llm_agent import LLMAgent, TRANSCRIPT_LIMIT
from local import eval_werewolf as ev
from local import werewolf_benchmark as wb
from local.werewolf_env import OfficialBaseline, Seat
from local.werewolf_research import PairCounts, ResearchGame

ENDPOINT = "http://127.0.0.1:11437"
MODEL = "qwen2.5:7b-instruct-q4_K_M"
DIGEST = "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"
SEED = 205
BACKGROUNDS = ("llmq", "pop_prompt_seer", "pop_coordinated")
PROFILES = (("agent", "agent"), ("f48", "local.werewolf_factorial_research:f48"),
            ("f08", "local.werewolf_factorial_research:f08"))
SETTINGS = {"ARENA_RECORD": "0", "ARENA_STATE": "0", "ARENA_SUMMARY": "0", "ARENA_WW_TIMING": "rt",
            "ARENA_WW_TACTICS": "off", "ARENA_WW_DM": "off", "ARENA_WW_LISTEN": "on",
            "ARENA_WW_WORDING": "new", "ARENA_WW_MESSAGING": "on", "ARENA_WW_SAME_NAMES": "off"}
RAW_KEYS = frozenset(("session_id", "game_type", "runtime_adapter", "status", "state_version", "phase",
                      "observation", "is_terminal", "is_current_actor", "current_actor", "legal_actions",
                      "next_actions", "your_position", "players", "game_state", "eliminated", "messaging_enabled",
                      "new_messages", "terminated_messaging", "messaging_seconds_left"))
PDT = ZoneInfo("America/Los_Angeles")
ENCODING_MODES = ("numeric_string", "label_string", "numeric_object", "label_object")
GUARD_ROTATIONS = (1, 2, 3, 4, 5, 6)
CONTEXT_TOKENS = (8192, 16384)
PRIOR_SELECTION = Path("logs/werewolf_benchmarks/overnight-local-llm-205-interface-observer-v3/selection.json")


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def validate_endpoint(endpoint):
    value = urlsplit(endpoint)
    if (value.scheme != "http" or value.hostname != "127.0.0.1" or value.port != 11437
            or value.username is not None or value.password is not None or value.path not in ("", "/")
            or value.query or value.fragment or value.netloc != "127.0.0.1:11437"):
        raise ValueError("Only literal http://127.0.0.1:11437 is allowed")
    return ENDPOINT


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON property")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite JSON constant")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def validate_schema(value, schema):
    types = schema.get("type")
    types = [types] if isinstance(types, str) else types
    matches = {"object": isinstance(value, dict), "string": isinstance(value, str),
               "integer": type(value) is int, "null": value is None, "boolean": type(value) is bool,
               "array": isinstance(value, list), "number": type(value) in (int, float)}
    if types and not any(matches.get(kind, False) for kind in types):
        raise ValueError("JSON schema type mismatch")
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        raise ValueError("value outside legal enum")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            raise ValueError("missing required JSON property")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError("unexpected JSON property")
        for key, child in value.items():
            if key in properties:
                validate_schema(child, properties[key])
    if isinstance(value, list) and "items" in schema:
        for item in value:
            validate_schema(item, schema["items"])


class LocalProvider:
    gate = threading.Lock()
    model = MODEL

    def __init__(self, *, endpoint=ENDPOINT, order="reason_first", num_ctx=8192, output_tokens=192,
                 timeout=48.0, transport=None, out=None, cache=None, encoding="numeric_string",
                 model=MODEL, expected_digest=DIGEST):
        self.endpoint = validate_endpoint(endpoint)
        if model not in tuple(f"qwen2.5:{size}b-instruct-q4_K_M" for size in (7, 14, 32)) \
                or not isinstance(expected_digest, str) or len(expected_digest) != 64 \
                or any(c not in "0123456789abcdef" for c in expected_digest):
            raise ValueError("approved local model and pinned SHA-256 required")
        self.model, self.expected_digest = model, expected_digest
        if encoding not in ENCODING_MODES:
            raise ValueError("unsupported interface encoding")
        self.encoding = encoding
        if order not in ("original", "reason_first") or num_ctx not in CONTEXT_TOKENS:
            raise ValueError("unsupported schema order or context")
        if not 0 < timeout <= 50 or not 32 <= output_tokens <= 256:
            raise ValueError("request or output budget exceeds bounds")
        self.order = order
        self.options = {"temperature": 0, "seed": SEED, "num_ctx": num_ctx,
                        "num_predict": output_tokens, "num_thread": 4}
        self.input_byte_cap = num_ctx - output_tokens - 1024
        self.timeout = timeout
        self.transport = transport
        self.out = out
        self.cache = {} if cache is None else cache
        self.events = []
        self.stats = Counter()
        self.health = []

    async def _request(self, method, path, body, limit):
        async with httpx.AsyncClient(base_url=self.endpoint, trust_env=False, follow_redirects=False,
                                     timeout=limit, transport=self.transport) as client:
            return await asyncio.wait_for(client.request(method, path, json=body), timeout=limit)

    def request(self, method, path, body=None, timeout=None):
        if path not in ("/api/chat", "/api/tags", "/api/ps", "/api/version"):
            raise ValueError("unapproved local endpoint path")
        validate_endpoint(self.endpoint)
        limit = min(self.timeout, timeout if timeout is not None else self.timeout)
        with self.gate:
            response = asyncio.run(self._request(method, path, body, limit))
        if response.status_code != 200:
            raise ProviderError(f"local HTTP status {response.status_code}; redirects forbidden")
        return response

    def inspect_server(self):
        values = {}
        for path in ("/api/version", "/api/tags", "/api/ps"):
            values[path] = self.request("GET", path).json()
        models = values["/api/tags"].get("models", [])
        if not any(m.get("name") == self.model and m.get("digest") == self.expected_digest for m in models):
            raise ProviderError("installed model digest mismatch")
        self.health.append({"at": datetime.now(PDT).isoformat(), "responses": values})
        return values

    def context_verified(self, health):
        return any(model.get("name") == self.model and model.get("digest") == self.expected_digest
                   and model.get("context_length") == self.options["num_ctx"]
                   for model in health["/api/ps"].get("models", []))

    def adapted_schema(self, schema):
        result = deepcopy(schema)
        properties = result.get("properties", {})
        if self.order == "reason_first" and "reasoning_summary" in properties:
            result["properties"] = {"reasoning_summary": properties["reasoning_summary"],
                                    **{k: v for k, v in properties.items() if k != "reasoning_summary"}}
        return result

    def encode(self, messages, schema_name, schema):
        outgoing = deepcopy(messages)
        schema_out = self.adapted_schema(schema)
        aliases = {}
        if self.encoding == "numeric_string":
            return outgoing, schema_out, aliases
        if len(outgoing) < 2 or outgoing[1].get("role") != "user":
            raise ProviderError("encoding requires the official decision payload")
        payload = strict_json(outgoing[1]["content"])
        if not isinstance(payload, dict) or payload.get("decision") != schema_name:
            raise ProviderError("encoding requires the official decision payload")
        if self.encoding.endswith("object"):
            state = strict_json(payload["state"])
            if not isinstance(state, dict):
                raise ProviderError("official state must encode a JSON object")
            payload["state"] = state
        if self.encoding.startswith("label") and schema_name == "choose_action":
            actions = payload.get("legal_actions")
            if not isinstance(actions, list) or not actions:
                raise ProviderError("action aliases require offered legal actions")
            ids = []
            for action in actions:
                if not isinstance(action, dict):
                    raise ProviderError("malformed offered action")
                ident, label = action.get("action_id"), action.get("label")
                if (not isinstance(ident, str) or not ident or ident in ids
                        or not isinstance(label, str) or not label.strip() or label != label.strip()
                        or label in aliases):
                    raise ProviderError("malformed or ambiguous offered action labels/IDs")
                ids.append(ident)
                aliases[label] = ident
            enum = schema_out["properties"]["action_id"].get("enum")
            if not isinstance(enum, list) or len(enum) != len(ids) or set(enum) != set(ids):
                raise ProviderError("offered actions and schema enum disagree")
            by_id = {ident: label for label, ident in aliases.items()}
            for action in actions:
                action["action_id"] = by_id[action["action_id"]]
            schema_out["properties"]["action_id"]["enum"] = [by_id[ident] for ident in enum]
        outgoing[1]["content"] = json.dumps(payload, ensure_ascii=False)
        return outgoing, schema_out, aliases

    def complete_structured(self, messages, schema_name, schema, *, timeout=None):
        event = {"index": len(self.events), "started": datetime.now(PDT).isoformat(), "key": None,
                 "schema_name": schema_name, "provider_order": self.order, "encoding": self.encoding,
                 "original_messages": deepcopy(messages), "input_schema": deepcopy(schema),
                 "input_byte_cap": self.input_byte_cap, "network": False, "cache_hit": False}
        started = time.monotonic()
        self.stats["attempts"] += 1
        try:
            if any(set(m) != {"role", "content"} or m["role"] not in ("system", "user", "assistant")
                   or not isinstance(m["content"], str) for m in messages):
                raise ProviderError("text-only messages required; tools forbidden")
            if any("...(truncated)" in m["content"] for m in messages):
                self.stats["context_rejects"] += 1
                raise ProviderError("context refused: official state was truncated")
            outgoing, schema_out, aliases = self.encode(messages, schema_name, schema)
            body = {"model": self.model, "messages": outgoing, "format": schema_out,
                    "stream": False, "keep_alive": -1, "options": dict(self.options)}
            identity = {"model_digest": self.expected_digest, "schema_name": schema_name, "input_schema": schema,
                        "provider_order": self.order, "encoding": self.encoding, "original_messages": messages,
                        "request": body}
            key = hashlib.sha256(dump(identity).encode("utf-8")).hexdigest()
            event.update(key=key, request=body, aliases=aliases, input_bytes=len(dump(body).encode("utf-8")))
            if event["input_bytes"] > self.input_byte_cap:
                self.stats["context_rejects"] += 1
                raise ProviderError("context refused: full UTF-8 payload exceeds conservative byte budget")
            if key in self.cache:
                saved = self.cache[key]
                event.update(cache_hit=True, source_event=saved["event"], answer=deepcopy(saved["answer"]))
                self.stats["cache_hits"] += 1
                return deepcopy(saved["answer"])
            event["network"] = True
            self.stats["network_calls"] += 1
            response = self.request("POST", "/api/chat", body, timeout)
            data = response.json()
            event["response"] = data
            for name, field in (("prompt_tokens", "prompt_eval_count"), ("output_tokens", "eval_count")):
                self.stats[name] += int(data.get(field, 0))
            if not data.get("done") or data.get("done_reason") == "length":
                self.stats["output_truncations"] += 1
                raise ProviderError("model output incomplete or exhausted token limit")
            if data.get("model") != self.model:
                raise ProviderError("response model differs from the pinned requested model")
            message = data.get("message", {})
            if message.get("tool_calls") or message.get("thinking"):
                raise ProviderError("unexpected tool call or private reasoning channel")
            if int(data.get("prompt_eval_count", 0)) + self.options["num_predict"] + 256 > self.options["num_ctx"]:
                raise ProviderError("server token count violates context reserve")
            try:
                answer = strict_json(message.get("content") or "")
            except (ValueError, TypeError):
                self.stats["invalid_json"] += 1
                raise ProviderError("model reply is not strict JSON") from None
            try:
                validate_schema(answer, schema_out)
                event["model_answer"] = deepcopy(answer)
                if aliases:
                    answer["action_id"] = aliases[answer["action_id"]]
                validate_schema(answer, schema)
            except ValueError as exc:
                self.stats["invalid_schema"] += 1
                raise ProviderError(str(exc)) from None
            event["answer"] = answer
            self.cache[key] = {"event": event["index"], "answer": deepcopy(answer)}
            return answer
        except (httpx.HTTPError, TimeoutError) as exc:
            self.stats["timeouts" if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else "http_errors"] += 1
            self.stats["errors"] += 1
            event["error"] = type(exc).__name__
            raise ProviderError(f"local request failed ({type(exc).__name__})") from None
        except (ProviderError, ValueError, KeyError, TypeError) as exc:
            self.stats["errors"] += 1
            event["error"] = str(exc)
            raise ProviderError(str(exc)) from None
        finally:
            event["latency_s"] = time.monotonic() - started
            self.events.append(event)
            if self.out is not None:
                write_json(self.out / "requests" / f"{event['index']:06d}.json", event)


class LocalLLMSeat(Seat):
    name = "official_local_llm"

    def __init__(self, provider):
        self.provider = provider
        self.reset()

    def reset(self):
        self.stats = Counter()
        self.logs = []
        self.decisions = []
        self.agent = LLMAgent(self.provider, log=self._log)

    def _log(self, text):
        self.logs.append(text)
        if "invalid model response" in text:
            self.stats["invalid_answers"] += 1
        if "FALLBACK" in text:
            self.stats["fallbacks"] += 1

    def _context(self, raw):
        me = raw.get("your_position")
        if type(me) is not int or not 0 <= me < 7 or set(raw) - RAW_KEYS:
            raise ValueError("seat input boundary violation")
        for message in raw.get("new_messages") or []:
            if (message.get("visibility") == "direct" and message.get("sender") != me
                    and me not in (message.get("recipients") or [])):
                raise ValueError("another seat's private message")
        return DecisionContext(session_id="synthetic-local", tournament_id=None, game_type="werewolf",
                               agent_id="synthetic-local-llm", seat_position=me)

    def _call(self, raw, kind):
        context = self._context(raw)
        state = GameState.from_mcp_state(raw)
        start = len(self.provider.events)
        before = time.monotonic()
        value = (self.agent.choose_action(state, context) if kind == "action"
                 else self.agent.choose_message(state, context))
        attempts = len(self.provider.events) - start
        self.stats["decisions"] += 1
        self.stats["retries"] += max(0, attempts - 1)
        self.stats["official_transcript_omitted_observations"] += max(0, len(self.agent._transcript) - TRANSCRIPT_LIMIT)
        if kind == "message" and not attempts:
            self.stats["quota_or_ended"] += 1
        self.decisions.append({"kind": kind, "seat": context.seat_position, "day": raw["game_state"]["day"],
                               "request_range": [start, len(self.provider.events)], "attempts": attempts,
                               "latency_s": time.monotonic() - before,
                               "official_transcript_omitted": max(0, len(self.agent._transcript) - TRANSCRIPT_LIMIT)})
        return value, state

    def choose(self, raw):
        value, state = self._call(raw, "action")
        _validate_decision(value, state.legal_actions)
        if isinstance(value, WithReasoning):
            value = value.action
        if not isinstance(value, LegalAction) or value.action_id not in {a.action_id for a in state.legal_actions}:
            raise ValueError("official agent returned an illegal action")
        return value.action_id

    def message(self, raw):
        value, _ = self._call(raw, "message")
        if value is TERMINATE_MESSAGING:
            return None
        if not isinstance(value, SendMessage):
            raise ValueError("official agent returned an invalid message")
        recipients = list(value.recipients or [])
        if (not value.content.strip() or len(value.content.split()) > 50 or len(value.content) > 1024
                or len(recipients) > 1 or any(type(r) is not int or not 0 <= r < 7 or r == raw["your_position"] for r in recipients)):
            raise ValueError("message exceeds simulator limits")
        return {"content": value.content, "recipients": recipients}


class ObservingResearchGame(ResearchGame):
    def _ask_message(self, seat, sent):
        event = super()._ask_message(seat, sent)
        if event in ("msg", "term"):
            for other in self.living():
                listener = self.seats[other]
                if other not in self.terminated or self.broken[other] or not isinstance(listener, LocalLLMSeat):
                    continue
                official = listener.agent
                if not official._round_ended or official._round != self.version:
                    raise RuntimeError("terminated local LLM has inconsistent official round state")
                before = (len(listener.provider.events), official._round_requests, official.message_calls,
                          official.decision_calls, len(self.messages), listener.stats["retries"])
                answer = listener.message(self.state(other, "messaging", "day_vote"))
                after = (len(listener.provider.events), official._round_requests, official.message_calls,
                         official.decision_calls, len(self.messages), listener.stats["retries"])
                if answer is not None or before != after:
                    raise RuntimeError("post-termination observation attempted another action or model call")
                listener.stats["post_termination_updates"] += 1
        return event


def guard_cases():
    def game(roles):
        result = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(SEED), strict_messaging=False)
        result.roles = roles
        result.day = 2
        return result

    seer = game(["seer", "villager", "wolf", "villager", "wolf", "villager", "villager"])
    seer.invest = {2: "wolf"}
    clear = game(["seer", "villager", "wolf", "villager", "villager", "villager", "wolf"])
    clear.invest = {1: "villager"}
    for i in (3, 4, 5, 6):
        clear._kill(i, "vote")
    wolf = game(["wolf", "villager", "seer", "villager", "wolf", "villager", "villager"])
    wolf.messages = [{"seq": 0, "sender": 4, "recipients": [0], "visibility": "direct", "type": "chat",
                      "content": "Player2 claimed Seer. I will attack Player2 tonight.", "day": 2}]
    final = game(["villager", "villager", "wolf", "villager", "seer", "villager", "wolf"])
    for i in (3, 4, 5, 6):
        final._kill(i, "vote")
    final.messages = [{"seq": 0, "sender": 4, "recipients": [], "visibility": "broadcast", "type": "chat",
                       "content": "I am the Seer. I investigated Player2: wolf.", "day": 1}]
    cases = [("known_wolf_seer", seer.state(0, "moving", "day_vote", [1, 2, 3, 4, 5, 6, 7]), "2"),
             ("known_clear_three_alive", clear.state(0, "moving", "day_vote", [1, 2, 7]), "2"),
             ("private_wolf_partner_night", wolf.state(0, "moving", "night_wolf", [1, 2, 3, 5, 6]), "2"),
             ("three_alive_revealed_seer", final.state(0, "moving", "day_vote", [1, 2, 7]), "2")]
    reversed_wolf = deepcopy(cases[0][1])
    reversed_wolf["legal_actions"]["actions"].reverse()
    cases.append(("known_wolf_reversed_legal_order", reversed_wolf, "2"))
    return cases


def run_quality(provider):
    rows = []
    for order in ("original", "reason_first"):
        provider.order = order
        for name, raw, expected in guard_cases():
            seat = LocalLLMSeat(provider)
            start = len(provider.events)
            action = seat.choose(deepcopy(raw))
            rows.append({"case": name, "order": order, "expected": expected, "actual": action,
                         "correct": action == expected, "legal": action in {a["action_id"] for a in raw["legal_actions"]["actions"]},
                         "seat_stats": dict(seat.stats), "logs": seat.logs,
                         "requests": list(range(start, len(provider.events)))})
    provider.order = "reason_first"
    return rows


def rotate_guard(raw, shift):
    def seat(value):
        return (value + shift) % 7

    def text(value):
        return re.sub(r"\b(Player|agent|id)([0-6])\b", lambda m: m[1] + str(seat(int(m[2]))), value)

    def strings(value):
        if isinstance(value, dict):
            return {text(k): strings(v) for k, v in value.items()}
        if isinstance(value, list):
            return [strings(v) for v in value]
        return text(value) if isinstance(value, str) else value

    rotated = strings(raw)
    rotated["your_position"] = seat(raw["your_position"])
    for player in rotated["players"]:
        player["position"] = seat(player["position"])
    rotated["players"].sort(key=lambda p: p["position"])
    rotated["terminated_messaging"] = [seat(i) for i in raw["terminated_messaging"]]
    for message in rotated["new_messages"]:
        message["sender"] = seat(message["sender"])
        message["recipients"] = [seat(i) for i in message["recipients"]]
    for action in rotated["legal_actions"]["actions"]:
        ident = action["action_id"]
        if ident != "7":
            action["action_id"] = str(seat(int(ident)))
            action["input"]["action_id"] = action["action_id"]
    return rotated


def interface_guard_cases():
    prior = guard_cases()
    rows = [{"name": name, "raw": raw, "expected": expected, "source": "prior_diagnostic",
             "known_wolf": name.startswith("known_wolf"), "rotation": 0}
            for name, raw, expected in prior]
    for shift in GUARD_ROTATIONS:
        for name, raw, expected in prior[:4]:
            rows.append({"name": f"{name}_rotate{shift}", "raw": rotate_guard(raw, shift),
                         "expected": str((int(expected) + shift) % 7), "source": "new_rotation_selection",
                         "known_wolf": name.startswith("known_wolf"), "rotation": shift})
    return rows


def select_encoding(rows):
    planned = {case["name"] for case in interface_guard_cases()}
    metrics = []
    for mode in ENCODING_MODES:
        cases = [row for row in rows if row["encoding"] == mode]
        known = [row for row in cases if row["known_wolf"]]
        complete = len(cases) == len(planned) and {row["case"] for row in cases} == planned
        correct = sum(row["correct"] for row in cases)
        eligible = complete and bool(known) and all(row["correct"] for row in known) and correct / len(cases) >= .8
        metrics.append({"encoding": mode, "n": len(cases), "correct": correct,
                        "known_wolf_n": len(known), "known_wolf_correct": sum(row["correct"] for row in known),
                        "eligible": eligible, "complete": complete})
    passing = [m for m in metrics if m["eligible"]] if all(m["complete"] for m in metrics) else []
    selected = max(passing, key=lambda m: m["correct"])["encoding"] if passing else None
    return {"selected": selected, "metrics": metrics,
            "rule": "All 8 known-wolf cases correct without fallback and at least 80% of all 29 cases; highest guard accuracy, ties in predeclared mode order.",
            "warning": "These 5 prior diagnostics and 24 new rotations are reused for interface selection, not independent validation. Property order alone previously failed. No game outcomes enter selection; no stronger-opponent claim if no mode qualifies."}


def fixed_encoding_selection(encoding):
    if encoding not in ENCODING_MODES:
        raise ValueError("unsupported fixed encoding")
    prior_bytes = (ROOT / PRIOR_SELECTION).read_bytes()
    prior = strict_json(prior_bytes)
    return {"selected": encoding, "mode": "fixed_cli_no_reselection", "independent_validation": False,
            "provenance": {"path": str(PRIOR_SELECTION), "sha256": hashlib.sha256(prior_bytes).hexdigest(),
                           "prior_selected": prior["selected"], "matches_prior_selected": encoding == prior["selected"],
                           "prior_context_tokens": 8192, "prior_selection": prior},
            "rule": "Explicit CLI encoding frozen before requests; repeated diagnostics cannot select or veto games.",
            "warning": "Context robustness follow-up on the SAME v3 selection cases and seed-205 games; no new independent validation, no promotion or quality auto-pass."}


def run_interface_quality(provider, stop_at=None, *, encodings=ENCODING_MODES, reused=False):
    rows = []
    provider.order = "reason_first"
    for mode in encodings:
        provider.encoding = mode
        for case in interface_guard_cases():
            if stop_at is not None and time.time() >= stop_at:
                return rows
            seat = LocalLLMSeat(provider)
            start = len(provider.events)
            raw = deepcopy(case["raw"])
            action = seat.choose(raw)
            legal = action in {a["action_id"] for a in raw["legal_actions"]["actions"]}
            row = {"case": case["name"], "source": "reused_v3_selection_diagnostic" if reused else case["source"],
                   "prior_source": case["source"], "independent_validation": False, "rotation": case["rotation"],
                   "context_tokens": provider.options["num_ctx"],
                   "known_wolf": case["known_wolf"], "encoding": mode, "order": provider.order,
                   "expected": case["expected"], "actual": action, "legal": legal,
                   "correct": legal and action == case["expected"] and not seat.stats["fallbacks"],
                   "seat_stats": dict(seat.stats), "logs": seat.logs,
                   "requests": list(range(start, len(provider.events)))}
            rows.append(row)
            if provider.out is not None:
                write_json(provider.out / "quality.json", rows)
            print("INTERFACE_GUARD " + dump({k: row[k] for k in ("case", "encoding", "actual", "expected", "correct")}), flush=True)
    return rows


def scenario_plan(max_scenarios):
    if max_scenarios not in (12, 24, 40):
        raise ValueError("predeclared scenario totals are 12, 24, or 40")
    plan = []
    for count, indices in ((1, range(12)), (2, range(12)), (1, range(12, 20)), (2, range(12, 20))):
        for index in indices:
            background = BACKGROUNDS[index % len(BACKGROUNDS)]
            g = index // len(BACKGROUNDS)
            gseed, ours, types = ev.deal(SEED, g, background, 1)
            shuffled = sorted(types)
            random.Random(f"local-model-swap:{SEED}:{g}:{background}:1").shuffle(shuffled)
            plan.append({"id": f"{SEED}/{background}/{g}/llm{count}", "seed": SEED, "game": g,
                         "background": background, "llm_count": count, "deal_seed": gseed,
                         "ours": ours[0], "types": types, "llm_seats": sorted(shuffled[:count])})
    return plan[:max_scenarios]


def counter_delta(after, before):
    return {key: value - before.get(key, 0) for key, value in after.items() if value != before.get(key, 0)}


def play_profile(scenario, profile, factory, provider):
    before = dict(provider.stats)
    first = len(provider.events)
    started = time.monotonic()
    me = scenario["ours"]
    controlled = wb.MeasuredSeat(ev.factory(factory), SETTINGS)
    seats = [controlled if i == me else LocalLLMSeat(provider) if i in scenario["llm_seats"]
             else ev.make_seat(scenario["types"][i]) for i in range(7)]
    game = ObservingResearchGame(seats, random.Random(scenario["deal_seed"]), strict_messaging=True,
                                 timing="rt", wording="new", messaging=True)
    returns = game.play()
    llm_stats = Counter()
    for i in scenario["llm_seats"]:
        llm_stats.update(seats[i].stats)
    failures = {k: game.stats[k][me] for k in ev.FAIL_KEYS}
    failures.update(fallbacks=getattr(controlled.agent, "fallbacks", 0), overruns=getattr(controlled.agent, "overruns", 0),
                    policy_fallbacks=controlled.policy_fallbacks, policy_errors=controlled.policy_errors,
                    logged_errors=controlled.logged_errors, listen_errors=getattr(controlled.agent, "listen_stats", {}).get("errors", 0))
    bg_fail = {k: sum(game.stats[k][i] for i in range(7) if i != me) for k in ev.FAIL_KEYS}
    provider_stats = counter_delta(provider.stats, before)
    clean = (not any(v for k, v in failures.items() if k != "window_close")
             and not any(v for k, v in bg_fail.items() if k != "window_close")
             and not provider_stats.get("errors", 0) and not llm_stats["invalid_answers"]
             and not llm_stats["fallbacks"] and not llm_stats["official_transcript_omitted_observations"])
    return {"id": scenario["id"], "profile": profile, "scenario": scenario, "seat": me,
            "role": game.roles[me], "roles": game.roles, "win": returns[me] > 0, "winner": game.winner,
            "days": game.day, "clean": clean, "failures": failures, "background_failures": bg_fail,
            "llm_stats": dict(llm_stats), "provider_stats": provider_stats,
            "request_range": [first, len(provider.events)], "elapsed_s": time.monotonic() - started,
            "llm_seats": [{"seat": i, "role": game.roles[i], "win": returns[i] > 0, "stats": dict(seats[i].stats),
                           "engine_stats": {k: v[i] for k, v in game.stats.items()}, "logs": seats[i].logs,
                           "decisions": seats[i].decisions} for i in scenario["llm_seats"]],
            "synthetic_trace": {"votes": game.votes, "dead": game.dead, "messages": game.messages,
                                "actions": game.action_trace}, "controlled_max_ms": game.stats["max_ms"][me]}


def summarize(records, plan):
    groups = defaultdict(dict)
    for row in records:
        groups[row["id"]][row["profile"]] = row
    metrics = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    failures = defaultdict(Counter)
    for row in records:
        if "harness_error" not in row:
            failures[row["profile"]].update(row["failures"])
            failures[row["profile"]].update({"background_" + k: v for k, v in row["background_failures"].items()})
            failures[row["profile"]].update({"llm_" + k: v for k, v in row["llm_stats"].items()})
    complete = 0
    for count in (1, 2):
        for subset in ("all_attempts", "clean_paired_subset"):
            samples = defaultdict(PairCounts)
            for scenario in plan:
                if scenario["llm_count"] != count:
                    continue
                rows = groups.get(scenario["id"], {})
                if set(rows) != {p for p, _ in PROFILES} or any("harness_error" in r for r in rows.values()):
                    continue
                if subset == "all_attempts":
                    complete += 1
                if subset == "clean_paired_subset" and not all(r["clean"] for r in rows.values()):
                    continue
                reference = rows["agent"]
                if any((r["roles"], r["seat"], r["scenario"]) != (reference["roles"], reference["seat"], reference["scenario"]) for r in rows.values()):
                    raise ValueError("unpaired role/deal/background/timing")
                for profile in ("f48", "f08"):
                    for role in ("all", reference["role"]):
                        samples[(profile, role)].add(int(rows[profile]["win"]), int(reference["win"]))
            for profile in ("f48", "f08"):
                for role in ("all", "villager", "seer", "wolf"):
                    metrics[f"llm{count}"][subset][profile][role] = samples[(profile, role)].report()
    return {"completed_scenarios": complete, "planned_scenarios": len(plan), "games_attempted": len(records),
            "all_game_attempts": [{key: r.get(key) for key in ("id", "profile", "role", "win", "clean", "harness_error")}
                                  for r in records],
            "partial": complete != len(plan), "metrics": metrics, "failures": failures,
            "harness_errors": [{"id": r["id"], "profile": r["profile"], "error": r["harness_error"]}
                               for r in records if "harness_error" in r],
            "warning": "Real free local Qwen model, not real-platform tournament proof. Tiny samples: uncertainty is huge. Clean subset conditions on successful model calls and can be selection-biased. One/two-LLM conditions reuse deals and must not be pooled as independent samples. Official transcript cap omissions are explicit and excluded from clean subset."
            }


def source_manifest():
    source = wb.source_manifest()
    for relative in ("scripts/werewolf_local_model_eval.py", "tests/strategy/test_werewolf_local_model.py",
                     "data/werewolf_local_model_setup.json"):
        path = ROOT / relative
        if path.exists():
            source["files"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    source["digest"] = hashlib.sha256(dump(sorted(source["files"].items())).encode()).hexdigest()
    used = {}
    for module in list(sys.modules.values()):
        location = getattr(module, "__file__", None)
        if not location:
            continue
        path = Path(location).resolve()
        if path.suffix == ".py" and path.is_relative_to(ROOT) and ".venv" not in path.relative_to(ROOT).parts and path.is_file():
            name = str(path.relative_to(ROOT))
            used[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    source["loaded_python_files"] = used
    source["dependency_scope"] = {"httpx_version": httpx.__version__, "python": sys.version,
                                  "note": "Repository source files and dependency lockfiles hashed. External stdlib/site-packages source not re-read: an initial pre-request attempt blocked on an unavailable anyio source file."}
    return source


def request_scope(events):
    network = [event for event in events if event["network"]]
    return {"attempts": len(events), "network_calls": len(network),
            "unique_network_keys": len({event["key"] for event in network}),
            "unique_attempt_keys": len({event["key"] for event in events if event["key"] is not None}),
            "cache_hits": sum(event["cache_hit"] for event in events),
            "errors": dict(Counter(event["error"] for event in events if "error" in event)),
            "note": "Run-local exact-request memoization, not independent model samples; no cache imported from prior runs. Smoke and diagnostics caches cleared before games. Network keys may repeat after cache clears or failures."}


def provider_summary(provider):
    network = [event for event in provider.events if event["network"]]
    latencies = sorted(event["latency_s"] for event in network)
    return {"stats": dict(provider.stats), "request_scope": request_scope(provider.events), "latencies_s": latencies,
            "max_s": max(latencies, default=0), "mean_s": sum(latencies) / max(1, len(latencies)),
            "health": provider.health, "model": provider.model, "model_digest": provider.expected_digest,
            "options": provider.options, "encoding": provider.encoding,
            "all_errors": [{"index": e["index"], "error": e["error"]} for e in provider.events if "error" in e],
            "first_response_examples": [e for e in provider.events if "response" in e][:5]}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scenarios", type=int, choices=(12, 24, 40), default=40)
    parser.add_argument("--stop-at", default="2026-10-13T07:00:00-07:00")
    parser.add_argument("--guards-only", action="store_true")
    parser.add_argument("--context-tokens", type=int, choices=CONTEXT_TOKENS, default=8192)
    parser.add_argument("--encoding", choices=ENCODING_MODES,
                        help="Freeze this encoding and repeat only its prior diagnostics, without reselection")
    args = parser.parse_args(argv)
    stop = datetime.fromisoformat(args.stop_at)
    hard_stop = datetime(2026, 10, 13, 7, 0, tzinfo=PDT)
    if stop.tzinfo is None or stop > hard_stop or stop.timestamp() <= time.time():
        parser.error("stop-at must be future, timezone-aware and no later than 2026-10-13 07:00 PDT")
    output = args.out.resolve()
    if output.parent != (ROOT / "logs/werewolf_benchmarks").resolve() or not output.name.startswith("overnight-local-llm-") or output.exists():
        parser.error("use a new logs/werewolf_benchmarks/overnight-local-llm-* directory")
    output.mkdir()
    (output / "requests").mkdir()
    for _, factory in PROFILES:
        ev.factory(factory)
    plan = scenario_plan(args.scenarios)
    provider = LocalProvider(out=output, num_ctx=args.context_tokens, encoding=args.encoding or "numeric_string")
    selection = fixed_encoding_selection(args.encoding) if args.encoding else select_encoding([])
    source = source_manifest()
    manifest = {"command": shlex.join([sys.executable, *sys.argv]), "seed": SEED, "profiles": PROFILES,
                "plan": plan, "settings": SETTINGS, "stop_at": stop.isoformat(), "source": source,
                "started": datetime.now(PDT).isoformat(), "python": sys.version, "model": MODEL, "digest": DIGEST,
                "provider_options": provider.options, "input_byte_cap": provider.input_byte_cap,
                "header_template_reserve": 1024, "request_total_timeout_s": provider.timeout,
                "provider_adaptation": "Fixed reasoning_summary-first; four predeclared reversible encodings: numeric_string, label_string, numeric_object, label_object. Labels are exact offered action labels mapped bijectively back to original IDs. Object state is parsed official JSON without added information. System prompt unchanged; never infer action from summary.",
                "observer": "ObservingResearchGame delivers filtered messaging updates to already-terminated living LocalLLMSeats after every message/termination event; official choose_message remembers before ending. Assertions prohibit extra model attempts, quota, retries or chats. Unended LLM seats retain normal slow scheduling.",
                "guard_plan": [{k: v for k, v in case.items() if k != "raw"} for case in interface_guard_cases()],
                "encoding_modes": [args.encoding] if args.encoding else ENCODING_MODES,
                "selection_rule": selection["rule"], "selection_warning": selection["warning"],
                "predeclared_selection": selection, "independent_validation": False,
                "context_change": "Only per-request num_ctx; no server restart, global changes, truncation, or extra game information.",
                "source_frozen_before_requests": True,
                "prior_pilot": "Old v2 120 games retained as limited pilot with missing post-termination observations, not confirmation.",
                "scope": "Synthetic local research only. No credentials, paid calls, remote endpoints, tools, model learning, or production changes.",
                "cache": "Run-local exact digest/options/messages/input schema/output schema/order. Only validated successful replies. Pure function reuse; no role labels or candidate factory in key. Files contain only synthetic requests/responses.",
                "schedule": "12 one-LLM, 12 two-LLM, then fixed 8+8 extension if time. Fixed llmq/pop_prompt_seer/pop_coordinated rotation. Stop before starting each game at deadline, finish current bounded game; never select by winner.",
                "timing_limit": "rt simulator assigns fixed other-seat talk ticks, not measured asynchronous platform latency. Actual provider latency is separately recorded.",
                "official_bounds": "Unmodified LLMAgent keeps last 30 transcript entries and clips message text. Transcript omissions are explicitly counted; provider never truncates and refuses oversized/truncated state inputs."}
    write_json(output / "manifest.json", manifest)
    records = []
    quality = []
    game_provider_start = {}
    game_event_start = None
    context_verified_end = False
    status = "running"
    started = time.monotonic()
    error = None
    try:
        provider.inspect_server()
        smoke = LocalLLMSeat(provider)
        name, raw, expected = guard_cases()[0]
        actual = smoke.choose(raw)
        health = provider.inspect_server()
        context_ok = provider.context_verified(health)
        write_json(output / "smoke.json", {"case": name, "expected": expected, "actual": actual,
                                          "seat_stats": dict(smoke.stats), "context_verified": context_ok,
                                          "requested_context_tokens": args.context_tokens,
                                          "server": health, "requests": list(range(len(provider.events)))})
        if not context_ok or smoke.stats["fallbacks"]:
            raise RuntimeError(f"{args.context_tokens} context smoke failed; no comparison games started; do not restart server")
        provider.cache.clear()
        quality = run_interface_quality(provider, stop.timestamp(),
                                        encodings=(args.encoding,) if args.encoding else ENCODING_MODES,
                                        reused=args.encoding is not None)
        write_json(output / "quality.json", quality)
        if args.encoding is None:
            selection = select_encoding(quality)
        if not provider.context_verified(provider.inspect_server()):
            raise RuntimeError("requested context not active after diagnostics; do not restart server")
        write_json(output / "selection.json", selection)
        print("INTERFACE_SELECTION " + dump(selection), flush=True)
        provider.cache.clear()
        game_provider_start = dict(provider.stats)
        game_event_start = len(provider.events)
        write_json(output / "game-provider-start.json", {"stats": game_provider_start, "event_index": game_event_start})
        if selection["selected"] is not None:
            provider.encoding = selection["selected"]
        with wb.environment(SETTINGS):
            for scenario in ([] if args.guards_only or selection["selected"] is None else plan):
                for label, factory in PROFILES:
                    if time.time() >= stop.timestamp():
                        status = "deadline_partial"
                        break
                    try:
                        row = play_profile(scenario, label, factory, provider)
                    except Exception as exc:
                        row = {"id": scenario["id"], "profile": label, "harness_error": f"{type(exc).__name__}: {exc}"}
                    records.append(row)
                    write_json(output / f"game-{len(records):04d}.json", row)
                    write_json(output / "summary.json", summarize(records, plan) | {"status": "running", "selection": selection, "provider": provider_summary(provider)})
                    print(dump({"game": len(records), "scenario": scenario["id"], "profile": label,
                                "win": row.get("win"), "clean": row.get("clean"), "provider": dict(provider.stats),
                                "time": datetime.now(PDT).isoformat()}), flush=True)
                summary = summarize(records, plan)
                if summary["completed_scenarios"] == 12:
                    print("FIRST_12_PAIRED_SCENARIOS_COMPLETE " + dump(summary["metrics"]["llm1"]), flush=True)
                if status == "deadline_partial":
                    break
            else:
                status = ("deadline_partial" if time.time() >= stop.timestamp() else "quality_gate_failed") if selection["selected"] is None else "guards_complete" if args.guards_only else "complete"
        context_verified_end = provider.context_verified(provider.inspect_server())
        if not context_verified_end:
            raise RuntimeError("final server context differs from request; results not context-verified")
        if status == "complete" and summarize(records, plan)["partial"]:
            status = "incomplete_partial"
    except Exception as exc:
        status = "blocked_partial"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        end = source_manifest()
        used_changed = [name for name, digest in source["loaded_python_files"].items()
                        if end["loaded_python_files"].get(name) != digest]
        summary = summarize(records, plan) | {"status": status, "error": error, "quality": quality,
                    "selection": selection, "provider": provider_summary(provider), "elapsed_s": time.monotonic() - started,
                    "context_verified_end": context_verified_end, "independent_validation": False,
                    "quality_failures": [row for row in quality if not row["correct"]],
                    "game_provider_stats": counter_delta(provider.stats, game_provider_start) if game_event_start is not None else {},
                    "game_request_scope": request_scope(provider.events[game_event_start:]) if game_event_start is not None else {},
                    "ended": datetime.now(PDT).isoformat(), "source_digest_start": source["digest"],
                    "source_digest_end": end["digest"], "all_archived_source_unchanged": source["digest"] == end["digest"],
                    "loaded_source_changed": used_changed, "promotion": "not assessed"}
        write_json(output / "final-source.json", end)
        write_json(output / "summary.json", summary)
        print(dump({"status": status, "error": error, "completed": summary["completed_scenarios"],
                    "games": len(records), "out": str(output)}), flush=True)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
