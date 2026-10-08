"""Red team `wolfpack`: an outsider's Werewolf bot whose wolves play to beat a strong villager.

Written as a context-free challenger: only the public rules (docs/platform/werewolf.md, 06-messaging.md), the local
simulator and the existing reference bots were read; our agent was treated as a black box (its moves and chat in game
transcripts). Nothing here imports or inspects the agent.

Background types (registered in local/eval_werewolf.py):
  wolfpack    villager / seer seats play exactly like `llmq` (honest, persuadable); wolf seats play the wolfpack brain
  wolfpack_h  villager / seer seats play like `heuristic` (vote record only, deaf to chat): the wolves' talk can then
              only move our agent

The wolf brain (both wolves run the same deterministic rules on the same public record, so they never split a kill or
a vote; the private channel carries the night target). Shipped configuration (`_Best`, iteration 5):
  * day 1: the first wolf to speak fake-claims Seer and calls a villager a wolf -- preferably the table's organiser
    (the player talking in "everyone vote / split vote / vote record" language); the partner backs the claim
  * any real seer claim is counter-claimed ("PlayerS is lying. I am the real Seer and PlayerS is a wolf")
  * votes: the fake seer's latest call, else the counter-claimed seer, else the non-wolf the table already leans to
  * night: a live seer claimer first; an organiser only while 2+ of them are alive (a voting bloc) or once a wolf is
    alone (he would lead the final vote); else the player who pushed hardest against the wolves
  * talk stays open (HOLD) for a few turns to answer late claims; at most 4 chats a day, 50 words each.
    HOLD is werewolf_env's SIM_HOLD: these seats are for local/eval_werewolf.py, not local/werewolf_platform.py.

Iteration log (paired runner below, seed 2100, our agent's win rate with BOTH wolves in background seats, k=1 / k=3;
rt = the official runner's discussion timing, rounds = the simulator default):
  1. v0 loud pair (claim, counter, push, kill seer claimer / heat): rounds 18.2 / 46.0 (llmq wolves: 46.5 / 69.1)
  2. toggles: night-killing the organiser helps k=3 (33.4) but hurts k=1 (20.1); a "deep cover" pair that lies only to
     save a wolf is worse (24.5 / 45.4) -- rejected
  3. never frame the organiser + bus a doomed partner + counter every claim: rt 21.0 / 38.0 -- framing the organiser
     is the point: copies of one agent follow the fake seer and vote out their own copy
  4. frame the organiser: rt 20.3 / 19.6, rounds 19.8 / 31.1
  5. kill the organiser only against a bloc or for a lone wolf (alive alone, he splits the village by following a
     dead seer's call while the crowd votes the exposed liar): rt 14.9 / 19.6, rounds 14.9 / 31.1  <- shipped
  6. hedge: the partner of an endangered fake seer votes elsewhere / votes him (agents flag "voted with a revealed
     wolf"): rt 22.1-22.8 / 24.6-33.8 -- rejected, coordinated votes are worth more than the tell
Fresh seed 2101 (3000 games): rt 14.0 / 19.6, rounds 14.0 / 30.7 (llmq wolves 45.4 / 67.3, heuristic 58.9 / 57.4);
an `llmq` bot in our seats on the same deals: rt 14.3 / 14.4.

What the black box showed (transcripts, seeds 2100-2101):
  * it follows any single seer claim on day 1, and copies of it vote out their own copy when a fake seer names one
  * two seer claims: it always votes a claimer who called it a wolf, else leans to the lower seat (492:232), to the
    claimer the other one called a wolf (327:125) and to the one with more chat pressure (407:188): fooled 47%
  * official-runner timing: it says one line at t = 0 before anyone else, then nothing: framed by a fake seer it never
    answered in chat (1007 of 1007 framed days) and was voted out 56% of those days. With ARENA_WW_LISTEN=on it holds
    and sometimes answers: rt 17.5 / 33.4 against this pack (llmq wolves unchanged, 45.4 / 68.0)
  * when a revealed seer's call and an exposed fake seer are both alive it votes the dead seer's call even when the
    rest of the village votes the liar: no elimination 36 of 44 such days, lost 34 of those 36 games
  * its day-2 "most suspicious" pick is a wolf 84-96% of the time; it never picks a player who voted for a revealed
    wolf, and voting with a revealed wolf is its strongest tell
  * fixed phrasings ("To avoid a split vote", "My read from the vote record", "Following the Seer") make every copy
    identifiable: framing / killing the organiser is what moves k=3 tables

    python -m local.redteam_wolfpack --games 1000 --seed 2100 --ks 1,3 --bgs llmq,heuristic,wolfpack,wolfpack_h
    python -m local.redteam_wolfpack --agent bg:llmq ...      # control: a reference bot in our seats, same deals
"""
from __future__ import annotations

import argparse
import math
import os
import random
import re
import sys
from collections import defaultdict

from .werewolf_bots import _SAID, _T, HeuristicSeat, LLMQwenSeat, LLMSeat, _legal, _private, _public
from .werewolf_env import ABSTAIN, SIM_HOLD

_LIAR = re.compile(_T + r"\s*(?:is|was)\s+(?:a\s+)?(?:lying|liar|fake|faking)", re.I)
# organiser's language: the player steering the table's vote (the villager a wolf most wants gone)
_LEADER = re.compile(r"everyone vote|so we do not split|split (?:the )?vote|vote record|don'?t abstain|do not abstain|"
                     r"reveal will show|better info|hard info|following the seer|two seer claims|lying about being",
                     re.I)


def _mean(text: str, s) -> tuple[bool, set, set, set]:
    """(claims seer, seats called wolf, seats called clear, seats pressed), the way an LLM-ish reader takes `text`."""
    hit = _SAID.get((s, text))
    claim, wolves, clears, press = hit if hit is not None else LLMSeat._meaning(text, s)
    liars = {int(x) for x in _LIAR.findall(text)} - {s}
    return claim, set(wolves), set(clears), set(press) | liars


class WolfBrain:
    """Mixin: a wolf seat plays this brain; every other role falls through to the base seat class (honest play)."""

    CLAIM_D1 = True        # fake-claim Seer on day 1 when no one has claimed yet
    CLAIMER = "first"      # which wolf fake-claims: first (to speak) | high (seat) | low (seat)
    COUNTER = True         # counter-claim a seer who calls one of us a wolf
    BACK = True            # the partner publicly backs the fake seer
    KILL = "seer_first"    # seer_first | leader_first | seer_unless_called
    LEADER_KILL = False    # seer_first: with no seer claimer, kill the table's organiser before the heat target
    LEADER_MIN = 1         # ... only while at least this many organisers are alive (2: break a voting bloc)
    LONE_LEADER = False    # ... and always once a wolf is alone (the organiser would lead the final vote)
    HEDGE = ""             # "" | bus | split: the partner of an endangered fake seer votes off the fake's target
    FRAME = "heat"         # heat | nonleader (never frame the organiser: they know they are innocent)
    MAX_TURNS = 5          # discussion turns a wolf stays in (holding) per day before it terminates
    COUNTER_ALL = False    # our fake seer also calls a seer who only cleared someone a wolf
    COUNTER_CLEAR = False  # with no fake seer yet, counter-claim a seer who only cleared someone
    BUS_LOUD = False       # loud: vote out a partner most of the village has announced it votes (buys trust)
    BUS_SHARE = 0.5        # ... when at least this share of the non-wolves announced it
    VOTE_LATEST = True     # follow our fake seer's latest call (else the first)
    STYLE = "loud"         # loud: claim and push every day | adaptive: blend in, lie only to save a wolf
    LISTEN = 2             # adaptive: discussion turns spent listening before deciding to lie
    BUS = True             # adaptive: vote out a partner the whole table is already voting

    # ---- state ------------------------------------------------------------------------------------------------
    def reset(self):
        super().reset()
        self.wmsgs: dict[int, dict] = {}
        self.wday_of: dict[int, int] = {}
        self.turns: dict[int, int] = defaultdict(int)
        self.said: dict[int, list[str]] = defaultdict(list)    # day -> tags of what this wolf already said

    def _wread(self, raw: dict) -> None:
        gs = raw.get("game_state") or {}
        day = gs.get("day") or 1
        if gs.get("phase") != "day_vote":
            day -= 1                                   # at night the window shown is the previous day's
        for m in raw.get("new_messages") or []:
            if isinstance(m, dict) and m.get("seq") is not None and m["seq"] not in self.wmsgs:
                self.wmsgs[m["seq"]] = m
                self.wday_of[m["seq"]] = m.get("day", day)

    def _is_wolf(self, raw) -> tuple[bool, list[int]]:
        role, allies, _ = _private(raw)
        return role == "wolf", allies

    # ---- reading the table (broadcasts + public record only: both wolves compute the same thing) ---------------
    def _table(self, raw: dict, allies: list[int]) -> dict:
        me, alive, dead, votes = _public(raw)
        gs = raw.get("game_state") or {}
        day = gs.get("day") or 1
        if gs.get("phase") != "day_vote":
            day -= 1
        wolves = set(allies) | {me}
        claims: dict[int, dict] = {}
        accused = defaultdict(set)          # seat -> who called it wolf / liar (any day)
        intent: dict[int, int] = {}         # today: sender -> last seat they pressed
        pressure = defaultdict(float)       # today: seat -> times pressed
        heat_on_wolves = defaultdict(float)  # all days: seat -> how hard it pushed against a wolf
        leader = defaultdict(float)         # all days: seat -> messages in organiser's language
        for seq in sorted(self.wmsgs):
            m = self.wmsgs[seq]
            if m.get("recipients") or m.get("type", "chat") != "chat":
                continue
            s, text = m.get("sender"), str(m.get("content") or "")
            if s not in wolves and _LEADER.search(text):
                leader[s] += 1.0
            claim, called, clears, press = _mean(text, s)
            if claim or s in claims:
                c = claims.setdefault(s, {"order": len(claims), "wolves": set(), "clears": set(), "calls": [],
                                          "day": self.wday_of.get(seq, day)})
                c["wolves"] |= called
                c["clears"] |= clears
                c["calls"] += sorted(x for x in called if x not in c["calls"])   # in the order they were made
            for t in called | press:
                accused[t].add(s)
                if t in wolves and s not in wolves:
                    heat_on_wolves[s] += 2.0 if t in called else 1.0
            if self.wday_of.get(seq, day) == day:
                for t in press:
                    pressure[t] += 1.0
                pick = [t for t in press if t != s]
                if pick:
                    intent[s] = pick[-1]
        for vv, elim in votes:
            for voter, tgt in vv.items():
                if tgt in wolves and voter not in wolves:
                    heat_on_wolves[voter] += 1.0
        live_claims = {c: v for c, v in claims.items() if c in alive}
        fake = {c: v for c, v in live_claims.items() if c in wolves}
        real = {c: v for c, v in live_claims.items() if c not in wolves}
        return {"me": me, "alive": alive, "dead": dead, "votes": votes, "day": day, "wolves": wolves,
                "claims": live_claims, "fake": fake, "real": real, "accused": accused, "intent": intent,
                "pressure": pressure, "heat": heat_on_wolves, "leader": leader}

    @staticmethod
    def _tiebreak(t: dict, key: str) -> random.Random:
        return random.Random(f"{key}:{t['day']}:{','.join(map(str, t['alive']))}")

    def _frame_target(self, t: dict) -> int | None:
        """The villager a fake seer calls a wolf: one already under pressure, else the hottest on us, else random."""
        pool = [s for s in t["alive"] if s not in t["wolves"] and s not in t["claims"]]
        if self.FRAME == "nonleader":
            pool = [s for s in pool if not t["leader"].get(s)] or pool
        if not pool:
            pool = [s for s in t["alive"] if s not in t["wolves"]]
        if not pool:
            return None
        rng = self._tiebreak(t, "frame")
        lead = (lambda s: t["leader"].get(s, 0.0)) if self.FRAME == "leader" else (lambda s: 0.0)
        return max(pool, key=lambda s: (lead(s), t["heat"].get(s, 0.0), t["pressure"].get(s, 0.0), rng.random()))

    def _doomed_partner(self, t: dict) -> int | None:
        """A partner most of the village has announced it will vote today (BUS: join them, it buys trust)."""
        if not self.BUS_LOUD:
            return None
        tally = self._forecast(t)
        voters = len([s for s in t["alive"] if s not in t["wolves"]])
        for w in t["wolves"]:
            if w != t["me"] and w in t["alive"] and tally.get(w, 0.0) >= max(2.0, self.BUS_SHARE * voters) \
                    and tally[w] >= max(tally.values()):
                return w
        return None

    def _fake_in_danger(self, t: dict, f: int) -> bool:
        """Our fake seer `f` may be voted out today: a live real claimer contradicts them, or the record already
        exposes them (a seat they called a wolf died a villager, or the real seer died and was revealed)."""
        v = t["fake"].get(f)
        if v is None:
            return False
        if t["real"]:
            return True
        if any(t["dead"].get(s) in ("villager", "seer") for s in v["calls"]):
            return True
        return any(r == "seer" for r in t["dead"].values())

    def _hedge(self, t: dict, non: list[int]) -> int | None:
        """HEDGE: the partner of an endangered fake seer does not vote the fake seer's target (a good villager
        reading the record suspects whoever voted with a wolf revealed later): bus the fake seer, or split off."""
        if not self.HEDGE:
            return None
        me = t["me"]
        partner = next((w for w in t["wolves"] if w != me and w in t["alive"]), None)

        def danger(w):
            return (w in t["fake"] and self._fake_in_danger(t, w)) or any(w in v["wolves"] for v in t["real"].values())
        if partner is None or not danger(partner) or danger(me):
            return None
        if self.HEDGE == "bus":
            return partner
        calls = {s for v in t["fake"].values() for s in v["calls"]}
        calls |= {c for c, v in t["real"].items() if v["wolves"] & t["wolves"]}
        pool = [s for s in non if s not in calls]
        if not pool:
            return None
        tally = self._forecast(t)
        rng = self._tiebreak(t, "hedge")
        return max(pool, key=lambda s: (tally.get(s, 0.0), t["pressure"].get(s, 0.0), rng.random()))

    def _vote_target(self, t: dict) -> int | None:
        non = [s for s in t["alive"] if s not in t["wolves"]]
        if not non:
            return None
        bus = self._doomed_partner(t)
        if bus is not None:
            return bus
        hedge = self._hedge(t, non)
        if hedge is not None:
            return hedge
        for c, v in sorted(t["fake"].items(), key=lambda kv: kv[1]["order"]):
            called = [s for s in v["calls"] if s in non]
            if called:
                return called[-1] if self.VOTE_LATEST else called[0]
        for c, v in sorted(t["real"].items(), key=lambda kv: kv[1]["order"]):
            if v["wolves"] & t["wolves"] or t["fake"]:
                return c
        tally = defaultdict(float)
        for s, tgt in t["intent"].items():
            if s not in t["wolves"]:
                tally[tgt] += 1.0
        rng = self._tiebreak(t, "vote")
        lean = [s for s in non if tally.get(s)]
        if lean:
            return max(lean, key=lambda s: (tally[s], t["pressure"].get(s, 0.0), rng.random()))
        return max(non, key=lambda s: (t["heat"].get(s, 0.0), t["pressure"].get(s, 0.0), rng.random()))

    def _kill_target(self, t: dict, targets: list[int]) -> int | None:
        if not targets:
            return None
        rng = self._tiebreak(t, "kill")
        seers = [c for c in t["real"] if c in targets]
        leaders = [s for s in targets if t["leader"].get(s)]
        lone = len([w for w in t["wolves"] if w in t["alive"]]) == 1
        if len(leaders) < self.LEADER_MIN and not (self.LONE_LEADER and lone):
            leaders = []                      # one organiser is not a bloc (and splits the village): keep him
        called = [c for c in seers if t["real"][c]["wolves"] & t["wolves"]]
        if leaders and (self.KILL == "leader_first" or (self.KILL == "seer_unless_called" and called)
                        or (self.KILL == "seer_first" and not seers and self.LEADER_KILL)):
            return max(leaders, key=lambda s: (t["leader"][s], rng.random()))
        if seers:
            return min(seers, key=lambda c: (-len(t["real"][c]["wolves"] & t["wolves"]), t["real"][c]["order"]))
        return max(targets, key=lambda s: (t["heat"].get(s, 0.0) - 0.5 * t["pressure"].get(s, 0.0), rng.random()))

    # ---- decisions -------------------------------------------------------------------------------------------
    def choose(self, raw: dict) -> str:
        wolf, allies = self._is_wolf(raw)
        if not wolf:
            return super().choose(raw)
        self._wread(raw)
        legal = _legal(raw)
        targets = [int(a) for a in legal if a != str(ABSTAIN)]
        t = self._table(raw, allies)
        sub = (raw.get("game_state") or {}).get("phase")
        if sub == "night_wolf":
            pick = self._kill_target(t, [x for x in targets if x not in t["wolves"]])
        elif self.STYLE == "adaptive":
            pick = self._vote_adaptive(t)
        else:
            pick = self._vote_target(t)
        if pick is not None and str(pick) in legal:
            return str(pick)
        pool = [a for a in legal if a != str(ABSTAIN) and int(a) not in t["wolves"]]
        return (pool or legal)[0]

    def message(self, raw: dict):
        wolf, allies = self._is_wolf(raw)
        if not wolf:
            return super().message(raw)
        self._wread(raw)
        t = self._table(raw, allies)
        day = t["day"]
        self.turns[day] += 1
        if self.turns[day] > self.MAX_TURNS or len(self.said[day]) >= 4:
            return None
        said = self.said[day]
        me = t["me"]
        partner = [a for a in allies if a in t["alive"]]
        say = self._say_adaptive if self.STYLE == "adaptive" else self._say
        out = say(t, said, me, partner)
        if out is None:
            return SIM_HOLD                    # keep listening: answer a later claim
        tag, text, rec = out
        said.append(tag)
        return {"content": text, "recipients": rec}

    # ---- adaptive style: blend in with the table's own choice, lie only to redirect a vote off a wolf --------
    def _forecast(self, t: dict) -> dict[int, float]:
        """Today's announced votes of the non-wolves (their last stated target)."""
        tally = defaultdict(float)
        for s, tgt in t["intent"].items():
            if s not in t["wolves"] and tgt in t["alive"]:
                tally[tgt] += 1.0
        return tally

    def _vote_adaptive(self, t: dict) -> int | None:
        me, wolves = t["me"], t["wolves"]
        non = [s for s in t["alive"] if s not in wolves]
        if not non:
            return None
        for c, v in sorted(t["fake"].items(), key=lambda kv: kv[1]["order"]):
            called = [s for s in v["wolves"] if s in non]
            if called:
                return called[-1]
        for c, v in sorted(t["real"].items(), key=lambda kv: kv[1]["order"]):
            if c in non and v["wolves"] & wolves and t["fake"]:
                return c
        tally = self._forecast(t)
        rng = self._tiebreak(t, "vote")
        voters = len([s for s in t["alive"] if s not in wolves])
        top = max(tally, key=lambda s: (tally[s], rng.random()), default=None)
        if top is not None and top in wolves and top != me and self.BUS and tally[top] >= max(2.0, voters - 1):
            return top                                   # the partner is gone anyway: vote with the village
        lean = [s for s in non if tally.get(s)]
        if lean:
            return max(lean, key=lambda s: (tally[s], t["pressure"].get(s, 0.0), rng.random()))
        return max(non, key=lambda s: (t["heat"].get(s, 0.0), t["pressure"].get(s, 0.0), rng.random()))

    def _say_adaptive(self, t: dict, said: list[str], me: int, partner: list[int]):
        day, wolves = t["day"], t["wolves"]
        turn = self.turns[day]
        hits = [(c, v) for c, v in sorted(t["real"].items(), key=lambda kv: kv[1]["order"])
                if v["wolves"] & wolves]
        if hits and "counter" not in said:
            c, v = hits[0]
            if me in t["fake"]:
                return "counter", f"Player{c} is lying. I am the real Seer and Player{c} is a wolf. Vote Player{c}.", []
            if self.COUNTER and not t["fake"] and (me in v["wolves"] or not partner):
                return "counter", f"I am the real Seer. Player{c} is a wolf. Vote Player{c}.", []
        if turn < self.LISTEN:
            return None
        tally = self._forecast(t)
        rng = self._tiebreak(t, "say")
        top = max(tally, key=lambda s: (tally[s], rng.random()), default=None)
        if (top is not None and top in wolves and top != me and not t["fake"] and not hits
                and "claim" not in said and self.CLAIM_D1):
            tgt = self._frame_target(dict(t, heat=defaultdict(float, {s: tally.get(s, 0.0) for s in t["alive"]})))
            if tgt is not None and tgt not in wolves:
                return "claim", f"I am the Seer. Player{tgt} is a wolf. Vote Player{tgt}.", []
        if me in t["fake"] and "result" not in said and day > t["fake"][me]["day"]:
            called = [s for s in t["fake"][me]["wolves"] if s in t["alive"] and s not in wolves]
            tgt = self._vote_adaptive(dict(t, fake={}))
            if not called and tgt is not None and tgt not in wolves:
                return "result", (f"I am the Seer. Last night I checked Player{tgt}. Player{tgt} is a wolf. "
                                  f"Vote Player{tgt}."), []
        return None

    def _say(self, t: dict, said: list[str], me: int, partner: list[int]):
        day = t["day"]
        # 1. a seer (not one of us) calls one of us a wolf (or, COUNTER_ALL / COUNTER_CLEAR, claims at all):
        #    counter-claim, once
        real = sorted(t["real"].items(), key=lambda kv: kv[1]["order"])
        hits = [(c, v) for c, v in real if v["wolves"] & t["wolves"]]
        if "counter" not in said:
            if me in t["fake"] and (hits or (self.COUNTER_ALL and real)):
                c = (hits or real)[0][0]
                return "counter", f"Player{c} is lying. I am the real Seer and Player{c} is a wolf. Vote Player{c}.", []
            if self.COUNTER and not t["fake"] and (hits or (self.COUNTER_CLEAR and real)):
                c = (hits or real)[0][0]
                return "counter", f"I am the real Seer. Player{c} is a wolf. Vote Player{c}.", []
            if t["fake"] and hits and "back" not in said and self.BACK:
                f = min(t["fake"])
                return "back", f"I trust Player{f}. Vote Player{hits[0][0]}.", []
        # 2. day 1, nobody has claimed: fake-claim
        mine = (self.CLAIMER == "first" or not partner or (self.CLAIMER == "high" and me > partner[0])
                or (self.CLAIMER == "low" and me < partner[0]))
        if self.CLAIM_D1 and mine and day == 1 and not t["claims"] and "claim" not in said:
            tgt = self._frame_target(t)
            if tgt is not None:
                return "claim", f"I am the Seer. Player{tgt} is a wolf. Vote Player{tgt}.", []
        # 3. our fake seer is alive: keep producing results / back them
        if me in t["fake"] and "result" not in said and day > t["fake"][me]["day"]:
            tgt = self._vote_target(dict(t, fake={}))
            called = [s for s in t["fake"][me]["wolves"] if s in t["alive"] and s not in t["wolves"]]
            if not called and tgt is not None:
                return "result", (f"I am the Seer. Last night I checked Player{tgt}. Player{tgt} is a wolf. "
                                  f"Vote Player{tgt}."), []
        if self.BACK and partner and partner[0] in t["fake"] and "back" not in said \
                and self._doomed_partner(t) is None:
            v = t["fake"][partner[0]]
            called = [s for s in v["calls"] if s in t["alive"] and s not in t["wolves"]]
            if called:
                return "back", f"I trust Player{partner[0]}. Vote Player{called[-1]}.", []
        # 4. tell the partner tonight's target (private channel)
        if partner and "dm" not in said and len(said) >= 1:
            k = self._kill_target(t, [s for s in t["alive"] if s not in t["wolves"]])
            if k is not None:
                return "dm", f"Kill Player{k} tonight.", [partner[0]]
        # 5. push the vote
        if "push" not in said and (said or self.turns[day] >= 2):
            tgt = self._vote_target(t)
            if tgt is not None:
                return "push", f"I think Player{tgt} is suspicious. I'll vote Player{tgt}.", []
        return None


class _V0Seat(WolfBrain, LLMQwenSeat):
    """The brain's defaults (iteration 1, v0 + fixes): the base the experiment variants below are written against."""
    name = "wolfpack_v0"


class _Best:
    """The shipped configuration (iteration 5, `wp5`): frame the organiser, counter-claim every real seer claim,
    kill an organiser only against a bloc of them (2+) or once a wolf is alone."""
    LEADER_KILL = True
    FRAME = "leader"
    COUNTER_ALL = True
    LEADER_MIN = 2
    LONE_LEADER = True


class WolfpackSeat(_Best, WolfBrain, LLMQwenSeat):
    """`llmq` villagers and seer, wolfpack wolves."""
    name = "wolfpack"


class WolfpackHSeat(_Best, WolfBrain, HeuristicSeat):
    """`heuristic` villagers and seer (deaf to chat), wolfpack wolves."""
    name = "wolfpack_h"


KINDS = {"wolfpack": WolfpackSeat, "wolfpack_h": WolfpackHSeat}


def _variant(base, name: str, **kw):
    return type(f"Variant_{name}", (base,), dict(kw, name=name))


# experiment variants for the runner (not registered with eval_werewolf): name -> seat class, written against the
# v0 defaults so every iteration of docs stays reproducible
VARIANTS = {
    "wolfpack_v0": _V0Seat,
    "wp_noback": _variant(_V0Seat, "wp_noback", BACK=False),
    "wp_high": _variant(_V0Seat, "wp_high", CLAIMER="high"),
    "wp_low": _variant(_V0Seat, "wp_low", CLAIMER="low"),
    "wp_lkill": _variant(_V0Seat, "wp_lkill", LEADER_KILL=True),
    "wp_leader": _variant(_V0Seat, "wp_leader", KILL="leader_first"),
    "wp_suc": _variant(_V0Seat, "wp_suc", KILL="seer_unless_called"),
    "wp_nlf": _variant(_V0Seat, "wp_nlf", FRAME="nonleader"),
    "wp_noclaim": _variant(_V0Seat, "wp_noclaim", CLAIM_D1=False),
    "wp_nocounter": _variant(_V0Seat, "wp_nocounter", COUNTER=False),
    "wp_ad": _variant(_V0Seat, "wp_ad", STYLE="adaptive", FRAME="nonleader"),
    "wp_ad_lk": _variant(_V0Seat, "wp_ad_lk", STYLE="adaptive", FRAME="nonleader", LEADER_KILL=True),
    "wp_ad_nobus": _variant(_V0Seat, "wp_ad_nobus", STYLE="adaptive", FRAME="nonleader", BUS=False),
    "wp_ad_l1": _variant(_V0Seat, "wp_ad_l1", STYLE="adaptive", FRAME="nonleader", LISTEN=1),
}
V3 = dict(LEADER_KILL=True, FRAME="leader")
for _name, _kw in {"wp3": {}, "wp3_bus": {"BUS_LOUD": True}, "wp3_ca": {"COUNTER_ALL": True},
                   "wp3_nolk": {"LEADER_KILL": False}, "wp3_lk1": {"KILL": "leader_first"},
                   "wp3_high": {"CLAIMER": "high"}, "wp3_noback": {"BACK": False}}.items():
    VARIANTS[_name] = _variant(_V0Seat, _name, **dict(V3, **_kw))
V4 = dict(LEADER_KILL=True, FRAME="leader", COUNTER_ALL=True, LEADER_MIN=2)
for _name, _kw in {"wp4": {}, "wp4_lk1": {"KILL": "leader_first"}, "wp4_cc": {"COUNTER_CLEAR": True},
                   "wp4_m1": {"LEADER_MIN": 1}, "wp4_noback": {"BACK": False}, "wp4_nolk": {"LEADER_KILL": False}}.items():
    VARIANTS[_name] = _variant(_V0Seat, _name, **dict(V4, **_kw))
V5 = dict(V4, LONE_LEADER=True)
for _name, _kw in {"wp5": {}, "wp5_noca": {"COUNTER_ALL": False}, "wp5_bus": {"BUS_LOUD": True},
                   "wp5_high": {"CLAIMER": "high"}}.items():
    VARIANTS[_name] = _variant(_V0Seat, _name, **dict(V5, **_kw))
for _name, _kw in {"wp6_bus": {"HEDGE": "bus"}, "wp6_split": {"HEDGE": "split"}}.items():
    VARIANTS[_name] = _variant(_V0Seat, _name, **dict(V5, **_kw))
V2 = dict(LEADER_KILL=True, FRAME="nonleader", BUS_LOUD=True, COUNTER_ALL=True)
for _name, _kw in {"wp2": {}, "wp2_high": {"CLAIMER": "high"}, "wp2_nobus": {"BUS_LOUD": False},
                   "wp2_cc": {"COUNTER_CLEAR": True}, "wp2_first": {"VOTE_LATEST": False},
                   "wp2_nolk": {"LEADER_KILL": False}, "wp2_bus3": {"BUS_SHARE": 0.34},
                   "wp2_noca": {"COUNTER_ALL": False}, "wp2_noback": {"BACK": False}}.items():
    VARIANTS[_name] = _variant(_V0Seat, _name, **dict(V2, **_kw))


def seat_class(kind: str):
    return KINDS.get(kind) or VARIANTS.get(kind)


# ---------------------------------------------------------------- paired red-team runner
def _wilson(w: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def _mci(xs: list[float], z: float = 1.96) -> tuple[float, float, float]:
    n = len(xs)
    if not n:
        return 0.0, 0.0, 0.0
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / max(1, n - 1)
    h = z * math.sqrt(v / n)
    return m, m - h, m + h


def play(spec: str, seed: int, g: int, bg: str, k: int) -> dict:
    """One game on the `llmq` deal of (seed, g, k) -- the same roles and our seats for every background (paired) --
    with every background seat played by `bg`."""
    from .eval_werewolf import deal, factory, make_seat
    from .werewolf_env import ContestantSeat, Game, deal_roles
    gseed, ours, _types = deal(seed, g, "llmq", k)
    roles = deal_roles(random.Random(gseed))
    cls = seat_class(bg)

    def ours_seat():
        if spec.startswith("bg:"):                 # control: a reference bot in our seats on the same deal
            c = seat_class(spec[3:])
            return c() if c else make_seat(spec[3:])
        return ContestantSeat(factory(spec))
    seats = [ours_seat() if i in ours else (cls() if cls else make_seat(bg)) for i in range(7)]
    game = Game(seats, random.Random(gseed))
    ret = game.play()
    wolves = [i for i in range(7) if roles[i] == "wolf"]
    ours_set = set(ours)
    agent_votes = [(v["day"], s, t) for v in game.votes for s, t in v["votes"].items() if s in ours_set]
    death = {d["seat"]: d for d in game.dead}
    claims = defaultdict(set)
    for m in game.messages:
        if not m["recipients"] and LLMSeat._meaning(m["content"], m["sender"])[0]:
            claims[m["day"]].add(m["sender"])
    return {"g": g, "roles": [roles[i] for i in ours], "won": ret[ours[0]] > 0 if ours else None,
            "winner": game.winner, "days": game.day,
            "wolves_bg": all(w not in ours_set for w in wolves),
            "votes": [(d, t is not None and roles[t] == "wolf", t is None) for d, s, t in agent_votes
                      if roles[s] != "wolf"],
            "our_death": [(death[i]["cause"], death[i]["day"]) if i in death else None for i in ours],
            "d1_elim_role": next((roles[v["eliminated"]] if v["eliminated"] is not None else "none"
                                  for v in game.votes if v["day"] == 1), "none"),
            "d1_claims": len(claims.get(1, ())),
            "fails": {key: sum(game.stats[key][i] for i in ours)
                      for key in ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate")},
            "bg_fails": {key: sum(game.stats[key][i] for i in range(7) if i not in ours_set)
                         for key in ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate")}}


def _chunk(args):
    spec, seed, bg, k, games, timing = args
    os.environ["ARENA_RECORD"] = "0"
    if timing:
        os.environ["ARENA_WW_TIMING"] = timing
    return [(bg, k, play(spec, seed, g, bg, k)) for g in games]


def report(res: dict, bgs: list[str], ks: list[int], base: str) -> None:
    def pct(x):
        return f"{100 * x:5.1f}"
    for k in ks:
        print(f"\n== k={k} (our seats per table); rate % [Wilson 95%] (n)")
        for bg in bgs:
            recs = res.get((bg, k), {})
            if not recs:
                continue
            parts = []
            for role in ("villager", "seer", "wolf"):
                xs = [1.0 if r["won"] else 0.0 for r in recs.values() if role in r["roles"]]
                if xs:
                    lo, hi = _wilson(sum(xs), len(xs))
                    parts.append(f"{role} {pct(sum(xs) / len(xs))} [{pct(lo)},{pct(hi)}] ({len(xs)})")
            vs = [1.0 if r["won"] else 0.0 for r in recs.values() if r["wolves_bg"]]
            lo, hi = _wilson(sum(vs), len(vs))
            parts.append(f"VS-BG-WOLVES {pct(sum(vs) / max(1, len(vs)))} [{pct(lo)},{pct(hi)}] ({len(vs)})")
            print(f"  {bg:11s} " + " | ".join(parts))
        print("  diagnostics (agent on the villager side, both wolves background):")
        for bg in bgs:
            recs = [r for r in res.get((bg, k), {}).values() if r["wolves_bg"]]
            if not recs:
                continue
            v1 = [hit for r in recs for d, hit, ab in r["votes"] if d == 1 and not ab]
            v2 = [hit for r in recs for d, hit, ab in r["votes"] if d >= 2 and not ab]
            ab = [a for r in recs for d, hit, a in r["votes"]]
            lyn = [1.0 if any(x and x[0] == "vote" for x in r["our_death"]) else 0.0 for r in recs]
            nk = [1.0 if any(x and x[0] == "night_kill" for x in r["our_death"]) else 0.0 for r in recs]
            d1 = defaultdict(int)
            for r in recs:
                d1[r["d1_elim_role"]] += 1
            n = len(recs)
            print(f"  {bg:11s} agent vote hits wolf: d1 {pct(sum(v1) / max(1, len(v1)))} d2+ "
                  f"{pct(sum(v2) / max(1, len(v2)))} abstain {pct(sum(ab) / max(1, len(ab)))} | agent voted out "
                  f"{pct(sum(lyn) / n)} killed {pct(sum(nk) / n)} | d1 out: "
                  + " ".join(f"{x} {pct(d1[x] / n)}" for x in ("wolf", "seer", "villager", "none"))
                  + f" | days {sum(r['days'] for r in recs) / n:.2f} | d1 claims "
                  f"{sum(r['d1_claims'] for r in recs) / n:.2f}")
        if base in bgs:
            b = res.get((base, k), {})
            for bg in bgs:
                if bg == base:
                    continue
                a = res.get((bg, k), {})
                common = sorted(set(a) & set(b))
                for label, sel in (("all", lambda r: True), ("vs-bg-wolves", lambda r: r["wolves_bg"])):
                    diffs = [(1.0 if a[g]["won"] else 0.0) - (1.0 if b[g]["won"] else 0.0) for g in common
                             if sel(b[g])]
                    m, lo, hi = _mci(diffs)
                    print(f"  paired {bg}-{base} [{label}]: {100 * m:+.1f} [{100 * lo:+.1f},{100 * hi:+.1f}] "
                          f"(n={len(diffs)})")
    fails = defaultdict(int)
    bgf = defaultdict(int)
    for recs in res.values():
        for r in recs.values():
            for key, x in r["fails"].items():
                fails[key] += x
            for key, x in r["bg_fails"].items():
                bgf[key] += x
    print(f"\nfailures: ours {dict(fails)}; background {dict(bgf)}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=2100)
    ap.add_argument("--ks", default="1,3")
    ap.add_argument("--bgs", default="llmq,heuristic,wolfpack,wolfpack_h")
    ap.add_argument("--base", default="llmq", help="paired comparison baseline background")
    ap.add_argument("--agent", default="agent", help="agent spec, or bg:<kind> for a reference bot in our seats")
    ap.add_argument("--timing", default="", help="ARENA_WW_TIMING for the games (rounds | rt)")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--chunk", type=int, default=250)
    a = ap.parse_args(argv)
    os.environ["ARENA_RECORD"] = "0"
    ks = [int(x) for x in a.ks.split(",") if x]
    bgs = [x for x in a.bgs.split(",") if x]
    tasks = [(a.agent, a.seed, bg, k, list(range(lo, min(a.games, lo + a.chunk))), a.timing)
             for bg in bgs for k in ks for lo in range(0, a.games, a.chunk)]
    res: dict = defaultdict(dict)
    if a.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(a.jobs) as ex:
            for out in ex.map(_chunk, tasks):
                for bg, k, r in out:
                    res[(bg, k)][r["g"]] = r
    else:
        for tk in tasks:
            for bg, k, r in _chunk(tk):
                res[(bg, k)][r["g"]] = r
    print(f"agent={a.agent} seed={a.seed} games={a.games} timing={a.timing or 'rounds'}", file=sys.stderr)
    report(res, bgs, ks, a.base)


if __name__ == "__main__":
    main()
