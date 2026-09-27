"""Team preview via a 6x6 matchup matrix (exact damage calc + real speed formulas).

At team preview both rosters are known: our drafted cards and the opponent's (recovered by species from every
card we saw during the draft / the real card catalog). For every (ours, theirs) pair we compute the best-move
damage fraction in both directions (doubles gameType, so spread moves already carry the 0.75x), who moves first
(priority of the KO move, then effective speed) and a pair score. `choose_lineup` then scores all 15 x 6 = 90
(bring 4, lead 2) lineups against a coverage objective and picks the best. Everything degrades gracefully:
`matchup_matrix` returns None when the calc bridge is unavailable and the caller falls back to the heuristic.
"""
from __future__ import annotations

import itertools

from . import dex
from .calc import DamageCalc, calc_pokemon
from .dex import to_id
from .stats import effective_speed, move_priority
from .typechart import effectiveness

# A representative single-target STAB move per type, used only when a set's moves are unknown.
GENERIC_PHYS = {"NORMAL": "Body Slam", "FIRE": "Flare Blitz", "WATER": "Waterfall", "ELECTRIC": "Wild Charge",
                "GRASS": "Seed Bomb", "ICE": "Icicle Crash", "FIGHTING": "Close Combat", "POISON": "Poison Jab",
                "GROUND": "High Horsepower", "FLYING": "Brave Bird", "PSYCHIC": "Zen Headbutt", "BUG": "X-Scissor",
                "ROCK": "Stone Edge", "GHOST": "Shadow Claw", "DRAGON": "Dragon Claw", "DARK": "Crunch",
                "STEEL": "Iron Head", "FAIRY": "Play Rough"}
GENERIC_SPEC = {"NORMAL": "Tri Attack", "FIRE": "Flamethrower", "WATER": "Scald", "ELECTRIC": "Thunderbolt",
                "GRASS": "Energy Ball", "ICE": "Ice Beam", "FIGHTING": "Aura Sphere", "POISON": "Sludge Bomb",
                "GROUND": "Earth Power", "FLYING": "Air Slash", "PSYCHIC": "Psychic", "BUG": "Bug Buzz",
                "ROCK": "Power Gem", "GHOST": "Shadow Ball", "DRAGON": "Dragon Pulse", "DARK": "Dark Pulse",
                "STEEL": "Flash Cannon", "FAIRY": "Moonblast"}
GENERIC_CARD = {"evs": {"hp": 4, "atk": 252, "spa": 252, "spe": 252}, "nature": "Hardy", "level": 50}

# Abilities / items that blunt an opposing Intimidate.
INTIMIDATE_PROOF = {"defiant", "competitive", "innerfocus", "owntempo", "oblivious", "scrappy", "guarddog",
                    "clearbody", "whitesmoke", "fullmetalbody", "hypercutter", "mirrorarmor"}
FIELD = {"gameType": "Doubles", "attackerSide": {}, "defenderSide": {}}
HALF_HP_MOVES = {"ruination", "superfang", "naturesmadness"}

LEAD_SUPPORT = {"fakeout": 1.0, "followme": 0.8, "ragepowder": 0.8, "tailwind": 0.9, "icywind": 0.3,
                "electroweb": 0.3, "trickroom": 0.4, "spore": 0.5, "helpinghand": 0.3, "wideguard": 0.2,
                "coaching": 0.2, "snarl": 0.25, "partingshot": 0.3, "thunderwave": 0.25, "reflect": 0.25,
                "lightscreen": 0.25, "encore": 0.2, "taunt": 0.2}


class Mon:
    """One roster slot: species id, full card if known, and the preview view the platform sent."""

    def __init__(self, view: dict | None, card: dict | None):
        view = view or {}
        self.view = view
        self.card = card
        self.sid = to_id(view.get("species") or (card or {}).get("species") or "")
        self.known = bool(card and card.get("evs") is not None)
        self.types = [t.upper() for t in (view.get("types") or dex.types_of(self.sid))]
        moves = (card or {}).get("moves") or view.get("moves") or []
        self.moves = [to_id(m) for m in moves]
        self.ability = to_id((card or {}).get("ability") or view.get("ability") or "")
        self.item = to_id((card or {}).get("item") or view.get("item") or "")
        if self.item == "unknownitem":
            self.item = ""
        self.calc_card = card if self.known else {**GENERIC_CARD, "species": self.sid,
                                                   "ability": self.ability or None, "item": self.item or None}
        self.calc = calc_pokemon(None, self.calc_card, self.sid)
        self.speed = effective_speed({"species": self.sid, "item": self.item, "ability": self.ability}, self.calc_card)
        bs = dex.base_stats(self.sid)
        self.physical = bs.get("atk", 80) >= bs.get("spa", 80)

    def attacking_moves(self) -> list[tuple[str, int]]:
        """(calc move name, priority) for every damaging move; generic STAB moves when the set is unknown."""
        out = []
        for m in self.moves:
            md = dex.move(m)
            if (md.get("basePower") or 0) > 0 or md.get("basePowerCallback") or m in HALF_HP_MOVES:
                out.append((md.get("name") or m, move_priority(m, {"ability": self.ability})))
        if out or self.moves:
            return out
        table = GENERIC_PHYS if self.physical else GENERIC_SPEC
        return [(table[t], 0) for t in self.types if t in table]


def matchup_matrix(mine: list[Mon], theirs: list[Mon]) -> dict | None:
    """out[i][j] / inn[i][j]: best-move max-roll damage fraction ours i -> theirs j and theirs j -> ours i.
    first[i][j]: True if ours i lands its best hit before theirs j (KO-move priority, then speed), None on tie.
    Returns None when the exact calc is unavailable."""
    calcs, index = [], []
    for i, a in enumerate(mine):
        for j, d in enumerate(theirs):
            for name, pr in a.attacking_moves():
                index.append(("out", i, j, pr))
                calcs.append({"attacker": a.calc, "defender": d.calc, "move": name, "field": FIELD})
            for name, pr in d.attacking_moves():
                index.append(("in", i, j, pr))
                calcs.append({"attacker": d.calc, "defender": a.calc, "move": name, "field": FIELD})
    res = DamageCalc.get().batch(calcs)
    if res is None:
        return None
    n, m = len(mine), len(theirs)
    out = [[0.0] * m for _ in range(n)]
    out_min = [[0.0] * m for _ in range(n)]
    out_pr = [[0] * m for _ in range(n)]
    inn = [[0.0] * m for _ in range(n)]
    in_min = [[0.0] * m for _ in range(n)]
    in_pr = [[0] * m for _ in range(n)]
    for (kind, i, j, pr), r, c in zip(index, res, calcs):
        if not r or not r.get("rolls") or not r.get("defender_hp"):
            continue
        hp = float(r["defender_hp"])
        hi, lo = max(r["rolls"]) / hp, min(r["rolls"]) / hp
        if to_id(c["move"]) in HALF_HP_MOVES and hi < 0.5:
            hi = lo = 0.5
        if kind == "out":
            if hi > out[i][j]:
                out[i][j], out_min[i][j], out_pr[i][j] = hi, lo, pr
        elif hi > inn[i][j]:
            inn[i][j], in_min[i][j], in_pr[i][j] = hi, lo, pr
    first = [[None] * m for _ in range(n)]
    for i, a in enumerate(mine):
        for j, d in enumerate(theirs):
            if out_pr[i][j] != in_pr[i][j]:
                first[i][j] = out_pr[i][j] > in_pr[i][j]
            elif a.speed != d.speed:
                first[i][j] = a.speed > d.speed
    # Focus Sash: a full-HP OHKO becomes a 2HKO.
    for i, a in enumerate(mine):
        for j, d in enumerate(theirs):
            if d.item == "focussash" and out_min[i][j] >= 1.0:
                out_min[i][j] = 0.99
            if a.item == "focussash" and in_min[i][j] >= 1.0:
                in_min[i][j] = 0.99
    return {"out": out, "out_min": out_min, "in": inn, "in_min": in_min, "first": first}


def pair_score(mx: dict, i: int, j: int) -> float:
    """How good ours i is into theirs j, in roughly [-1.5, 1.5]."""
    o, oi = mx["out"][i][j], mx["out_min"][i][j]
    n, ni = mx["in"][i][j], mx["in_min"][i][j]
    first = mx["first"][i][j]
    s = min(o, 1.0) - min(n, 1.0)
    if oi >= 1.0 and (first is not False):            # guaranteed OHKO, and we land it first (or tie)
        s += 0.4 if first else 0.2
    if ni >= 1.0 and (first is not True):             # they OHKO us first
        s -= 0.5 if first is False else 0.25
    if first is True:
        s += 0.1
    elif first is False:
        s -= 0.1
    return s


def threat(mx: dict, i: int, j: int) -> float:
    """Pressure ours i puts on theirs j: >=1 means a 2HKO-or-better that they don't pre-empt with an OHKO."""
    o = mx["out"][i][j]
    p = min(o / 0.5, 2.0)
    if mx["in_min"][i][j] >= 1.0 and mx["first"][i][j] is False:
        p *= 0.3                                       # they OHKO first: the threat rarely materialises
    return p


def opp_attack_types(theirs: list[Mon]) -> dict[str, float]:
    """Weight of each attacking type in the opposing roster (STAB counts double)."""
    w: dict[str, float] = {}
    for d in theirs:
        seen = set()
        for m in d.moves:
            md = dex.move(m)
            if (md.get("basePower") or 0) > 0 and md.get("type"):
                t = md["type"].upper()
                if t in seen:
                    continue
                seen.add(t)
                w[t] = w.get(t, 0.0) + (2.0 if t in d.types else 1.0)
        if not d.moves:
            for t in d.types:
                w[t] = w.get(t, 0.0) + 2.0
    return w


# Linear objective weights over the lineup features below (a lineup's score = sum W[k] * feature[k]).
W = {
    "cov": 1.6,         # coverage: every opposing mon threatened (2HKO-or-better, not pre-empted) by someone we bring
    "cov_hard": 0.8,    # ... share of opposing mons with a full >=2HKO threat
    "vul": -1.2,        # brought mons that get OHKO'd first / out-sped and 2HKO'd while doing little back
    "weak": -0.06,      # shared weaknesses to the opponent's real attacking types
    "team": 1.0,        # mean pair score of the brought four
    "base": 1.0,        # draft-time card value (staples, BST, support)
    "intim": -0.12,     # physical attackers facing an opposing Intimidate
    "speed_ctrl": 0.0,  # a Tailwind / Trick Room setter among the four, weighted by how much it flips speed
    "lead_sup": 0.25,   # support lead (Fake Out, Intimidate, Tailwind, redirection, TR) next to a strong hitter
    "lead_off": 0.15,   # raw lead offense
    "lead_fast": 0.1,   # leads that move first
    "lead_pair": 0.1,   # leads' mean pair score
    "lead_weak": -0.05, # both leads weak to the same opposing type
    "double_fakeout": -0.15,
    "lead_spread": 0.0, # spread damage from the leads (hits both opposing leads)
    "lead_vs_lead": 0.0,  # leads' pair score against the opponent's two most likely leads
}
OPP_BRING = 0.5         # weight of the opponent's 2 least likely brings (1.0 = uniform over all 6)
SPEED_SETTERS = {"tailwind": "tw", "trickroom": "tr"}


def opp_bring_weights(mine: list[Mon], theirs: list[Mon], mx: dict) -> list[float]:
    """A sensible opponent brings its best-matched four: down-weight their two worst mons into our roster."""
    n, m = len(mine), len(theirs)
    if m <= 4:
        return [1.0] * m
    their_score = [sum(-pair_score(mx, i, j) for i in range(n)) / max(1, n) for j in range(m)]
    order = sorted(range(m), key=lambda j: their_score[j], reverse=True)
    w = [1.0] * m
    for j in order[4:]:
        w[j] = OPP_BRING
    return w


def lineup_features(mine: list[Mon], theirs: list[Mon], mx: dict, base: list[float] | None = None):
    """Yield ((bring, leads), {feature: value}) for every bring-4 / lead-2 lineup (90 for a full roster)."""
    n, m = len(mine), len(theirs)
    base = base or [0.0] * n
    atk_types = opp_attack_types(theirs)
    opp_intimidate = any(d.ability == "intimidate" for d in theirs)
    weak = [[t for t in atk_types if effectiveness(t, a.types) >= 2] for a in mine]
    ow = opp_bring_weights(mine, theirs, mx)
    osum = sum(ow) or 1.0
    mean_pair = [sum(ow[j] * pair_score(mx, i, j) for j in range(m)) / osum for i in range(n)]
    intim = [1.0 if opp_intimidate and a.physical and a.ability not in INTIMIDATE_PROOF and a.item != "clearamulet"
             else 0.0 for a in mine]
    support = [sum(LEAD_SUPPORT.get(mv, 0.0) for mv in a.moves) + (1.0 if a.ability == "intimidate" else 0.0)
               for a in mine]
    offense = [sum(ow[j] * min(mx["out"][i][j], 1.2) for j in range(m)) / osum for i in range(n)]
    fast = [sum(ow[j] for j in range(m) if mx["first"][i][j]) / osum for i in range(n)]
    slow = [sum(ow[j] for j in range(m) if mx["first"][i][j] is False) / osum for i in range(n)]
    thr = [[threat(mx, i, j) for j in range(m)] for i in range(n)]
    setters = [{SPEED_SETTERS[mv] for mv in a.moves if mv in SPEED_SETTERS} for a in mine]
    spread = [max((1.0 for mv in a.moves if dex.move(mv).get("target") in ("allAdjacentFoes", "allAdjacent")
                   and (dex.move(mv).get("basePower") or 0) > 0), default=0.0) * offense[i] for i, a in enumerate(mine)]
    # The opponent's likely leads: support movers (Fake Out / Intimidate / Tailwind / redirection) and top hitters.
    opp_sup = [sum(LEAD_SUPPORT.get(mv, 0.0) for mv in d.moves) + (1.0 if d.ability == "intimidate" else 0.0)
               for d in theirs]
    opp_off = [sum(min(mx["in"][i][j], 1.2) for i in range(n)) / max(1, n) for j in range(m)]
    likely = sorted(range(m), key=lambda j: (-(ow[j] + opp_sup[j] + opp_off[j]), j))[:2]
    vs_lead = [sum(pair_score(mx, i, j) for j in likely) / max(1, len(likely)) for i in range(n)]
    vul1 = []
    for i in range(n):
        v = 0.0
        for j in range(m):
            if mx["in_min"][i][j] >= 1.0 and mx["first"][i][j] is not True:
                v += ow[j]
            elif mx["in"][i][j] >= 0.5 and mx["first"][i][j] is False and mx["out"][i][j] < 0.5:
                v += 0.35 * ow[j]
        vul1.append(v / osum)

    for bring in itertools.combinations(range(n), 4):
        cov = hard = 0.0
        for j in range(m):
            t = max(thr[i][j] for i in bring)
            cov += ow[j] * (min(t, 1.0) + 0.15 * max(0.0, min(t, 2.0) - 1.0))
            hard += ow[j] * (t >= 1.0)
        weak_pen = 0.0
        for t, wt in atk_types.items():
            k = sum(1 for i in bring if t in weak[i])
            if k >= 2:
                weak_pen += wt * (k - 1)
        # Speed control: Tailwind helps the mons that are out-sped, Trick Room the ones that are slow anyway.
        kinds = set().union(*(setters[i] for i in bring))
        sc = 0.0
        if "tw" in kinds:
            sc = max(sc, sum(slow[i] for i in bring) / 4)
        if "tr" in kinds:
            sc = max(sc, sum(slow[i] - fast[i] for i in bring) / 4)
        team = {"cov": cov / osum, "cov_hard": hard / osum, "vul": sum(vul1[i] for i in bring) / 4,
                "weak": weak_pen, "team": sum(mean_pair[i] for i in bring) / 4,
                "base": sum(base[i] for i in bring) / 4, "intim": sum(intim[i] for i in bring) / 4, "speed_ctrl": sc}
        for leads in itertools.combinations(bring, 2):
            a, b = leads
            sup_i, hit_i = (a, b) if support[a] >= support[b] else (b, a)
            lw = 0.0
            for t, wt in atk_types.items():
                if t in weak[a] and t in weak[b]:
                    lw += wt
            f = dict(team)
            f.update({"lead_sup": min(support[sup_i], 2.0) * (0.5 + offense[hit_i]),
                      "lead_off": offense[a] + offense[b], "lead_fast": fast[a] + fast[b],
                      "lead_pair": mean_pair[a] + mean_pair[b], "lead_weak": lw,
                      "double_fakeout": float("fakeout" in mine[a].moves and "fakeout" in mine[b].moves),
                      "lead_spread": spread[a] + spread[b], "lead_vs_lead": vs_lead[a] + vs_lead[b]})
            yield (bring, leads), f


def choose_lineup(mine: list[Mon], theirs: list[Mon], mx: dict, base: list[float] | None = None,
                  weights: dict | None = None) -> tuple[list[int], list[int], float]:
    """Return (bring indices with the leads first, lead indices, score) maximising the linear objective."""
    w = weights or W
    best, best_key = None, None
    for (bring, leads), f in lineup_features(mine, theirs, mx, base):
        score = sum(w.get(k, 0.0) * v for k, v in f.items())
        key = (score, -sum(bring), -sum(leads))          # deterministic tie-break
        if best_key is None or key > best_key:
            best, best_key = (list(bring), list(leads)), key
    bring, leads = best
    bring = list(leads) + [i for i in bring if i not in leads]
    return bring, leads, best_key[0]

def summarize(mine: list[Mon], theirs: list[Mon], mx: dict) -> str:
    rows = []
    for i, a in enumerate(mine):
        cells = [f"{mx['out'][i][j] * 100:3.0f}/{mx['in'][i][j] * 100:3.0f}{'>' if mx['first'][i][j] else '<' if mx['first'][i][j] is False else '='}"
                 for j in range(len(theirs))]
        rows.append(f"{a.sid:>14} " + " ".join(cells))
    return "\n".join(rows)
