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
import math
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

# ---- opponent model (ARENA_DRAFT_OPP; "off" = the v4.3 search exactly) ------------------------------------------------
# v4.3 assumes the opponent drafts by OUR model (the minimax's adversary, its greedy roll-outs take the opponent's model
# gain). The real contestants draft the standard competitive core instead (Flutter Mane, Incineroar, Dragonite, ...):
# our model names only ~19% of their real picks, a conditional logit fitted on them ~58% (scripts/fit_meta_drafter.py
# -> META_PATH, per-species utility u). With ARENA_DRAFT_OPP=meta the search treats the opponent as a mixture:
#   w   = posterior probability that this opponent drafts like the real contestants (OPP_PRIOR before its first pick;
#         each observed pick multiplies the odds by P_meta(pick) / P_model(pick), P_model = softmax(model gain / OPP_TEMP))
#   an opponent node is worth (1 - w) * min over the top-K by model gain (as v4.3) + w * the expectation over the
#   top-OPP_K cards by u, weighted by softmax(u); its greedy roll-out pick is the top card by u when w >= 0.5.
# Our own valuation (the model) is unchanged: this only changes WHICH cards we expect the opponent to take.
META_PATH = Path(__file__).resolve().parent / "draft_meta.json"
OPP_MODE = "meta"        # meta (v4.4-draft) | off (= v4.3)
OPP_PRIOR = 0.2          # prior probability of a meta-like opponent (0.2 beat 0.5 / 0.8 in draft-only tuning, seed 101)
OPP_TEMP = 0.15          # temperature (logit units of model gain) of the model-like opponent's pick likelihood (max-
                         # likelihood value for the v4.3 drafter's own picks, 145 simulated drafts)
OPP_K = 3                # meta candidates expanded at an opponent node
OPP_W = -1.0             # >= 0: a fixed mixture weight (no likelihood update); experiments only
OPP_MIN_KNOWN = 6        # the prior must know at least this many of the draft's species, else the model stays off
W_EPS = 1e-3             # w below this: the v4.3 search; above 1 - W_EPS: no adversary branch
# Counter term of the meta-like opponent (10-13, ARENA_DRAFT_OPPCTR; 0 = the v4.4 opponent model exactly). The real
# contestants also pick cards that beat OUR roster (fit_meta_drafter.py's counter term gamma, 2.41 on 41 real drafts;
# Dragonfly + Steve Nash Equilibrium alone 2.31). With OPP_CTR = 1 and a prior file that carries `gamma` (fitted WITH the counter term), the meta-like
# opponent's preference for card x at a search node is  u[x] + gamma * mean_{d in our roster there} (beats(x, d) - 0.5),
# in the likelihood of its observed picks (w), in the expected pick of an opponent node and in its greedy roll-outs.
OPP_CTR = 0.0            # scales the file's gamma; 0 = off. A prior file without `gamma` (draft_meta.json) = off too
# Which opponent-model set (ARENA_DRAFT_METASET): the prior file, OPP_PRIOR and OPP_CTR together. The explicit knobs
# (ARENA_DRAFT_META / OPPPRIOR / OPPCTR) still override each part.
#   v44   draft_meta.json (22 real drafts 10-08/09, u only), prior 0.2, no counter term = the v4.4 opponent model
#   1013  draft_meta_1013.json (41 real drafts to 10-13, incl. 10 vs Dragonfly, 1 vs Steve Nash Equilibrium; u fitted
#         with the counter term), prior 0.5, counter term on (used only if the file carries gamma)
META_PATH_1013 = Path(__file__).resolve().parent / "draft_meta_1013.json"
META_SETS = {"v44": (META_PATH, 0.2, 0.0), "1013": (META_PATH_1013, 0.5, 1.0)}
# 1013 (default since 10-13): vs a Dragonfly stand-in on fresh seed 173, 121/160 vs v4.4 106/160 (+9.4, McNemar
# p 0.04); docs/progress.md. ARENA_DRAFT_METASET=v44 restores the v4.4 opponent model exactly.
META_SET = "1013"

# ---- v4-pilot residual strengths (ARENA_DRAFT_RESID=1; off = v4.3) ------------------------------------------------
# The model above was fitted on v3 self-play (classic battle pilot); since v4 both seats battle with the engine search.
# scripts/fit_draft_resid.py fits, on local search-vs-search games (gate seeds excluded), a per-species residual d on
# top of the model's log-odds; with RESID on, d is added to a card's strength on both sides (m and m_opp).
RESID_PATH = Path(__file__).resolve().parent / "draft_resid.json"
RESID = 1.0              # 1 = on (v4.4-draft), 0 = off (= v4.3); any other number scales d (experiments)


class _Timeout(Exception):
    pass


def _knob(name: str, default: float) -> float:
    """Experiment override (local ladders only): ARENA_DRAFT_<NAME>=value. Deployed runs never set these."""
    try:
        v = os.environ.get(f"ARENA_DRAFT_{name}")
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def _knob_str(name: str, default: str) -> str:
    v = os.environ.get(f"ARENA_DRAFT_{name}")
    return str(v).strip().lower() if v not in (None, "") else default


class DraftMeta:
    """What real opponents take (META_PATH): species id -> utility u; unknown species -> mu. Empty when missing."""

    def __init__(self, d: dict | None = None):
        d = d or {}
        self.u = {str(k): float(v) for k, v in (d.get("u") or {}).items()}
        self.mu = float(d.get("mu", 0.0))
        self.gamma = float(d.get("gamma", 0.0) or 0.0)       # counter term (used only with OPP_CTR)

    def __bool__(self) -> bool:
        return bool(self.u)

    def utility(self, card: dict) -> float:
        return self.u.get(to_id(card.get("species", "")), self.mu)

    _cache: "dict | None" = None             # file path -> DraftMeta (None = empty; tests reset it)

    @classmethod
    def load(cls, path: "str | Path | None" = None) -> "DraftMeta":
        """ARENA_DRAFT_META (experiments) > `path` (the META_SET's file) > META_PATH."""
        p = Path(os.environ.get("ARENA_DRAFT_META") or path or META_PATH)
        if cls._cache is None:
            cls._cache = {}
        if str(p) not in cls._cache:
            meta = None
            try:
                if p.exists():
                    meta = cls(json.loads(p.read_text()))
            except Exception:
                log.warning("draft meta prior unreadable; opponent model off", exc_info=True)
                meta = None
            cls._cache[str(p)] = meta or cls(None)
        return cls._cache[str(p)]


_RESID: dict | None = None


def load_resid() -> dict:
    """species id -> residual strength (RESID_PATH, or ARENA_DRAFT_RESID_FILE); {} when missing or unreadable."""
    global _RESID
    if _RESID is None:
        out = {}
        try:
            p = Path(os.environ.get("ARENA_DRAFT_RESID_FILE") or RESID_PATH)
            if p.exists():
                out = {str(k): float(v) for k, v in (json.loads(p.read_text()).get("d") or {}).items()}
        except Exception:
            log.warning("draft residual file unreadable; residuals off", exc_info=True)
            out = {}
        _RESID = out
    return _RESID


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
                 tie_eps: float | None = None, wall_cap: float | None = None, singles: bool = False,
                 opp_mode: str | None = None, opp_prior: float | None = None, opp_temp: float | None = None,
                 opp_k: int | None = None, opp_w: float | None = None, meta: DraftMeta | None = None,
                 resid: float | bool | None = None, opp_ctr: float | None = None):
        self.model = model or DraftModel.load()
        self.singles = singles               # OU singles draft: counters from singles_threat_table()
        # v4-pilot residual strengths (see RESID): a scale, 0 = off (v4.3); never for the OU singles draft
        self.resid = 0.0 if singles else float(resid if resid is not None else _knob("RESID", RESID))
        # opponent model (see OPP_MODE); never for the OU singles draft (the prior is fitted on VGC drafts)
        self.opp_mode = (opp_mode if opp_mode is not None else _knob_str("OPP", OPP_MODE)).lower()
        if singles:
            self.opp_mode = "off"
        self.meta_set = _knob_str("METASET", META_SET)
        meta_path, prior0, ctr0 = META_SETS.get(self.meta_set, (META_PATH, OPP_PRIOR, OPP_CTR))
        self.opp_prior = float(opp_prior if opp_prior is not None else _knob("OPPPRIOR", prior0))
        self.opp_temp = max(1e-3, float(opp_temp if opp_temp is not None else _knob("OPPTEMP", OPP_TEMP)))
        self.opp_k = max(1, int(opp_k if opp_k is not None else _knob("OPPK", OPP_K)))
        self.opp_w = float(opp_w if opp_w is not None else _knob("OPPW", OPP_W))
        self.meta = meta if meta is not None else (DraftMeta.load(meta_path) if self.opp_mode == "meta"
                                                   else DraftMeta(None))
        self.opp_ctr = float(opp_ctr if opp_ctr is not None else _knob("OPPCTR", ctr0))
        self.B: list | None = None           # calc duel matrix of this draft's cards (set by _tables; None without)
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
        self.B = B
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
        if self.resid:                       # v4-pilot residuals (RESID): the same shift on both sides
            rd = load_resid()
            if rd:
                shift = [self.resid * rd.get(to_id(c.get("species", "")), 0.0) for c in cards]
                m = [v + s for v, s in zip(m, shift)]
                self.m_opp = [v + s for v, s in zip(self.m_opp, shift)]
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

        # ---- opponent model (ARENA_DRAFT_OPP=meta). Off, or w ~ 0: the v4.3 functions above, untouched. ----
        w = 0.0
        pref: list[float] = []
        if self.opp_mode == "meta" and self.meta and \
                sum(to_id(c.get("species", "")) in self.meta.u for c in cards) >= OPP_MIN_KNOWN:
            pref = [self.meta.utility(c) for c in cards]
            gam = self.opp_ctr * self.meta.gamma if self.B is not None else 0.0
            w = self._opp_weight(cards, my_idx, opp_idx, taken, me_letter, mo, P, S, pref, items, gam)
        else:
            gam = 0.0
        B = self.B

        # ---- counter-aware meta opponent (OPP_CTR, gam != 0): search_m / greedy_m plus our roster's duel sums ----
        def apply_c(c, ours, rem, gm, go, L, mi, oi, cs):
            out = apply(c, ours, rem, gm, go, L, mi, oi)
            if ours:
                nm, sums = cs
                cs = (nm + 1, [s + B[x][c] - 0.5 for x, s in enumerate(sums)])
            return (*out, cs)

        def pref_c(cand, cs):
            nm, sums = cs
            if not nm:
                return {x: pref[x] for x in cand}
            return {x: pref[x] + gam * sums[x] / nm for x in cand}

        def greedy_c(k, rem, gm, go, L, mi, oi, cs):
            while k < len(seq_me) and rem:
                ours = seq_me[k]
                cand = legal(rem, mi if ours else oi)
                if cand:
                    if ours:
                        c = max(cand, key=gm.__getitem__)
                    elif w >= 0.5:
                        pv = pref_c(cand, cs)
                        c = max(cand, key=pv.__getitem__)
                    else:
                        c = max(cand, key=go.__getitem__)
                    rem, gm, go, L, mi, oi, cs = apply_c(c, ours, rem, gm, go, L, mi, oi, cs)
                k += 1
            return L

        def search_c(k, depth, rem, gm, go, L, mi, oi, cs):
            if k >= len(seq_me) or not rem:
                return L
            if depth <= 0:
                return greedy_c(k, rem, gm, go, L, mi, oi, cs)
            if nodes[0] > budget:
                raise _Timeout
            if time.monotonic() > wall:
                wall_hit[0] = True
                raise _Timeout
            ours = seq_me[k]
            cand = legal(rem, mi if ours else oi)
            if not cand:
                return search_c(k + 1, depth, rem, gm, go, L, mi, oi, cs)
            if ours:
                best = None
                for c in sorted(cand, key=gm.__getitem__, reverse=True)[:self.top_k]:
                    v = search_c(k + 1, depth - 1, *apply_c(c, True, rem, gm, go, L, mi, oi, cs))
                    if best is None or v > best:
                        best = v
                return best
            pv = pref_c(cand, cs)
            adv = sorted(cand, key=go.__getitem__, reverse=True)[:self.top_k] if w < 1.0 - W_EPS else []
            prd = sorted(cand, key=pv.__getitem__, reverse=True)[:self.opp_k]
            vals = {}
            for c in dict.fromkeys(adv + prd):
                vals[c] = search_c(k + 1, depth - 1, *apply_c(c, False, rem, gm, go, L, mi, oi, cs))
            top = max(pv[c] for c in prd)
            ws = [math.exp(pv[c] - top) for c in prd]
            exp_v = sum(wi * vals[c] for wi, c in zip(ws, prd)) / sum(ws)
            if not adv:
                return exp_v
            return (1.0 - w) * min(vals[c] for c in adv) + w * exp_v

        def greedy_m(k, rem, gm, go, L, mi, oi):
            while k < len(seq_me) and rem:
                ours = seq_me[k]
                cand = legal(rem, mi if ours else oi)
                if cand:
                    g = gm if ours else (pref if w >= 0.5 else go)
                    c = max(cand, key=g.__getitem__)
                    rem, gm, go, L, mi, oi = apply(c, ours, rem, gm, go, L, mi, oi)
                k += 1
            return L

        def search_m(k, depth, rem, gm, go, L, mi, oi):
            if k >= len(seq_me) or not rem:
                return L
            if depth <= 0:
                return greedy_m(k, rem, gm, go, L, mi, oi)
            if nodes[0] > budget:
                raise _Timeout
            if time.monotonic() > wall:
                wall_hit[0] = True
                raise _Timeout
            ours = seq_me[k]
            cand = legal(rem, mi if ours else oi)
            if not cand:
                return search_m(k + 1, depth, rem, gm, go, L, mi, oi)
            if ours:
                best = None
                for c in sorted(cand, key=gm.__getitem__, reverse=True)[:self.top_k]:
                    v = search_m(k + 1, depth - 1, *apply(c, True, rem, gm, go, L, mi, oi))
                    if best is None or v > best:
                        best = v
                return best
            # the opponent: (1 - w) x the adversary of v4.3 + w x the expected pick of a meta-like drafter
            adv = sorted(cand, key=go.__getitem__, reverse=True)[:self.top_k] if w < 1.0 - W_EPS else []
            prd = sorted(cand, key=pref.__getitem__, reverse=True)[:self.opp_k]
            vals = {}
            for c in dict.fromkeys(adv + prd):
                vals[c] = search_m(k + 1, depth - 1, *apply(c, False, rem, gm, go, L, mi, oi))
            top = max(pref[c] for c in prd)
            ws = [math.exp(pref[c] - top) for c in prd]
            exp_v = sum(wi * vals[c] for wi, c in zip(ws, prd)) / sum(ws)
            if not adv:
                return exp_v
            return (1.0 - w) * min(vals[c] for c in adv) + w * exp_v

        run = search_m if w > W_EPS else search
        cs0 = None
        if w > W_EPS and gam:
            run = search_c
            cs0 = (len(my_idx), [sum(B[x][d] - 0.5 for d in my_idx) for x in range(n)])
        values, depth_done, budget_hit = None, 0, False
        for depth in range(1, self.max_depth + 1):
            try:
                vals = {}
                for c in roots:
                    if cs0 is not None:
                        vals[c] = search_c(1, depth - 1, *apply_c(c, True, rem, gm, go, L0, my_items0, opp_items0, cs0))
                        continue
                    vals[c] = run(1, depth - 1, *apply(c, True, rem, gm, go, L0, my_items0, opp_items0))
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
        # diagnostics only (scripts / local evaluation): every legal root's search value and immediate gain
        self.root_values = {cards[c]["card_id"]: (values[c], gm[c]) for c in roots}
        self.info = {"depth": depth_done, "nodes": nodes[0], "ms": round(1000 * (time.monotonic() - t0), 1),
                     "value": round(values[best], 3), "fitted": self.model.fitted,
                     "budget_hit": budget_hit, "wall_hit": wall_hit[0], "ties": len(near),
                     "top": sorted(((round(values[c], 3), cards[c]["species"]) for c in roots), reverse=True)[:4]}
        if self.opp_mode == "meta":
            self.info["opp_w"] = round(w, 3)
        return cards[best]["card_id"]

    def _opp_weight(self, cards, my_idx, opp_idx, taken, me_letter, mo, P, S, pref, items, gam: float = 0.0) -> float:
        """Posterior probability that the opponent drafts like the real contestants (meta prior) rather than like
        our model: OPP_PRIOR, times P_meta / P_model of each pick it made so far (rosters are in pick order, the
        snake order says which earlier picks were whose). Every legal set is rebuilt from the full pool (Item
        Clause included). A history that does not fit the snake order keeps the prior."""
        if self.opp_w >= 0:
            return min(1.0, self.opp_w)
        p = min(1 - 1e-3, max(1e-3, self.opp_prior))
        lo = math.log(p / (1 - p))
        n = len(cards)

        def lse(xs):
            mx = max(xs)
            return mx + math.log(sum(math.exp(x - mx) for x in xs))

        if len(my_idx) + len(opp_idx) == taken:
            ours, theirs = iter(my_idx), iter(opp_idx)
            mine_before, opp_before, gone = [], [], set()
            for k in range(taken):
                if SNAKE[k] == me_letter:
                    c = next(ours)
                    mine_before.append(c)
                    gone.add(c)
                    continue
                c = next(theirs)
                held = {items[j] for j in opp_before if items[j]}
                leg = [x for x in range(n) if x not in gone and not (items[x] and items[x] in held)] \
                    or [x for x in range(n) if x not in gone]
                if c not in leg:
                    leg.append(c)
                gain = {x: mo[x] + sum(P[x][y] for y in mine_before) + sum(S[x][y] for y in opp_before) for x in leg}
                pk = pref
                if gam and mine_before:              # OPP_CTR: the counter term against our roster at that pick
                    pk = list(pref)
                    for x in leg:
                        pk[x] = pref[x] + gam * sum(self.B[x][y] - 0.5 for y in mine_before) / len(mine_before)
                lo += (pk[c] - lse([pk[x] for x in leg])) \
                    - (gain[c] / self.opp_temp - lse([gain[x] / self.opp_temp for x in leg]))
                opp_before.append(c)
                gone.add(c)
        lo = max(-30.0, min(30.0, lo))
        return 1.0 / (1.0 + math.exp(-lo))
