"""Self-play data for the draft model: randomised drafts, champion preview + battle, one JSON line per game.

    python scripts/draft_selfplay.py --games 500 --seed 1 --eps 0.5 --out data/selfplay/s1.jsonl

Each pick is a uniformly random legal card with probability --eps, else the champion's own draft choice,
so rosters are diverse (card main effects are identifiable) but still look like real drafts. Team preview
and every battle decision are the champion's, i.e. exactly the code that plays the drafted teams in the
ladder. Output rows: pool, first drafter seat, rosters (card ids, pick order), brought/leads, winner, turns.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

os.environ["ARENA_RECORD"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from champion.agent.agent import create_agent as champ_factory   # noqa: E402
from champion.agent.pokemon.dex import to_id                       # noqa: E402
from local.pokemon_env import ContestantPlayer, Match               # noqa: E402
from local.pool import POOL                                         # noqa: E402
from local.showdown import Showdown                                 # noqa: E402


class EpsDrafter(ContestantPlayer):
    """Champion everywhere, but a random legal draft pick with probability eps."""

    def __init__(self, factory, eps: float, rng: random.Random, name: str):
        super().__init__(factory, name)
        self.eps, self.rng = eps, rng

    def choose(self, raw: dict):
        out = super().choose(raw)
        if raw.get("phase") != "draft" or self.rng.random() >= self.eps:
            return out
        la = raw["legal_actions"]["actions"]
        pick = self.rng.choice(la)["input"]["action"]
        card = next(c for c in raw["observation"]["available_cards"] if c["card_id"] == pick["card_id"])
        pk = self.agent.pk
        # the champion remembered its own (overridden) choice: drop it, remember the real pick
        own = next((c for c in raw["observation"]["available_cards"] if c["card_id"] == getattr(out, "action_id", "x").split(":", 1)[-1]), None)
        if own is not None and to_id(own["species"]) != to_id(card["species"]):
            pk.cards.pop(to_id(own["species"]), None)
        pk.cards[to_id(card["species"])] = card
        return {"type": "draft_pick", "card_id": card["card_id"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--eps", type=float, default=0.5)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rng = random.Random(a.seed * 7919)
    players = [EpsDrafter(champ_factory, a.eps, rng, "a"), EpsDrafter(champ_factory, a.eps, rng, "b")]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with Showdown() as sd, open(a.out, "a") as fo:
        for g in range(a.games):
            seed = a.seed * 1000003 + g
            mrng = random.Random(seed)
            random.seed(seed)
            m = Match(sd, players, mrng)
            try:
                m.draft()
                orders = m.preview()
                r = m.battle(orders)
            except (BrokenPipeError, OSError) as e:      # the Showdown bridge died: stop, do not spin
                print("bridge died", g, repr(e), flush=True)
                return
            except Exception as e:                       # noqa: BLE001
                print("game failed", g, repr(e), flush=True)
                continue
            first = next(i for i, sid in enumerate(m.ids) if m.picks[0]["seat_id"] == sid)
            fo.write(json.dumps({"seed": seed, "pool": [c["card_id"] for c in m.pool], "first": first,
                                 "rosters": [[c["card_id"] for c in ro] for ro in m.rosters],
                                 "orders": orders, "winner": r.winner, "turns": r.turns,
                                 "invalid": r.invalid}) + "\n")
            fo.flush()
            if (g + 1) % 25 == 0:
                print(f"{g + 1}/{a.games} games, {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
