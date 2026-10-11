"""Fit how REAL opponents draft (conditional logit over the legal cards of each of their picks).

    python scripts/fit_meta_drafter.py --logs ~/ucla-agent-tournament/logs              # report only
    python scripts/fit_meta_drafter.py --logs ... --write                                 # write both files below
    python scripts/fit_meta_drafter.py --logs ... --half odd --out work/meta_odd.json     # a hold-out fit

Data: every real VGC draft we played against ANOTHER contestant, read from our own recordings
(<logs>/<session>.jsonl: our draft states carry the full pool with sets and every pick so far; the opponent's picks
after our last draft decision come from the team-preview roster). Sessions where we held both seats (self-play, the
old two-agent recordings) are skipped. Only public information is used: the pool, the pick order and the picks.

Model: the opponent picks card c among the legal cards S (pool minus taken minus Item-Clause clashes with its own
roster) with probability  exp(u[c] + gamma * ctr(c)) / sum_{x in S} exp(u[x] + gamma * ctr(x)),  where u is a
per-species utility (L2 toward a common mean, --sigma) and ctr(c) is c's mean calc duel against OUR roster at that
moment (agent/pokemon/matchup.py beats - 0.5; the real opponents' public reasons read "beats Gyarados"). Leave-one-game-
out accuracy is printed next to our own draft model's greedy prediction of the same picks.

Outputs (--write):
  data/meta_drafter.json          u + gamma: the local evaluation opponent `meta*` (local/meta_opp.py)
  agent/pokemon/draft_meta.json   u only (gamma 0, refit without the counter term): the drafter's prior over what a
                                  real opponent takes (agent/pokemon/drafter.py, ARENA_DRAFT_OPP; off by default)
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["ARENA_RECORD"] = "0"

from agent.pokemon.dex import to_id  # noqa: E402

SNAKE = "ABBAABBAABBA"


def _item(c: dict) -> str | None:
    return to_id(c.get("item") or "") or None


def load_games(log_dir: str, exclude: set[str] = frozenset()) -> list[dict]:
    """One entry per real draft against another contestant: pool (card_id -> card), picks [(who, card_id)]."""
    catalog = {c["card_id"]: c for c in json.loads((ROOT / "data" / "vgc_cards.json").read_text())}
    games = []
    for f in sorted(glob.glob(os.path.join(log_dir, "*.jsonl"))):
        sess = Path(f).stem
        if any(sess.startswith(x) for x in exclude):
            continue
        drafts, tp = [], None
        for line in open(f, errors="replace"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            raw = rec.get("raw") or {}
            o = raw.get("observation")
            if not isinstance(o, dict) or raw.get("game_type") not in (None, "pokemon_vgc_doubles_draft"):
                continue
            if o.get("phase") == "draft":
                drafts.append(rec)
            elif o.get("phase") == "team_preview" and tp is None:
                tp = o
        if not drafts or tp is None:
            continue
        seats = {d["raw"]["observation"].get("current_seat") for d in drafts}
        if len(seats) != 1:                                   # we held both seats: self-play
            continue
        me = seats.pop()
        pool: dict[str, dict] = {}
        for d in drafts:
            for c in d["raw"]["observation"].get("available_cards") or []:
                pool[c["card_id"]] = c
        last = drafts[-1]["raw"]["observation"]
        picks = sorted(last.get("picks") or [], key=lambda p: p["pick_number"])
        try:
            out = drafts[-1]["out"]
            out = json.loads(out) if isinstance(out, str) else out
            mine_last = out["input"]["action"]["card_id"]
        except Exception:
            mine_last = None
        seq = [("us" if p["seat_id"] == me else "them", p["card_id"]) for p in picks]
        if mine_last and all(cid != mine_last for _, cid in seq):
            seq.append(("us", mine_last))
        have = {to_id((pool.get(cid) or catalog.get(cid) or {}).get("species", cid)) for _, cid in seq}
        for p in tp.get("opponent_roster") or []:             # picks after our last draft decision
            s = to_id(p.get("species", ""))
            if s in have or any(h.startswith(s) for h in have):   # masked formes: "urshifu" for Urshifu-Rapid-Strike
                continue
            c = next((c for c in pool.values() if to_id(c["species"]) == s), None) or \
                next((c for c in pool.values() if to_id(c["species"]).startswith(s)), None)
            if c is None:
                break
            seq.append(("them", c["card_id"]))
            have.add(to_id(c["species"]))
        if len(seq) != 12:
            continue
        for _, cid in seq:
            if cid not in pool and cid in catalog:
                pool[cid] = catalog[cid]
        if any(cid not in pool for _, cid in seq):
            continue
        we_first = seq[0][0] == "us"
        if any((SNAKE[i] == "A") != ((who == "us") == we_first) for i, (who, _) in enumerate(seq)):
            continue
        games.append({"session": sess, "pool": pool, "picks": seq})
    return games


def events_of(games: list[dict], tt) -> list[dict]:
    """Every opponent pick: the legal cards, the chosen one, and each legal card's counter feature."""
    from agent.pokemon.matchup import card_key
    evs = []
    for gi, g in enumerate(games):
        pool = g["pool"]
        tt.ensure(list(pool.values()))
        taken, ours, theirs = set(), [], []
        for who, cid in g["picks"]:
            if who == "them":
                items = {_item(pool[x]) for x in theirs} - {None}
                legal = [x for x in pool if x not in taken and not (_item(pool[x]) and _item(pool[x]) in items)]
                if cid in legal:
                    ctr = {x: (sum(tt.beats(card_key(pool[x]), card_key(pool[d])) - 0.5 for d in ours) / len(ours)
                               if ours else 0.0) for x in legal}
                    evs.append({"game": gi, "legal": [to_id(pool[x]["species"]) for x in legal],
                                "chosen": to_id(pool[cid]["species"]),
                                "ctr": [ctr[x] for x in legal], "cids": legal, "ours": list(ours),
                                "theirs": list(theirs), "n": len(taken) + 1})
                theirs.append(cid)
            else:
                ours.append(cid)
            taken.add(cid)
    return evs


def fit(evs: list[dict], species: list[str], sigma: float, use_ctr: bool, iters: int = 3000, lr: float = 0.05):
    """MAP conditional logit by gradient ascent (no numpy needed: ~50 parameters, ~130 events)."""
    u = dict.fromkeys(species, 0.0)
    mu, gam = 0.0, 0.0
    for _ in range(iters):
        gu = dict.fromkeys(species, 0.0)
        gg = 0.0
        for e in evs:
            s = [u[x] + (gam * c if use_ctr else 0.0) for x, c in zip(e["legal"], e["ctr"])]
            mx = max(s)
            w = [math.exp(v - mx) for v in s]
            z = sum(w)
            for x, c, wx in zip(e["legal"], e["ctr"], w):
                gu[x] -= wx / z
                gg -= wx / z * c
            gu[e["chosen"]] += 1
            gg += e["ctr"][e["legal"].index(e["chosen"])]
        for x in species:
            gu[x] -= (u[x] - mu) / sigma ** 2
        gmu = sum(u[x] - mu for x in species) / sigma ** 2
        for x in species:
            u[x] += lr * gu[x]
        mu += lr * gmu / len(species) * 5
        if use_ctr:
            gam += lr * gg / 5
    return u, mu, gam


def score(u, mu, gam, e, i):
    return u.get(e["legal"][i], mu) + gam * e["ctr"][i]


def evaluate(evs, fit_fn):
    """Leave-one-game-out top-1 accuracy and log-likelihood per event."""
    hits, ll = 0, 0.0
    for gi in sorted({e["game"] for e in evs}):
        u, mu, gam = fit_fn([e for e in evs if e["game"] != gi])
        for e in (e for e in evs if e["game"] == gi):
            s = [score(u, mu, gam, e, i) for i in range(len(e["legal"]))]
            best = max(range(len(s)), key=lambda i: (s[i], e["legal"][i]))
            hits += e["legal"][best] == e["chosen"]
            mx = max(s)
            ll += s[e["legal"].index(e["chosen"])] - mx - math.log(sum(math.exp(v - mx) for v in s))
    return hits / len(evs), ll / len(evs)


def model_greedy_accuracy(games, evs) -> float:
    """How often OUR draft model's greedy view of the opponent (its gain go) names the real opponent's pick."""
    from agent.pokemon.drafter import Drafter, DraftModel
    d = Drafter(model=DraftModel.load(), prior_score=lambda c: 0.0)
    hits = 0
    for e in evs:
        pool = games[e["game"]]["pool"]
        lst = list(pool.values())
        idx = {c["card_id"]: i for i, c in enumerate(lst)}
        m, P, S = d._tables(lst)
        go = {x: d.m_opp[idx[x]] + sum(P[idx[x]][idx[y]] for y in e["ours"]) + sum(S[idx[x]][idx[y]] for y in e["theirs"])
              for x in e["cids"]}
        best = max(e["cids"], key=lambda x: (go[x], x))
        hits += to_id(pool[best]["species"]) == e["chosen"]
    return hits / len(evs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", required=True, help="directory holding our real recordings (<session>.jsonl)")
    ap.add_argument("--sigma", type=float, default=1.5, help="prior s.d. of a species' utility around the mean")
    ap.add_argument("--exclude", default="", help="comma list of session-id prefixes to leave out")
    ap.add_argument("--half", choices=("", "odd", "even"), default="",
                    help="hold-out fits: keep only the odd / even games (sorted by session id)")
    ap.add_argument("--write", action="store_true", help="write data/meta_drafter.json and agent/pokemon/draft_meta.json")
    ap.add_argument("--out", default="", help="write the evaluation-opponent fit (u + gamma) here instead")
    ap.add_argument("--prior-out", default="", help="write the drafter prior (u only) here instead")
    a = ap.parse_args()
    from agent.pokemon.drafter import threat_table
    games = load_games(os.path.expanduser(a.logs), {x for x in a.exclude.split(",") if x})
    if a.half:
        games = [g for i, g in enumerate(games) if (i % 2 == 1) == (a.half == "odd")]
    evs = events_of(games, threat_table())
    catalog = json.loads((ROOT / "data" / "vgc_cards.json").read_text())
    species = sorted({x for e in evs for x in e["legal"]} | {to_id(c["species"]) for c in catalog})
    print(f"{len(games)} real drafts against other contestants, {len(evs)} opponent picks")
    for use_ctr in (False, True):
        acc, ll = evaluate(evs, lambda tr: fit(tr, species, a.sigma, use_ctr, iters=1500))
        print(f"  conditional logit sigma {a.sigma} {'with' if use_ctr else 'without'} counter term: leave-one-game-out "
              f"top-1 {acc:.3f}, log-lik/pick {ll:.3f}")
    print(f"  our draft model's greedy view of the opponent: top-1 {model_greedy_accuracy(games, evs):.3f}")
    u1, mu1, gam1 = fit(evs, species, a.sigma, True)
    u0, mu0, _ = fit(evs, species, a.sigma, False)
    avail, took = {}, {}
    for e in evs:
        for x in e["legal"]:
            avail[x] = avail.get(x, 0) + 1
        took[e["chosen"]] = took.get(e["chosen"], 0) + 1
    print(f"gamma (counter term) {gam1:.3f}; utilities (with counter / without), times legal, times taken:")
    for x in sorted(species, key=lambda x: -u1[x]):
        print(f"  {x:24s} {u1[x]:+.2f} / {u0[x]:+.2f}   {avail.get(x, 0):3d} {took.get(x, 0):3d}")
    meta = {"about": "real opponents' draft preferences (scripts/fit_meta_drafter.py): P(pick c) ~ exp(u[species] + "
                     "gamma * mean calc duel of c vs the other side's roster); unknown species: mu",
            "sigma": a.sigma, "games": len(games), "picks": len(evs), "sessions": [g["session"][:8] for g in games],
            "half": a.half or None}
    eval_fit = {**meta, "gamma": round(gam1, 4), "mu": round(mu1, 4), "u": {k: round(v, 4) for k, v in u1.items()}}
    prior_fit = {**meta, "gamma": 0.0, "mu": round(mu0, 4), "u": {k: round(v, 4) for k, v in u0.items()}}
    out = a.out or (str(ROOT / "data" / "meta_drafter.json") if a.write else "")
    pout = a.prior_out or (str(ROOT / "agent" / "pokemon" / "draft_meta.json") if a.write else "")
    if out:
        Path(out).write_text(json.dumps(eval_fit, indent=1, sort_keys=True) + "\n")
        print("wrote", out)
    if pout:
        Path(pout).write_text(json.dumps(prior_fit, indent=1, sort_keys=True) + "\n")
        print("wrote", pout)


if __name__ == "__main__":
    main()
