"""Time and sanity-check the preview matrix game on random catalog rosters.

    python scripts/preview_bench.py --n 5 [--profile]
"""
from __future__ import annotations

import argparse
import cProfile
import pstats
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # this tree's agent/, not the editable install's

from agent.pokemon import preview  # noqa: E402
from agent.pokemon.dex import card_catalog, to_id  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--full", action="store_true", help="also build the full 90x90 matrix and compare")
    ap.add_argument("--default-budget", action="store_true", help="use the deployed work budget instead of none")
    a = ap.parse_args()
    cards = list(card_catalog().values())
    rng = random.Random(a.seed)
    for k in range(a.n):
        pick = rng.sample(cards, 12)
        mine, theirs = pick[:6], pick[6:]
        mv = [{"species": to_id(c["species"])} for c in mine]
        ov = [{"species": to_id(c["species"]), "item": "unknown_item", "ability": None, "moves": []} for c in theirs]
        t = time.perf_counter()
        if a.profile and k == 0:
            pr = cProfile.Profile()
            pr.enable()
        out = preview.choose(mv, mine, ov, theirs, seed_text=f"bench{k}",
                             knobs={} if a.default_budget else {"work_units": 1e12, "wall_cap_s": 60})
        if a.profile and k == 0:
            pr.disable()
            pstats.Stats(pr).sort_stats("cumulative").print_stats(18)
        dt = time.perf_counter() - t
        names = [c["species"] for c in mine]
        onames = [c["species"] for c in theirs]
        print(f"[{k}] {dt:.2f}s {out['info']}")
        print("   ours  :", names)
        print("   theirs:", onames)
        print("   bring :", [names[i] for i in out["bring"]], "leads", [names[i] for i in out["leads"]])
        lus = preview.lineups(6)
        top = sorted(range(len(out["pr"])), key=lambda i: -out["pr"][i])[:5]
        print("   eq ours:", [([names[j] for j in lus[out["rows"][i]]], round(out["pr"][i], 2)) for i in top])
        topc = sorted(range(len(out["pc"])), key=lambda i: -out["pc"][i])[:3]
        print("   eq them:", [([onames[j] for j in lus[out["cols"][i]]], round(out["pc"][i], 2)) for i in topc])
        if a.full and k < 2:
            kn = dict(preview.KNOBS)
            units = [preview.Unit(0, v, c) for v, c in zip(mv, mine)] + [preview.Unit(1, v, c) for v, c in zip(ov, theirs)]
            tb = preview.Tables(units)
            t = time.perf_counter()
            A, _ = preview.payoff_matrix(tb, list(range(6)), list(range(6, 12)), kn, preview.Deadline(100, 100))
            pr, pc, v = preview.solve_rm_plus(A, 2000)
            print(f"   FULL matrix {time.perf_counter() - t:.2f}s value {v:.3f} expl {preview.exploitability(A, pr, pc):.3f}")
            # exploitability of the double-oracle strategy in the full game
            prf = [0.0] * 90
            for r, p in zip(out["rows"], out["pr"]):
                prf[r] += p
            pcf = [0.0] * 90
            for c, p in zip(out["cols"], out["pc"]):
                pcf[c] += p
            print(f"   DO profile in full game: expl {preview.exploitability(A, prf, pcf):.3f}, "
                  f"worst-case of DO row strategy {min(sum(p * A[r][c] for r, p in enumerate(prf)) for c in range(90)):.3f}")
            top = sorted(range(90), key=lambda i: -pr[i])[:4]
            print("   full eq ours:", [([names[j] for j in lus[i]], round(pr[i], 2)) for i in top])


if __name__ == "__main__":
    main()
