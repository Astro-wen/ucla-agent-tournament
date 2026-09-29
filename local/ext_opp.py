"""`simbot`: an outside-style VGC doubles opponent that searches the real Showdown engine (local/js/simbot.js).

Why: every other sparring partner is our own code (v0-v3, cautious) or a scripted max-damage bot (greedy,
protect_spam), and the open learned bots need downloads (VGC-Bench: torch + checkpoints) or an LLM. simbot is
built on a different principle from our agent: no damage calculator or hand-written tactics in the battle, just
play-outs of the real engine. Each battle decision is a matrix game over joint two-slot actions, every cell played
on a clone of the live battle (fresh random seed, the other side's already-sent choice cleared) for one turn plus one
greedy turn, scored by HP / alive / status / boosts / Tailwind / screens / Trick Room; the equilibrium mix is solved
by regret matching and sampled. So it uses Protect, Fake Out, Follow Me, Helping Hand, speed control and switches
whenever the engine says they pay, which the scripted bots never do. See local/js/simbot.js for the details.

Information edge (documented, small): the clone is the real battle, so simbot knows the opponent's exact HP and which
four they brought; their sets are public anyway (Open Team Sheets / the draft). It never sees the opponent's choice
for the current turn or the real future random rolls.

Draft and team preview: simbot has no drafter of its own. By default (`simbot`) it drafts and picks its four with
the CHAMPION's own code (champion/agent: model drafter + 90x90 preview game), so a normal-draft match measures battle
skill on realistic, equally well drafted teams; `simbot_v1` drafts and previews like versions/v1_exact_damage (the
fixed card_score rule the scripted panel bots use). Same-team matches (play_match(same_team=True), local.panel
--same-team) take the draft out entirely.

Every answer goes through the env's normal protocol (doubles_turn dict checked against the template; anything
else counts as invalid there). If the search fails or answers something the template does not allow, simbot plays a
random legal action and counts a fallback (stats["fallbacks"]).

Knobs (environment, read when the player is made; recorded in `params`): SIMBOT_SAMPLES (2), SIMBOT_ROLLOUT (1),
SIMBOT_KEEP (8), SIMBOT_PROBES (3), SIMBOT_BUDGET_MS (3000), SIMBOT_MODE (nash | maximin | argmax), SIMBOT_MIN_PROB
(0.05), SIMBOT_ITERS (1500).
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import os
import random
import time

from .pokemon_env import ContestantPlayer, Player, random_action

log = logging.getLogger("local.ext_opp")

NAMES = ("simbot", "simbot_v1")
DRAFTERS = {"simbot": "champion.agent", "simbot_v1": "versions.v1_exact_damage.agent"}
KNOBS = {"samples": ("SIMBOT_SAMPLES", int), "rollout": ("SIMBOT_ROLLOUT", int), "keep": ("SIMBOT_KEEP", int),
         "probes": ("SIMBOT_PROBES", int), "budget_ms": ("SIMBOT_BUDGET_MS", int), "mode": ("SIMBOT_MODE", str),
         "min_prob": ("SIMBOT_MIN_PROB", float), "iters": ("SIMBOT_ITERS", int)}
STAT_KEYS = ("decisions", "fallbacks", "errors", "sims", "ms_total", "ms_max")


def knobs_from_env(environ=None) -> dict:
    environ = os.environ if environ is None else environ
    out = {}
    for key, (var, cast) in KNOBS.items():
        if environ.get(var):
            out[key] = cast(environ[var])
    return out


def template_ok(raw: dict, out: dict) -> bool:
    """Is `out` (a doubles_turn dict) allowed by the public template of `raw`? Same rules as the env's _to_choice."""
    tmpl = raw["legal_actions"]["actions"][0]["input"]["action"]
    used = set()
    for slot in tmpl.get("slots", []):
        c = out.get(f"slot_{slot['slot']}")
        if not isinstance(c, dict):
            return False
        opts = slot.get("options") or []
        if c.get("type") == "move":
            o = next((o for o in opts if o["type"] == "move" and o["move_id"] == c.get("move_id")), None)
            if o is None or c.get("target") not in o["targets"]:
                return False
        elif c.get("type") == "switch":
            if c.get("species") in used or not any(o["type"] == "switch" and o["species"] == c.get("species")
                                                   for o in opts):
                return False
            used.add(c.get("species"))
        elif c.get("type") == "pass":
            if not any(o["type"] == "pass" for o in opts):
                return False
        else:
            return False
    return True


class SimBot(Player):
    """The engine-search opponent. Needs Match.bind (the live bridge and its seat); draft / preview delegated."""

    def __init__(self, name: str = "simbot", params: dict | None = None):
        if name not in DRAFTERS:
            raise ValueError(f"unknown simbot variant {name!r}; known: {NAMES}")
        self.name = name
        self.params = {**knobs_from_env(), **(params or {})}
        self.calc_pkg = DRAFTERS[name]
        mod = importlib.import_module(f"{self.calc_pkg}.agent")
        self.inner = ContestantPlayer(mod.create_agent, f"{name}-drafter")
        self.match = None
        self.sid = None
        self.rng = random.Random(0)
        self.stats: dict = dict.fromkeys(STAT_KEYS, 0)
        self.trace: list[dict] = []

    @property
    def agent(self):
        return None                      # no BotContestant counters (the panel reads ext stats separately)

    def reset(self) -> None:
        self.inner.reset()
        self.stats = dict.fromkeys(STAT_KEYS, 0)
        self.stats["ms_max"] = 0.0
        self.trace = []

    def bind(self, match, seat: int) -> None:
        """Called by Match.__init__: the live Showdown bridge, our side, and a per-match RNG derived from (not drawn
        from) the match's random state, so the match's own random stream is untouched."""
        self.match, self.sid = match, ("p1" if seat == 0 else "p2")
        h = hashlib.sha256(repr(match.rng.getstate()).encode()).hexdigest()
        self.rng = random.Random(int(h[:16], 16) ^ (seat + 1))

    def choose(self, raw: dict):
        phase = raw.get("phase")
        if phase in ("draft", "team_preview"):
            return self.inner.choose(raw)
        t0 = time.perf_counter()
        self.stats["decisions"] += 1
        out, info = None, {}
        try:
            seed = [self.rng.randrange(1, 65535) for _ in range(4)]
            res = self.match.sd.search(self.sid, seed, self.params)
            if "error" in res:
                raise RuntimeError(f"search error: {res.get('error')} {str(res.get('detail'))[:300]}")
            info = dict(res.get("info") or {}, slots=res["slots"])
            out = {"type": "doubles_turn", **{f"slot_{i}": s for i, s in enumerate(res["slots"])}}
            if not template_ok(raw, out):
                raise RuntimeError(f"search answer {res.get('choice')!r} not in the template")
            self.stats["sims"] += int(info.get("sims") or 0)
            self.stats["errors"] += int(info.get("errors") or 0)
        except Exception as e:  # noqa: BLE001 - never crash the seat: a random legal action, counted
            self.stats["fallbacks"] += 1
            log.warning("simbot fallback (turn %s): %s", (raw.get("observation") or {}).get("turn"), e)
            out = random_action(raw)
            info = {"fallback": str(e)[:200]}
        ms = 1000 * (time.perf_counter() - t0)
        self.stats["ms_total"] += ms
        self.stats["ms_max"] = max(self.stats["ms_max"], ms)
        self.trace.append({"turn": (raw.get("observation") or {}).get("turn"), "ms": round(ms, 1),
                           **{k: info.get(k) for k in ("choice", "slots", "value", "mix", "opp_mix", "rows", "cols",
                                                         "sims", "forced", "fallback") if k in info}})
        return out


def make(name: str) -> SimBot:
    return SimBot(name)
