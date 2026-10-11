"""Draft-only simulation: our drafter (several configurations) vs one opponent drafter on the same sampled pools,
the final sixes judged by the v4.3 draft model (no battles; seconds per draft, so thousands of drafts are cheap).

    python scripts/draft_sim.py --opp meta --games 400 --seed 101 \
        --cand off='{}' --cand meta='{"opp_mode": "meta"}' --cand w1='{"opp_mode": "meta", "opp_w": 1}'

Opponents (--opp): champion (champion/agent's drafter = v4.3), meta / meta_soft / meta_u (local/meta_opp.py's
real-contestant drafter: argmax, softmax draws, or u only without the counter term), cs (card_score greedy).
A candidate is this tree's Drafter with the given keyword arguments (agent/pokemon/drafter.py: opp_mode, opp_prior,
opp_temp, opp_k, opp_w, ...), called exactly as agent/pokemon/policy.py calls it. Pools: local/pool.py's sample_pool
with the match seed seed * 100000 + g, first drafter g % 2 (like local.panel), Item Clause as local/pokemon_env.py
(a dead end waives it for that pick).

The judge is the model that drafts for v4.3 (agent/pokemon/draft_model.json): predicted P(our six beats theirs) =
sigmoid(log-odds). It is our own valuation, so this measures how well a configuration plans against an opponent
GIVEN our values (what an opponent model changes), not whether the values are right: battles decide that.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["ARENA_RECORD"] = "0"

SNAKE = "ABBAABBAABBA"


def _item(c):
    from agent.pokemon.dex import to_id
    return to_id(c.get("item") or "") or None


class Ours:
    def __init__(self, kwargs: dict, pkg: str = "agent"):
        self.kw = kwargs
        self.drafter_mod = importlib.import_module(f"{pkg}.pokemon.drafter")
        self.policy = importlib.import_module(f"{pkg}.pokemon.policy")
        self.last_info = {}

    def pick(self, pool, mine, opp, legal, n_taken, rng):
        d = self.drafter_mod.Drafter(prior_score=lambda c: self.policy.card_score(c, []), **self.kw)
        cid = d.pick(pool, mine, opp, legal, n_taken=n_taken)
        self.last_info = d.info
        return cid


class Meta:
    def __init__(self, soft=False, gamma=None):
        from local.meta_opp import MetaDrafter
        self.d = MetaDrafter(tt_pkg="champion.agent")
        if gamma is not None:
            self.d.gamma = gamma
        self.soft = soft

    def pick(self, pool, mine, opp, legal, n_taken, rng):
        cards = [c for c in pool if c["card_id"] in legal]
        sc = self.d.scores(cards, opp)
        if self.soft:
            mx = max(sc.values())
            return rng.choices([c["card_id"] for c in cards], weights=[math.exp(sc[c["card_id"]] - mx) for c in cards])[0]
        return max(cards, key=lambda c: (sc[c["card_id"]], c["card_id"]))["card_id"]


class CardScore:
    def __init__(self):
        from agent.pokemon.policy import card_score
        self.cs = card_score

    def pick(self, pool, mine, opp, legal, n_taken, rng):
        cards = [c for c in pool if c["card_id"] in legal]
        return max(cards, key=lambda c: (self.cs(c, mine), c["card_id"]))["card_id"]


def make_opp(name):
    if name == "champion":
        return Ours({}, pkg="champion.agent")
    if name == "meta":
        return Meta()
    if name == "meta_soft":
        return Meta(soft=True)
    if name == "meta_u":
        return Meta(gamma=0.0)
    if name == "cs":
        return CardScore()
    raise SystemExit(f"unknown opponent {name}")


def draft(pool, players, first, rng_opp):
    """players[0] = candidate seat, players[1] = opponent; first = the seat that drafts first. Returns the sixes."""
    rosters = [[], []]
    taken = set()
    for n, letter in enumerate(SNAKE):
        seat = first if letter == "A" else 1 - first
        mine_items = {_item(c) for c in rosters[seat]} - {None}
        undrafted = [c for c in pool if c["card_id"] not in taken]
        offered = [c for c in undrafted if not (_item(c) and _item(c) in mine_items)] or undrafted
        legal = {c["card_id"] for c in offered}
        try:
            cid = players[seat].pick(undrafted, rosters[seat], rosters[1 - seat], legal, n, rng_opp)
        except Exception as e:  # noqa: BLE001
            print("pick failed:", repr(e), file=sys.stderr)
            cid = None
        if cid not in legal:
            cid = sorted(legal)[0]
        card = next(c for c in undrafted if c["card_id"] == cid)
        rosters[seat].append(card)
        taken.add(cid)
    return rosters


def judge(pool, ours, theirs, drafter_mod, resid=0.0):
    d = drafter_mod.Drafter(opp_mode="off", resid=resid)
    cards = list(pool)
    idx = {c["card_id"]: i for i, c in enumerate(cards)}
    m, P, S = d._tables(cards)
    mo = d.m_opp
    a = [idx[c["card_id"]] for c in ours]
    b = [idx[c["card_id"]] for c in theirs]
    L = sum(m[i] for i in a) - sum(mo[j] for j in b) + sum(P[i][j] for i in a for j in b) \
        + sum(S[x][y] for k, x in enumerate(a) for y in a[k + 1:]) - sum(S[x][y] for k, x in enumerate(b) for y in b[k + 1:])
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--opp", default="meta")
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--seed", type=int, default=101)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--cand", action="append", default=[], help="name=JSON kwargs for this tree's Drafter")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    from local.pool import POOL, sample_pool
    import agent.pokemon.drafter as drafter_mod
    print("agent from", importlib.import_module("agent").__file__, flush=True)
    cands = []
    for spec in a.cand or ["off={}"]:
        name, _, js = spec.partition("=")
        cands.append((name, Ours(json.loads(js or "{}"))))
    opp = make_opp(a.opp)
    rows = []
    fo = open(a.out, "a") if a.out else None
    t0 = time.time()
    for g in range(a.start, a.start + a.games):
        seed = a.seed * 100000 + g
        pool = sample_pool(random.Random(seed), POOL, 18)
        first = g % 2                     # seat 0 = candidate
        row = {"g": g, "seed": seed, "cand_first": first == 0}
        for name, c in cands:
            r6 = draft(pool, [c, opp], first, random.Random(seed * 7919 + 1))
            L = judge(pool, r6[0], r6[1], drafter_mod)
            Lr = judge(pool, r6[0], r6[1], drafter_mod, resid=1.0)
            row[name] = {"L": round(L, 4), "p": round(1 / (1 + math.exp(-L)), 4),
                         "pr": round(1 / (1 + math.exp(-Lr)), 4),
                         "ours": [x["card_id"] for x in r6[0]], "theirs": [x["card_id"] for x in r6[1]]}
        rows.append(row)
        if fo:
            fo.write(json.dumps(row) + "\n")
            fo.flush()
        if (g - a.start + 1) % 50 == 0:
            print(f"  {g - a.start + 1} drafts, {time.time() - t0:.0f}s", flush=True)
    names = [n for n, _ in cands]
    print(f"\nopponent {a.opp}, {len(rows)} drafts, seed {a.seed}")
    base = names[0]
    for n in names:
        same = sum(r[n]["ours"] == r[base]["ours"] for r in rows)
        txt = []
        for key, label in (("p", "v4.3 model"), ("pr", "+v4 residuals")):
            ps = [r[n][key] for r in rows]
            d = [r[n][key] - r[base][key] for r in rows]
            md = sum(d) / len(d)
            sd = math.sqrt(sum((x - md) ** 2 for x in d) / max(1, len(d) - 1))
            txt.append(f"{label}: {100 * sum(ps) / len(ps):5.1f}% ({100 * md:+5.2f} se {100 * sd / math.sqrt(len(d)):.2f})")
        print(f"  {n:14s} " + "   ".join(txt) + f"   identical sixes {same}/{len(rows)}")
    if fo:
        fo.close()


if __name__ == "__main__":
    main()
