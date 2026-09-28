"""Fit the drafter's roster-vs-roster value model (agent/pokemon/draft_model.json) from self-play / population games.

    python scripts/draft_population.py --games 3000 --seed 401 --out work/pop/p401.jsonl  (x3 in parallel)
    python scripts/draft_selfplay.py --games 8000 --seed 1 --eps 1.0 --out work/selfplay/r1.jsonl
    python scripts/fit_draft_model.py --data 'work/pop/*.jsonl,work/selfplay/*.jsonl'   # -> agent/pokemon/draft_model.json
    python scripts/fit_draft_model.py --data ... --grid          # held-out log-loss of several configurations
    python scripts/fit_draft_model.py --data ... --heldout-family --baseline agent/pokemon/draft_model.json
                                                                 # fit without one opponent family, test on it
    python scripts/fit_draft_model.py --data ... --heldout-family --groups 'v1+bst,v2+rand,v0,v3+cs3' \
        --baseline ... --variants 'all:full:16,600,600;all:full_asym:16,600,600,1,160'
                                                                 # hold out a whole pilot (stricter)

Rows. A population row (scripts/draft_population.py: has `family` and `ours`) is oriented OUR roster first: roster A
= the seat our pilot played, y = 1 if it won. A self-play row (no `family`; scripts/draft_selfplay.py) keeps seat 0
first and gets the family "selfplay".

Model (log-odds that roster A beats roster B):
    bias + bias[family] + beta * sum_{a in A, b in B} (beats(a,b) - 0.5)
         + sum_A m - sum_B (m + d) + sum_{A x B} X + sum_{pairs A} S - sum_{pairs B} S
`bias[family]` (lightly regularised) absorbs how strong each opponent family's pilot is, so a weak family's losses
are not blamed on the cards it drafts. `d` (config "full_asym" only) is how much MORE a card is worth to a foreign
pilot than to ours: it is only active on rows whose opponent pilot is not ours (families outside SELF_PILOT) and is
stored as `main_opp` = m + d (the drafter then values the opponent's cards with main_opp). The shipped configs
"full" / "pairs" / "main*" are symmetric (d = 0), exactly the old model.
L2-regularised logistic regression (separate strengths for m, X, S, d), plain-Python AdaGrad SGD: the agent must not
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

SELF_PILOT = {"selfplay", "v3", "cs3"}      # families whose opponent seat is piloted by our own code: no d terms
CONFIGS = {"main": (False, False, False), "main_beta": (False, False, True),
           "pairs": (True, False, True), "full": (True, True, True), "full_asym": (True, True, True)}


def load_rows(pattern: str) -> list[dict]:
    """Rows of every file matching `pattern` (several glob patterns may be joined with commas), oriented: `ros` =
    [roster A, roster B], `y` = 1.0 if A won, `fam` = the opponent family ("selfplay" for self-play rows)."""
    rows = []
    for f in sorted({f for p in pattern.split(",") if p.strip() for f in glob.glob(p.strip())}):
        for line in open(f):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("winner") not in (0, 1):
                continue
            ros = r.get("rosters") or []
            if len(ros) != 2 or len(ros[0]) != 6 or len(ros[1]) != 6:
                continue
            if "family" in r and r.get("ours") in (0, 1):
                o = r["ours"]
                r["ros"], r["y"], r["fam"] = [ros[o], ros[1 - o]], 1.0 if r["winner"] == o else 0.0, str(r["family"])
            else:
                r["ros"], r["y"], r["fam"] = [ros[0], ros[1]], 1.0 if r["winner"] == 0 else 0.0, "selfplay"
            rows.append(r)
    return rows


class Fitter:
    def __init__(self, cards: list[str], beats: dict | None, use_pairs: bool, use_within: bool, use_beta: bool,
                 families: list[str] | None = None, use_asym: bool = False):
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
        self.families = list(families or [])
        self.fi = {f: i for i, f in enumerate(self.families)}
        self.off_fam = self.off_within + k
        self.off_opp = self.off_fam + len(self.families)
        self.use_asym = use_asym
        self.dim = self.off_opp + (n if use_asym else 0)
        self.beats = beats
        self.use_pairs, self.use_within, self.use_beta = use_pairs, use_within, use_beta
        # 0 = unregularised, 1 = main, 2 = cross, 3 = within, 4 = family bias, 5 = d (foreign-pilot offset)
        self.group = [0] * self.dim
        for i in range(n):
            self.group[self.off_main + i] = 1
        for q in range(k):
            self.group[self.off_cross + q] = 2
            self.group[self.off_within + q] = 3
        for i in range(len(self.families)):
            self.group[self.off_fam + i] = 4
        if use_asym:
            for i in range(n):
                self.group[self.off_opp + i] = 5

    def features(self, r: dict, with_family: bool = True) -> list[tuple[int, float]]:
        ros = r.get("ros") or r["rosters"]
        A = [self.ci[c] for c in ros[0]]
        B = [self.ci[c] for c in ros[1]]
        f = [(0, 1.0)]
        fam = r.get("fam")
        if with_family and fam in self.fi:
            f.append((self.off_fam + self.fi[fam], 1.0))
        if self.use_beta and self.beats is not None:
            M = sum(self.beats[(self.cards[a], self.cards[b])] - 0.5 for a in A for b in B)
            f.append((1, M))
        f += [(self.off_main + a, 1.0) for a in A] + [(self.off_main + b, -1.0) for b in B]
        if self.use_asym and fam not in SELF_PILOT:
            f += [(self.off_opp + b, -1.0) for b in B]
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

    def vector(self, model: dict, fill_unseen: bool = False) -> list[float]:
        """A fitted model file (draft_model.json) as this fitter's weight vector (cards it lacks are dropped, or with
        `fill_unseen` get the model's own unseen-card prior strength, as the drafter would give them)."""
        w = [0.0] * self.dim
        w[0], w[1] = float(model.get("bias", 0.0)), float(model.get("beta", 0.0))
        main = model.get("main") or {}
        for c, v in main.items():
            if c in self.ci:
                w[self.off_main + self.ci[c]] = float(v)
        if fill_unseen:
            prior = model.get("prior") or {}
            cat = dex.card_catalog()
            for c in self.cards:
                if c not in main and c in cat:
                    z = 0.0
                    if self.beats is not None:
                        others = [d for d in self.cards if d != c and d in cat]
                        z = sum(self.beats[(c, d)] - 0.5 for d in others) / max(1, len(others))
                    w[self.off_main + self.ci[c]] = (float(prior.get("a", 0.0)) + float(prior.get("b", 0.0))
                                                     * card_score(cat[c], []) + float(prior.get("c", 0.0)) * z)
        if self.use_asym:
            for c, v in (model.get("main_opp") or {}).items():
                if c in self.ci:
                    w[self.off_opp + self.ci[c]] = float(v) - w[self.off_main + self.ci[c]]
        for key, off in (("cross", self.off_cross), ("within", self.off_within)):
            for k, v in (model.get(key) or {}).items():
                a, _, b = k.partition("|")
                if a in self.ci and b in self.ci and a != b:
                    i, j = sorted((self.ci[a], self.ci[b]))
                    # cross terms are antisymmetric (a's roster vs b's): stored for the sorted pair with sign +
                    sgn = 1.0 if key == "within" or self.ci[a] < self.ci[b] else -1.0
                    w[off + self.pair_index[(i, j)]] = sgn * float(v)
        return w

    def fit(self, X: list, Y: list, lam: tuple, epochs: int = 12, lr: float = 0.08,
            seed: int = 0, center: list[float] | None = None) -> list[float]:
        """`lam` = (main, cross, within[, family bias[, d]]) total L2 strengths. `center`: shrink toward these weights
        instead of 0 (a previous model as the prior; also the start)."""
        lam = tuple(lam) + (1.0, 40.0)[max(0, len(lam) - 3):]
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


def _ll(z: float, y: float) -> float:
    p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
    p = min(1 - 1e-9, max(1e-9, p))
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def fit_offset(zs: list[float], ys: list[float], iters: int = 50) -> float:
    """The intercept shift t maximising the likelihood of y given logits z + t (1-D Newton)."""
    t = 0.0
    for _ in range(iters):
        g = h = 0.0
        for z, y in zip(zs, ys):
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z + t))))
            g += p - y
            h += p * (1 - p)
        if h < 1e-12:
            break
        step = g / h
        t -= max(-2.0, min(2.0, step))
        if abs(step) < 1e-9:
            break
    return t


def crossfit_logloss(zs: list[float], ys: list[float], folds: int = 2) -> tuple[float, list[float]]:
    """Log-loss of logits z on an UNSEEN family, whose intercept (how strong that family's pilot is) is unknown: the
    intercept shift is fitted on the other fold(s) and scored on this one (1 parameter, no card weight is refitted).
    Returns (mean log-loss, per-row losses)."""
    n = len(zs)
    per = [0.0] * n
    for k in range(folds):
        tr = [i for i in range(n) if i % folds != k]
        t = fit_offset([zs[i] for i in tr], [ys[i] for i in tr])
        for i in range(n):
            if i % folds == k:
                per[i] = _ll(zs[i] + t, ys[i])
    return sum(per) / max(1, n), per


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


def _paired_se(a: list[float], b: list[float]) -> float:
    d = [x - y for x, y in zip(a, b)]
    n = len(d)
    if n < 2:
        return float("nan")
    m = sum(d) / n
    return math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1) / n)


def heldout_family(rows: list[dict], cards: list[str], beats: dict, variants: list[tuple[str, str, tuple]],
                   baselines: dict[str, dict], epochs: int, only: list[str] | None = None,
                   test_without: set | None = None, groups: list[list[str]] | None = None) -> dict:
    """For every held-out group G of opponent families (default: each population family alone): fit each variant
    on all rows NOT of G (the variant's data filter decides which), then score every family F of G on its own rows
    with crossfit_logloss (F's intercept, i.e. how strong its pilot is, is cross-fitted; no card weight is). Baselines
    (model files) are scored the same way. Holding out a whole group, e.g. "v1+bst" (both piloted by v1), keeps a
    pilot out of training entirely. `test_without`: score only the rows in which none of these cards was drafted
    (e.g. cards a baseline never saw). Returns {name: {family: per-row losses}}."""
    fams = sorted({r["fam"] for r in rows if r["fam"] != "selfplay"})
    if only:
        fams = [f for f in fams if f in only]
    groups = [list(g) for g in groups] if groups else [[f] for f in fams]
    t0 = time.time()
    table = []
    pooled: dict[str, dict[str, list[float]]] = {}
    names = list(baselines) + [v[0] for v in variants]
    for G in groups:
        gset = set(G)
        test = {F: [r for r in rows if r["fam"] == F
                    and not (test_without and any(c in test_without for ro in r["ros"] for c in ro))] for F in G}
        zs_by = {F: {} for F in G}
        for name, model in baselines.items():
            Fb = Fitter(cards, beats, True, True, True, [], use_asym=bool(model.get("main_opp")))
            w = Fb.vector(model, fill_unseen=True)
            for F in G:
                zs_by[F][name] = [sum(w[i] * v for i, v in Fb.features({**r, "fam": "foreign"}, with_family=False))
                                  for r in test[F]]
        for name, data_filter, (config, lam) in variants:
            train = [r for r in rows if r["fam"] not in gset and (data_filter == "all" or
                                                                   (data_filter == "pop" and r["fam"] != "selfplay"))]
            fams_tr = sorted({r["fam"] for r in train})
            Fv = Fitter(cards, beats, *CONFIGS[config], families=fams_tr, use_asym=config == "full_asym")
            X = [Fv.features(r) for r in train]
            w = Fv.fit(X, [r["y"] for r in train], lam, epochs=epochs)
            # the held-out families are foreign: no family bias (intercepts cross-fitted), d active if asym
            for F in G:
                zs_by[F][name] = [sum(w[i] * v for i, v in Fv.features({**r, "fam": "foreign"}, with_family=False))
                                  for r in test[F]]
            print(f"  [{'+'.join(G)}] {name}: train {len(train)} rows ({time.time() - t0:.0f}s)", flush=True)
        for F in G:
            ys = [r["y"] for r in test[F]]
            if not ys:
                continue
            const, _ = crossfit_logloss([0.0] * len(ys), ys)
            res = {"family": F if len(G) == 1 else f"{F} (out: {'+'.join(G)})", "n": len(ys),
                   "win": sum(ys) / len(ys), "const": const}
            per_rows = {}
            for nm in names:
                res[nm], per_rows[nm] = crossfit_logloss(zs_by[F][nm], ys)
                pooled.setdefault(nm, {}).setdefault(F, []).extend(per_rows[nm])
            for nm in names[1:]:
                res[f"{nm}-{names[0]}"] = (res[nm] - res[names[0]], _paired_se(per_rows[nm], per_rows[names[0]]))
            table.append(res)
            print(json.dumps({k: (round(v, 4) if isinstance(v, float) else
                                  [round(x, 4) for x in v] if isinstance(v, tuple) else v) for k, v in res.items()}),
                  flush=True)
    print("summary (held-out log-loss per family; last columns: difference vs the first name, +-1 SE paired)")
    for res in table:
        print("  " + " | ".join(f"{k} {v:.4f}" if isinstance(v, float) else
                                f"{k} {v[0]:+.4f}+-{v[1]:.4f}" if isinstance(v, tuple) else f"{k} {v}"
                                for k, v in res.items()))
    # pooled over families (each family weighted by its rows): all families, and the foreign-pilot ones only
    done = [F for G in groups for F in G]
    for label, keep in (("all families", done), ("foreign pilots", [f for f in done if f not in SELF_PILOT])):
        if not names or not keep:
            continue
        per = {nm: [x for F in keep for x in pooled[nm].get(F, [])] for nm in names}
        n = len(per[names[0]])
        parts = [f"{nm} {sum(per[nm]) / max(1, n):.4f}" for nm in names]
        parts += [f"{nm}-{names[0]} {(sum(per[nm]) - sum(per[names[0]])) / max(1, n):+.4f}"
                  f"+-{_paired_se(per[nm], per[names[0]]):.4f}" for nm in names[1:]]
        print(f"  pooled {label} ({', '.join(keep)}; {n} rows): " + " | ".join(parts))
    return pooled


def model_cards(rows: list[dict]) -> list[str]:
    """The cards the model is fitted on: those drafted in at least one training game. A catalog card without games
    would keep m = 0.0 (the ridge's centre) yet be written with a fingerprint, so the drafter would score it as a known
    average card instead of with the unseen-card prior (and local/pool.py NOT_IN_DRAFT_MODEL would pass unfitted)."""
    return sorted({c for r in rows for ro in r["ros"] for c in ro})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="work/selfplay/*.jsonl")
    ap.add_argument("--out", default=str(ROOT / "agent" / "pokemon" / "draft_model.json"))
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--heldout-family", action="store_true",
                    help="fit without one opponent family, score on it (per family); see heldout_family()")
    ap.add_argument("--baseline", default="", help="comma list of model files scored in --heldout-family")
    ap.add_argument("--variants", default="all:full:16,600,600",
                    help="--heldout-family variants: comma-free items 'data:config:lam' joined by ';' "
                         "(data = all | pop, config = main_beta | pairs | full | full_asym)")
    ap.add_argument("--only", default="", help="--heldout-family: only these families (comma list)")
    ap.add_argument("--groups", default="",
                    help="--heldout-family: held-out groups instead of single families, e.g. "
                         "'v1+bst,v2+rand,v0,v3+cs3' (one pilot per group: the pilot is never in training)")
    ap.add_argument("--test-without", default="",
                    help="--heldout-family: score only held-out games in which none of these cards (comma list) "
                         "was drafted, e.g. the cards a --baseline model never saw")
    ap.add_argument("--config", default="full", choices=list(CONFIGS))
    ap.add_argument("--lam", default="4,40,40",
                    help="L2 totals main,cross,within[,family bias[,d]] (defaults for the last two: 1, 40)")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--holdout", type=float, default=0.15)
    ap.add_argument("--prior-model", default="",
                    help="shrink toward this model's weights instead of 0 (refit on fewer games after a pilot change)")
    a = ap.parse_args()
    rows = load_rows(a.data)
    cat = dex.card_catalog()
    cards = model_cards(rows)
    unplayed = sorted(set(cat) - set(cards))
    if unplayed:
        print(f"catalog cards without training games, left out of the model (the drafter gives them its unseen-card "
              f"prior; keep them in local/pool.py NOT_IN_DRAFT_MODEL): {', '.join(unplayed)}", flush=True)
    fam_counts = {}
    for r in rows:
        fam_counts[r["fam"]] = fam_counts.get(r["fam"], 0) + 1
    print(f"{len(rows)} games, {len(cards)} cards, families {dict(sorted(fam_counts.items()))}", flush=True)
    t0 = time.time()
    beats = beats_table(cards)
    print(f"beats table {time.time() - t0:.1f}s", flush=True)
    families = sorted(fam_counts)
    if a.heldout_family:
        baselines = {}
        for p in [p for p in a.baseline.split(",") if p.strip()]:
            baselines[Path(p).stem] = json.loads(Path(p).read_text())
        variants = []
        for item in [v for v in a.variants.split(";") if v.strip()]:
            data_filter, config, lam = item.split(":")
            variants.append((f"{data_filter}-{config}-{lam}", data_filter,
                             (config, tuple(float(x) for x in lam.split(",")))))
        heldout_family(rows, cards, beats, variants, baselines, a.epochs,
                       [f for f in a.only.split(",") if f.strip()] or None,
                       {c.strip() for c in a.test_without.split(",") if c.strip()} or None,
                       [g.split("+") for g in a.groups.split(",") if g.strip()] or None)
        return
    rng = random.Random(1)
    idx = list(range(len(rows)))
    rng.shuffle(idx)
    cut = int(len(rows) * (1 - a.holdout))
    tr, te = [rows[i] for i in idx[:cut]], [rows[i] for i in idx[cut:]]
    if a.grid:
        grid = [("main", (4, 0, 0)), ("main_beta", (1, 0, 0)), ("main_beta", (4, 0, 0)), ("main_beta", (16, 0, 0)),
                ("pairs", (4, 100, 0)), ("pairs", (4, 300, 0)), ("full", (4, 60, 60)), ("full", (4, 150, 150)),
                ("full", (4, 400, 400)), ("full", (16, 600, 600)), ("full_asym", (16, 600, 600, 1, 40)),
                ("full_asym", (16, 600, 600, 1, 160))]
        base = sum(r["y"] for r in te) / max(1, len(te))
        print(f"holdout {len(te)} games; our win rate {base:.3f} -> constant log-loss "
              f"{-(base * math.log(base) + (1 - base) * math.log(1 - base)):.4f}")
        for name, lam in grid:
            F = Fitter(cards, beats, *CONFIGS[name], families=families, use_asym=name == "full_asym")
            Xtr, Ytr = [F.features(r) for r in tr], [r["y"] for r in tr]
            Xte, Yte = [F.features(r) for r in te], [r["y"] for r in te]
            w = F.fit(Xtr, Ytr, lam, epochs=a.epochs)
            ll, acc = F.logloss(w, Xte, Yte)
            lltr, acctr = F.logloss(w, Xtr, Ytr)
            print(f"{name:10s} lam={lam}: holdout logloss {ll:.4f} acc {acc:.3f} | train {lltr:.4f} {acctr:.3f} "
                  f"| beta {w[1]:.3f} | {time.time() - t0:.0f}s", flush=True)
        return
    lam = tuple(float(x) for x in a.lam.split(","))
    F = Fitter(cards, beats, *CONFIGS[a.config], families=families, use_asym=a.config == "full_asym")
    X, Y = [F.features(r) for r in rows], [r["y"] for r in rows]
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
           "families": {f: {"games": fam_counts[f], "bias": round(w[F.off_fam + F.fi[f]], 5)} for f in families},
           "bias": round(w[0], 5), "beta": round(w[1], 5), "prior": prior, "fingerprints": fingerprints,
           "species": {c: cat[c]["species"] for c in cards if c in cat},
           "main": {c: round(v, 5) for c, v in main_w.items()}, "cross": cross, "within": within}
    if F.use_asym:
        out["main_opp"] = {c: round(main_w[c] + w[F.off_opp + i], 5) for i, c in enumerate(cards)}
    Path(a.out).write_text(json.dumps(out, indent=0, sort_keys=True))
    print(f"wrote {a.out}: train logloss {ll:.4f} acc {acc:.3f}, beta {w[1]:.3f}, bias {w[0]:.3f}, prior {prior}, "
          f"{len(cross)} cross, {len(within)} within; family biases "
          + ", ".join(f"{f} {w[F.off_fam + F.fi[f]]:+.3f}" for f in families))
    for c, v in sorted(main_w.items(), key=lambda kv: -kv[1]):
        extra = f"  opp {out['main_opp'][c]:+.3f}" if F.use_asym else ""
        print(f"  {v:+.3f}{extra}  {c}  (card_score {card_score(cat[c], []) if c in cat else float('nan'):.2f})")


if __name__ == "__main__":
    main()
