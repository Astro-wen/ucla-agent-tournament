"""Bayesian deal inference for 7-seat Werewolf (2 wolves, 1 seer, 4 villagers).

Every deal (which two seats are wolves and which seat is the seer: 105 of them) that fits what this seat knows (its own
role, its ally, its checks, the roles revealed at death) is weighed by how likely the public behaviour is under it:

* seer claims: a true seer states true results and usually claims on its own; a wolf usually claims only after a
  claimer named it (a reactive counter-claim) and rarely names its partner; a plain villager rarely claims; a living
  true seer rarely stays silent while another seat claims;
* day votes: a wolf rarely votes its partner and likes voting a claimer who named a wolf; a villager leans a little
  toward wolves and away from a claimed true seer;
* night kills: the wolves kill claimed seers and seats that voted for or named a wolf the day before;
* chat: a wolf rarely calls its partner a wolf or pushes a vote on it.

The weights are generic guesses about players at large, not fitted to any one opponent. Our own votes and chat are
left out (they carry no information to us). The result is P(wolf) and P(seer) per seat; with no consistent deal (a
misread claim against our private knowledge) the private knowledge is dropped, then the call returns None.
"""
from __future__ import annotations

import math
from itertools import combinations

N = 7
DEALS = tuple((frozenset(w), s) for w in combinations(range(N), 2) for s in range(N) if s not in w)

P_WOLF_PROACTIVE = 0.15     # a wolf claims seer before any claimer named it
P_SEER_REACTIVE = 0.5       # the true seer claims only after a claimer named it wolf
P_VILL_CLAIM = 0.03         # a villager claims seer
SEER_SILENT = 0.4           # the living true seer stays silent while another seat claims
LIE = 1e-3                  # a true seer's stated result is wrong (in practice: we misread the claim)
BUS_CLAIM = 0.05            # a fake seer names its own partner a wolf
BUS = 0.25                  # a wolf votes its partner (weight among the voter's options)
VOTE_ACCUSER = 2.0          # a wolf votes a claimer who named a wolf
RHO = 1.3                   # a villager votes a wolf
VOTE_SEER_V = 0.4           # a villager votes the claimed true seer
ABST_W, ABST_V = 0.3, 0.15  # abstaining
KILL_CLAIMED = 4.0          # the wolves kill a claimed seer
KILL_ACCUSER = 2.0          # ... or a seat that voted for / named a wolf the day before
BUS_CHAT = 0.4              # a wolf names its partner in chat (wolf / suspicious / vote it)
RHO_CHAT = 1.15             # a villager names a wolf in chat

_CACHE: dict = {}


class Evidence:
    """What the likelihood reads, extracted once from a view: claims, ballots, kills, chat accusations."""

    def __init__(self, v, claims, accusations):
        self.me = v.me
        self.claims = []              # (claimer, named wolves, named clears, reactive, first-claim day)
        for s in claims.order:
            self.claims.append((s, frozenset(claims.wolves.get(s, ())), frozenset(claims.clears.get(s, ())),
                                s in claims.reactive, claims.day.get(s)))
        self.claimers = {c[0] for c in self.claims}
        self.claim_day = {c[0]: (c[4] if c[4] is not None else 0) for c in self.claims}
        self.votes = []               # (day, [(voter, target or None, options)])
        for day, ballots, _elim in v.votes:
            voters = sorted(ballots)
            rows = [(u, t, [x for x in voters if x != u] + [None]) for u, t in sorted(ballots.items()) if u != v.me]
            if isinstance(day, int):
                self.votes.append((day, rows))
        self.accus = {(d, sp, t) for d, sp, t in accusations if sp != v.me and t != sp}
        self.kills = []               # (day of the dawn, victim, the seats alive that night)
        for x, cause in v.dead_cause.items():
            d = v.dead_day.get(x)
            if cause != "night_kill" or not isinstance(d, int):
                continue
            alive = [y for y in range(N) if y not in v.dead or (isinstance(v.dead_day.get(y), int) and v.dead_day[y] >= d)]
            self.kills.append((d, x, alive))


def _consistent(v, ws, s, private: bool) -> bool:
    for x, r in v.dead.items():
        if r == "wolf" and x not in ws or r in ("villager", "seer") and x in ws or (r == "seer") != (x == s) and r != "?":
            return False
    if not private:
        return True
    if v.role == "wolf":
        if v.me not in ws or any(a not in ws for a in v.allies):
            return False
    elif v.role == "seer":
        if s != v.me or any((t in ws) != (r == "wolf") for t, r in v.investigations.items()):
            return False
    elif v.me in ws or v.me == s:
        return False
    return True


def _loglik(ev: Evidence, ws, s, alive_now) -> float:
    L = 0.0
    log = math.log
    for c, wolves, clears, reactive, _day in ev.claims:
        if c == s:
            if reactive:
                L += log(P_SEER_REACTIVE)
            for t in wolves:
                if t not in ws:
                    L += log(LIE)
            for t in clears:
                if t in ws:
                    L += log(LIE)
        elif c in ws:
            if not reactive:
                L += log(P_WOLF_PROACTIVE)
            if wolves & ws:
                L += log(BUS_CLAIM)
        else:
            L += log(P_VILL_CLAIM)
    if ev.claims and s not in ev.claimers and s in alive_now:
        L += log(SEER_SILENT)

    def named_wolf(y, day) -> bool:          # y had claimed by `day` and named a wolf of this deal
        if y not in ev.claimers or ev.claim_day.get(y, 0) > day:
            return False
        return any(c == y and (w & ws) for c, w, _cl, _r, _d in ev.claims)

    for day, rows in ev.votes:
        for u, t, options in rows:
            wolf_v = u in ws
            tot, mine = 0.0, None
            for x in options:
                if x is None:
                    p = ABST_W if wolf_v else ABST_V
                elif wolf_v:
                    p = BUS if x in ws else 1.0
                    if named_wolf(x, day):
                        p *= VOTE_ACCUSER
                else:
                    p = RHO if x in ws else 1.0
                    if x == s and x in ev.claimers and ev.claim_day.get(x, 0) <= day:
                        p *= VOTE_SEER_V
                tot += p
                if x == t:
                    mine = p
            if mine is not None and tot > 0:
                L += log(mine / tot)
    for d, x, before in ev.kills:
        if x in ws:
            return -1e9
        tot, mine = 0.0, 1.0
        for y in before:
            if y in ws:
                continue
            p = 1.0
            if y in ev.claimers and ev.claim_day.get(y, 0) <= d - 1:
                p *= KILL_CLAIMED
            accused = any(dd == d - 1 and sp == y and t in ws for dd, sp, t in ev.accus) or named_wolf(y, d - 1) or any(
                day == d - 1 and any(u == y and t in ws for u, t, _o in rows) for day, rows in ev.votes)
            if accused:
                p *= KILL_ACCUSER
            tot += p
            if y == x:
                mine = p
        if tot > 0:
            L += log(mine / tot)
    for _d, sp, t in ev.accus:
        if sp in ws:
            if t in ws:
                L += log(BUS_CHAT)
        elif t in ws:
            L += log(RHO_CHAT)
    return L


def posterior(v, claims, accusations, private: bool = True):
    """(P(wolf), P(seer)) per seat as two lists of N floats, or None when no deal fits the public record."""
    key = (v.me, v.role, private, tuple(v.alive), tuple(sorted(v.dead.items())), len(v.votes), v.day,
           tuple(sorted(v.investigations.items())), tuple(v.allies), len(v.messages), tuple(claims.order),
           tuple(sorted((s, tuple(sorted(w))) for s, w in claims.wolves.items())))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    deals = [(ws, s) for ws, s in DEALS if _consistent(v, ws, s, private)]
    if not deals and private:
        deals = [(ws, s) for ws, s in DEALS if _consistent(v, ws, s, False)]
    if not deals:
        return None
    ev = Evidence(v, claims, accusations)
    alive_now = set(v.alive)
    lls = [_loglik(ev, ws, s, alive_now) for ws, s in deals]
    mx = max(lls)
    w = [math.exp(x - mx) for x in lls]
    z = sum(w) or 1.0
    pw, ps = [0.0] * N, [0.0] * N
    for (ws, s), x in zip(deals, w):
        for y in ws:
            pw[y] += x / z
        ps[s] += x / z
    out = (pw, ps)
    if len(_CACHE) > 512:
        _CACHE.clear()
    _CACHE[key] = out
    return out
