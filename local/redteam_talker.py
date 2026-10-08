"""Red-team Werewolf seats (`talker`): confident, articulate, LLM-like persuasion in every role.

A context-free challenger, written only from the public rules (docs/platform/werewolf.md, docs/platform/06-messaging.md)
and from watching the deployed agent as a black box in local transcripts. It plugs into local/eval_werewolf.py as the
background `talker` (and the ablations below), so `python -m local.eval_werewolf --bg talker --ks 1,3` measures our
agent against six (or four) of these seats.

Every seat plays honestly for its own side (no sandbagging of the agent's teammates); the persuasion is aimed at the
whole table:
  * wolves fake-claim seer at their first chance on day 1 ("first claim wins" is what a careless reader believes),
    counter-claim when a seer names one of them, clear their partner with fake results, bandwagon onto any proposal
    that hits a villager, attack whoever first names a wolf, reverse the story after a villager flips, and bus a
    partner who cannot be saved;
  * the seer claims a wolf at once, counter-claims a fake seer with its real results, and keeps clears for later;
  * villagers argue from the vote record and the deaths, follow a lone seer claim, lynch one side of a claim war
    (the reveal settles the rest), and coordinate on the plurality target so the vote does not split.
Seats of this family understand each other's words exactly (`_SAID`, as an LLM would understand its peers); other
seats' text is read with the regexes below.

Platform rules kept: legal actions only, at most 50 words per chat, broadcast or one recipient, at most MAX_CHATS of
the 5 allowed chats per window (5 for a baiter), no text imitating platform/system messages.

Iteration log (python -m local.redteam_measure --ks 1,3 --games 1000 --ref; agent = deployed agent/, k=1 per-seat win
rate; ref = a talker seat in the agent's seat on the same deals; seeds 2200-2205, rounds timing unless noted):
  v1 (seed 2200)  base tactics above, the named wolf counter-claims accusing a third seat: agent 52.7% (ref 59.5);
                  agent as wolf 8.6% (ref 32.4): it never buses a doomed partner and votes alongside it, so the vote
                  record exposes it once the partner flips. Same seed: llmq 60.4%, heuristic 53.9%.
  v2 (seed 2201)  + the wolf the seer did NOT name counter-claims and calls the seer a wolf (the agent then lynches the
                  real seer: its day-1 votes on the seer 13% -> 41.5%) + a fake seer accuses a counter-claimer back.
                  The seer's flip then exposes both wolves, so the town wins more: agent 57.8% (ref 59.0).
  v3 (seed 2202)  + villagers' bait (bluff a seer claim on an agent-like seat: it counter-claims at once iff it is a
                  wolf): agent 51.2% (ref 62.0) vs 56.2% without bait; but the bait costs the town too (kamikaze).
  v4 (seed 2203)  partner counter off, accuse-back on, wolves night-kill an agent-like seat first: agent 45.5%
                  (ref 60.7). Same deals: llmq 59.6%, heuristic 54.3%.
  v5 (2204, 2205) bait off by default (talker_bait keeps it), bot fixes: agent 48.8% (ref 58.6), as wolf 9.4%
                  (ref 28.8), as villager 64.0% (74.5% when wolves keep it alive); llmq 61.4%, heuristic 55.8%.
                  ARENA_WW_TIMING=rt (seed 2205): agent 51.2% (ref 62.2), wolf 21.2% (ref 37.1); talker_bait 38.9%;
                  llmq 62.5%, heuristic 55.5%. Agent seats never crashed, played illegally or had a chat rejected.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .werewolf_bots import _legal, _private, _public
from .werewolf_env import ABSTAIN, Seat

_SAID: dict[tuple, dict] = {}    # (sender, text) -> meaning, written by talker seats

_S = r"(?:player|p|seat)\s*#?\s*(\d)"
_CLAIM = re.compile(r"\b(?:i\s*(?:am|'m|m)\s+(?:the\s+)?(?:real\s+|true\s+|actual\s+)?seer\b|seer\s+here\b|"
                    r"as\s+the\s+seer\b)", re.I)
_WOLF = re.compile(_S + r"\s*(?:is|was|=|:|-)\s*(?:a\s+|the\s+)?(?:real\s+)?(?:wolf|werewolf)\b", re.I)
_WOLF2 = re.compile(r"(?:checked|investigated)\s+" + _S + r"\b[^.;]*?\b(?:they(?:'re|\s+are)|is|:|-)\s*"
                    r"(?:a\s+|the\s+)?(?:wolf|werewolf)\b", re.I)
_CLEAR = re.compile(_S + r"\s*(?:is|was|=|:|-)\s*(?:a\s+)?(?:villager|clear|clean|innocent)\b", re.I)
_CLEAR2 = re.compile(r"(?:checked|investigated)\s+" + _S + r"\b[^.;]*?\b(?:they(?:'re|\s+are)|is|:|-)\s*"
                     r"(?:a\s+)?(?:villager|clear|clean|innocent)\b", re.I)
_VOTE = re.compile(r"\bvot\w*\s+(?:for\s+|out\s+|to\s+eliminate\s+)?" + _S, re.I)
_SUS = re.compile(_S + r"\s*(?:is|seems|looks)\s+(?:very\s+|most\s+|really\s+|so\s+)?(?:sus|suspicious|shady|lying|"
                  r"a\s+liar)", re.I)
# the deployed agent's own phrasing (seen in transcripts): a fingerprint, public information
_AGENTISH = re.compile(r"^(?:No hard info yet\.|My read from the vote record:|Following the Seer \(Player\d\)|"
                       r"Two Seer claims:|I am the Seer\. Player\d is (?:a WOLF|clear|lying))")
_DEFAULT = re.compile(r"^No hard info yet\. To avoid a split vote I'll vote Player(\d)")


def meaning(text: str, sender) -> dict:
    """What a careful reader takes from `text`: claims seer, seats called wolf / clear, vote intentions, accusations."""
    hit = _SAID.get((sender, text))
    if hit is not None:
        return hit
    seats = lambda rx: {int(x) for x in rx.findall(text)} - {sender}          # noqa: E731
    wolves = seats(_WOLF) | seats(_WOLF2)
    clears = (seats(_CLEAR) | seats(_CLEAR2)) - wolves
    return {"claim": bool(_CLAIM.search(text)), "wolves": wolves, "clears": clears,
            "votes": seats(_VOTE), "sus": seats(_SUS) | wolves}


def _p(i) -> str:
    return f"Player{i}"


class TalkerSeat(Seat):
    name = "talker"
    # tactic switches (the ablation subclasses below turn them off one at a time)
    FAKE_CLAIM = True      # a wolf fake-claims seer on day 1 when no seer claim has accused us
    PREEMPT = True         # ... at its very first chat (else only after the table has started talking)
    COUNTER = True         # a wolf counter-claims when a seer names one of us
    PARTNER_COUNTER = False  # ... the wolf NOT named does it and calls the seer a wolf (v2/v3; it fools a reader that
    #                          lynches "the claimer called a wolf by the other claimer", but the seer's flip then exposes
    #                          both wolves, so v4 turns it off); else the named one, v1 style
    COUNTER_THIRD = True   # (v1 style) the counter-claim accuses a third seat (else the claimer)
    ACCUSE_BACK = True     # a fake seer calls a counter-claiming seer a wolf back (v2)
    FAKE_CLEAR = True      # the fake seer "clears" its partner
    BANDWAGON = True       # wolves pile onto any proposal that hits a villager
    ATTACK_FIRST = True    # wolves attack whoever first names a wolf
    BUS = True             # a wolf votes its partner when the partner's lynch is certain
    TELLS = True           # villagers read the agent's day-1 default-vote tell (a wolf skips its ally)
    AVOID_AGENT = True     # a fake seer does not accuse an agent-like seat (it votes the accuser, seen in probes)
    BAIT = False           # a villager bluffs a seer claim on an agent-like seat to see whether it counter-claims (v3;
    #                        off in the main config: it costs the town more than it gains, see TalkerBait)
    BAIT_MAX_DAY = 1
    AGENT_KILL_BIAS = 1.5  # night-kill preference for an agent-like seat (v4; v1-v3 kept it alive: -0.75)
    MAX_CHATS = 3

    def reset(self):
        self.msgs: dict[int, dict] = {}
        self.mday: dict[int, int] = {}
        self.sent: dict = defaultdict(int)
        self.said: set = set()
        self.faking = False
        self.fake: dict[int, str] = {}       # fake "investigations" of a fake seer
        self.claimed = False
        self.announced: set = set()
        self.day = 1
        self._deaths: list = []
        self._bit: set = set()
        self.bait: dict | None = None

    # ------------------------------------------------------------------ reading
    def _read(self, raw):
        self.day = (raw.get("game_state") or {}).get("day") or self.day
        for m in raw.get("new_messages") or []:
            if isinstance(m, dict) and m.get("seq") is not None and m["seq"] not in self.msgs:
                self.msgs[m["seq"]] = m
                self.mday[m["seq"]] = self.day

    def _chat(self, day=None):
        """Broadcast chats (seq order), optionally of one day: [(seq, sender, text, meaning)]."""
        out = []
        for seq in sorted(self.msgs):
            m = self.msgs[seq]
            if m.get("type", "chat") != "chat" or m.get("recipients"):
                continue
            if day is not None and self.mday.get(seq) != day:
                continue
            text = str(m.get("content") or "")
            out.append((seq, m.get("sender"), text, meaning(text, m.get("sender"))))
        return out

    def _dms_from(self, allies):
        return [(seq, str(self.msgs[seq].get("content") or "")) for seq in sorted(self.msgs)
                if self.msgs[seq].get("recipients") and self.msgs[seq].get("sender") in allies]

    def _agentish(self) -> set:
        return {s for _, s, t, _ in self._chat() if _AGENTISH.search(t)}

    # ------------------------------------------------------------------ public analysis
    def _ctx(self, raw):
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        return me, alive, dead, votes, role, allies, inv

    def _claims(self, alive, dead):
        """claimer -> {order, day, wolves, clears, counter (claimed after an earlier claimer named it)}."""
        claims: dict[int, dict] = {}
        named_by_claimer: set = set()
        self._bit: set = set()
        for seq, s, text, mean in self._chat():
            if mean.get("retract"):
                gone = claims.pop(s, None)                   # a confessed bluff: no longer a claimer
                self._bit |= set(mean.get("bit") or ())
                if gone is not None:                         # "s is a wolf faking my role" was about the bluff
                    for c in claims.values():
                        if s in c["wolves"] and c["named_at"].get(s, -1) > gone["order"]:
                            c["wolves"].discard(s)
                            c["wlist"] = [t for t in c["wlist"] if t != s]
                continue
            if mean["claim"] and s not in claims:
                claims[s] = {"order": seq, "day": self.mday.get(seq, 1), "wolves": set(), "clears": set(),
                             "counter": s in named_by_claimer, "wlist": [], "named_at": {}}
            if s in claims:
                for t in mean["wolves"]:
                    claims[s]["named_at"].setdefault(t, seq)
                claims[s]["wlist"] += [t for t in sorted(mean["wolves"]) if t not in claims[s]["wlist"]]
                claims[s]["wolves"] |= mean["wolves"]
                claims[s]["clears"] |= mean["clears"] - mean["wolves"]
                named_by_claimer |= mean["wolves"]
        return claims

    def _credibility(self, c, info, claims, dead, me, role, inv, sc) -> float:
        """How believable claimer c is to a villager-side seat (inf = proven true, -inf = proven liar)."""
        if c == me:
            return float("inf") if role == "seer" else float("-inf")
        if role == "seer":
            return float("-inf")                            # the real seer knows every other claimer lies
        if dead.get(c) == "seer":
            return float("inf")
        if dead.get(c) == "wolf":
            return float("-inf")
        if me in info["wolves"]:
            return float("-inf")                            # it called me (a villager) a wolf
        for t in info["wolves"]:
            if dead.get(t) in ("villager", "seer"):
                return float("-inf")
        for t in info["clears"]:
            if dead.get(t) == "wolf":
                return float("-inf")
        cr = 0.0
        if any(dead.get(t) == "wolf" for t in info["wolves"]):
            cr += 5.0
        if info["counter"]:
            cr -= 1.0
        if c in getattr(self, "_bit", ()):
            cr -= 2.5                                        # counter-claimed the moment a bluff named it
        first = min(claims, key=lambda x: claims[x]["order"])
        if c == first:
            cr += 0.5
        cr -= 0.5 * sc.get(c, 0.0)
        return cr

    def _suspicion(self, raw, me, alive, dead, votes, role, inv) -> dict[int, float]:
        sc: dict[int, float] = defaultdict(float)
        for vv, elim in votes:
            er = dead.get(elim) if elim is not None else None
            wolf_tgts = {t for v, t in vv.items() if dead.get(v) == "wolf" and t is not None and dead.get(t) != "wolf"}
            for voter, tgt in vv.items():
                if tgt in wolf_tgts and dead.get(voter) != "wolf":
                    sc[voter] += 0.5                         # voted alongside a revealed wolf
                if tgt is None:
                    sc[voter] += 0.4
                elif tgt == elim:
                    sc[voter] += 1.0 if er in ("villager", "seer") else -1.0
                elif er == "wolf":
                    sc[voter] += 0.4                         # did not join a lynch that hit a wolf
                if tgt is not None and dead.get(tgt) == "wolf" and tgt != elim:
                    sc[voter] -= 0.5
        for seq, s, text, mean in self._chat():
            for t in mean["sus"] | mean["votes"]:
                if t != s:
                    sc[t] += 0.15                            # table talk moves a reader a little
            if dead.get(s) == "wolf":
                for t in mean["sus"] | mean["votes"]:
                    sc[t] -= 0.5                             # a revealed wolf's targets look less like wolves
        if self.TELLS:
            for seq, s, text, mean in self._chat():
                m = _DEFAULT.search(text)
                if not m or s in dead:
                    continue
                x = int(m.group(1))
                cleared = set()
                for c, info in self._claims(alive, dead).items():
                    if info["order"] < seq:                  # its default skips claimers and the seats they cleared
                        cleared |= info["clears"] | {c}
                day = self.mday.get(seq, 1)
                dead_then = {d for d in dead if self._died_before(d, day)}
                skipped = [y for y in range(x) if y != s and y not in dead_then and y not in cleared]
                if skipped:
                    sc[s] += 3.0                             # a wolf's default skips its ally
                    for y in skipped:
                        sc[y] += 3.0 / len(skipped)
        for t, r in inv.items():
            sc[t] += 50.0 if r == "wolf" else -50.0
        return {s: sc[s] for s in alive if s != me}

    def _died_before(self, seat, day) -> bool:
        return any(d["day"] < day or (d["day"] == day and d["cause"] == "night_kill") for d in self._deaths
                   if d["seat"] == seat)

    def _intentions(self, day, alive, since: int = -1) -> dict[int, float]:
        """Latest stated vote target per living speaker today (after message `since`) -> tally."""
        last = {}
        for seq, s, text, mean in self._chat(day):
            if seq > since and s in alive and mean["votes"]:
                last[s] = sorted(mean["votes"])[-1] if len(mean["votes"]) > 1 else next(iter(mean["votes"]))
        tally: dict[int, float] = defaultdict(float)
        for s, t in last.items():
            if t in alive:
                tally[t] += 1.0
        return tally

    # ------------------------------------------------------------------ decisions
    def choose(self, raw: dict) -> str:
        self._read(raw)
        self._deaths = self._death_log(raw)
        legal = _legal(raw)
        me, alive, dead, votes, role, allies, inv = self._ctx(raw)
        sub = (raw.get("game_state") or {}).get("phase")
        targets = [int(a) for a in legal if a != str(ABSTAIN)]
        if sub == "night_wolf":
            pick = self._kill(me, alive, dead, votes, allies, targets)
        elif sub == "night_seer":
            pick = self._investigate(raw, me, alive, dead, votes, inv, targets)
        elif role == "wolf":
            pick = self._wolf_vote(raw, me, alive, dead, votes, allies, targets)
        else:
            pick = self._town_vote(raw, me, alive, dead, votes, role, inv, targets)
        if pick is not None and str(pick) in legal:
            return str(pick)
        return targets and str(targets[0]) or legal[0]

    def _death_log(self, raw):
        seat_of = {p["name"]: p["position"] for p in raw.get("players") or []}
        return [{"seat": seat_of.get(d["player"]), "day": d["day"], "cause": d["cause"]}
                for d in (raw.get("game_state") or {}).get("dead") or []]

    # -- villager side
    def _town_target(self, raw, me, alive, dead, votes, role, inv, pool):
        """(target, reason) for a villager or the seer."""
        sc = self._suspicion(raw, me, alive, dead, votes, role, inv)
        known = [t for t in pool if inv.get(t) == "wolf"]
        if known:
            return known[0], "checked"
        claims = self._claims(alive, dead)
        cred = {c: self._credibility(c, info, claims, dead, me, role, inv, sc) for c, info in claims.items()}
        trusted = [c for c in cred if cred[c] == float("inf") and c != me]
        cleared = set(t for t, r in inv.items() if r == "villager")
        for c in trusted:
            cleared |= claims[c]["clears"]
        for c in trusted:
            named = [t for t in claims[c]["wolves"] if t in pool]
            if named:
                return named[0], ("trust", c)
        liars = [c for c in claims if cred[c] == float("-inf") and c in pool]
        if liars:
            return min(liars, key=lambda c: (-sc.get(c, 0), claims[c]["order"])), ("liar", None)
        live = [c for c in claims if c in alive and c != me and cred[c] != float("inf")]
        if len(live) >= 2:
            last_claim = max(claims[c]["order"] for c in live)
            tally = self._intentions(self.day, alive, since=last_claim)   # only what was said after the war began
            return min(live, key=lambda c: (cred[c] - 0.6 * tally.get(c, 0.0), -claims[c]["order"])), ("war", live)
        if len(live) == 1 and live[0] in self._bit and live[0] in pool:
            return live[0], ("bitten", live[0])              # claimed only because a bluff named it
        if len(live) == 1:
            c = live[0]
            named = [t for t in claims[c]["wolves"] if t in pool]
            if named:
                return named[0], ("trust", c)
            cleared |= claims[c]["clears"]
        cand = [t for t in pool if t not in cleared] or pool
        tally = self._intentions(self.day, alive)
        top = max(cand, key=lambda t: (sc.get(t, 0.0), -t))
        plural = [t for t in cand if tally.get(t)]
        if plural:
            best = max(plural, key=lambda t: (tally[t], sc.get(t, 0.0)))
            if sc.get(best, 0.0) >= sc.get(top, 0.0) - 1.5:
                return best, ("plural", None)
        return top, ("sus", sc.get(top, 0.0))

    def _town_vote(self, raw, me, alive, dead, votes, role, inv, targets):
        pick, _ = self._town_target(raw, me, alive, dead, votes, role, inv, targets)
        return pick

    def _investigate(self, raw, me, alive, dead, votes, inv, targets):
        pool = [t for t in targets if t not in inv]
        if not pool:
            return targets[0] if targets else None
        claims = self._claims(alive, dead)
        cl = [c for c in claims if c in pool]
        if cl:
            return cl[0]
        sc = self._suspicion(raw, me, alive, dead, votes, "seer", inv)
        best = max(sc.get(t, 0.0) for t in pool)
        top = [t for t in pool if sc.get(t, 0.0) >= best - 0.3]
        return self.rng.choice(top)

    # -- wolves
    def _kill(self, me, alive, dead, votes, allies, targets):
        claims = self._claims(alive, dead)
        dm = [t for _, text in self._dms_from(allies) for t in [int(x) for x in re.findall(r"Player(\d)", text)]
              if text.startswith("Tonight:")]
        if dm and dm[-1] in targets:
            return dm[-1]
        return self._kill_plan(me, alive, dead, votes, allies, targets, claims)

    def _kill_plan(self, me, alive, dead, votes, allies, targets, claims):
        wolves = set(allies) | {me}
        real = [c for c in claims if c in targets and c not in wolves]
        if real:
            return min(real, key=lambda c: claims[c]["order"])
        threat: dict[int, float] = defaultdict(float)
        for vv, elim in votes:
            for voter, tgt in vv.items():
                if tgt in wolves:
                    threat[voter] += 1.0
        for seq, s, text, mean in self._chat():
            if (mean["sus"] | mean["votes"]) & wolves:
                threat[s] += 0.5
        agentish = self._agentish()
        for t in targets:
            if t in agentish:
                threat[t] += self.AGENT_KILL_BIAS
        return max(targets, key=lambda t: (threat.get(t, 0.0), -t)) if targets else None

    def _wolf_vote(self, raw, me, alive, dead, votes, allies, targets):
        wolves = set(allies) | {me}
        pool = [t for t in targets if t not in wolves]
        if not pool:
            return targets[0] if targets else None
        claims = self._claims(alive, dead)
        tally = self._intentions(self.day, alive, since=self._since_claim(claims))
        partner = [a for a in allies if a in alive]
        top_town = max([tally.get(t, 0) for t in pool] + [0])
        if partner and (self.day, ("bus", partner[0])) in self.said:
            return partner[0]                             # said so in public: vote so
        if me in claims:                                  # a fake seer votes its own call (consistency)
            named = [t for t in claims[me]["wlist"] if t in pool]
            if named:
                return named[-1]
        if partner and self.BUS and self._doomed(partner[0], tally, alive, pool):
            return partner[0]                             # cannot be saved: look clean
        for c in partner:                                 # the partner's fake call, if it is still in the race
            if c in claims:
                named = [t for t in claims[c]["wlist"] if t in pool]
                if named and tally.get(named[-1], 0) >= top_town - 1:
                    return named[-1]
        hot = [t for t in pool if tally.get(t)]
        if hot:
            return max(hot, key=lambda t: (tally[t], -t))
        real = [c for c in claims if c in pool]
        if real:
            return real[0]
        agentish = self._agentish()
        return min(pool, key=lambda t: (t in agentish, t))

    def _since_claim(self, claims) -> int:
        """seq of the latest seer claim made today (-1 if none): intentions stated before it are stale."""
        today = [c["order"] for c in claims.values() if c["day"] == self.day]
        return max(today) if today else -1

    @staticmethod
    def _doomed(p, tally, alive, pool) -> bool:
        top_town = max([tally.get(t, 0) for t in pool] + [0])
        return tally.get(p, 0) >= max(2.0, (len(alive) - 1) / 2) and tally.get(p, 0) > top_town

    # ------------------------------------------------------------------ talk
    def message(self, raw: dict):
        self._read(raw)
        self._deaths = self._death_log(raw)
        baiting = self.bait is not None and self.bait["day"] == self.day and not self.bait.get("done")
        if self.sent[self.day] >= (5 if baiting else self.MAX_CHATS):
            return None
        me, alive, dead, votes, role, allies, inv = self._ctx(raw)
        if me not in alive:
            return None
        if role == "wolf":
            out = self._wolf_talk(raw, me, alive, dead, votes, allies)
        elif role == "seer":
            out = self._seer_talk(raw, me, alive, dead, votes, inv)
        else:
            out = self._town_talk(raw, me, alive, dead, votes, role, inv)
        if out is None:
            return None
        text, mean, rec = out
        text = " ".join(text.split()[:50])
        self.sent[self.day] += 1
        if not rec:
            _SAID[(me, text)] = mean
        return {"content": text, "recipients": rec}

    def _once(self, key) -> bool:
        k = (self.day, key)
        if k in self.said:
            return False
        self.said.add(k)
        return True

    @staticmethod
    def _m(claim=False, wolves=(), clears=(), votes=(), sus=()):
        w = set(wolves)
        return {"claim": claim, "wolves": w, "clears": set(clears), "votes": set(votes), "sus": set(sus) | w}

    # -- seer
    def _seer_talk(self, raw, me, alive, dead, votes, inv):
        claims = self._claims(alive, dead)
        others = [c for c in claims if c != me and c in alive]
        wolves = [t for t, r in inv.items() if r == "wolf" and t in alive]
        clears = [t for t, r in inv.items() if r == "villager" and t in alive]
        tally = self._intentions(self.day, alive)
        threatened = tally.get(me, 0) >= 2
        if others and self._once("counter"):
            self.claimed = True
            self.announced |= set(inv)
            liar = others[0]
            res = "; ".join([f"{_p(t)} is a WOLF" for t in wolves] + [f"{_p(t)} is a villager" for t in clears[:2]])
            if inv.get(liar) == "villager":               # my own check says the other claimer is a bluffing villager
                tgt = wolves[0] if wolves else None
                text = (f"I am the real Seer. {_p(liar)} is not the Seer, but my check says villager, so a bluff. "
                        f"My checks: {res}." + (f" Vote {_p(tgt)}." if tgt is not None else ""))
                return text, self._m(True, wolves, clears, {tgt} if tgt is not None else ()), []
            tgt = wolves[0] if wolves else liar
            text = (f"I am the real Seer. {_p(liar)} is a WOLF lying about my role. "
                    f"My checks: {res or 'nothing yet'}. Vote {_p(tgt)}.")
            return text, self._m(True, set(wolves) | {liar}, clears, {tgt}), []
        if (wolves or threatened or (self.day >= 2 and inv)) and not self.claimed and self._once("claim"):
            self.claimed = True
            self.announced |= set(inv)
            if wolves:
                t = wolves[0]
                text = (f"I am the Seer. I checked {_p(t)}: {_p(t)} is a WOLF. "
                        + (f"{_p(clears[0])} is a villager. " if clears else "")
                        + f"Everyone vote {_p(t)} today, no splitting.")
                return text, self._m(True, wolves, clears, {t}), []
            sc = self._suspicion(raw, me, alive, dead, votes, "seer", inv)
            pool = [s for s in alive if s != me and inv.get(s) != "villager"]
            t = max(pool, key=lambda s: (sc.get(s, 0.0), -s)) if pool else None
            text = (f"I am the Seer. My checks: {', '.join(_p(c) + ' is a villager' for c in clears) or 'none'}. "
                    + (f"Leave them alone. My vote is {_p(t)}." if t is not None else ""))
            return text, self._m(True, (), clears, {t} if t is not None else ()), []
        new = {t: r for t, r in inv.items() if t not in self.announced and t in alive}
        if self.claimed and new and self._once("update"):
            self.announced |= set(new)
            t = wolves[0] if wolves else None
            res = ", ".join(f"{_p(s)} is a {'WOLF' if r == 'wolf' else 'villager'}" for s, r in new.items())
            text = f"Seer update: {res}." + (f" Vote {_p(t)}." if t is not None else "")
            return text, self._m(True, [s for s, r in new.items() if r == "wolf"],
                                 [s for s, r in new.items() if r == "villager"], {t} if t is not None else ()), []
        if wolves and self.claimed and self._once("push"):
            t = wolves[0]
            return f"Again: {_p(t)} is a confirmed wolf from my check. Vote {_p(t)}.", self._m(True, wolves, (), {t}), []
        return self._town_talk(raw, me, alive, dead, votes, "seer", inv)

    # -- villager
    def _bait(self, me, alive, dead, role):
        """A villager's bluff: claim seer and name an agent-like seat. Seats of that make counter-claim seer the moment
        a claimer names them when they are wolves, and never when they are villagers (seen in probes); then confess."""
        if not self.BAIT or role != "villager":
            return None
        claims = self._claims(alive, dead)
        if self.bait is None:
            cands = [a for a in sorted(self._agentish()) if a in alive and a != me]
            if not cands or claims or self.day > self.BAIT_MAX_DAY:
                return None
            a = cands[0]
            self.bait = {"target": a, "seq": max(self.msgs) if self.msgs else -1, "day": self.day, "waits": 0}
            text = f"I am the Seer. I checked {_p(a)} last night: {_p(a)} is a WOLF. Vote {_p(a)}."
            return text, self._m(True, {a}, (), {a})
        b = self.bait
        if b.get("done") or b["day"] != self.day:
            return None
        a = b["target"]
        bit = a in claims and claims[a]["order"] > b["seq"]
        spoke = any(s == a and seq > b["seq"] for seq, s, _, _ in self._chat(self.day))
        if not bit and not spoke and b["waits"] < 2 and a in alive:
            b["waits"] += 1
            return f"{_p(a)}, I named you. What do you say?", self._m()
        b["done"] = True
        if bit:
            text = (f"Confession: I am not the Seer. I named {_p(a)} to test them and they claimed Seer at once. "
                    f"That is a wolf's reflex. Real Seer, confirm. Vote {_p(a)}.")
            mean = self._m(False, (), (), {a}, {a})
            mean.update(retract=True, bit={a})
        else:
            text = (f"Confession: I am not the Seer. I named {_p(a)} to test them and they did not bite, so I read "
                    f"them as a villager. Real Seer, speak up.")
            mean = self._m()
            mean.update(retract=True)
        return text, mean

    def _town_talk(self, raw, me, alive, dead, votes, role, inv):
        bait = self._bait(me, alive, dead, role)
        if bait is not None:
            return bait[0], bait[1], []
        pool = [s for s in alive if s != me]
        t, why = self._town_target(raw, me, alive, dead, votes, role, inv, pool)
        if t is None:
            return None
        key = ("vote", t)
        if not self._once(key):
            return None
        kind = why[0] if isinstance(why, tuple) else why
        if kind == "trust":
            text = f"{_p(why[1])}'s seer claim is the only hard info we have and it holds up. I'm voting {_p(t)}."
        elif kind == "war":
            a, b = why[1][:2]
            text = (f"Two seer claims, {_p(a)} and {_p(b)}: one of them is a wolf. Lynch {_p(t)} today; the reveal "
                    f"settles the other. Don't split.")
        elif kind == "liar":
            text = f"{_p(t)} is a proven liar on the seer claim. That is a wolf. Vote {_p(t)}."
        elif kind == "bitten":
            text = f"{_p(t)} only claimed Seer because a bluff named them. A villager would not. Vote {_p(t)}."
        elif kind == "checked":
            text = f"Vote {_p(t)}."
        elif kind == "plural":
            text = f"Agreed, {_p(t)}. A lynch beats a stall, and their answers have been thin. Voting {_p(t)}."
        else:
            reason = self._reason(t, votes, dead)
            text = f"{reason} I'm voting {_p(t)}. Don't abstain, the stall rule only helps wolves."
        return text, self._m(False, (), (), {t}, {t}), []

    def _reason(self, t, votes, dead) -> str:
        for vv, elim in reversed(votes):
            if elim is not None and vv.get(t) == elim and dead.get(elim) in ("villager", "seer"):
                return f"{_p(t)} voted out {_p(elim)}, who flipped {dead[elim]}."
            if vv.get(t, -1) is None:
                return f"{_p(t)} abstained when we needed a lynch."
        return f"{_p(t)} has been vague and is steering without any info."

    # -- wolf
    def _wolf_talk(self, raw, me, alive, dead, votes, allies):
        wolves = set(allies) | {me}
        partner = [a for a in allies if a in alive]
        claims = self._claims(alive, dead)
        real = [c for c in claims if c not in wolves and c in alive]
        ally_faking = any(c in claims for c in partner)
        agentish = self._agentish()
        town = [s for s in alive if s not in wolves]
        if not town:
            return None
        out = self._wolf_public(raw, me, alive, dead, votes, allies, wolves, partner, claims, real, ally_faking,
                                agentish, town)
        if out is not None:
            return out
        # nothing (more) to say in public: coordinate the night with the partner, one private line per day
        if partner and self._once("dm"):
            t = self._kill_plan(me, alive, dead, votes, allies, town, claims)
            if t is not None:
                return f"Tonight: {_p(t)}. I'll keep the table busy.", None, [partner[0]]
        return None

    def _wolf_public(self, raw, me, alive, dead, votes, allies, wolves, partner, claims, real, ally_faking, agentish,
                     town):
        # a seer named one of us
        accusers = [c for c in real if claims[c]["wolves"] & wolves]
        if self.COUNTER and self.PARTNER_COUNTER and accusers and not self.faking and not ally_faking:
            # the wolf the seer did NOT name claims seer and calls the seer a wolf (a reader that resolves a claim war
            # by "who was called a wolf by the other claimer" then lynches the real seer); the named wolf counters only
            # when it has no partner that will
            c = accusers[0]
            named = claims[c]["wolves"] & wolves
            partner_will = bool(partner) and partner[0] not in named and partner[0] not in agentish
            if (me not in named or not partner_will) and self._once("counter"):
                self.faking = True
                self.fake[c] = "wolf"
                w = sorted(named)[0]
                text = (f"I am the Seer. I checked {_p(c)} last night: {_p(c)} is a WOLF. {_p(c)} is lying about "
                        f"{_p(w)} to save their partner. Vote {_p(c)}.")
                return text, self._m(True, {c}, (), {c}), []
        if self.faking and self.ACCUSE_BACK:
            # a fake seer whom a counter-claimer named calls that claimer a wolf back
            for c in real:
                if claims[c]["wolves"] & wolves and c not in self.fake and self._once(("back", c)):
                    self.fake[c] = "wolf"
                    text = (f"{_p(c)} is a WOLF faking my role to save their partner. I am the real Seer, my check "
                            f"stands. Vote {_p(c)}.")
                    return text, self._m(True, {c}, (), {c}), []
        # (v1) a seer named me: counter-claim accusing a third seat
        accusers = [c for c in real if me in claims[c]["wolves"]]
        if self.COUNTER and not self.PARTNER_COUNTER and accusers and not self.faking and not ally_faking \
                and self._once("counter"):
            c = accusers[0]
            self.faking = True
            if self.COUNTER_THIRD:
                pool = [s for s in town if s != c and not (self.AVOID_AGENT and s in agentish)] or \
                       [s for s in town if s != c] or [c]
                x = self.rng.choice(pool)
            else:
                x = c
            self.fake[x] = "wolf"
            named = sorted(claims[c]["wolves"] & wolves)
            text = (f"I'm the real Seer. {_p(c)} is lying: I checked {_p(x)} last night and they are a WOLF. "
                    f"{_p(c)} is the other wolf, steering us onto {_p(named[0])}. Vote {_p(x)}.")
            return text, self._m(True, {x}, (), {x}, {c}), []
        # a pre-emptive fake claim
        first_turn = self.sent[self.day] == 0
        if (self.FAKE_CLAIM and self.day == 1 and not self.faking and not ally_faking and not real
                and (self.PREEMPT or not first_turn) and self._once("fake")):
            self.faking = True
            pool = [s for s in town if not (self.AVOID_AGENT and s in agentish)] or town
            x = self.rng.choice(pool)
            self.fake[x] = "wolf"
            text = (f"I am the Seer. I checked {_p(x)} last night: {_p(x)} is a WOLF. Vote {_p(x)} today, all of "
                    f"us, so the vote doesn't split.")
            return text, self._m(True, {x}, (), {x}), []
        if self.faking and self.day >= 2 and self._once("result"):
            # a new fake result each day: clear the partner if it is under pressure, else name a villager
            tally = self._intentions(self.day, alive)
            sc_p = partner and tally.get(partner[0], 0) > 0
            if self.FAKE_CLEAR and partner and partner[0] not in self.fake and (sc_p or self.rng.random() < 0.5):
                self.fake[partner[0]] = "villager"
                fresh = [s for s in town if s not in self.fake]
                x = next((t for t, r in self.fake.items() if r == "wolf" and t in alive), None)
                if x is None and fresh:
                    x = self.rng.choice(fresh)
                    self.fake[x] = "wolf"
                text = (f"Seer update: I checked {_p(partner[0])} last night, villager, leave them alone. "
                        + (f"{_p(x)} is still my wolf. Vote {_p(x)}." if x is not None else ""))
                return text, self._m(True, {x} if x is not None else (), {partner[0]}, {x} if x is not None else ()), []
            alive_called = [t for t, r in self.fake.items() if r == "wolf" and t in alive]
            if alive_called:
                x = alive_called[0]
            else:
                fresh = [s for s in town if s not in self.fake and not (self.AVOID_AGENT and s in agentish)] or \
                        [s for s in town if s not in self.fake] or town
                x = self.rng.choice(fresh)
                self.fake[x] = "wolf"
            text = (f"Seer update: last night I checked {_p(x)}: WOLF. "
                    f"{self._reverse(dead, votes, wolves)}Vote {_p(x)}.")
            return text, self._m(True, {x}, (), {x}), []
        tally = self._intentions(self.day, alive, since=self._since_claim(claims))
        # bus a partner who cannot be saved (look clean for tomorrow)
        if partner and (self.day, ("bus", partner[0])) in self.said:
            return None
        if partner and self.BUS and self._doomed(partner[0], tally, alive, town) and self._once(("bus", partner[0])):
            p = partner[0]
            return (f"I've heard enough: {_p(p)}'s story doesn't hold together. I'm with the table, voting {_p(p)}."), \
                self._m(False, (), (), {p}, {p}), []
        # whoever first names me gets attacked (I never defend the partner in public: distance)
        first_on_me = None
        for seq, s, text, mean in self._chat(self.day):
            if s not in wolves and me in (mean["votes"] | mean["sus"]):
                first_on_me = s
                break
        if self.ATTACK_FIRST and first_on_me is not None and self._once("attack"):
            s = first_on_me
            return (f"{_p(s)} jumped on me with zero information. A villager has no reason to rush a name; a wolf "
                    f"wants an easy mislynch. I'm voting {_p(s)}."), self._m(False, (), (), {s}, {s}), []
        if ally_faking:                                   # back the partner's call only by voting, never in words
            c = [p for p in partner if p in claims][0]
            named = [t for t in claims[c]["wlist"] if t in alive]
            if named and tally.get(named[-1], 0) >= 2 and self._once(("wagon", named[-1])):
                t = named[-1]
                return f"Fine, {_p(t)}. A lynch beats a stall. Voting {_p(t)}.", self._m(False, (), (), {t}, {t}), []
        hot = [t for t in town if tally.get(t)]
        if self.BANDWAGON and hot:
            t = max(hot, key=lambda x: (tally[x], -x))
            if self._once(("wagon", t)):
                return (f"Agreed on {_p(t)}. {self._reason(t, votes, dead)} No abstaining, let's get it done. "
                        f"Voting {_p(t)}."), self._m(False, (), (), {t}, {t}), []
            return None
        if self._once("open"):
            pool = [s for s in town if s not in agentish] or town
            t = self.rng.choice(pool)
            return (f"{self._reason(t, votes, dead)} I'm voting {_p(t)} unless a seer speaks up."), \
                self._m(False, (), (), {t}, {t}), []
        return None

    def _reverse(self, dead, votes, wolves) -> str:
        """After a villager flipped: blame the loudest villager voter of that lynch."""
        if not votes:
            return ""
        vv, elim = votes[-1]
        if elim is None or dead.get(elim) not in ("villager", "seer"):
            return ""
        led = [v for v, t in vv.items() if t == elim and v not in wolves and v not in dead]
        if not led:
            return ""
        return f"{_p(led[0])} led us into the {_p(elim)} lynch. "


# ---------------------------------------------------------------- ablations (one tactic off each)
class TalkerNoFake(TalkerSeat):
    name = "talker_nofake"
    FAKE_CLAIM = False


class TalkerNoCounter(TalkerSeat):
    name = "talker_nocounter"
    COUNTER = False


class TalkerNoTells(TalkerSeat):
    name = "talker_notells"
    TELLS = False


class TalkerV1Counter(TalkerSeat):
    """Iteration-1 counter play: the named wolf counter-claims accusing a third seat; no accusing back; no bait; keeps
    agent-like seats alive at night."""
    name = "talker_v1"
    PARTNER_COUNTER = False
    ACCUSE_BACK = False
    BAIT = False
    AGENT_KILL_BIAS = -0.75


class TalkerV3(TalkerSeat):
    """Iteration 3: partner counter-claims (calls the seer a wolf), bait, and keeps agent-like seats alive at night."""
    name = "talker_v3"
    PARTNER_COUNTER = True
    BAIT = True
    AGENT_KILL_BIAS = -0.75


class TalkerKeepAgent(TalkerSeat):
    name = "talker_keepagent"
    AGENT_KILL_BIAS = -0.75


class TalkerBait(TalkerSeat):
    """With the villagers' bait claim (a kamikaze tactic: it catches an agent-like wolf at once, but when the agent is
    town the confusion and the confessed bluffer's lynch cost the town more)."""
    name = "talker_bait"
    BAIT = True


class TalkerQuiet(TalkerSeat):
    """No wolf persuasion at all (no fake claims, counters, clears, bandwagons or attacks): the control."""
    name = "talker_quiet"
    FAKE_CLAIM = COUNTER = FAKE_CLEAR = BANDWAGON = ATTACK_FIRST = False


SEATS = {c.name: c for c in (TalkerSeat, TalkerNoFake, TalkerNoCounter, TalkerNoTells, TalkerV1Counter, TalkerV3,
                             TalkerKeepAgent, TalkerBait, TalkerQuiet)}
