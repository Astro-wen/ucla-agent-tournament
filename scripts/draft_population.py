"""Mixed-population draft data: OUR pilot (this tree's agent) in one seat, an opponent drawn from a population of
drafter + pilot families in the other. One JSON line per game, for scripts/fit_draft_model.py.

    python scripts/draft_population.py --games 3000 --seed 401 --out work/pop/p401.jsonl
    python scripts/draft_population.py --games 14 --seed 1 --out /tmp/x.jsonl --families v3,v1     # smoke test

Why: scripts/draft_selfplay.py puts our pilot in BOTH seats, so the fitted card values are "what wins when v3
plays both sides". On the platform the other seat drafts and pilots with its own code. Here the other seat is one of

  family  drafts with                               pilots with (team preview + battle)
  v3      our Drafter (eps random)                  this tree's agent (= self-play)
  cs3     card_score greedy (v0-v2 draft)           this tree's agent
  v2      card_score greedy (its own)               versions/v2_joint_lookahead
  v1      card_score greedy (its own)               versions/v1_exact_damage
  v0      card_score greedy (its own)               versions/v0_heuristic (base-stat heuristic battle)
  bst     highest base-stat total                   versions/v1_exact_damage
  rand    uniformly random                          versions/v2_joint_lookahead

Every opponent pick is a uniformly random legal card with probability `eps` (per family, below), so its rosters
vary; our own seat drafts randomly with probability --our-eps (a comma list: one value drawn per game), else by
its own Drafter. Pools are the local env's (platform card frequencies, item-blocked cards listed: local/pool.py).

Row: seed, family, ours (our seat), eps [seat0, seat1], pool, first, rosters [seat0, seat1] (card ids, pick order),
orders, winner (seat), turns, invalid [seat0, seat1]. Self-play seeds (seed * 1000003 + g) never collide with
ladder / benchmark seeds (seed * 100000 + g for small seeds).
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import random
import sys
import time
from pathlib import Path

os.environ["ARENA_RECORD"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.pokemon.dex import base_stats, to_id                      # noqa: E402
from agent.pokemon.policy import card_score                          # noqa: E402
from altruagent import LegalAction                                    # noqa: E402
from local.pokemon_env import ContestantPlayer, Match                 # noqa: E402
from local.showdown import Showdown                                   # noqa: E402

# family -> (agent module whose preview + battle pilot the seat, drafter override or None = its own, eps)
FAMILIES = {
    "v3": ("agent.agent", None, 0.5),
    "cs3": ("agent.agent", "card_score", 0.3),
    "v2": ("versions.v2_joint_lookahead.agent.agent", None, 0.5),
    "v1": ("versions.v1_exact_damage.agent.agent", None, 0.5),
    "v0": ("versions.v0_heuristic.agent.agent", None, 0.5),
    "bst": ("versions.v1_exact_damage.agent.agent", "bst", 0.3),
    "rand": ("versions.v2_joint_lookahead.agent.agent", "random", 1.0),
}


def _card_id(out) -> str | None:
    if isinstance(out, LegalAction):
        act = out.input.get("action") if isinstance(out.input, dict) else None
        if isinstance(act, dict) and act.get("card_id"):
            return str(act["card_id"])
        return str(out.action_id).split(":", 1)[-1] if out.action_id else None
    if isinstance(out, dict):
        act = out.get("action") if isinstance(out.get("action"), dict) else out
        return act.get("card_id")
    if isinstance(out, str):
        return out.split(":", 1)[-1]
    return None


def bst_pick(cards: list[dict], rng: random.Random) -> dict:
    """Highest base-stat total; ties by card_score, then at random."""
    def key(c):
        return (sum(base_stats(c.get("species", "")).values()), card_score(c, []), rng.random())
    return max(cards, key=key)


class PopulationSeat(ContestantPlayer):
    """An agent that pilots team preview + battle itself; its draft pick may be replaced by a random legal card
    (probability eps) or by another drafter (card_score greedy / highest base stats / random). The agent still sees
    every draft state, and its memory of its own picks is corrected like scripts/draft_selfplay.py does."""

    def __init__(self, factory, name: str, drafter: str | None, eps: float, rng: random.Random):
        super().__init__(factory, name)
        self.drafter, self.eps, self.rng = drafter, eps, rng

    def choose(self, raw: dict):
        out = super().choose(raw)
        if raw.get("phase") != "draft":
            return out
        la = raw["legal_actions"]["actions"]
        legal = [a["input"]["action"]["card_id"] for a in la]
        obs = raw["observation"]
        offered = {c["card_id"]: c for c in obs["available_cards"]}
        cards = [offered[c] for c in legal if c in offered]
        own = _card_id(out)
        if self.rng.random() < self.eps or self.drafter == "random":
            pick = self.rng.choice(cards)
        elif self.drafter == "card_score":
            mine = [offered.get(r["card_id"]) or {"species": r["species"]}
                    for r in obs["rosters"].get(raw["current_actor"]["agent_id"], [])]
            pick = max(cards, key=lambda c: card_score(c, mine))
        elif self.drafter == "bst":
            pick = bst_pick(cards, self.rng)
        else:
            return out
        if own == pick["card_id"]:
            return out
        pk = self.agent.pk
        own_card = offered.get(own) if own else None
        if own_card is not None and to_id(own_card["species"]) != to_id(pick["species"]):
            pk.cards.pop(to_id(own_card["species"]), None)
        pk.cards[to_id(pick["species"])] = pick
        return {"type": "draft_pick", "card_id": pick["card_id"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--families", default=",".join(FAMILIES))
    ap.add_argument("--our-eps", default="1.0,1.0,1.0,0.5,0.5", help="comma list; one value drawn per game")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    fams = [f.strip() for f in a.families.split(",") if f.strip()]
    for f in fams:
        if f not in FAMILIES:
            sys.exit(f"unknown family {f}; known: {', '.join(FAMILIES)}")
    our_eps = [float(x) for x in a.our_eps.split(",")]
    rng = random.Random(a.seed * 7919)
    ours_factory = importlib.import_module("agent.agent").create_agent
    ours = PopulationSeat(ours_factory, "ours", None, 1.0, rng)
    opps = {}
    for f in fams:
        mod, drafter, eps = FAMILIES[f]
        opps[f] = PopulationSeat(importlib.import_module(mod).create_agent, f, drafter, eps, rng)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    done = {f: [0, 0] for f in fams}
    with Showdown() as sd, open(a.out, "a") as fo:
        for g in range(a.games):
            fam = fams[g % len(fams)]
            seat = (g // len(fams)) % 2
            ours.eps = rng.choice(our_eps)
            players = [ours, opps[fam]] if seat == 0 else [opps[fam], ours]
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
            except KeyboardInterrupt:
                print("interrupted", g, flush=True)
                break
            except Exception as e:                       # noqa: BLE001
                print("game failed", g, fam, repr(e), flush=True)
                continue
            first = next(i for i, sid in enumerate(m.ids) if m.picks[0]["seat_id"] == sid)
            eps = [ours.eps, opps[fam].eps] if seat == 0 else [opps[fam].eps, ours.eps]
            fo.write(json.dumps({"seed": seed, "family": fam, "ours": seat, "eps": eps,
                                 "pool": [c["card_id"] for c in m.pool], "first": first,
                                 "rosters": [[c["card_id"] for c in ro] for ro in m.rosters],
                                 "orders": orders, "winner": r.winner, "turns": r.turns,
                                 "invalid": r.invalid, "waived": r.clause_waived}) + "\n")
            fo.flush()
            done[fam][0] += 1
            done[fam][1] += r.winner == seat
            if (g + 1) % 35 == 0:
                print(f"{g + 1}/{a.games} games, {time.time() - t0:.0f}s | our wins "
                      + " ".join(f"{f} {w}/{n}" for f, (n, w) in done.items()), flush=True)
    print(f"done {sum(n for n, _ in done.values())} games, {time.time() - t0:.0f}s | our wins "
          + " ".join(f"{f} {w}/{n}" for f, (n, w) in done.items()), flush=True)


if __name__ == "__main__":
    main()
