"""Fit the drafter's roster-vs-roster value model (agent/pokemon/draft_model.json) from self-play games.

    python scripts/draft_selfplay.py --games 8000 --seed 1 --eps 1.0 --out work/selfplay/r1.jsonl   (x4 in parallel)
    python scripts/fit_draft_model.py --data 'work/selfplay/*.jsonl'                  # -> agent/pokemon/draft_model.json
    python scripts/fit_draft_model.py --data ... --grid          # held-out log-loss of several configurations

Model (log-odds that seat 0's roster A beats seat 1's roster B):
    bias + beta * sum_{a in A, b in B} (beats(a,b) - 0.5)
         + sum_A m - sum_B m + sum_{A x B} X + sum_{pairs A} S - sum_{pairs B} S
L2-regularised logistic regression (separate strengths for m, X, S), plain-Python AdaGrad SGD: the agent must not
depend on numpy, and neither does this. `beats` comes from agent/pokemon/matchup.py (exact damage calc).
The prior for unseen cards (m = a + b * card_score + c * mean calc duel; v3-harden, was a + b * card_score) is a
least-squares fit of the learned m on both features. The model file also stores each fitted card's full-set
fingerprint (agent/pokemon/drafter.py card_fingerprint): a live card whose set differs counts as unseen.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.pokemon import dex                       # noqa: E402
from agent.pokemon.drafter import card_fingerprint   # noqa: E402
from agent.pokemon.matchup import ThreatTable        # noqa: E402
from agent.pokemon.policy import card_score          # noqa: E402


def load_rows(pattern: str) -> list[dict]:
    """Rows of every file matching `pattern` (several glob patterns may be joined with commas)."""
    rows = []
    for f in sorted({f for p in pattern.split(",") if p.strip() for f in glob.glob(p.strip())}):
        for line in open(f):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("winner") in (0, 1) and len(r["rosters"][0]) == 6 and len(r["rosters"][1]) == 6:
                rows.append(r)
    return rows


class Fitter:
    def __init__(self, cards: list[str], beats: dict | None, use_pairs: bool, use_within: bool, use_beta: bool):
        self.cards = cards
        self.ci = {c: i for i, c in enumerate(cards)}
        n = len(cards)
        self.n = n
        self.off_main = 2
        self.off_cross = 2 + n
        self.pair_index = {}
        k = 0
        for i in range(n):
            for j in range(i + 1, n):
                self.pair_index[(i, j)] = k
                k += 1
        self.npairs = k
        self.off_within = self.off_cross + k
        self.dim = self.off_within + k
        self.beats = beats
        self.use_pairs, self.use_within, self.use_beta = use_pairs, use_within, use_beta
        self.group = [0] * self.dim                     # 0 = unregularised, 1 = main, 2 = cross, 3 = within
        for i in range(n):
            self.group[self.off_main + i] = 1
        for q in range(k):
            self.group[self.off_cross + q] = 2
            self.group[self.off_within + q] = 3

    def features(self, r: dict) -> list[tuple[int, float]]:
        A = [self.ci[c] for c in r["rosters"][0]]
        B = [self.ci[c] for c in r["rosters"][1]]
        f = [(0, 1.0)]
        if self.use_beta and self.beats is not None:
            M = sum(self.beats[(self.cards[a], self.cards[b])] - 0.5 for a in A for b in B)
            f.append((1, M))
        f += [(self.off_main + a, 1.0) for a in A] + [(self.off_main + b, -1.0) for b in B]
        if self.use_pairs:
            for a in A:
                for b in B:
                    if a < b:
                        f.append((self.off_cross + self.pair_index[(a, b)], 1.0))
                    else:
                        f.append((self.off_cross + self.pair_index[(b, a)], -1.0))
        if self.use_within:
            for T, sgn in ((A, 1.0), (B, -1.0)):
                Ts = sorted(T)
                for x in range(len(Ts)):
                    for y in range(x + 1, len(Ts)):
                        f.append((self.off_within + self.pair_index[(Ts[x], Ts[y])], sgn))
        return f

    def vector(self, model: dict) -> list[float]:
        """A fitted model file (draft_model.json) as this fitter's weight vector (cards it lacks are dropped)."""
        w = [0.0] * self.dim
        w[0], w[1] = float(model.get("bias", 0.0)), float(model.get("beta", 0.0))
        for c, v in (model.get("main") or {}).items():
            if c in self.ci:
                w[self.off_main + self.ci[c]] = float(v)
        for key, off in (("cross", self.off_cross), ("within", self.off_within)):
            for k, v in (model.get(key) or {}).items():
                a, _, b = k.partition("|")
                if a in self.ci and b in self.ci and a != b:
                    i, j = sorted((self.ci[a], self.ci[b]))
                    # cross terms are antisymmetric (a's roster vs b's): stored for the sorted pair with sign +
                    sgn = 1.0 if key == "within" or self.ci[a] < self.ci[b] else -1.0
                    w[off + self.pair_index[(i, j)]] = sgn * float(v)
        return w

    def fit(self, X: list, Y: list, lam: tuple[float, float, float], epochs: int = 12, lr: float = 0.08,
            seed: int = 0, center: list[float] | None = None) -> list[float]:
        """`center`: shrink toward these weights instead of 0 (a previous model as the prior; also the start)."""
        c0 = list(center) if center is not None else [0.0] * self.dim
        w = list(c0)
        G = [1e-8] * self.dim
        cnt = [0] * self.dim
        for f in X:
            for i, _ in f:
                cnt[i] += 1
        reg = [0.0] * self.dim
        for i in range(self.dim):
            g = self.group[i]
            if g and cnt[i]:
                reg[i] = lam[g - 1] / cnt[i]
        order = list(range(len(X)))
        rng = random.Random(seed)
        for ep in range(epochs):
            rng.shuffle(order)
            step = lr / math.sqrt(1 + ep)
            for r in order:
                f = X[r]
                z = 0.0
                for i, v in f:
                    z += w[i] * v
                p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
                e = p - Y[r]
                for i, v in f:
                    g = e * v + reg[i] * (w[i] - c0[i])
                    G[i] += g * g
                    w[i] -= step * g / math.sqrt(G[i])
        return w

    @staticmethod
    def logloss(w, X, Y) -> tuple[float, float]:
        ll, acc = 0.0, 0
        for f, y in zip(X, Y):
            z = sum(w[i] * v for i, v in f)
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            p = min(1 - 1e-9, max(1e-9, p))
            ll -= y * math.log(p) + (1 - y) * math.log(1 - p)
            acc += (p > 0.5) == (y == 1)
        return ll / max(1, len(X)), acc / max(1, len(X))


def beats_table(cards: list[str]) -> dict:
    cat = dex.card_catalog()
    tt = ThreatTable()
    tt.ensure([cat[c] for c in cards if c in cat])
    out = {}
    for a in cards:
        for b in cards:
            if a != b:
                out[(a, b)] = tt.beats(a, b) if a in cat and b in cat else 0.5
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="work/selfplay/*.jsonl")
    ap.add_argument("--out", default=str(ROOT / "agent" / "pokemon" / "draft_model.json"))
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--config", default="full", choices=["main", "main_beta", "pairs", "full"])
    ap.add_argument("--lam", default="4,40,40")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--holdout", type=float, default=0.15)
    ap.add_argument("--prior-model", default="",
                    help="shrink toward this model's weights instead of 0 (refit on fewer games after a pilot change)")
    a = ap.parse_args()
    rows = load_rows(a.data)
    cat = dex.card_catalog()
    cards = sorted(set(cat) | {c for r in rows for ro in r["rosters"] for c in ro})
    print(f"{len(rows)} games, {len(cards)} cards", flush=True)
    t0 = time.time()
    beats = beats_table(cards)
    print(f"beats table {time.time() - t0:.1f}s", flush=True)
    configs = {"main": (False, False, False), "main_beta": (False, False, True),
               "pairs": (True, False, True), "full": (True, True, True)}
    rng = random.Random(1)
    idx = list(range(len(rows)))
    rng.shuffle(idx)
    cut = int(len(rows) * (1 - a.holdout))
    tr, te = [rows[i] for i in idx[:cut]], [rows[i] for i in idx[cut:]]
    if a.grid:
        grid = [("main", (4, 0, 0)), ("main_beta", (1, 0, 0)), ("main_beta", (4, 0, 0)), ("main_beta", (16, 0, 0)),
                ("pairs", (4, 100, 0)), ("pairs", (4, 300, 0)), ("full", (4, 60, 60)), ("full", (4, 150, 150)),
                ("full", (4, 400, 400))]
        base = sum(r["winner"] == 0 for r in te) / max(1, len(te))
        print(f"holdout {len(te)} games; seat-0 win rate {base:.3f} -> constant log-loss "
              f"{-(base * math.log(base) + (1 - base) * math.log(1 - base)):.4f}")
        for name, lam in grid:
            F = Fitter(cards, beats, *configs[name])
            Xtr, Ytr = [F.features(r) for r in tr], [1.0 if r["winner"] == 0 else 0.0 for r in tr]
            Xte, Yte = [F.features(r) for r in te], [1.0 if r["winner"] == 0 else 0.0 for r in te]
            w = F.fit(Xtr, Ytr, lam, epochs=a.epochs)
            ll, acc = F.logloss(w, Xte, Yte)
            lltr, acctr = F.logloss(w, Xtr, Ytr)
            print(f"{name:10s} lam={lam}: holdout logloss {ll:.4f} acc {acc:.3f} | train {lltr:.4f} {acctr:.3f} "
                  f"| beta {w[1]:.3f} | {time.time() - t0:.0f}s", flush=True)
        return
    lam = tuple(float(x) for x in a.lam.split(","))
    F = Fitter(cards, beats, *configs[a.config])
    X, Y = [F.features(r) for r in rows], [1.0 if r["winner"] == 0 else 0.0 for r in rows]
    center = F.vector(json.loads(Path(a.prior_model).read_text())) if a.prior_model else None
    w = F.fit(X, Y, lam, epochs=a.epochs, center=center)
    ll, acc = F.logloss(w, X, Y)
    main_w = {c: w[F.off_main + i] for i, c in enumerate(cards)}
    cross, within = {}, {}
    for (i, j), q in F.pair_index.items():
        if abs(w[F.off_cross + q]) > 1e-4:
            cross[f"{cards[i]}|{cards[j]}"] = round(w[F.off_cross + q], 5)
        if abs(w[F.off_within + q]) > 1e-4:
            within[f"{cards[i]}|{cards[j]}"] = round(w[F.off_within + q], 5)
    # prior for unseen cards: least squares of m on the v0 card_score and on the card's mean calc duel score.
    # v3-harden ships the two-feature fit (m = a + b * card_score + c * mean duel; the drafter computes an unseen
    # card's mean duel against the other cards of its draft, z_mean when it has no threat table).
    known = [c for c in cards if c in cat]
    xs = [card_score(cat[c], []) for c in known]
    zs = [sum(beats[(c, d)] - 0.5 for d in known if d != c) / (len(known) - 1) for c in known]
    ys = [main_w[c] for c in known]
    mx, mz, my = sum(xs) / len(xs), sum(zs) / len(zs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    szz = sum((z - mz) ** 2 for z in zs)
    sxz = sum((x - mx) * (z - mz) for x, z in zip(xs, zs))
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    szy = sum((z - mz) * (y - my) for z, y in zip(zs, ys))
    det = sxx * szz - sxz * sxz
    b2 = (szz * sxy - sxz * szy) / det if det else 0.0
    c2 = (sxx * szy - sxz * sxy) / det if det else 0.0
    syy = sum((y - my) ** 2 for y in ys) or 1e-9
    r2_1 = (sxy * sxy / sxx) / syy if sxx else 0.0
    r2_2 = (b2 * sxy + c2 * szy) / syy
    b = sxy / max(1e-9, sxx)
    print(f"prior fit: R2 card_score only {r2_1:.3f}; card_score + mean duel {r2_2:.3f} (b={b2:.4f}, c={c2:.3f})")
    if det:
        prior = {"a": round(my - b2 * mx - c2 * mz, 5), "b": round(b2, 5), "c": round(c2, 5), "z_mean": round(mz, 5),
                 "features": "card_score + mean calc duel", "r2": round(r2_2, 4),
                 "one_feature": {"a": round(my - b * mx, 5), "b": round(b, 5), "r2": round(r2_1, 4)}}
    else:                                       # degenerate (e.g. one card): the one-feature prior
        prior = {"a": round(my - b * mx, 5), "b": round(b, 5)}
    # v3-harden: every fitted card's full-set fingerprint; the drafter treats a card whose set differs as unseen
    fingerprints = {c: card_fingerprint(cat[c]) for c in cards if c in cat and card_fingerprint(cat[c])}
    out = {"version": 2, "config": a.config, "lam": lam, "games": len(rows), "train_logloss": round(ll, 5),
           "prior_model": Path(a.prior_model).name if a.prior_model else None,
           "bias": round(w[0], 5), "beta": round(w[1], 5), "prior": prior, "fingerprints": fingerprints,
           "species": {c: cat[c]["species"] for c in cards if c in cat},
           "main": {c: round(v, 5) for c, v in main_w.items()}, "cross": cross, "within": within}
    Path(a.out).write_text(json.dumps(out, indent=0, sort_keys=True))
    print(f"wrote {a.out}: train logloss {ll:.4f} acc {acc:.3f}, beta {w[1]:.3f}, bias {w[0]:.3f}, prior {prior}, "
          f"{len(cross)} cross, {len(within)} within")
    for c, v in sorted(main_w.items(), key=lambda kv: -kv[1]):
        print(f"  {v:+.3f}  {c}  (card_score {card_score(cat[c], []) if c in cat else float('nan'):.2f})")


if __name__ == "__main__":
    main()
