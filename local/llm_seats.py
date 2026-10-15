"""Real-LLM opponents for the local simulators, on Nebius Token Factory through local/llm_client.py.

Both seats are the organizers' generic LLM agent, examples/llm_agent.py's LLMAgent, unchanged: the same system
prompt, the same request JSON (game type, seat, phase, legal options, recent messages, state), the same strict-JSON
answer built from the current legal options and validated again, one retry with the validation error, then its
deterministic FALLBACK (first legal action; smoke_agent's lineup / turn in Pokemon; ending the chat round). Only the
provider differs: NebiusProvider in place of examples/llm/providers.py's OpenAIProvider (gpt-4o-mini), same 60 s
request timeout. The official example is what the brief expects much of the real field to run.

  VGC       `llm_real` in local.field_opps (hence local.eval_pokemon --opponent llm_real and local.panel --opps
            llm_real): played behind the official runner's rules and the platform's VGC timer, with each model call's
            REAL wall time charged to the seat's clock (field_opps.WallTimed), like `llm_live`.
  Werewolf  LLMWerewolfSeat, a local.werewolf_env seat (local.eval_werewolf background kind `llm_real` / --llm-real):
            reads the chat (the env's new_messages), votes / kills / investigates through the legal action ids,
            speaks at most 2 messages per discussion window (LLMAgent's own budget) of at most 40 words, to everyone
            or one recipient. Nothing is filtered here: the engine applies the platform's failure model to this seat
            like to every other (a crash, an illegal id, a chat the platform would reject or a decision over the
            120 s idle limit -> the process is gone and the platform auto-plays the seat; Game.stats counts it).

No key, an exhausted budget or a per-run cap: the client refuses at once (no request is sent), the provider raises
ProviderError, and the agent plays its rule fallback -- the seat then plays exactly like the deterministic stand-in
`llm_fallback`. Nothing crashes. The deterministic stand-ins (llm_fallback, llm_timeout) are unchanged.

Counters per seat (`stats`): decisions, model_calls (provider attempts), invalid_answers (model answers that did not
fit the options, incl. failed / refused requests), fallbacks (decisions played by the rule fallback), and in Werewolf
message_requests / message_fallbacks / messages_sent / dms (what the seat sent; the engine's Game.stats has what the
platform accepted). Cost, tokens and latency are on the shared client (`llm_client.shared_client().stats`) and in the
spend ledger.
"""
from __future__ import annotations

from .llm_client import LLMClient, LLMError, shared_client
from .werewolf_env import ContestantSeat

MAX_ANSWER_TOKENS = 400      # a JSON decision with a one-sentence reasoning_summary is ~30-150 tokens
WW_STAT_KEYS = ("games", "decisions", "model_calls", "invalid_answers", "fallbacks", "errors", "illegal",
                "message_requests", "message_fallbacks", "messages_sent", "dms")


class NebiusProvider:
    """examples.llm.providers.LLMProvider over LLMClient: complete_structured(messages, schema_name, schema) -> dict,
    or ProviderError (which LLMAgent answers with one retry, then its fallback)."""

    def __init__(self, client: LLMClient | None = None, *, tag: str = "", max_tokens: int = MAX_ANSWER_TOKENS,
                 temperature: float | None = None):
        self.client = client if client is not None else shared_client()
        self.model = self.client.model
        self.tag, self.max_tokens, self.temperature = tag, max_tokens, temperature
        self.calls = 0

    def __repr__(self) -> str:
        return f"NebiusProvider(model={self.model!r})"

    def complete_structured(self, messages: list[dict], schema_name: str, schema: dict) -> dict:
        """The model's answer, conforming to `schema` (as OpenAI's strict mode guarantees the official provider), or
        ProviderError -- for EVERY failure, including our own plumbing's, so the example agent retries once and then
        plays its fallback instead of the seat crashing. Messages carry exception type names only."""
        from examples.llm.providers import ProviderError
        self.calls += 1
        data, problem = None, None
        try:
            data, _ = self.client.complete_json(messages, schema=schema, schema_name=schema_name,
                                                max_tokens=self.max_tokens, temperature=self.temperature,
                                                tag=f"{self.tag}:{schema_name}" if self.tag else schema_name)
        except LLMError as exc:
            problem = f"Nebius {type(exc).__name__}: {exc}"
        except Exception as exc:                        # noqa: BLE001 - a client bug is a failed request, not a crash
            problem = f"Nebius client error ({type(exc).__name__})"
        if problem is not None:
            raise ProviderError(problem)
        return data


def vgc_agent(sim=None, log=print, client: LLMClient | None = None):
    """The `llm_real` VGC agent: LLMAgent + NebiusProvider, its wall time charged to `sim` (a field_opps.SimClock)."""
    from examples.llm_agent import LLMAgent
    provider = NebiusProvider(client, tag="vgc")
    if sim is not None:
        from .field_opps import WallTimed
        provider = WallTimed(provider, sim)
    return LLMAgent(provider, log=log)


class LLMWerewolfSeat(ContestantSeat):
    """examples/llm_agent.py's LLMAgent on Nebius as a local.werewolf_env seat (a fresh agent every game). Its
    exceptions and answers go to the engine unchanged (the engine models the runner / platform); only counted here.

    `slow`: under ARENA_WW_TIMING=rt it talks in the other seats' rounds, not at t = 0 with our seats (each of its
    messages is a model call of seconds, ours answer in milliseconds; before 10-14 it was shuffled in with ours)."""

    slow = True

    def __init__(self, client: LLMClient | None = None, name: str = "llm_real"):
        super().__init__(self._make_agent, name)
        self.client = client
        self.provider: NebiusProvider | None = None
        self.stats = dict.fromkeys(WW_STAT_KEYS, 0)
        self._in_message = False

    def _make_agent(self):
        from examples.llm_agent import LLMAgent
        self.provider = NebiusProvider(self.client, tag="werewolf")
        return LLMAgent(self.provider, log=self._log)

    def _log(self, line) -> None:
        line = str(line)
        if "FALLBACK" in line:
            self.stats["message_fallbacks" if self._in_message else "fallbacks"] += 1
        elif "invalid model response" in line:
            self.stats["invalid_answers"] += 1

    def _sync(self, before: int) -> None:
        if self.provider is not None:
            self.stats["model_calls"] += self.provider.calls - before

    def reset(self):
        super().reset()
        self.stats["games"] += 1

    def choose(self, raw: dict) -> str:
        self.stats["decisions"] += 1
        before = self.provider.calls if self.provider is not None else 0
        legal = [a["action_id"] for a in (raw.get("legal_actions") or {}).get("actions") or []]
        try:
            out = super().choose(raw)
        except Exception:                               # noqa: BLE001 - the engine counts the crash (runner: seat down)
            self.stats["errors"] += 1
            self._sync(before)
            raise
        self._sync(before)
        if out not in legal:
            self.stats["illegal"] += 1
        return out

    def message(self, raw: dict):
        self.stats["message_requests"] += 1
        before = self.provider.calls if self.provider is not None else 0
        self._in_message = True
        try:
            m = super().message(raw)
        except Exception:                               # noqa: BLE001 - the engine counts the crash (runner: seat down)
            self.stats["errors"] += 1
            raise
        finally:
            self._in_message = False
            self._sync(before)
        if m is not None:
            self.stats["messages_sent"] += 1
            self.stats["dms"] += bool(m.get("recipients"))
        return m


def invalid_rate(stats: dict) -> dict:
    """Invalid-action rates of an LLM seat: decisions left to the rule fallback, and model answers that did not fit
    the options (per model call)."""
    d, calls = stats.get("decisions") or stats.get("requests") or 0, stats.get("model_calls") or 0
    return {"fallback_rate": stats.get("fallbacks", 0) / d if d else 0.0,
            "invalid_answer_rate": stats.get("invalid_answers", 0) / calls if calls else 0.0}
