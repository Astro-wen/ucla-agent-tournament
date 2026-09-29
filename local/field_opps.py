"""Opponents that resemble the real field: the upstream example agents, run the way the platform would run them.

The organizers' starter ships four example agents (examples/): the brief (docs/competition_brief.md 3.2 #6) expects
many real opponents to be one of them, or the "generic agent we provide" = examples/llm_agent.py (gpt-4o-mini).
This module plays the REAL example code in the local simulator behind a model of the official runner and of the
platform's clocks, so a local game shows what would happen on the platform:

* Decisions go through altruagent.runner's own `_validate_decision` (WithReasoning unwrapped, action ids matched).
* A structured template sent back as-is (`state.legal_actions[0]` at Team Preview or a doubles turn) is what GameAPI
  answers with INVALID_ACTION ("Sending only action_id='doubles_turn' is not enough", docs/platform/pokemon.md);
  any other payload the platform would reject, or an exception in the agent, is a DecisionError too. The runner then
  drops the match (altruagent/runtime.py: 60 s cooldown, then it retries and a deterministic agent fails the same way),
  so the seat counts as DOWN for the rest of the match (`crashes`, `rejected`).
* clock="vgc" (default): Showdown's "VGC Timer" as the brief measured it on the platform (docs/competition_brief.md
  section 2: 90 s team preview, 55 s per request, 420 s bank + 90 s grace; the platform's documented 300 s random play
  never fires first). A request the seat does not answer in time is auto-chosen by Showdown (pokemon_env.TIMER_DEFAULT:
  team preview = the first four in roster order leading with the first two, a battle request = Showdown's own
  `default` choice = first usable move at a random target / first healthy bench Pokemon), and the bank loses the whole
  turn; when the bank is empty the seat loses "due to inactivity" (answered as RESIGN -> MatchResult.forfeit).
  A down seat therefore plays team preview + 7 battle requests on Showdown's defaults and forfeits on the 8th.
  clock="none": the documented platform behaviour instead (a seat that does not answer is random-played after 300 s,
  no bank) - with basic_agent this reproduces local.pokemon_env.OfficialBaseline game for game.
* The draft has no idle timer on the platform: a late answer still lands; a down seat would stall the real match
  (here: counted as `draft_stalls`, the env picks a random card and counts it invalid).
* Decision time is simulated (SimClock), never slept: the example agents answer instantly, except the LLM stand-ins.

Opponents (FIELD):
  basic         examples/basic_agent.py: `return state.legal_actions[0]` = first offered card in the draft, then the
                Team Preview template goes back as-is -> INVALID_ACTION -> down -> Showdown's timer plays for it.
  messaging     examples/messaging_agent.py: Werewolf chat example; in Pokemon its choose_action is legal_actions[0]
                and choose_message is never called, so it plays exactly like basic (kept to show that).
  smoke         examples/smoke_agent.py: first card; brings the first 4, leads the first 2; each slot the first move
                at the first foe target (else the first free switch, else pass). Always legal.
  llm_fallback  STAND-IN for examples/llm_agent.py whose key is set but whose model calls fail at once (a bad or
                revoked key, an exhausted quota, an outage): the real LLMAgent class with a provider that raises, so
                every decision is the agent's own deterministic FALLBACK after its retry (first legal action in the
                draft, smoke_agent's lineup and turn). Plays like smoke, through the LLM agent's code path. NOT the
                missing-key case: without OPENAI_API_KEY, examples/llm_agent.py's create_agent() raises in
                provider_from_env() before the first decision, so that seat never plays at all (the draft has no idle
                timer; what the platform then does with the match is not modelled here).
  llm_timeout   STAND-IN for examples/llm_agent.py whose model calls hang: each call costs the provider's
                REQUEST_TIMEOUT_SECONDS (60 s) on the simulated clock and fails, twice per decision (MAX_ATTEMPTS), so
                every battle request misses the 55 s limit (team preview: 120 s > 90 s) and Showdown's timer plays it;
                the draft still gets the fallback pick (no timer there). ASSUMPTION: the answer that arrives 120 s
                late is discarded, so the seat never crashes and forfeits like basic. The runner submits it with the
                state_version it read before deciding, and docs/platform/pokemon.md section 8 says a battle
                state_version is not bumped when the timer plays; if that holds for Showdown's VGC timer too, the late
                answer lands on a LATER request instead (played as a stale move, or rejected as INVALID_ACTION ->
                DecisionError -> 60 s cooldown). A review re-simulation of that variant (seed 31, 150 games vs v3):
                INVALID_ACTION in every game, 21 stale moves accepted, 44 forfeits instead of 46, and v3 won the same
                150/150 games. Unverified on the platform either way; only the opponent's counters depend on it.
Neither stand-in reproduces what gpt-4o-mini would CHOOSE when it answers in time; that needs a key (see
local/sparring and the greedy / protect_spam bots of local/panel_bots.py for scripted "plausible" play).

The panel's candidate (our own agent) plays on the same VGC timer in REAL wall time: WallClockSeat.

Opt-in, not deterministic (LIVE, never in a panel group):
  llm_live      examples/llm_agent.py with a REAL model: the provider from OPENAI_API_KEY / OPENAI_MODEL /
                OPENAI_BASE_URL in the environment (any OpenAI-compatible endpoint, e.g. a local server; this module
                never reads .env), and each model call's real wall time charged to the seat's clock, so the VGC timer
                sees the model's actual latency. Costs API calls; rate limits show up as fallbacks.
  llm_real      the same examples/llm_agent.py LLMAgent on Nebius Token Factory (local/llm_seats.py + local/llm_client.py:
                NEBIUS_API_KEY from the environment or the checkout's .env, default model Qwen3-30B-A3B-Instruct-2507,
                60 s timeout, response cache, spend ledger and hard caps), real wall time on the seat's clock as
                llm_live. Without a key or over a cap every call is refused at once and the agent plays its rule
                fallback, i.e. exactly like llm_fallback; it never crashes.

Per-match counters are on `PlatformSeat.stats` (local/panel.py writes them to its JSONL as pf_* fields).
"""
from __future__ import annotations

import importlib
import math
import time

from altruagent import RESIGN, DecisionContext, GameState
from altruagent.runner import _resolve_decision_fn, _validate_decision

from .pokemon_env import GAME, TIMER_DEFAULT, Player, _to_choice, lineup_ok, random_action

CLOCKS = ("vgc", "none")


# ------------------------------------------------------------------------------------------ clocks
class SimClock:
    """Simulated seconds for one seat (stand-in model calls advance it; nothing sleeps)."""

    def __init__(self) -> None:
        self.t = 0.0

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class VGCClock:
    """Showdown's "VGC Timer" for one seat, as pokemon-showdown 0.11.11 runs it (data/rulesets "VGC Timer": Timer
    Starting 420, Grace 90, Add Per Turn 0, Max Per Turn 55, Max First Turn 90, Timeout Auto Choose; server/
    room-battle RoomBattleTimer). The bank starts at STARTING + GRACE; the first request (Team Preview) may take
    MAX_FIRST_TURN, every later one min(bank, MAX_PER_TURN), and a new turn caps the bank at STARTING (+ ADD_PER_TURN).
    The bank is charged in TICK-second ticks while a request is open (an answer within 5 s costs nothing). A request
    still open when its turn time is gone costs the whole turn and is auto-chosen while the bank is positive;
    otherwise the seat "lost due to inactivity". Assumes the platform keeps the seat connected (Showdown's
    knownActive) and adds no latency of its own."""

    STARTING, GRACE, MAX_PER_TURN, MAX_FIRST_TURN, ADD_PER_TURN, TICK = 420.0, 90.0, 55.0, 90.0, 0.0, 5.0

    def __init__(self) -> None:
        self.left = self.STARTING + self.GRACE
        self.first = True

    def request(self, used: float) -> str:
        """One request answered after `used` seconds (math.inf = never): 'ok' | 'default' | 'forfeit'."""
        limit = self.MAX_FIRST_TURN if self.first else self.MAX_PER_TURN
        if not self.first:
            self.left = min(self.left + self.ADD_PER_TURN, self.STARTING)
        self.first = False
        turn = min(self.left, limit)
        if used < turn:
            self.left -= math.floor(used / self.TICK) * self.TICK
            return "ok"
        self.left -= turn
        return "default" if self.left > 0 else "forfeit"


# ------------------------------------------------------------------------------------------ platform checks
def is_template(action) -> bool:
    """A structured template (Team Preview `select_lineup`, doubles `doubles_turn`): a legal action whose
    input['action'] describes what to fill in (its type is the action_id itself, or it carries instructions), the
    same test as examples/llm_agent.py `_is_template`. Returning it as-is (by action id) is INVALID_ACTION."""
    tmpl = (getattr(action, "input", None) or {}).get("action")
    return isinstance(tmpl, dict) and (tmpl.get("type") == action.action_id or "instructions" in tmpl)


def _indexed(tmpl: dict) -> dict:
    """The public doubles template with the private option indices _to_choice expects (legality only)."""
    return {"slots": [{**s, "options": [{**o, "_k": i, "_j": i} for i, o in enumerate(s.get("options") or [])]}
                      for s in tmpl.get("slots") or []]}


def platform_accepts(raw: dict, action) -> bool:
    """Would GameAPI accept this structured `action` in this state? Same rules as the local env's own checks
    (draft: an offered card; Team Preview: pokemon_env.lineup_ok; doubles: pokemon_env._to_choice)."""
    try:
        phase = raw.get("phase")
        if phase == "draft":
            return (isinstance(action, dict) and action.get("type") == "draft_pick"
                    and action.get("card_id") in raw["observation"]["available_card_ids"])
        if phase == "team_preview":
            return bool(lineup_ok(action, [p["species"] for p in raw["observation"]["your_roster"]]))
        if phase == "moving":
            tmpl = raw["legal_actions"]["actions"][0]["input"]["action"]
            return _to_choice(action, {}, _indexed(tmpl)) is not None
    except Exception:                                    # noqa: BLE001 - a malformed payload is a rejection
        return False
    return False


# ------------------------------------------------------------------------------------------ the seat
STAT_KEYS = ("requests", "crashes", "rejected", "timer_defaults", "late", "forfeit", "random_played", "draft_stalls",
             "resigned", "fallbacks", "model_calls", "invalid_answers")


class PlatformSeat(Player):
    """A contestant (create_agent-style factory) played through the official runner's rules and the platform's clocks.

    factory(seat) -> a decision function or an object with choose_action; called once per match (a fresh agent, as a
    fresh match process would build). `seat.sim` (SimClock) and `seat.log` (a print-like callable) are available to it.
    """

    def __init__(self, factory, name: str, *, clock: str = "vgc"):
        if clock not in CLOCKS:
            raise ValueError(f"clock must be one of {CLOCKS}, got {clock!r}")
        self.factory, self.name, self.clock_mode = factory, name, clock
        self.agent = None
        self.stats: dict = {}
        self.crash_reason: str | None = None

    def reset(self) -> None:
        self.sim = SimClock()
        self.stats = dict.fromkeys(STAT_KEYS, 0)
        self.crash_reason = None
        self.down = False
        self.clock = VGCClock() if self.clock_mode == "vgc" else None
        self.agent = self.factory(self)
        self.fn = _resolve_decision_fn(self.agent)

    def log(self, line: str) -> None:
        if "FALLBACK" in str(line):
            self.stats["fallbacks"] += 1
        elif "invalid model response" in str(line):      # examples/llm_agent.py: an answer that did not fit, retried
            self.stats["invalid_answers"] += 1

    # ---- one request ----------------------------------------------------------------------------------
    def choose(self, raw: dict):
        phase = raw.get("phase")
        self.stats["requests"] += 1
        out, used = None, math.inf
        if not self.down:
            t0 = self.sim.t
            out = self._decide(raw)
            used = self.sim.t - t0
            if out is RESIGN:
                self.stats["resigned"] += 1
                return RESIGN
        self._sync_model_calls()
        if phase == "draft":                             # no idle timer in the draft: late answers still land
            if out is None:
                self.stats["draft_stalls"] += 1
            return out
        if self.clock is None:                           # documented platform fallback: random play after 300 s
            if out is None:
                self.stats["random_played"] += 1
                return random_action(raw)
            return out
        verdict = self.clock.request(used if out is not None else math.inf)
        if verdict == "ok":
            return out
        if verdict == "forfeit":
            self.stats["forfeit"] = 1
            return RESIGN
        self.stats["timer_defaults"] += 1
        self.stats["late"] += out is not None
        return TIMER_DEFAULT

    def _decide(self, raw: dict):
        """The payload the platform would accept from this agent now, RESIGN, or None (the runner went down)."""
        state = GameState.from_mcp_state(raw)
        ctx = DecisionContext(session_id=raw.get("session_id", "local"), tournament_id=None, game_type=GAME,
                              agent_id=self.name)
        try:
            play = _validate_decision(self.fn(state, ctx), state.legal_actions)
        except Exception as e:                           # noqa: BLE001 - the runner's DecisionError: match dropped
            return self._crash(f"{type(e).__name__}: {e}")
        if play is RESIGN:
            return RESIGN
        if play.action is not None:
            action = play.action
        else:
            la = next(a for a in state.legal_actions if a.action_id == play.action_id)
            if is_template(la):
                self.stats["rejected"] += 1
                return self._crash(f"INVALID_ACTION: the {play.action_id!r} template was sent back as-is")
            action = la.input.get("action")
        if not platform_accepts(raw, action):
            self.stats["rejected"] += 1
            return self._crash(f"INVALID_ACTION: {str(action)[:200]}")
        return action

    def _crash(self, reason: str):
        self.stats["crashes"] += 1
        self.down = True
        if self.crash_reason is None:
            self.crash_reason = reason
        return None

    def _sync_model_calls(self) -> None:
        provider = getattr(self.agent, "_provider", None)
        if provider is not None:
            self.stats["model_calls"] = getattr(provider, "calls", 0)


TIMED_PHASES = ("team_preview", "moving")
WALL_KEYS = ("requests", "timer_defaults", "forfeit", "max_s", "draft_max_s", "bank_left")


class WallClockSeat(Player):
    """Any local Player (the panel's candidate) on the platform's VGC timer, charged with the REAL wall time of each
    of its decisions. PlatformSeat runs the example agents on simulated time; our own agent takes real time, and a
    team-preview or battle answer slower than the timer allows would never have been played on the platform. Such
    an answer becomes TIMER_DEFAULT (Showdown chose for the seat) and an empty bank RESIGN (lost by inactivity),
    with the same VGCClock as PlatformSeat, so a too-slow candidate shows timer-played decisions and forfeits
    instead of winning with answers that could not have landed. The draft has no idle timer: only measured.

    stats (per match): requests (timed ones), timer_defaults, forfeit, max_s (slowest timed decision, s),
    draft_max_s, bank_left (VGC bank at the end, s). `timer` is injectable for tests."""

    def __init__(self, player, *, timer=time.perf_counter):
        self.player, self.timer = player, timer
        self.name = getattr(player, "name", "player")
        self.clock = VGCClock()
        self.stats: dict = {}

    @property
    def agent(self):
        return getattr(self.player, "agent", None)

    def reset(self) -> None:
        self.player.reset()
        self.clock = VGCClock()
        self.stats = dict.fromkeys(WALL_KEYS, 0)
        self.stats["bank_left"] = self.clock.left

    def choose(self, raw: dict):
        t0 = self.timer()
        out = self.player.choose(raw)
        used = self.timer() - t0
        if raw.get("phase") not in TIMED_PHASES:
            self.stats["draft_max_s"] = max(self.stats["draft_max_s"], used)
            return out
        self.stats["requests"] += 1
        self.stats["max_s"] = max(self.stats["max_s"], used)
        verdict = self.clock.request(used)
        self.stats["bank_left"] = self.clock.left
        if verdict == "ok":
            return out
        if verdict == "forfeit":
            self.stats["forfeit"] = 1
            return RESIGN
        self.stats["timer_defaults"] += 1
        return TIMER_DEFAULT


# ------------------------------------------------------------------------------------------ the LLM stand-in
class StandInModel:
    """Deterministic stand-in for examples/llm/providers.py's OpenAIProvider (we have no OPENAI_API_KEY). It never
    answers: mode 'error' fails at once (what a bad key, an exhausted quota or an outage returns), mode 'timeout'
    hangs for the provider's REQUEST_TIMEOUT_SECONDS on the seat's simulated clock and then fails. Either way the
    real LLMAgent retries once (MAX_ATTEMPTS) and then plays its deterministic FALLBACK."""

    model = "stand-in (no model)"

    def __init__(self, mode: str, sim: SimClock):
        if mode not in ("error", "timeout"):
            raise ValueError(f"mode must be 'error' or 'timeout', got {mode!r}")
        self.mode, self.sim, self.calls = mode, sim, 0

    def complete_structured(self, messages, schema_name, schema):
        from examples.llm.providers import REQUEST_TIMEOUT_SECONDS, ProviderError
        self.calls += 1
        if self.mode == "timeout":
            self.sim.advance(REQUEST_TIMEOUT_SECONDS)
            raise ProviderError("OpenAI request failed (ReadTimeout)")
        raise ProviderError("OpenAI request failed (HTTP 401)")


class WallTimed:
    """A live provider whose calls' real wall time is charged to the seat's SimClock (so the VGC timer sees the
    model's actual latency); counts calls like StandInModel."""

    def __init__(self, provider, sim: SimClock):
        self.provider, self.sim, self.calls = provider, sim, 0
        self.model = getattr(provider, "model", "?")

    def complete_structured(self, messages, schema_name, schema):
        self.calls += 1
        t0 = time.perf_counter()
        try:
            return self.provider.complete_structured(messages, schema_name, schema)
        finally:
            self.sim.advance(time.perf_counter() - t0)


# ------------------------------------------------------------------------------------------ registry
def _example(module: str):
    def factory(seat: PlatformSeat):
        return importlib.import_module(module).create_agent()
    return factory


def _llm(mode: str):
    def factory(seat: PlatformSeat):
        from examples.llm_agent import LLMAgent
        return LLMAgent(StandInModel(mode, seat.sim), log=seat.log)
    return factory


def _llm_live(seat: PlatformSeat):
    from examples.llm.providers import provider_from_env   # raises without OPENAI_API_KEY in the environment
    from examples.llm_agent import LLMAgent
    return LLMAgent(WallTimed(provider_from_env(), seat.sim), log=seat.log)


FIELD = {
    "basic": _example("examples.basic_agent"),
    "messaging": _example("examples.messaging_agent"),
    "smoke": _example("examples.smoke_agent"),
    "llm_fallback": _llm("error"),
    "llm_timeout": _llm("timeout"),
}
def _llm_real(seat: PlatformSeat):
    from .llm_seats import vgc_agent                    # Nebius; no key / over budget -> the agent's rule fallback
    return vgc_agent(seat.sim, seat.log)


LIVE = {"llm_live": _llm_live, "llm_real": _llm_real}


def make(name: str, clock: str = "vgc") -> PlatformSeat:
    factory = FIELD.get(name) or LIVE.get(name)
    if factory is None:
        raise ValueError(f"unknown field opponent {name!r}; known: {', '.join([*FIELD, *LIVE])}")
    return PlatformSeat(factory, name, clock=clock)
