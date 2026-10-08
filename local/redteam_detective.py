"""Red-team Werewolf background `detective`: a strong, coordinated village that hunts wolves from the public record.

Written as an outsider's challenger (it reads only the public rules and what any seat sees at the table). Every seat
plays honestly for its own side and treats every other seat alike: no handshakes, no knowledge of which seat is which
contestant, legal actions only, chats within the platform caps (<= 50 words, broadcast or one recipient).

The core is an exact Bayesian detective over the 105 possible deals (2 wolves, 1 seer among 7 seats) consistent with
the seat's private knowledge and the revealed roles of the dead. Each deal is weighted by how likely the observed
behaviour is under it:

* seer claims: a true seer reports true results and usually claims proactively; a wolf usually claims only after a
  claimant named it (a reactive counter-claim); plain villagers rarely claim; a living silent seer while someone else
  claims is unlikely;
* day votes: wolves rarely vote (or name in chat) their partner and like voting a claimant who named a wolf; villagers
  lean slightly toward wolves;
* night kills: wolves kill claimed seers and players who voted for or named a wolf the day before;
* the day-1 "no information" convention: a speaker proposing the lowest other seat is following the convention; one
  who skips exactly the lowest seat (and only it) leaks that seat as a partner.

Chat consistency (iteration 2, a player model of a scripted style seen at the table -- "No hard info yet ...",
"My read from the vote record ...", "Following the Seer ...", "Two Seer claims ..."; applied only to speakers who use
it):

* PROFILE: a pick said to come from the vote record (or from having no information) is checked against the record's
  top suspect (lowest seat on ties, seer claimants and the seats they cleared excluded); a different pick means the
  speaker skipped that suspect -- a wolf shielding its partner; the matching pick clears that pair;
* FAB: citing "the vote record" on day 1, before any vote exists, is fabricated reasoning (wolf side);
* FOLLOW / VOTE_FOLLOW: after a lone seer claim naming a wolf, villagers of that style follow the claimant's call (and
  vote the named seat); its wolves never do.

Village play: vote the most likely wolf, with announced votes of trusted seats as a tie-breaker so the village never
splits (day 1 without information: the lowest seat, the shared Schelling point). The seer checks the most likely wolf
it has not checked (night 1: not the day-1 Schelling target, which the vote reveals anyway) and claims when it found a
living wolf the table is not already voting out, when someone else claims seer, when the vote heads for itself or a
seat it cleared, on a do-or-die day (a wrong elimination gives the wolves parity), or in the endgame.
Wolf play: blend with the village vote (bus a doomed partner rather than defend it), kill a claimed seer, else the
likeliest seer / the loudest accuser of a wolf, and tell the partner the target by private message.

Feature switches are class attributes so iterations can be compared side by side on the same deals
(REDTEAM_DETECTIVE="FLAG=value,..." overrides them per run; see `VARIANTS` and local/eval_werewolf.py backgrounds
`detective`, `det_v1`, `det_noskip`).

Iteration log (agent = our deployed player, k = its seats; paired games on identical deals, 1000 per k; "rounds" =
the simulator's default discussion timing on seed 2300, "rt" = the official runner's timing on seed 2301; agent-wolf
win % and agent-wolf survival % -- baselines on seed 2300: llmq k1 93.8 / k3 58.6, heuristic k1 39.6 / k3 44.4):
  v1 Bayesian village over all deals:           rounds k1 20.8 (surv 4.7) k3 23.0 (11.7); rt k1 22.5 (8.9) k3 25.1
  v2 chat consistency (PROFILE, FAB, FOLLOW):     rounds k1 12.4 (0.4)  k3 16.1 (3.9)
  v3 day-1 "record" picks read only as FAB:       same for the agent wolf; agent villager +6 pp at k1
  v4 quiet seer, VOTE_FOLLOW, CRITICAL_CLAIM:     rounds k1 11.3 (0.4)  k3 14.7 (3.9);  rt k1 18.2 (6.4) k3 17.4 (8.4)
  rejected (worse or no effect): TIE_EPS, RNG_TIES, SUPPORT_W 0.03/0.15, LEAD_P, FAB_NOHARD, BUS_STYLED,
  SCHELLING_RECORD, FOCAL_EPS, LEADER_*, KILL_STYLED, KILL_CLAIMED=20, CRIT_FOCAL.
  PERSUADE_P=0.9 (the seer tells a deduced wolf as a result when claim-followers vote elsewhere): k3 agent-wolf
  survival halves (rounds 3.9 -> 1.8, rt 8.4 -> 4.4) but the exposed seer dies, so the wolf side's win rate does not
  move (k3 -1.0 / +0.7) and k1 rounds gets worse (+2.2); left off.
  Fresh seed 2302 with local/eval_werewolf.py: agent wolf k1 8.1 / k3 20.0 (rounds), 13.8 / 18.5 (rt).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from itertools import combinations

from .werewolf_env import ABSTAIN, SIM_HOLD, Seat
from .werewolf_bots import _legal, _private

N = 7
_SEAT = r"(?:player|p|seat)\s*#?\s*([0-6])\b"
_SEAT_RX = re.compile(r"\b" + _SEAT, re.I)
_CLAIM = re.compile(r"\bi\s*(?:am|'m|’m|m)\s+(?:the\s+)?(?:real\s+|true\s+|actual\s+)?seer\b|\bseer\s+here\b|"
                    r"\bas\s+the\s+seer\b", re.I)
_NOT_CLAIM = re.compile(r"what if i am the seer|kidding|\bnot\s+(?:the\s+)?seer\b|seer claim: none", re.I)
_VILL_WORD = re.compile(r"\b(?:not\s+a\s+(?:wolf|werewolf)|clear|clean|cleared|villager|innocent|town)\b", re.I)
_WOLF_WORD = re.compile(r"\b(?:wolf|werewolf|wolves)\b", re.I)
_VOTE = re.compile(r"\bvot(?:e|ing)\s+(?:for\s+|out\s+|to\s+eliminate\s+)?" + _SEAT, re.I)
_NEG_VOTE = re.compile(r"(?:not|n't|never)\s+vote\s*$", re.I)
_RULE_NOHARD = re.compile(r"^\s*no hard info yet", re.I)
_RULE_RECORD = re.compile(r"^\s*my read from the vote record", re.I)
# a scripted chat style seen at the table (the player-model features below are calibrated on it and apply only to
# speakers who use it)
_STYLE = re.compile(r"^\s*(?:no hard info yet|my read from the vote record|following the seer|two seer claims)|"
                    r"lying about being seer", re.I)
_SUS = re.compile(r"\b" + _SEAT + r"\s+(?:is|seems|looks)\s+(?:very\s+|most\s+|really\s+|the\s+most\s+)?"
                  r"(?:sus\b|suspicious|shady)", re.I)


def _record(raw: dict):
    """(me, alive, dead [(day, seat, role, cause)], votes [(day, {voter: target|None}, eliminated)], day, phase)."""
    seat_of = {p["name"]: p["position"] for p in raw.get("players") or []}
    gs = raw.get("game_state") or {}
    alive = sorted(seat_of[n] for n in gs.get("alive") or [] if n in seat_of)
    dead = []
    for d in gs.get("dead") or []:
        s = seat_of.get(d.get("player"))
        if s is not None:
            dead.append((d.get("day") or 0, s, d.get("role"), "vote" if d.get("cause") in ("vote", "lynch") else "night_kill"))
    votes = []
    for vh in gs.get("vote_history") or []:
        ballots = {seat_of[a]: (seat_of.get(b) if b != "abstain" else None)
                   for a, b in (vh.get("votes") or {}).items() if a in seat_of}
        votes.append((vh.get("day") or 0, ballots, seat_of.get(vh.get("eliminated"))))
    return raw.get("your_position"), alive, dead, votes, gs.get("day") or 1, gs.get("phase")


def _results(text: str, speaker) -> dict[int, str]:
    """Seer results stated in a claim message: seat -> "wolf" | "villager" (first seat of each clause)."""
    out = {}
    for clause in re.split(r"[.!?;:\n]+", text):
        seats = [int(x) for x in _SEAT_RX.findall(clause)]
        seats = [x for x in seats if x != speaker]
        if not seats:
            continue
        if _VILL_WORD.search(clause):
            out.setdefault(seats[0], "villager")
        elif _WOLF_WORD.search(clause):
            out.setdefault(seats[0], "wolf")
    return out


def _intents(text: str, speaker) -> list[int]:
    out = []
    for m in _VOTE.finditer(text):
        if _NEG_VOTE.search(text[:m.start() + 4]):
            continue
        t = int(m.group(1))
        if t != speaker:
            out.append(t)
    return out


class Facts:
    """Everything the detective reads off one state: the record plus the parsed broadcast chat."""

    def __init__(self, seat: "DetectiveSeat", raw: dict):
        self.me, self.alive, self.dead, self.votes, self.day, self.sub = _record(raw)
        self.dead_roles = {s: r for _, s, r, _ in self.dead}
        self.role, self.allies, self.inv = _private(raw)
        # kills with the living set at that night
        self.kills = []
        gone = set()
        for d, s, r, cause in self.dead:
            if cause == "night_kill":
                self.kills.append((d, s, [x for x in range(N) if x not in gone]))
            gone.add(s)
        # chat
        self.claims: dict[int, dict] = {}
        self.intents: dict[int, dict[int, int]] = defaultdict(dict)     # day -> speaker -> latest vote target
        self.accus: set[tuple[int, int, int]] = set()                   # (day, speaker, target)
        self.skips: list[tuple[int, int]] = []                          # (speaker, skipped seat)
        self.dms: list[tuple[int, int, str]] = []                       # (seq, sender, text) private messages to me
        self.rule_checks: dict[tuple, bool] = {}     # (speaker, day, natural seat) -> its stated pick == natural
        self.d1_forms: dict[int, str] = {}           # speaker -> "record" (any day-1 pick citing the record) | "nohard"
        self.follows: dict[tuple, tuple] = {}        # (speaker, claimant, day) -> (followed?, claimant's target)
        claim_target: dict[int, int] = {}            # claimant -> the seat its claim asks to vote
        first_claim_seq = None
        d1_first = {}
        self.styled = {m.get("sender") for m in seat.msgs.values()
                       if not m.get("recipients") and _STYLE.search(str(m.get("content") or ""))}
        for seq in sorted(seat.msgs):
            m = seat.msgs[seq]
            if m.get("type", "chat") != "chat":
                continue
            s, text, d = m.get("sender"), str(m.get("content") or ""), seat.mday.get(seq, 1)
            if m.get("recipients"):
                if self.me in (m.get("recipients") or []):
                    self.dms.append((seq, s, text))
                continue
            is_claim = bool(_CLAIM.search(text) and not _NOT_CLAIM.search(text))
            ints = _intents(text, s)
            if is_claim:
                res = _results(text, s)
                c = self.claims.get(s)
                if c is None:
                    reactive = any(p["results"].get(s) == "wolf" for p in self.claims.values())
                    c = self.claims[s] = {"seq": seq, "day": d, "results": {}, "reactive": reactive}
                    if first_claim_seq is None:
                        first_claim_seq = seq
                for t, r in res.items():
                    c["results"].setdefault(t, r)
                named = [t for t, r in res.items() if r == "wolf" and t in self.alive_at(d)]
                if ints or named:
                    claim_target[s] = named[0] if named else ints[-1]
            elif ints:
                rule = "nohard" if _RULE_NOHARD.search(text) else "record" if _RULE_RECORD.search(text) else None
                if rule and seat.PROFILE and (rule == "nohard" or d > 1 or not seat.PROF_D2_RECORD):
                    # day 1 "vote record" picks follow chat pressure, not a record: only FAB reads them
                    a = self._natural(s, d)
                    if a is not None:
                        self.rule_checks.setdefault((s, d, a), ints[0] == a)
                if rule and d == 1 and self.d1_forms.get(s) != "record":
                    self.d1_forms[s] = rule                    # "record" once any day-1 pick cites the record
                if seat.FOLLOW and s not in self.claims and s in self.styled:
                    for c, tgt in claim_target.items():
                        if c != s and c in self.alive_at(d):
                            self.follows.setdefault((s, c, d), (ints[-1] == tgt, tgt))
            for t in ints:
                self.intents[d][s] = t
                self.accus.add((d, s, t))
                if d == 1 and s not in d1_first:
                    d1_first[s] = (seq, t)
            for x in _SUS.findall(text):
                if int(x) != s:
                    self.accus.add((d, s, int(x)))
        if seat.USE_SKIP:
            for s, (seq, t) in d1_first.items():
                if first_claim_seq is not None and first_claim_seq < seq:
                    continue                                  # a claim was on the table: not a no-information pick
                others = [x for x in range(N) if x != s]
                low = others[0]
                if t != low and t == min(x for x in others if x != low):
                    self.skips.append((s, low))
        # claimants who named a wolf, by the day of their claim
        self.claim_day = {c: v["day"] for c, v in self.claims.items()}
        self.claim_target = claim_target
        # day -> (claimant, seat) when exactly one living seer claimant had named exactly one living wolf by that vote
        self.lone_named: dict[int, tuple[int, int]] = {}
        for d, ballots, _ in self.votes:
            gone = self._gone_at(d)
            named = {}
            for c, info in self.claims.items():
                if info["day"] <= d and c not in gone and gone.get(c) is None:
                    ts = [t for t, r in info["results"].items() if r == "wolf" and t in ballots]
                    if ts:
                        named[c] = ts
            live_claimants = [c for c, info in self.claims.items() if info["day"] <= d and c in ballots]
            if len(named) == 1 and len(live_claimants) == 1:
                (c, ts), = named.items()
                if len(ts) == 1:
                    self.lone_named[d] = (c, ts[0])

    def _gone_at(self, d: int) -> dict[int, str]:
        """Seats dead (with their revealed role) when day d's discussion runs."""
        return {x: r for dd, x, r, cause in self.dead if dd < d or (dd == d and cause == "night_kill")}

    def alive_at(self, d: int) -> list[int]:
        gone = self._gone_at(d)
        return [x for x in range(N) if x not in gone]

    def _natural(self, s: int, d: int) -> int | None:
        """The seat a speaker who says it goes by the vote record (or has no information) should name on day d: the
        top vote-record suspect among living others who are not seer claimants nor cleared by one, lowest seat on
        ties. Suspicion: +1 for a vote on an eliminated non-wolf, -1 on an eliminated wolf, +0.5 for abstaining."""
        gone = self._gone_at(d)
        alive = [x for x in range(N) if x not in gone and x != s]
        excl = set()
        for c, info in self.claims.items():
            if gone.get(c) not in (None, "seer"):
                continue                                       # a claimant revealed as a liar
            excl.add(c)
            excl |= {t for t, r in info["results"].items() if r == "villager"}
        cands = [x for x in alive if x not in excl] or alive
        if not cands:
            return None
        sc = defaultdict(float)
        for vd, ballots, el in self.votes:
            if vd >= d:
                continue
            er = gone.get(el) if el is not None else None
            for v, tg in ballots.items():
                if tg is None:
                    sc[v] += 0.5
                elif tg == el:
                    sc[v] += 1.0 if er != "wolf" else -1.0
        return max(cands, key=lambda x: (sc[x], -x))


class DetectiveSeat(Seat):
    name = "detective"
    # claim model
    P_WOLF_PROACTIVE = 0.1
    P_SEER_REACTIVE = 0.5
    P_VILL_CLAIM = 0.02
    SEER_SILENT = 0.3
    LIE = 1e-4
    BUS_CLAIM = 0.05
    # vote / chat model
    BUS = 0.2
    BUS_STYLED = 0.2            # the same for speakers of the scripted style (iteration 4 tries lower)
    QUIET_SEER = True           # iteration 4: a seer that found a wolf the table already targets stays hidden
    QUIET_VOTES = True          # ... only once enough other seats announced a vote for it (else claims on its 2nd turn)
    CRITICAL_CLAIM = True       # iteration 6: the seer claims its clears when a wrong elimination today gives parity
    PERSUADE_P = 1.0            # iteration 7: < 1: the seer tells a deduced wolf (P >= this) as a result to move the vote
    PERSUADE_ALWAYS = False     # ... even when the table already goes for it
    RHO = 1.3
    BUS_CHAT = 0.4
    RHO_CHAT = 1.15
    ABST_W = 0.3
    ABST_V = 0.15
    VOTE_CLAIMANT_W = 2.0
    VOTE_SEER_V = 0.3
    # kill model
    KILL_CLAIMED = 4.0
    KILL_ACCUSER = 2.0
    KILL_STYLED = 1.0           # iteration 6: extra weight on killing a seat that voted for a wolf of the scripted style
    # day-1 convention leak (iteration 1; off from iteration 3: PROFILE reads it for the scripted style, and on free
    # text it misreads picks that follow table talk)
    USE_SKIP = False
    SKIP_K = 6.0
    # chat consistency (iteration 2): stated vote-record / no-information picks checked against the record
    PROFILE = True
    PROF_D2_RECORD = True       # iteration 3: check "vote record" picks only from day 2 (day 1 has no record)
    PROF_ALLY_MATCH = 0.03      # P(pick == top suspect | speaker wolf, top suspect its partner)
    PROF_W_DEV = 0.04           # P(pick != top suspect | speaker wolf, top suspect not its partner)
    PROF_V_DEV = 0.003          # P(pick != top suspect | speaker villager)
    FAB = True
    FAB_W = 0.5                # P(first day-1 rule phrase cites the (empty) vote record | wolf)
    FAB_V = 0.003
    FAB_NOHARD = False          # also count "no hard info" on day 1 as (weak) evidence against the wolf side
    VOTE_FOLLOW = True          # iteration 6: votes of the scripted style after a lone seer claim naming a wolf
    VF_V_TRUE = 0.97            # P(villager votes the named seat | the claim is true)   (measured 772/782)
    VF_W_TRUE = 0.02            # P(wolf votes its named partner | the claim is true)    (measured 0/160)
    VF_V_FAKE = 0.9
    VF_W_FAKE = 0.5
    FOLLOW = True
    FOL_V_TRUE = 0.85           # P(villager's next pick follows a true seer claim)
    FOL_W_TRUE_ALLY = 0.05      # ... a wolf, when the true claim names it or its partner
    FOL_W_TRUE_OTHER = 0.2      # ... a wolf, when the true claim only asks to vote a villager
    FOL_V_FAKE = 0.6
    FOL_W_FAKE_PARTNER = 0.7
    FOL_W_FAKE = 0.5
    # decisions
    SUPPORT_W = 0.08
    SCHELLING = 0.1
    TIE_EPS = 0.0          # > 0: near-ties of P(wolf) settled by announced votes, then lowest seat
    LEAD_P = 1.0           # < 1: villagers vote a seer claimant's call when P(claimant is seer) >= LEAD_P
    SCHELLING_RECORD = False  # iteration 5 (rejected): the focal seat on every day is the vote record's top suspect
    FOCAL_EPS = 0.0        # iteration 5 (rejected): > 0: P(wolf) near-ties -> lowest seat, keep today's announced seat
    CRIT_FOCAL = 0.0       # iteration 6: > 0: on do-or-die days, P(wolf) near-ties (within this) -> lowest seat
    LEADER_P = 0.0         # iteration 5 (rejected): announcements of seats with P(wolf) < LEADER_P weigh more
    LEADER_MULT = 1.0
    RNG_TIES = False       # seer checks and wolf kills break ties at random instead of lowest seat
    MAX_MSGS = 3
    HOLD_ASKS = 3
    RAISE = False          # tests: re-raise internal errors instead of falling back
    errors = 0             # internal errors caught (class-wide counter, per process)

    def __init__(self):
        # REDTEAM_DETECTIVE="USE_SKIP=0,SKIP_K=3": per-run overrides, so variants play the same deals (same --bg name)
        import os
        for item in filter(None, os.environ.get("REDTEAM_DETECTIVE", "").split(",")):
            k, _, v = item.partition("=")
            k = k.strip()
            if hasattr(type(self), k):
                cur = getattr(type(self), k)
                setattr(self, k, (v.strip() not in ("0", "false", "off")) if isinstance(cur, bool) else type(cur)(v))

    def reset(self):
        self.msgs: dict[int, dict] = {}
        self.mday: dict[int, int] = {}
        self.asks: dict[int, int] = defaultdict(int)
        self.sent: dict[int, int] = defaultdict(int)
        self.said_vote: dict[int, int | None] = {}
        self.claimed = False
        self.persuaded: set[int] = set()
        self.dm_sent: set[int] = set()
        self._cache = {}

    # ------------------------------------------------------------------ reading
    def _read(self, raw: dict) -> None:
        day = (raw.get("game_state") or {}).get("day") or 1
        for m in raw.get("new_messages") or []:
            if isinstance(m, dict) and m.get("seq") is not None and m["seq"] not in self.msgs:
                self.msgs[m["seq"]] = m
                self.mday[m["seq"]] = day

    # ------------------------------------------------------------------ the detective
    def _worlds(self, F: Facts, private: bool):
        out = []
        for w in combinations(range(N), 2):
            ws = frozenset(w)
            for s in range(N):
                if s in ws:
                    continue
                bad = False
                for x, r in F.dead_roles.items():
                    if (r == "wolf") != (x in ws) or (r == "seer") != (x == s):
                        bad = True
                        break
                if bad:
                    continue
                if private:
                    if F.role == "wolf":
                        if ws != frozenset([F.me] + list(F.allies)):
                            continue
                    elif F.role == "seer":
                        if s != F.me or any((t in ws) != (r == "wolf") for t, r in F.inv.items()):
                            continue
                    elif F.me in ws or F.me == s:
                        continue
                out.append((ws, s))
        return out

    def _vpref(self, u, wolf_v: bool, ws, s, d: int, F: Facts, styled: bool = False) -> float:
        if u is None:
            return self.ABST_W if wolf_v else self.ABST_V
        p = 1.0
        c = F.claims.get(u)
        live_claim = c is not None and c["day"] <= d
        if wolf_v:
            if u in ws:
                p *= self.BUS_STYLED if styled else self.BUS
            if live_claim and any(r == "wolf" and t in ws for t, r in c["results"].items()):
                p *= self.VOTE_CLAIMANT_W
        else:
            if u in ws:
                p *= self.RHO
            if live_claim and u == s:
                p *= self.VOTE_SEER_V
        return p

    def _accused_wolf(self, y: int, d: int, ws, F: Facts) -> bool:
        for vd, ballots, _ in F.votes:
            if vd == d and ballots.get(y) in ws:
                return True
        if any(dd == d and sp == y and t in ws for dd, sp, t in F.accus):
            return True
        c = F.claims.get(y)
        return bool(c and c["day"] <= d and any(r == "wolf" and t in ws for t, r in c["results"].items()))

    def _loglik(self, ws, s, F: Facts) -> float:
        L = 0.0
        log = math.log
        for c, info in F.claims.items():
            if c == s:
                if info["reactive"]:
                    L += log(self.P_SEER_REACTIVE)
                for t, r in info["results"].items():
                    if (r == "wolf") != (t in ws):
                        L += log(self.LIE)
            elif c in ws:
                if not info["reactive"]:
                    L += log(self.P_WOLF_PROACTIVE)
                for t, r in info["results"].items():
                    if r == "wolf" and t in ws:
                        L += log(self.BUS_CLAIM)
            else:
                L += log(self.P_VILL_CLAIM)
        if F.claims and s not in F.claims and s in F.alive:
            L += log(self.SEER_SILENT)
        for d, ballots, _ in F.votes:
            voters = list(ballots)
            for v, t in ballots.items():
                wolf_v = v in ws
                styled = v in F.styled
                tot, mine = 0.0, 1.0
                for u in voters + [None]:
                    if u == v:
                        continue
                    p = self._vpref(u, wolf_v, ws, s, d, F, styled)
                    tot += p
                    if u == t:
                        mine = p
                L += log(mine / tot)
        for n, x, before in F.kills:
            prefs = {}
            for y in before:
                if y in ws:
                    continue
                p = 1.0
                if y in F.claims and F.claims[y]["day"] <= n - 1:
                    p *= self.KILL_CLAIMED
                if self._accused_wolf(y, n - 1, ws, F):
                    p *= self.KILL_ACCUSER
                if self.KILL_STYLED != 1.0:
                    # a wolf of the scripted style kills a seat that voted for it the day before
                    for vd, ballots, _ in F.votes:
                        if vd == n - 1 and ballots.get(y) in ws and ballots.get(y) in F.styled:
                            p *= self.KILL_STYLED
                prefs[y] = p
            if x in prefs:
                L += log(prefs[x] / sum(prefs.values()))
        for d, sp, t in F.accus:
            if sp in ws:
                if t in ws:
                    L += log(self.BUS_CHAT)
            elif t in ws:
                L += log(self.RHO_CHAT)
        for sp, low in F.skips:
            if sp in ws and low in ws:
                L += log(self.SKIP_K)
        # chat consistency: a pick said to follow the vote record (or no information) that is not the record's top
        # suspect skipped that suspect -- a wolf protecting its partner; a pick that is the top suspect clears the pair
        for (sp, d, a), matched in F.rule_checks.items():
            if sp in ws:
                if a in ws:
                    L += log(self.PROF_ALLY_MATCH if matched else 1.0 - self.PROF_ALLY_MATCH)
                else:
                    L += log(1.0 - self.PROF_W_DEV if matched else self.PROF_W_DEV)
            else:
                L += log(1.0 - self.PROF_V_DEV if matched else self.PROF_V_DEV)
        # "my read from the vote record" before any vote was cast: reasoning that cites evidence which does not exist
        if self.FAB:
            for sp, form in F.d1_forms.items():
                if form == "record":
                    L += log(self.FAB_W if sp in ws else self.FAB_V)
                elif self.FAB_NOHARD:
                    L += log(1.0 - self.FAB_W if sp in ws else 1.0 - self.FAB_V)
        # votes after a lone seer claim naming a wolf: villagers of the scripted style vote the named seat, its wolves
        # never vote their named partner
        if self.VOTE_FOLLOW:
            for d, ballots, _ in F.votes:
                named = F.lone_named.get(d)
                if named is None:
                    continue
                c, t = named
                for v, b in ballots.items():
                    if v in (c, t) or v not in F.styled or t not in ballots:
                        continue
                    if c == s:
                        p = self.VF_W_TRUE if v in ws else self.VF_V_TRUE
                    else:
                        p = self.VF_W_FAKE if v in ws else self.VF_V_FAKE
                    L += log(p if b == t else 1.0 - p)
        # reaction to a seer claim: villagers follow a true claim; wolves ignore it (it names them or their partner)
        for (sp, c, d), (followed, tgt) in F.follows.items():
            if c == s:
                if sp in ws:
                    p = self.FOL_W_TRUE_ALLY if tgt in ws else self.FOL_W_TRUE_OTHER
                else:
                    p = self.FOL_V_TRUE
            else:
                if sp in ws:
                    p = self.FOL_W_FAKE_PARTNER if c in ws else self.FOL_W_FAKE
                else:
                    p = self.FOL_V_FAKE
            L += log(p if followed else 1.0 - p)
        return L

    def posterior(self, F: Facts, private: bool = True):
        """(P(wolf) per seat, P(seer) per seat) over the deals consistent with what this seat knows."""
        key = (private, len(self.msgs), len(F.dead), len(F.votes), F.day, len(F.inv))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        worlds = self._worlds(F, private)
        if not worlds:                                     # contradiction (a misread claim): drop private info
            worlds = self._worlds(F, False)
        lls = [self._loglik(ws, s, F) for ws, s in worlds]
        mx = max(lls)
        ws_ = [math.exp(x - mx) for x in lls]
        z = sum(ws_)
        pw, ps = [0.0] * N, [0.0] * N
        for (ws, s), w in zip(worlds, ws_):
            for x in ws:
                pw[x] += w / z
            ps[s] += w / z
        out = (pw, ps)
        self._cache[key] = out
        return out

    # ------------------------------------------------------------------ shared decisions
    def _support(self, F: Facts, pw) -> dict[int, float]:
        sup = defaultdict(float)
        for sp, t in F.intents.get(F.day, {}).items():
            if sp != F.me and sp in F.alive and t in F.alive:
                w = max(0.0, 1.0 - pw[sp])
                if pw[sp] < self.LEADER_P:
                    w *= self.LEADER_MULT          # a (near-)confirmed villager leads the table
                sup[t] += w
        return sup

    def _village_target(self, F: Facts) -> int | None:
        pw, ps = self.posterior(F)
        cands = [t for t in F.alive if t != F.me]
        if F.role == "seer":
            known = [t for t in cands if F.inv.get(t) == "wolf"]
            if known:
                return min(known)
            cands = [t for t in cands if F.inv.get(t) != "villager"] or cands
        if not cands:
            return None
        if self.LEAD_P < 1.0 and F.role != "seer":
            # follow a credible seer claimant's call (its named wolf, else the seat it votes): the village moves as one
            lead = [c for c in F.claims if c in F.alive and c != F.me and ps[c] >= self.LEAD_P
                    and F.claim_target.get(c) in cands]
            if lead:
                t = F.claim_target[max(lead, key=lambda c: (ps[c], -c))]
                if pw[t] >= 0.5 * max(pw[x] for x in cands):
                    return t
        sup = self._support(F, pw)
        low = min(cands)
        bonus = self.SCHELLING if F.day == 1 and not F.claims else 0.0
        if self.SCHELLING_RECORD:
            # every day: the table's shared focal seat is the vote record's top suspect (lowest seat on ties, seer
            # claimants and the seats they cleared left out) -- the pick a rule-following villager makes
            nat = F._natural(F.me, F.day)
            low, bonus = (nat, self.SCHELLING) if nat in cands else (low, bonus)
        base = {t: pw[t] + (bonus if t == low else 0.0) for t in cands}
        if self.CRIT_FOCAL > 0 and F.role != "seer":
            wolves = 2 - sum(1 for r in F.dead_roles.values() if r == "wolf")
            if len(F.alive) - 1 <= 2 * wolves:
                # a do-or-die day: near-ties go to the lowest seat, a focal point the wolves' chat cannot move
                m = max(base.values())
                tied = [t for t in cands if base[t] >= m - self.CRIT_FOCAL]
                return min(tied)
        if self.FOCAL_EPS > 0:
            # near-ties go to the lowest seat (a focal point nobody's chat can move), and a seat already announced
            # today stays the pick while it is still near the top: the village does not split over noise
            m = max(base.values())
            tied = [t for t in cands if base[t] >= m - self.FOCAL_EPS]
            mine = self.said_vote.get(F.day)
            return mine if mine in tied else min(tied)
        if self.TIE_EPS > 0:
            # near-ties are settled by the table's announced votes, then by the lowest seat: the village never splits
            # over noise
            m = max(base.values())
            tied = [t for t in cands if base[t] >= m - self.TIE_EPS]
            return max(tied, key=lambda t: (round(sup.get(t, 0.0), 3), round(base[t], 6), -t))

        def score(t):
            return round(base[t] + self.SUPPORT_W * sup.get(t, 0.0), 6)
        return max(cands, key=lambda t: (score(t), -t))

    def _wolf_target(self, F: Facts) -> int | None:
        """A wolf's day vote: day 1 without claims the village's Schelling seat (lowest other seat, even a partner);
        else the village's consensus if it is not a wolf (bus a doomed partner), else the village's likeliest
        non-wolf suspect; accused itself, the accusing claimant."""
        cands = [t for t in F.alive if t != F.me]
        if not cands:
            return None
        if F.day == 1 and not F.claims:
            return min(cands)
        pub, _ = self.posterior(F, private=False)
        wolves = set([F.me] + list(F.allies))
        sup = self._support(F, pub)
        mine = {t: v for t, v in sup.items() if t != F.me}
        if mine:
            top = max(mine, key=lambda t: (mine[t], -t))
            heat_me = sup.get(F.me, 0.0)
            if heat_me > mine[top]:
                accusers = [c for c, v in F.claims.items() if v["results"].get(F.me) == "wolf" and c in cands]
                if accusers:
                    return min(accusers, key=lambda c: F.claims[c]["seq"])
            if top not in wolves:
                return top
            if mine[top] >= 0.4 * (len(F.alive) - 1):          # doomed partner: vote with the table
                return top
        pool = [t for t in cands if t not in wolves] or cands
        return max(pool, key=lambda t: (round(pub[t] + self.SUPPORT_W * sup.get(t, 0.0), 6), -t))

    def _seer_should_claim(self, F: Facts) -> bool:
        if self.claimed:
            return True
        if any(c != F.me and c in F.alive for c in F.claims):
            return True
        found = [t for t, r in F.inv.items() if r == "wolf" and t in F.alive]
        if found:
            pred = self._predicted_lynch(F)
            if self.QUIET_SEER and pred in found:
                if not self.QUIET_VOTES:
                    return False                            # the table already goes for it: stay hidden, check more
                promised = sum(1 for sp, t in F.intents.get(F.day, {}).items()
                               if t == pred and sp != F.me and sp in F.alive)
                if promised >= len(F.alive) // 2 or self.asks[F.day] < 2:
                    return False                            # enough votes already promised (or wait a round)
            return True
        cleared = {t for t, r in F.inv.items() if r == "villager" and t in F.alive}
        predicted = self._predicted_lynch(F)
        if predicted is not None and (predicted == F.me or predicted in cleared):
            return True
        if self.CRITICAL_CLAIM and cleared:
            # a wrong elimination today hands the wolves parity: the village needs every result now
            wolves = 2 - sum(1 for r in F.dead_roles.values() if r == "wolf")
            if len(F.alive) - 1 <= 2 * wolves:
                return True
        return len(F.alive) <= 4 and bool(cleared)

    def _persuade_target(self, F: Facts) -> int | None:
        """A seat this seer is all but sure is a wolf (by deduction, not yet checked) that the table is not voting out:
        told as a seer result, so that claim-following players join the vote."""
        if self.PERSUADE_P >= 1.0:
            return None
        pw, _ = self.posterior(F)
        cands = [t for t in F.alive if t != F.me and t not in F.inv and pw[t] >= self.PERSUADE_P]
        if not cands or any(r == "wolf" and t in F.alive for t, r in F.inv.items()):
            return None
        x = max(cands, key=lambda t: (pw[t], -t))
        if x in self.persuaded or self.PERSUADE_ALWAYS:
            return x
        # announced votes so far: persuade unless x already leads clearly (my own vote included)
        count = defaultdict(int)
        for sp, t in F.intents.get(F.day, {}).items():
            if sp != F.me and sp in F.alive and t in F.alive:
                count[t] += 1
        best_other = max([v for t, v in count.items() if t != x], default=0)
        if self.asks[F.day] < 2 or best_other < 2:
            return None                                      # let the table speak first; act only on real opposition
        return x if count[x] < best_other else None

    def _predicted_lynch(self, F: Facts) -> int | None:
        """The seat a village of detectives without private information would vote now (maybe me)."""
        pub, _ = self.posterior(F, private=False)
        sup = self._support(F, pub)
        low = min(F.alive)
        bonus = self.SCHELLING if F.day == 1 and not F.claims else 0.0
        if self.SCHELLING_RECORD:
            nat = F._natural(-1, F.day)
            low, bonus = (nat, self.SCHELLING) if nat is not None else (low, bonus)

        def score(t):
            return round(pub[t] + self.SUPPORT_W * sup.get(t, 0.0) + (bonus if t == low else 0.0), 6)
        return max(F.alive, key=lambda t: (score(t), -t)) if F.alive else None

    # ------------------------------------------------------------------ actions
    def choose(self, raw: dict) -> str:
        self._read(raw)
        legal = _legal(raw)
        pick = None
        try:
            F = Facts(self, raw)
            if F.sub == "night_wolf":
                pick = self._kill(F, legal)
            elif F.sub == "night_seer":
                pick = self._check(F, legal)
            else:
                pick = self._wolf_target(F) if F.role == "wolf" else self._village_target(F)
        except Exception:                                    # noqa: BLE001 - never lose the seat to a bug
            DetectiveSeat.errors += 1
            if self.RAISE:
                raise
            pick = None
        if pick is not None and str(pick) in legal:
            return str(pick)
        targets = [a for a in legal if a != str(ABSTAIN)]
        return targets[0] if targets else legal[0]

    def _check(self, F: Facts, legal) -> int | None:
        pw, _ = self.posterior(F)
        cands = [int(a) for a in legal if int(a) not in F.inv and int(a) != F.me]
        if F.day == 1:
            schelling = min(x for x in F.alive if x != F.me) if F.me == min(F.alive) else min(F.alive)
            cands = [t for t in cands if t != schelling] or cands
        if not cands:
            return None
        return max(self._order(cands), key=lambda t: round(pw[t], 6))

    def _order(self, seats: list[int]) -> list[int]:
        """Seats in tie-break order for max(): lowest seat first, or shuffled with RNG_TIES (max keeps the first)."""
        seats = sorted(seats)
        if self.RNG_TIES and self.rng is not None:
            self.rng.shuffle(seats)
        return seats

    def _kill(self, F: Facts, legal) -> int | None:
        targets = [int(a) for a in legal]
        claimed = [c for c in F.claims if c in targets]
        if claimed:
            return min(claimed, key=lambda c: F.claims[c]["seq"])
        # a lower-seat partner leads: follow its latest private suggestion when it names a legal target
        for seq, sender, text in sorted(F.dms, reverse=True):
            if sender in F.allies and sender < F.me:
                hit = _SEAT_RX.search(text)
                if hit and int(hit.group(1)) in targets:
                    return int(hit.group(1))
                break
        return self._kill_pick(F, targets)

    def _kill_pick(self, F: Facts, targets) -> int | None:
        if not targets:
            return None
        _, ps = self.posterior(F)
        wolves = set([F.me] + list(F.allies))
        last = F.day - 1 if F.sub == "night_wolf" else F.day

        def threat(y):
            return ps[y] + (0.3 if self._accused_wolf(y, last, wolves, F) else 0.0)
        return max(self._order(targets), key=lambda y: round(threat(y), 6))

    # ------------------------------------------------------------------ chat
    def message(self, raw: dict):
        self._read(raw)
        day = (raw.get("game_state") or {}).get("day") or 1
        self.asks[day] += 1
        if self.sent[day] >= self.MAX_MSGS:
            return None
        try:
            out = self._compose(Facts(self, raw))
        except Exception:                                    # noqa: BLE001
            DetectiveSeat.errors += 1
            if self.RAISE:
                raise
            out = None
        if out is not None:
            self.sent[day] += 1
            return out
        return SIM_HOLD if self.asks[day] < self.HOLD_ASKS else None

    def _compose(self, F: Facts):
        day = F.day
        if F.role == "wolf":
            partner = [a for a in F.allies if a in F.alive]
            if partner and day not in self.dm_sent:                           # tell the partner tonight's target
                self.dm_sent.add(day)
                pool = [t for t in F.alive if t not in F.allies and t != F.me]
                claimed = [c for c in F.claims if c in pool]
                k = min(claimed, key=lambda c: F.claims[c]["seq"]) if claimed else self._kill_pick(F, pool)
                if k is not None:
                    return {"content": f"Kill Player{k} tonight.", "recipients": [partner[0]]}
            t = self._wolf_target(F)
            return self._announce(day, t, F)
        persuade = self._persuade_target(F) if F.role == "seer" else None
        if F.role == "seer" and (persuade is not None or self._seer_should_claim(F)):
            t = persuade if persuade is not None else self._village_target(F)
            if not self.claimed or self.said_vote.get(day) != t:
                self.claimed = True
                self.said_vote[day] = t
                parts = ["I am the Seer."]
                results = dict(F.inv)
                if persuade is not None:
                    self.persuaded.add(persuade)
                for x in self.persuaded:
                    results.setdefault(x, "wolf")              # a deduced wolf told as a result
                for x, r in sorted(results.items()):
                    parts.append(f"Player{x} is {'a wolf' if r == 'wolf' else 'clear'}.")
                if t is not None:
                    parts.append(f"Vote Player{t}.")
                return {"content": " ".join(parts), "recipients": []}
            return None
        return self._announce(day, self._village_target(F), F)

    def _announce(self, day: int, t, F: Facts):
        if t is None or self.said_vote.get(day) == t:
            return None
        first = day not in self.said_vote
        self.said_vote[day] = t
        if day == 1 and not F.claims and first and t == F._natural(F.me, day):
            text = f"No information yet; to avoid a split I take the lowest seat. I vote Player{t}."
        elif day == 1:
            text = f"From the chat so far, Player{t} is the likeliest wolf. I vote Player{t}."
        else:
            text = f"From the votes, kills and claims, Player{t} is the likeliest wolf. I vote Player{t}."
        return {"content": text, "recipients": []}


# ---------------------------------------------------------------- iteration variants (for side-by-side measurement)
class DetectiveV1(DetectiveSeat):
    """Iteration 1: the generic Bayesian village, without the chat-consistency player model."""
    name = "det_v1"
    PROFILE = False
    FAB = False
    FOLLOW = False
    USE_SKIP = True


class DetectiveNoSkip(DetectiveV1):
    """Iteration 1 without the day-1 convention-leak reading."""
    name = "det_noskip"
    USE_SKIP = False


VARIANTS = {"detective": DetectiveSeat, "det_v1": DetectiveV1, "det_noskip": DetectiveNoSkip}
