"""`meta`: an evaluation opponent that DRAFTS like the real contestants we met, and battles with our own code.

Why: every other local opponent drafts with our own model (champion, simbot, vgcbench_*) or with a fixed card_score
rule (v0-v2, the scripted bots). The real contestants (22 platform games, 10-08/09) draft the standard competitive
core instead: Flutter Mane, Incineroar, Dragonite, Iron Hands, Rillaboom, Landorus-T early, with public reasons such as
"beats Gyarados". Our draft model predicts only ~19% of their picks; a conditional logit fitted on those picks
(scripts/fit_meta_drafter.py -> data/meta_drafter.json) predicts ~58% (leave one game out).

Draft: the legal card with the highest  u[species] + gamma * (mean calc duel of the card against OUR roster so far)
(`meta`, `meta_v3`; ties by card_id), or a draw from the fitted softmax over those scores (`meta_soft`, from a per-match
random stream derived from, never drawn from, the match's own RNG, so paired runs see the same draws for the same
states). The Item Clause is the env's legal-action list. Unknown species score `mu`.

Pilot (team preview + battle): our own frozen code, which also sees every draft state (it caches the sets as usual)
and gets its memory of its own picks corrected, like scripts/draft_population.py's PopulationSeat:
  meta       champion/agent  (v4.3: engine-search battle, 90x90 preview game) -> a mirror with only the draft different
  meta_soft  champion/agent, softmax draft
  meta_v3    versions/v3_draft_preview (classic battle; much faster, weaker pilot)
META_DRAFTER_FILE in the shell selects another fit (hold-out experiments); the file used is in `params`.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import random
from pathlib import Path

from .pokemon_env import ContestantPlayer

NAMES = ("meta", "meta_soft", "meta_v3")
PILOTS = {"meta": "champion.agent", "meta_soft": "champion.agent", "meta_v3": "versions.v3_draft_preview.agent"}
DATA = Path(__file__).resolve().parents[1] / "data" / "meta_drafter.json"


def _sid(x: str) -> str:
    return "".join(ch for ch in str(x).lower() if ch.isalnum())


class MetaDrafter:
    """Scores legal cards like the real opponents (data/meta_drafter.json)."""

    def __init__(self, path: str | Path | None = None, tt_pkg: str = "champion.agent"):
        self.path = Path(path or os.environ.get("META_DRAFTER_FILE") or DATA)
        d = json.loads(self.path.read_text())
        self.u = {str(k): float(v) for k, v in d["u"].items()}
        self.mu = float(d.get("mu", 0.0))
        self.gamma = float(d.get("gamma", 0.0))
        self.tt_pkg = tt_pkg

    def scores(self, cards: list[dict], other: list[dict]) -> dict[str, float]:
        """card_id -> score; `other` = the opposing roster so far (full cards where known)."""
        ctr = dict.fromkeys((c["card_id"] for c in cards), 0.0)
        known = [o for o in other if o.get("moves")]
        if self.gamma and known:
            drafter = importlib.import_module(f"{self.tt_pkg}.pokemon.drafter")
            matchup = importlib.import_module(f"{self.tt_pkg}.pokemon.matchup")
            tt = drafter.threat_table()
            tt.ensure(list(cards) + known)
            for c in cards:
                ctr[c["card_id"]] = sum(tt.beats(matchup.card_key(c), matchup.card_key(o)) - 0.5
                                        for o in known) / len(known)
        return {c["card_id"]: self.u.get(_sid(c["species"]), self.mu) + self.gamma * ctr[c["card_id"]] for c in cards}


class MetaSeat(ContestantPlayer):
    """Our frozen agent piloting preview + battle; its draft picks replaced by the meta drafter's."""

    def __init__(self, name: str = "meta"):
        if name not in PILOTS:
            raise ValueError(f"unknown meta variant {name!r}; known: {NAMES}")
        self.calc_pkg = PILOTS[name]
        mod = importlib.import_module(f"{self.calc_pkg}.agent")
        super().__init__(mod.create_agent, name)
        self.soft = name == "meta_soft"
        self.drafter = MetaDrafter(tt_pkg=self.calc_pkg)
        self.params = {"file": str(self.drafter.path), "gamma": self.drafter.gamma, "soft": self.soft}
        self.rng = random.Random(0)
        self.seen: dict[str, dict] = {}
        self.picks: list[str] = []

    def reset(self) -> None:
        super().reset()
        self.seen = {}
        self.picks = []

    def bind(self, match, seat: int) -> None:
        """A per-match random stream derived from (not drawn from) the match's RNG (only meta_soft uses it)."""
        h = hashlib.sha256(repr(match.rng.getstate()).encode()).hexdigest()
        self.rng = random.Random(int(h[:16], 16) ^ (seat + 11))

    def choose(self, raw: dict):
        out = super().choose(raw)
        if raw.get("phase") != "draft":
            return out
        obs = raw.get("observation") or {}
        for c in obs.get("available_cards") or []:
            self.seen[c["card_id"]] = c
        legal = [a["input"]["action"]["card_id"] for a in raw["legal_actions"]["actions"]]
        cards = [self.seen[c] for c in legal if c in self.seen]
        if not cards:
            return out
        me = (raw.get("current_actor") or {}).get("agent_id") or obs.get("current_seat")
        other = [self.seen.get(r["card_id"]) or {"card_id": r["card_id"], "species": r.get("species", "")}
                 for seat, ro in (obs.get("rosters") or {}).items() if seat != me for r in ro]
        sc = self.drafter.scores(cards, other)
        if self.soft:
            mx = max(sc.values())
            w = [math.exp(sc[c["card_id"]] - mx) for c in cards]
            pick = self.rng.choices(cards, weights=w, k=1)[0]
        else:
            pick = max(cards, key=lambda c: (sc[c["card_id"]], c["card_id"]))
        own = _card_id(out)
        self.picks.append(pick["card_id"])
        if own == pick["card_id"]:
            return out
        pk = getattr(self.agent, "pk", None)
        if pk is not None:                       # the pilot's memory of its own six (scripts/draft_population.py)
            own_card = self.seen.get(own) if own else None
            if own_card is not None and _sid(own_card["species"]) != _sid(pick["species"]):
                pk.cards.pop(_sid(own_card["species"]), None)
            pk.cards[_sid(pick["species"])] = pick
        return {"type": "draft_pick", "card_id": pick["card_id"]}


def _card_id(out) -> str | None:
    if hasattr(out, "input") and isinstance(getattr(out, "input"), dict):
        act = out.input.get("action")
        if isinstance(act, dict) and act.get("card_id"):
            return str(act["card_id"])
        aid = getattr(out, "action_id", None)
        return str(aid).split(":", 1)[-1] if aid else None
    if isinstance(out, dict):
        act = out.get("action") if isinstance(out.get("action"), dict) else out
        return act.get("card_id")
    if isinstance(out, str):
        return out.split(":", 1)[-1]
    return None


def make(name: str) -> MetaSeat:
    return MetaSeat(name)
