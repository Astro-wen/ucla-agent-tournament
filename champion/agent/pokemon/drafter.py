"""Drafter: pick the card that maximises our FINAL roster's value against the opponent's likely final roster.

Value of a pair of 6-card rosters (log-odds that roster A beats roster B), fitted on self-play games by
scripts/fit_draft_model.py and stored in agent/pokemon/draft_model.json:

    sum_{c in A} m[c] - sum_{d in B} m[d]                            card strength
  + sum_{c in A, d in B} ( X[c,d] + beta * (beats(c,d) - 0.5) )     counters: learned + damage-calc prior
  + sum_{pairs in A} S[c,c'] - sum_{pairs in B} S[d,d']             synergy

`beats` is the 1v1 duel estimate of agent/pokemon/matchup.py (exact damage calc over the pool; a plain formula
without node), so counters exist for cards the model never saw. A model fitted with the `full_asym` config also has
`main_opp`: a card's strength when the opponent holds it (m + d, fitted on games against other pilots); the search
then uses it for the opponent's side (`sum_{d in B} main_opp[d]`). Without it both sides use `main`.

Which cards the model "knows" (v3-harden). The model file stores a fingerprint of every fitted card's full set
(`fingerprints`: card_id -> hash of species / item / ability / nature / level / moves / EVs / non-31 IVs, see
`card_fingerprint`). A live card uses the learned m / X / S of the model card whose fingerprint it matches (normally
its own card_id; a renamed card with an identical set also matches). A card whose set differs from the fitted one
(same card_id, new moves / item / spread) is treated as UNSEEN: its learned weights were fitted on another set.
An unseen card gets no X / S terms and the prior strength

    m = prior_a + prior_b * card_score(card) + prior_c * mean duel

(the two-feature prior: card_score is the v0 heuristic, `mean duel` its mean beats(c, d) - 0.5 against the other
cards of this draft; both least-squares fitted on the learned m of the known cards by fit_draft_model.py). A model
file without fingerprints (older fits) keeps the old rule: card_id, else the first card of the same species. Without
a model file the drafter runs on DEFAULT_PRIOR plus the calc matchups alone. Every card_id / set the platform shows
that is not in the card catalog (data/vgc_cards.json) is reported once per process on stderr (never in the
recorded logs or the reasoning summary).

Search. The pool is public, the pick order is ABBAABBAABBA and the Item Clause binds both sides, so the rest of
the draft can be played out. For each legal pick we run a minimax over the next `depth` picks (top-K cards by
marginal gain at every node, the opponent assumed to draft by the same model, i.e. to take what hurts us most:
this is where deny picks come from) and a greedy draft for everything after; iterative deepening under a NODE
budget (v3-harden; it was 0.3 s of wall clock): the deepest level completed within NODE_BUDGET nodes decides, so a
pick no longer depends on machine load. Near-ties (values within TIE_EPS logit of the best) go to the larger
immediate gain, then the card_id. SAFETY_WALL_S is only a guard against pathological slowness (it abandons the level
in progress, like the node budget). Every failure raises to PokemonPolicy._draft, which then keeps the old
card_score pick.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from .. import timecontrol
from .dex import to_id

log = logging.getLogger("agent.drafter")

SNAKE = "ABBAABBAABBA"
# Next to this module (not in data/) so scripts/freeze_champion.sh snapshots the model with the code.
MODEL_PATH = Path(__file__).resolve().parent / "draft_model.json"

# ---- knobs (see NOTES / docs) -------------------------------------------------------------------------
# Search nodes (card applications, greedy roll-outs included) per pick. Calibrated on the 228 real platform draft
# states + 150 catalog states (scripts/harden_calib_draft.py): ~416k nodes per CPU second here, so 120k is about the
# old 0.3 s budget, and the full depth-4 search never needed more than 26.6k nodes (p50 7.7k): like the old budget on
# an unloaded machine, it does not bind in practice -- but it can no longer shrink under load.
NODE_BUDGET = 120_000
SAFETY_WALL_S = 2.0      # wall-clock guard only (~7x the node budget's CPU time); never binds on a sane machine
DEADLINE_SHARE = 0.5     # ... nor past this share of the draft pick's time left (agent/timecontrol.py)
TIE_EPS = 0.05           # root values within this (logit) of the best are a tie: the larger immediate gain wins
TOP_K = 5                # branching of the minimax part
MAX_DEPTH = 4            # picks searched exactly (the root pick included); the rest is greedy
DEFAULT_BETA = 0.19      # weight of the calc matchup prior when no model file is present (fitted value)
DEFAULT_PRIOR = {"a": -0.11, "b": 0.02}  # m = a + b * card_score for unseen cards (no model file; fitted value)


class _Timeout(Exception):
    pass


def _knob(name: str, default: float) -> float:
    """Experiment override (local ladders only): ARENA_DRAFT_<NAME>=value. Deployed runs never set these."""
    try:
        v = os.environ.get(f"ARENA_DRAFT_{name}")
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def card_fingerprint(card: dict | None) -> str | None:
    """Hash of a card's full set, or None for a species-only stub (no moves / EVs: nothing to compare).

    Normalised so that the same set always hashes the same: ids instead of display names, moves as a set (order does
    not matter), EVs without zeros, IVs without 31s (the platform sends `ivs: {}`, the local sim fills 31s)."""
    if not isinstance(card, dict) or not card.get("moves") or card.get("evs") is None:
        return None

    def stat_map(d, skip):
        out = {}
        for k, v in (d or {}).items():
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v != skip:
                out[to_id(str(k))] = v
        return out

    try:
        level = int(card.get("level") or 50)
    except (TypeError, ValueError):
        level = 50
    canon = {"species": to_id(str(card.get("species") or "")), "item": to_id(str(card.get("item") or "")),
             "ability": to_id(str(card.get("ability") or "")), "nature": to_id(str(card.get("nature") or "")),
             "level": level, "moves": sorted(to_id(str(m)) for m in card.get("moves") or [] if m),
             "evs": stat_map(card.get("evs"), 0), "ivs": stat_map(card.get("ivs"), 31)}
    tera = card.get("tera_type") or card.get("teraType")
    if tera:
        canon["tera"] = to_id(str(tera))
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()[:16]


# ---- cards outside the catalog: stderr only ------------------------------------------------------------------
_REPORTED: set = set()
_CATALOG_FP: dict | None = None


def _catalog_fps() -> dict:
    global _CATALOG_FP
    if _CATALOG_FP is None:
        try:
            from .dex import card_catalog
            _CATALOG_FP = {cid: card_fingerprint(c) for cid, c in card_catalog().items()}
        except Exception:
            _CATALOG_FP = {}
    return _CATALOG_FP


def report_unknown_cards(cards, model: "DraftModel | None" = None, stream=None) -> list[str]:
    """Write one stderr line per card_id / set not in the card catalog (once per process per card_id + set).
    Deliberately NOT the logging module, the recorded logs or the reasoning summary. Returns the new lines."""
    cat = _catalog_fps()
    lines = []
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("card_id") or "")
        fp = card_fingerprint(c)
        if cid in cat and (fp is None or fp == cat[cid]):
            continue
        key = (cid, fp)
        if key in _REPORTED:
            continue
        _REPORTED.add(key)
        what = "not in the card catalog" if cid not in cat else f"set differs from the card catalog ({fp} vs {cat[cid]})"
        if fp is None and cid not in cat:
            what += " (no full set seen)"
        seen = "" if model is None else f"; draft model: {'known' if model.key(c) is not None else 'unseen, prior strength'}"
        lines.append(f"[agent.drafter] card {cid or '?'} ({c.get('species', '?')}): {what}{seen}")
    if lines:
        try:
            out = stream or sys.stderr
            out.write("\n".join(lines) + "\n")
            out.flush()
        except Exception:
            pass
    return lines


class DraftModel:
    def __init__(self, d: dict | None = None):
        d = d or {}
        self.main = {str(k): float(v) for k, v in (d.get("main") or {}).items()}
        # Optional (config full_asym): a card's strength when the OPPONENT holds it (fitted on games against other
        # pilots). Absent -> the same as `main`, i.e. the symmetric model.
        self.main_opp = {str(k): float(v) for k, v in (d.get("main_opp") or {}).items() if str(k) in self.main}
        self.cross: dict[tuple[str, str], float] = {}
        for k, v in (d.get("cross") or {}).items():
            a, b = k.split("|")
            self.cross[(a, b)] = float(v)
        self.within: dict[tuple[str, str], float] = {}
        for k, v in (d.get("within") or {}).items():
            a, b = sorted(k.split("|"))
            self.within[(a, b)] = float(v)
        self.beta = float(d.get("beta", DEFAULT_BETA))
        prior = d.get("prior") or DEFAULT_PRIOR
        self.prior_a, self.prior_b = float(prior.get("a", 0.0)), float(prior.get("b", 0.0))
        self.prior_c = float(prior.get("c", 0.0))              # two-feature prior: x mean calc duel
        self.prior_z = float(prior.get("z_mean", 0.0))         # mean duel of the fit's cards (used when unknown)
        self.fitted = bool(self.main)
        self.known = set(self.main) | {k for pair in list(self.cross) + list(self.within) for k in pair}
        # v3-harden: the full-set fingerprint of every fitted card (card_id -> fingerprint)
        self.fingerprints = {str(k): str(v) for k, v in (d.get("fingerprints") or {}).items() if v}
        self.by_fp: dict[str, str] = {}
        for cid in sorted(self.fingerprints):
            if cid in self.known:
                self.by_fp.setdefault(self.fingerprints[cid], cid)
        # legacy files (no fingerprints): one card per species; an unseen card_id of a known species borrows its weights
        self.by_species: dict[str, str] = {}
        for cid, sp in (d.get("species") or {}).items():
            self.by_species.setdefault(to_id(sp), cid)

    _cache: "DraftModel | None" = None

    @classmethod
    def load(cls) -> "DraftModel":
        if cls._cache is None:
            model = None
            try:
                p = Path(os.environ.get("ARENA_DRAFT_MODEL") or MODEL_PATH)
                if p.exists():
                    model = cls(json.loads(p.read_text()))
            except Exception:
                log.warning("draft model unreadable; using the heuristic prior", exc_info=True)
                model = None
            cls._cache = model or cls(None)
        return cls._cache

    def key(self, card: dict) -> str | None:
        """The model card whose learned weights this card uses, or None (unseen: prior strength, no X / S)."""
        cid = str(card.get("card_id") or "")
        if not self.fingerprints:                            # legacy model file
            if cid in self.known:
                return cid
            return self.by_species.get(to_id(card.get("species", "")))
        fp = card_fingerprint(card)
        if fp is None:                                       # a species-only stub: nothing contradicts its card_id
            return cid if cid in self.known else None
        want = self.fingerprints.get(cid)
        if want == fp or (want is None and cid in self.known):   # (a fitted card without a recorded set: by id)
            return cid
        return self.by_fp.get(fp)                            # the same set under another card_id; else unseen

    def prior_strength(self, card: dict, prior_score, duel: float | None = None) -> float:
        try:
            s = float(prior_score(card)) if prior_score else 0.0
        except Exception:
            s = 0.0
        z = self.prior_z if duel is None else float(duel)
        return self.prior_a + self.prior_b * s + self.prior_c * z

    def strength(self, card: dict, prior_score, duel: float | None = None) -> float:
        k = self.key(card)
        if k is not None and k in self.main:
            return self.main[k]
        return self.prior_strength(card, prior_score, duel)

    def x(self, ka: str | None, kb: str | None) -> float:
        if ka is None or kb is None or ka == kb:
            return 0.0
        if ka < kb:
            return self.cross.get((ka, kb), 0.0)
        return -self.cross.get((kb, ka), 0.0)

    def s(self, ka: str | None, kb: str | None) -> float:
        if ka is None or kb is None or ka == kb:
            return 0.0
        return self.within.get((ka, kb) if ka < kb else (kb, ka), 0.0)


_TT = None


def threat_table():
    global _TT
    if _TT is None:
        from .matchup import ThreatTable
        _TT = ThreatTable()
    return _TT


_TT_SINGLES = None


def singles_threat_table():
    """Process-wide threat table of the OU (singles) draft: the same duels on a singles field (a spread move is not
    cut to 0.75x there). Kept apart from the VGC instance above, which it never touches."""
    global _TT_SINGLES
    if _TT_SINGLES is None:
        from .matchup import ThreatTable
        _TT_SINGLES = ThreatTable("Singles")
    return _TT_SINGLES


class Drafter:
    def __init__(self, model: DraftModel | None = None, prior_score=None, node_budget: int | None = None,
                 top_k: int | None = None, max_depth: int | None = None, use_matchups: bool = True,
                 tie_eps: float | None = None, wall_cap: float | None = None, singles: bool = False):
        self.model = model or DraftModel.load()
        self.singles = singles               # OU singles draft: counters from singles_threat_table()
        self.prior_score = prior_score
        self.node_budget = int(node_budget if node_budget is not None else _knob("NODES", NODE_BUDGET))
        self.top_k = int(top_k if top_k is not None else _knob("TOPK", TOP_K))
        self.max_depth = int(max_depth if max_depth is not None else _knob("DEPTH", MAX_DEPTH))
        self.tie_eps = float(tie_eps if tie_eps is not None else _knob("TIE", TIE_EPS))
        self.wall_cap = float(wall_cap if wall_cap is not None else SAFETY_WALL_S)
        self.use_matchups = use_matchups
        self.info: dict = {}
        self.unseen: list[str] = []          # card_ids of this draft the model did not know (not in `info`)
        self.m_opp: list[float] = []         # per-card strength in the opponent's hands (set by _tables)

    # ---- tables over the cards of this draft ---------------------------------------------------------------
    def _tables(self, cards: list[dict]):
        n = len(cards)
        keys = [self.model.key(c) for c in cards]
        P = [[0.0] * n for _ in range(n)]
        S = [[0.0] * n for _ in range(n)]
        beta = self.model.beta
        tt = None
        if self.use_matchups and beta:
            tt = singles_threat_table() if self.singles else threat_table()
            tt.ensure(cards)
        from .matchup import card_key
        tkeys = [card_key(c) for c in cards]
        B = None
        if tt is not None:
            B = [[0.5] * n for _ in range(n)]
            for i in range(n):
                for j in range(i + 1, n):
                    b = tt.beats(tkeys[i], tkeys[j])
                    B[i][j], B[j][i] = b, 1.0 - b
        m = []
        for i, c in enumerate(cards):
            k = keys[i]
            if k is not None and k in self.model.main:
                m.append(self.model.main[k])
                continue
            # unseen: the two-feature prior; its mean duel against the other cards of this draft
            duel = sum(B[i][j] - 0.5 for j in range(n) if j != i) / (n - 1) if B is not None and n > 1 else None
            m.append(self.model.prior_strength(c, self.prior_score, duel))
        # the opponent's strengths: main_opp where the model has it, else the same as ours (symmetric model)
        self.m_opp = [self.model.main_opp[k] if k is not None and k in self.model.main_opp else m[i]
                      for i, k in enumerate(keys)]
        for i in range(n):
            for j in range(i + 1, n):
                v = self.model.x(keys[i], keys[j])
                if B is not None:
                    v += beta * (B[i][j] - 0.5)
                P[i][j], P[j][i] = v, -v
                s = self.model.s(keys[i], keys[j])
                S[i][j] = S[j][i] = s
        self.unseen = [str(c.get("card_id")) for c, k in zip(cards, keys) if k is None or k not in self.model.main]
        return m, P, S

    # ---- search ----------------------------------------------------------------------------------------------
    def pick(self, pool: list[dict], mine: list[dict], opp: list[dict], legal_ids, n_taken: int | None = None) -> str:
        """pool: every card still undrafted (full sets); mine / opp: the rosters so far; n_taken: picks already
        made (defaults to the roster sizes), which fixes whose turn it is in ABBAABBAABBA. Returns a card_id."""
        t0 = time.monotonic()
        legal_ids = set(legal_ids)
        cards, seen = [], set()
        for c in list(mine) + list(opp) + list(pool):
            cid = c.get("card_id")
            if cid in seen:
                continue
            seen.add(cid)
            cards.append(c)
        try:
            report_unknown_cards(cards, self.model)
        except Exception:
            pass
        idx = {c["card_id"]: i for i, c in enumerate(cards)}
        my_idx = [idx[c["card_id"]] for c in mine]
        opp_idx = [idx[c["card_id"]] for c in opp]
        rem = [idx[c["card_id"]] for c in pool if c["card_id"] not in {x["card_id"] for x in list(mine) + list(opp)}]
        roots = [i for i in rem if cards[i]["card_id"] in legal_ids]
        if not roots:
            raise ValueError("no legal card in the pool")
        taken = len(mine) + len(opp) if n_taken is None else max(0, int(n_taken))
        seq = SNAKE[taken:]
        me_letter = seq[0] if seq else "A"
        seq_me = [ch == me_letter for ch in seq]           # True = our pick
        m, P, S = self._tables(cards)
        mo = self.m_opp                                     # == m unless the model has main_opp
        budget = self.node_budget                           # the calc tables (first pick only) are not search work
        # the wall also never runs past DEADLINE_SHARE of the pick's time left (agent/timecontrol.py); unchanged
        # outside a decision and whenever the deadline is far (always, unless the machine is badly overloaded)
        wall = time.monotonic() + timecontrol.cap(self.wall_cap, DEADLINE_SHARE)
        items = [to_id(c.get("item") or "") or None for c in cards]
        n = len(cards)

        # gains: gm[c] = what adding c is worth to us, go[c] = to them (both from the picker's own view)
        gm = [m[c] + sum(P[c][j] for j in opp_idx) + sum(S[c][e] for e in my_idx) for c in range(n)]
        go = [mo[c] + sum(P[c][j] for j in my_idx) + sum(S[c][e] for e in opp_idx) for c in range(n)]
        L0 = sum(m[i] for i in my_idx) - sum(mo[j] for j in opp_idx) \
            + sum(P[i][j] for i in my_idx for j in opp_idx) \
            + sum(S[a][b] for x, a in enumerate(my_idx) for b in my_idx[x + 1:]) \
            - sum(S[a][b] for x, a in enumerate(opp_idx) for b in opp_idx[x + 1:])
        my_items0 = {items[i] for i in my_idx if items[i]}
        opp_items0 = {items[j] for j in opp_idx if items[j]}
        nodes = [0]
        wall_hit = [False]

        def apply(c, ours, rem, gm, go, L, mi, oi):
            nodes[0] += 1
            rem2 = [x for x in rem if x != c]
            gm2, go2 = gm[:], go[:]
            Sc = S[c]
            if ours:
                L += gm[c]
                for x in rem2:
                    gm2[x] += Sc[x]
                    go2[x] += P[x][c]
                if items[c]:
                    mi = mi | {items[c]}
            else:
                L -= go[c]
                for x in rem2:
                    gm2[x] += P[x][c]
                    go2[x] += Sc[x]
                if items[c]:
                    oi = oi | {items[c]}
            return rem2, gm2, go2, L, mi, oi

        def legal(rem, items_held):
            return [x for x in rem if not (items[x] and items[x] in items_held)]

        def greedy(k, rem, gm, go, L, mi, oi):
            while k < len(seq_me) and rem:
                ours = seq_me[k]
                cand = legal(rem, mi if ours else oi)
                if cand:
                    g = gm if ours else go
                    c = max(cand, key=g.__getitem__)
                    rem, gm, go, L, mi, oi = apply(c, ours, rem, gm, go, L, mi, oi)
                k += 1
            return L

        def search(k, depth, rem, gm, go, L, mi, oi):
            if k >= len(seq_me) or not rem:
                return L
            if depth <= 0:
                return greedy(k, rem, gm, go, L, mi, oi)
            if nodes[0] > budget:
                raise _Timeout
            if time.monotonic() > wall:
                wall_hit[0] = True
                raise _Timeout
            ours = seq_me[k]
            cand = legal(rem, mi if ours else oi)
            if not cand:
                return search(k + 1, depth, rem, gm, go, L, mi, oi)
            g = gm if ours else go
            cand = sorted(cand, key=g.__getitem__, reverse=True)[:self.top_k]
            best = None
            for c in cand:
                v = search(k + 1, depth - 1, *apply(c, ours, rem, gm, go, L, mi, oi))
                if best is None or (v > best if ours else v < best):
                    best = v
            return best

        values, depth_done, budget_hit = None, 0, False
        for depth in range(1, self.max_depth + 1):
            try:
                vals = {}
                for c in roots:
                    vals[c] = search(1, depth - 1, *apply(c, True, rem, gm, go, L0, my_items0, opp_items0))
                values, depth_done = vals, depth
            except _Timeout:
                budget_hit = True
                break
            if len(seq_me) <= depth:
                break
        if values is None:                                  # not even depth 1 within the budget: greedy on the gain
            values = {c: gm[c] for c in roots}
        top = max(values[c] for c in roots)
        near = [c for c in roots if values[c] >= top - self.tie_eps]
        best = max(near, key=lambda c: (gm[c], values[c], str(cards[c]["card_id"])))
        if wall_hit[0]:
            try:
                sys.stderr.write(f"[agent.drafter] safety wall cap {self.wall_cap}s hit at depth {depth_done} "
                                 f"({nodes[0]} nodes)\n")
            except Exception:
                pass
        self.info = {"depth": depth_done, "nodes": nodes[0], "ms": round(1000 * (time.monotonic() - t0), 1),
                     "value": round(values[best], 3), "fitted": self.model.fitted,
                     "budget_hit": budget_hit, "wall_hit": wall_hit[0], "ties": len(near),
                     "top": sorted(((round(values[c], 3), cards[c]["species"]) for c in roots), reverse=True)[:4]}
        return cards[best]["card_id"]
