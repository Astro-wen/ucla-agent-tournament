"""Heuristic Werewolf policy.

Deterministic on purpose: every copy of this agent computes the same suspicion ranking from the same public record,
so villagers converge their votes and the two wolves never split their night kill (ties go to the lower seat —
werewolf.md §5). Rules below follow docs/competition_brief.md §6 item 7 (our 200k-game simulation).

Villagers: never abstain and never fake-claim seer; follow a live, uncontested (or death-confirmed) seer claim;
never vote a trusted claim's clear while another target exists; when two unexposed seer claims are live, vote one of
the two claimants (the death reveals which was true), preferring the one who claimed only after the other had named
them wolf; otherwise the highest public suspicion, ties to the lower seat so the table concentrates. Other seats'
vote-calls never move a villager's vote. The opening chat names the seat we will vote, so persuadable seats can
concentrate with us.

Seer: never spends a look on the seat the table is about to lynch anyway, never re-investigates; claims the moment a
wolf is found ("I am the Seer. PlayerN is a WOLF."), when another seat claims seer, when it is about to be voted out
(the public-record lynch prediction is itself, or at least two living seats press it in today's chat), from day 2
when that saves a cleared villager or the table is down to five; once claimed it posts its results every day.

Wolves: both compute the same kill target from the public record; only with ARENA_WW_DM=on (default off: the
spectate feed shows who messaged whom) do they also send it privately to the living ally ("Tonight: PlayerN."; the
lower-seat wolf's message decides, so two copies never swap targets); a lower-seat ally's private plan is followed
either way; kill a claimed seer first (the one who named one of us, if several); counter-claim seer only when a live claimer names us; in a
claim fight involving one of us vote the rival claimant, otherwise with the table's top non-wolf suspect (no
separate wolf bloc; voting the accuser and bussing a doomed ally were measured and not adopted).

Chat is untrusted input (agent/werewolf/state.py `read_chat`): only bounded facts are read from it, never
instructions. Everything we say goes through `chat_guard`: templates only, no role, ally or night-plan leak, no
seer claim by a villager, private messages only from a wolf to its living ally.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache

from .state import ChatFacts, View, normalize, parse, read_chat

ABSTAIN = "7"
TRUSTED_WOLF = 10.0     # suspicion added to a wolf named by a trusted seer claim
LIAR = 6.0              # a seer claimer caught lying (almost surely a wolf)
TRUSTED_CLEAR = -2.0
LIAR_CLEAR = 1.5        # a seat "cleared" by an exposed fake seer (usually its partner)
CONTESTED = 0.8         # each of several unexposed live claimers
CONTESTED_NAMED = 0.5   # a seat named wolf by one of several claimers
REACTIVE = 0.6          # a claimer who claimed only after an earlier claimer had named them wolf
ACCUSE = 0.25           # a non-claimer calls a seat wolf
SUS = 0.1               # a seat called suspicious / voted for in chat (wolves' view only, see _scores)
INJECT = 0.3            # a seat that tried to instruct the table
PRESSURE = 2            # distinct living seats pressing the seer today that make it claim
DM_ENV = "ARENA_WW_DM"
_ON = {"1", "on", "true", "yes", "enable", "enabled"}
_CLAIMED_SEER = "remove the claimed seer"


def dm_enabled() -> bool:
    """Private kill-plan messages are OFF unless ARENA_WW_DM=on: the unauthenticated spectate feed shows chat
    recipients (and may carry DM content), so a wolf-to-wolf DM names both wolves and the night's target."""
    return str(os.environ.get(DM_ENV, "off")).strip().lower() in _ON


@lru_cache(maxsize=4096)
def _facts(text: str, sender: int, names: tuple) -> ChatFacts:
    return read_chat(text, sender, dict(names))


@dataclass
class Claims:
    """Seer claims read from the broadcast transcript."""
    wolves: dict[int, set[int]] = field(default_factory=dict)   # claimer -> seats they called wolf
    clears: dict[int, set[int]] = field(default_factory=dict)   # claimer -> seats they called villager
    order: list[int] = field(default_factory=list)              # claimers in order of first claim
    liars: set[int] = field(default_factory=set)
    trusted: int | None = None                                   # the one claimer the table should follow
    reactive: set[int] = field(default_factory=set)              # claimed only after being named wolf by a claimer

    def trusted_wolves(self, alive) -> list[int]:
        if self.trusted is None:
            return []
        return sorted(s for s in self.wolves.get(self.trusted, ()) if s in alive and s != self.trusted)

    def trusted_clears(self) -> set[int]:
        if self.trusted is None:
            return set()
        return set(self.clears.get(self.trusted, ())) | {self.trusted}

    def live(self, v: View) -> list[int]:
        """Unexposed claimers still alive, in claim order."""
        return [s for s in self.order if s in v.alive and s not in self.liars]


def _chats(v: View):
    """(seq, sender, facts, day) for every broadcast chat, in order. Direct messages are never read here."""
    names = tuple(sorted(v.names.items()))
    for i, m in enumerate(v.messages):
        if not isinstance(m, dict) or m.get("type", "chat") != "chat" or m.get("visibility") == "direct":
            continue
        if m.get("recipients"):
            continue
        sender = m.get("sender")
        if not isinstance(sender, int) or isinstance(sender, bool) or not 0 <= sender <= 6:
            continue
        text = m.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        yield m.get("seq", i), sender, _facts(text, sender, names), m.get("_day", m.get("day"))


class WerewolfPolicy:
    """One instance per match (the starter runs each match in its own process)."""

    reactive_chat = True        # chat() may be asked again after its messages were sent (agent.Contestant)

    def __init__(self):
        self.transcript: dict = {}          # seq -> message envelope (+ "_day": the day it was first seen)
        self._said_day = None
        self._topics: set[str] = set()      # what we already said today
        self._dm_target: int | None = None  # the kill target we sent our ally
        self._dm_day = None

    # ---- helpers -------------------------------------------------------
    def observe(self, st: dict) -> None:
        if not isinstance(st, dict):
            return
        gs = st.get("game_state")
        day = gs.get("day") if isinstance(gs, dict) else None
        msgs = st.get("new_messages")
        for m in (msgs or []) if isinstance(msgs, list) else []:
            if not isinstance(m, dict):
                continue
            key = m.get("seq", m.get("index"))
            if key is None or isinstance(key, (dict, list, set)):
                continue
            if key not in self.transcript:
                m = dict(m)
                m["_day"] = m["day"] if isinstance(m.get("day"), int) else day
                self.transcript[key] = m

    def _view(self, st: dict) -> View:
        self.observe(st)
        keys = sorted(self.transcript, key=lambda k: (not isinstance(k, (int, float)), k if isinstance(k, (int, float)) else str(k)))
        return parse(st, [self.transcript[k] for k in keys])

    @staticmethod
    def seer_claims(v: View) -> dict[int, list[int]]:
        """Broadcast seer claims: claimer seat -> seats they called wolf."""
        c = WerewolfPolicy.claims(v, private=False)
        return {s: sorted(c.wolves.get(s, ())) for s in c.order}

    @staticmethod
    def claims(v: View, private: bool = True) -> Claims:
        """Parse seer claims and decide which (if any) the table should trust.

        `private`: also use what this seat privately knows as a villager/seer
        (I am not a wolf; my own investigations; I am the only real seer)."""
        c = Claims()
        named_by_claimer: set[int] = set()           # seats some earlier claimer has already called wolf
        for _seq, sender, f, _day in _chats(v):
            if f.injection:
                continue
            if not f.claim:
                if sender in c.wolves:                # a claimer's later messages keep reporting results
                    c.wolves[sender] |= f.wolves
                    c.clears[sender] |= f.clears - f.wolves
                    named_by_claimer |= f.wolves
                continue
            if sender not in c.wolves:
                c.wolves[sender], c.clears[sender] = set(), set()
                c.order.append(sender)
                if sender in named_by_claimer:
                    c.reactive.add(sender)
            c.wolves[sender] |= f.wolves
            c.clears[sender] |= f.clears - f.wolves
            named_by_claimer |= f.wolves
        dead_wolves = sum(1 for r in v.dead.values() if r == "wolf")
        for s in c.order:
            wolves, clears = c.wolves[s], c.clears[s]
            if v.dead.get(s) in ("wolf", "villager"):
                c.liars.add(s)          # died and was not the seer
            elif any(v.dead.get(t) not in (None, "wolf", "?") for t in wolves):
                c.liars.add(s)          # called a revealed villager/seer a wolf
            elif any(v.dead.get(t) == "wolf" for t in clears):
                c.liars.add(s)          # cleared a revealed wolf
            elif len(wolves) > 2 or len([t for t in wolves if v.dead.get(t) != "wolf"]) > 2 - dead_wolves:
                c.liars.add(s)          # named more wolves than there are
            if private and v.role in ("villager", "seer"):
                if v.me in wolves:
                    c.liars.add(s)      # I know I am not a wolf
                if v.role == "seer" and s != v.me:
                    c.liars.add(s)      # I am the seer; anyone else claiming it is lying
                if v.role == "seer":
                    inv = v.investigations
                    if any(inv.get(t) == "villager" for t in wolves) or any(inv.get(t) == "wolf" for t in clears):
                        c.liars.add(s)
        confirmed = [s for s in c.order if v.dead.get(s) == "seer"]
        if confirmed:
            c.trusted = confirmed[0]
            c.liars |= {s for s in c.order if s != c.trusted}
        else:
            live = [s for s in c.order if s not in c.liars and s in v.alive]
            if len(live) == 1:
                c.trusted = live[0]
        return c

    @staticmethod
    def _scores(v: View, private: bool = True) -> dict[int, float]:
        """Public-record suspicion of every living seat (including me)."""
        score: dict[int, float] = defaultdict(float)
        for day, votes, elim in v.votes:
            elim_role = v.dead.get(elim) if elim is not None else None
            for voter, tgt in votes.items():
                if tgt is None:
                    score[voter] += 0.3 if elim is None else 0.1   # abstaining feeds the stall rule
                elif elim is not None and tgt == elim:
                    score[voter] += 1.0 if elim_role in ("villager", "seer") else -0.7
                elif v.dead.get(tgt) in ("villager", "seer") and v.dead_cause.get(tgt) == "night_kill":
                    score[voter] += 0.2   # voted someone the wolves later killed -> slightly suspicious
        c = WerewolfPolicy.claims(v, private=private)
        claimers = set(c.order)
        # Vote-calls ("I'll vote PlayerX", "p3 is sus") are the easiest way to steer a table. Villagers and the seer
        # are not moved by them (measured: +1.5 villager / +0.8 seer win rate, most in tables of silent bots); wolves
        # use them to vote with the crowd (measured: +1.5 wolf win rate).
        sus_w = SUS if v.role == "wolf" else 0.0
        for _seq, sender, f, _day in _chats(v):
            if f.injection:
                score[sender] += INJECT
                continue
            if sender in claimers:
                continue                  # claim results are weighed below
            for s in f.wolves:
                score[s] += ACCUSE
            for s in f.sus - f.wolves:
                score[s] += sus_w
        live_claimers = [s for s in c.order if s not in c.liars and (s in v.alive or v.dead.get(s) == "seer")]
        for s in c.liars:
            score[s] += LIAR
            if s != c.trusted:
                for t in c.clears.get(s, ()):
                    score[t] += LIAR_CLEAR
        if c.trusted is not None:
            for t in c.wolves.get(c.trusted, ()):
                score[t] += TRUSTED_WOLF
            for t in c.trusted_clears():
                score[t] += TRUSTED_CLEAR
        else:
            for s in live_claimers:       # contested: at least one of them is lying
                score[s] += CONTESTED + (REACTIVE if s in c.reactive else 0.0)
                for t in c.wolves.get(s, ()):
                    score[t] += CONTESTED_NAMED
        return {s: score[s] for s in v.alive}

    @staticmethod
    def suspicion(v: View, exclude: set[int] = frozenset()) -> dict[int, float]:
        """Suspicion of every living seat except `exclude` and me."""
        sc = WerewolfPolicy._scores(v, private=True)
        return {s: x for s, x in sc.items() if s != v.me and s not in exclude}

    @staticmethod
    def predicted_lynch(v: View) -> int | None:
        """The seat an information-less table of copies of us would lynch today."""
        sc = WerewolfPolicy._scores(v, private=False)
        return WerewolfPolicy._top(sc, list(sc))

    @staticmethod
    def pressure_on(v: View, seat: int) -> int:
        """Distinct living seats that called `seat` wolf / suspicious / vote-worthy in today's chat."""
        who = set()
        for _seq, sender, f, day in _chats(v):
            if day == v.day and sender != seat and sender in v.alive and not f.injection and seat in (f.wolves | f.sus):
                who.add(sender)
        return len(who)

    @staticmethod
    def _top(scores: dict[int, float], pool: list[int]) -> int | None:
        pool = [s for s in pool if s in scores]
        if not pool:
            return None
        return max(pool, key=lambda s: (scores[s], -s))   # tie -> lower seat, identical across copies

    @staticmethod
    def _legal(st: dict) -> dict[str, dict]:
        la = (st.get("legal_actions") if isinstance(st, dict) else None) or {}
        acts = la.get("actions", []) if isinstance(la, dict) else la
        return {str(a["action_id"]): a for a in acts or [] if isinstance(a, dict) and "action_id" in a}

    # ---- seer knowledge ------------------------------------------------
    @staticmethod
    def seer_knowledge(v: View) -> tuple[list[int], list[int]]:
        """(alive wolves I know or can deduce, alive seats I cleared)."""
        inv = v.investigations
        others = [s for s in v.alive if s != v.me]
        wolves = [s for s in others if inv.get(s) == "wolf"]
        clears = [s for s in others if inv.get(s) == "villager"]
        unknown = [s for s in others if s not in inv]
        left = v.wolves_left - len(wolves)
        if unknown and 0 < left and left >= len(unknown):
            wolves += unknown                          # every unread survivor must be a wolf
        return sorted(wolves), sorted(clears)

    def _investigate(self, v: View, targets: list[int]) -> tuple[int | None, str]:
        unread = [t for t in targets if t not in v.investigations]
        if not unread:
            return (min(targets) if targets else None), "nothing left to read"
        c = self.claims(v, private=True)
        pool = [t for t in unread if t not in c.liars] or unread     # fake seers are wolves already
        lynch = self.predicted_lynch(v)
        pool = [t for t in pool if t != lynch] or pool                # the lynch resolves that seat anyway
        sus = self._scores(v, private=True)
        influence: dict[int, float] = defaultdict(float)
        for _seq, sender, _f, _day in _chats(v):
            influence[sender] += 1.0
        # Most suspicious first; then the most influential; then the seat the
        # default (lowest-seat) lynch/kill order reaches last, i.e. the one the
        # table would otherwise never resolve.
        pick = max(pool, key=lambda s: (round(sus.get(s, 0.0), 3), min(influence[s], 3.0), s))
        return pick, "investigate a likely wolf the table is not already lynching"

    # ---- decisions -----------------------------------------------------
    def act(self, st: dict) -> dict:
        try:
            v = self._view(st)
            legal = self._legal(st)
            targets = sorted(int(a) for a in legal if a != ABSTAIN and a.isdigit())
            pick: int | None = None
            why = ""
            if v.sub_phase == "night_wolf":
                pick, why = self._wolf_kill(v, targets)
            elif v.sub_phase == "night_seer":
                pick, why = self._investigate(v, targets)
            elif v.sub_phase == "day_vote":
                pick, why = self._day_vote(v, targets)
            if pick is None or str(pick) not in legal:
                return self.fallback(st)
            return {"action_id": str(pick), "reasoning_summary": why}
        except Exception:
            return self.fallback(st)

    def _leader_dm(self, v: View) -> int | None:
        """The kill target the living lower-seat wolf sent in the last discussion (ours or the ally's), if any."""
        allies = [a for a in v.allies if a in v.alive]
        if not allies or v.day is None:
            return None
        chat_day = v.day - 1 if v.sub_phase == "night_wolf" else v.day      # the night after day D is "day D+1"
        leader = min([v.me] + allies)
        if leader == v.me:
            return self._dm_target if self._dm_day == chat_day else None
        want = None
        for m in v.messages:
            if not isinstance(m, dict) or m.get("sender") != leader or m.get("recipients") != [v.me]:
                continue
            if m.get("_day", m.get("day")) != chat_day:
                continue
            t = normalize(m.get("content"), v.names)
            seats = [int(x) for x in re.findall(r"player([0-6])\b", t)]
            seats = [s for s in seats if s not in allies and s != v.me]
            if seats:
                want = seats[0]                 # the latest message from the leader wins
        return want

    def _wolf_kill(self, v: View, targets: list[int]) -> tuple[int | None, str]:
        pick, why = self._kill_plan(v, targets)
        if why != _CLAIMED_SEER:
            dm = self._leader_dm(v)
            if dm is not None and dm in targets:
                return dm, "coordinated kill"
        return pick, why

    def _kill_plan(self, v: View, targets: list[int]) -> tuple[int | None, str]:
        wolfset = {v.me, *v.allies}
        c = self.claims(v, private=False)
        claimers = [s for s in c.order if s in targets]
        if claimers:
            real = [s for s in claimers if c.wolves.get(s, set()) & wolfset]     # named one of us: the real seer
            pool = real or [s for s in claimers if s not in c.liars] or claimers
            return min(pool), _CLAIMED_SEER
        # Kill whoever the table trusts most (lowest suspicion) — trusted villagers steer votes.
        sus = self.suspicion(v, exclude=set(v.allies))
        pool = [t for t in targets if t in sus]
        if not pool:
            return (min(targets) if targets else None), "default"
        return min(pool, key=lambda s: (sus[s], s)), "remove the most trusted villager"

    def _day_vote(self, v: View, targets: list[int]) -> tuple[int | None, str]:
        if v.role == "seer":
            wolves, clears = self.seer_knowledge(v)
            known = [s for s in wolves if s in targets]
            if known:
                return min(known), "seer result: wolf"
            c = self.claims(v, private=True)
            liars = [s for s in sorted(c.liars) if s in targets]
            if liars:
                return liars[0], "fake seer claim"
            sus = self.suspicion(v, exclude=set(clears))
            pool = [t for t in targets if t not in clears] or targets
            return self._top(sus, pool), "highest suspicion among unread seats"
        if v.role == "wolf":
            return self._wolf_vote(v, targets)
        c = self.claims(v, private=True)
        known = [t for t in c.trusted_wolves(v.alive) if t in targets]
        if known:
            return known[0], "follow the seer: named wolf"
        sus = self.suspicion(v)
        live = [s for s in c.live(v) if s in targets]
        if c.trusted is None and len(live) >= 2:
            return self._top(sus, live), "counter-claim: vote one of the claimants"
        cleared = c.trusted_clears()
        pool = [t for t in targets if t not in cleared] or targets
        pick = self._top(sus, pool)
        return pick, "highest public suspicion (never abstain — stall rule favors wolves)"

    def _wolf_vote(self, v: View, targets: list[int]) -> tuple[int | None, str]:
        wolfset = {v.me, *v.allies}
        c = self.claims(v, private=False)
        pool = [t for t in targets if t not in wolfset]
        live = [s for s in c.order if s in v.alive and s not in c.liars]
        if any(s in wolfset for s in live):                          # a claim fight involving one of us
            rivals = [s for s in live if s in pool]
            if rivals:
                return min(rivals), "claim fight"
        sus = self.suspicion(v, exclude=set(v.allies))
        return self._top(sus, pool), "vote with the table"

    def fallback(self, st: dict) -> dict:
        legal = self._legal(st)
        non_abstain = [a for a in legal if a != ABSTAIN]
        if non_abstain:
            try:
                return {"action_id": min(non_abstain, key=int)}
            except ValueError:
                return {"action_id": non_abstain[0]}
        return {"action_id": next(iter(legal), ABSTAIN)}

    # ---- discussion ----------------------------------------------------
    def chat(self, st: dict) -> list[dict]:
        """Messages to send now (empty: nothing more to say today). May be called again after they were sent:
        each topic is said at most once a day, so repeated calls only add reactions to what others said since."""
        try:
            v = self._view(st)
            if v.day != self._said_day:
                self._said_day, self._topics = v.day, set()
            out = []
            for topic, msg in self._plan(v):
                if topic in self._topics:
                    continue
                self._topics.add(topic)
                msg = chat_guard(msg, v, topic)
                if msg is not None:
                    out.append(msg)
            return out
        except Exception:
            return []

    def _plan(self, v: View):
        """(topic, message) candidates in priority order; the first call of a day also gets the opening line."""
        first = not self._topics
        out = []
        c = self.claims(v, private=True)
        # With ARENA_WW_DM=on a wolf's first message of the day is the private kill plan (off by default: the
        # spectate feed shows the sender and recipient of every DM, which names both wolves).
        if v.role == "wolf" and dm_enabled() and "dm" not in self._topics:
            allies = [a for a in v.allies if a in v.alive]
            targets = [s for s in v.alive if s not in allies and s != v.me]
            if allies and targets:
                k, _ = self._kill_plan(v, targets)
                if k is not None:
                    self._dm_target, self._dm_day = k, v.day
                    out.append(("dm", {"content": f"Tonight: Player{k}.", "recipients": [allies[0]]}))
        primary = None
        if v.role == "seer":
            primary = self._seer_chat(v, c)
        if primary is None and v.role == "wolf":
            primary = self._wolf_chat(v)
        if primary is None and v.role in ("villager", "seer"):
            primary = self._villager_chat(v, c)
        if primary is not None:
            out.append(primary)
        elif first:
            out.append(("open", self._opening(v)))
        return out

    def _seer_chat(self, v: View, c: Claims):
        wolves, clears = self.seer_knowledge(v)
        fakes = [s for s in c.order if s in c.liars and s in v.alive and s != v.me]
        clear_txt = " ".join(f"Player{x} is clear." for x in clears[:3])
        if wolves:
            w = wolves[0]
            txt = f"I am the Seer. Player{w} is a WOLF."
            if len(wolves) > 1:
                txt += " " + " ".join(f"Player{x} is a WOLF." for x in wolves[1:3])
            if clears:
                txt += " " + clear_txt
            return "claim", {"content": txt + f" Everyone vote Player{w} today.", "recipients": []}
        if fakes:
            f = fakes[0]
            txt = f"I am the Seer. Player{f} is lying about being Seer, so Player{f} is a WOLF."
            if clears:
                txt += " " + clear_txt
            return "claim", {"content": txt + f" Vote Player{f}.", "recipients": []}
        if not clears:
            return None
        lynch = self.predicted_lynch(v)
        claimed = v.me in c.order
        pressed = self.pressure_on(v, v.me) >= PRESSURE
        late = v.day is not None and v.day >= 2 and (lynch in clears or len(v.alive) <= 5)
        if claimed or pressed or lynch == v.me or late:
            others = [s for s in v.alive if s != v.me]
            pick, _ = self._day_vote(v, others)
            txt = "I am the Seer. " + " ".join(f"Player{x} is clear." for x in clears[:4])
            txt += " Do not vote them." + (f" I vote Player{pick}." if pick is not None and pick not in clears else "")
            return "claim", {"content": txt, "recipients": []}
        return None

    def _wolf_chat(self, v: View):
        """Counter-claim seer, only when a live claimer names me."""
        c = self.claims(v, private=False)
        if v.me in c.order:
            return None
        accusers = [s for s in c.order if s in v.alive and s != v.me and v.me in c.wolves.get(s, set())]
        if not accusers:
            return None
        a = accusers[0]
        return "counter", {"content": f"I am the Seer. Player{a} is a WOLF and is lying about being Seer. Vote Player{a}.",
                           "recipients": []}

    def _villager_chat(self, v: View, c: Claims):
        known = c.trusted_wolves(v.alive)
        if known and c.trusted != v.me:
            return "follow", {"content": f"Following the Seer (Player{c.trusted}): I vote Player{known[0]}. "
                                         f"Everyone vote Player{known[0]} so we do not split.", "recipients": []}
        live = c.live(v)
        if c.trusted is None and len(live) >= 2 and v.role == "villager":
            others = [s for s in v.alive if s != v.me]
            pick, _ = self._day_vote(v, others)
            if pick is not None:
                return "contest", {"content": f"Two Seer claims: Player{live[0]} and Player{live[1]}. One is lying. "
                                              f"I vote Player{pick}; the reveal will show who lied.", "recipients": []}
        return None

    def _opening(self, v: View) -> dict:
        others = [s for s in v.alive if s != v.me]
        pick = self._day_vote(v, others)[0] if others else None
        if v.role == "wolf":
            sus = self.suspicion(v, exclude=set(v.allies))
        elif v.role == "seer":
            sus = self.suspicion(v, exclude=set(self.seer_knowledge(v)[1]))
        else:
            sus = self.suspicion(v)
        if pick is not None and sus.get(pick, 0) > 0:
            return {"content": f"My read from the vote record: Player{pick} is most suspicious. "
                               f"I'll vote Player{pick} unless someone has better info.", "recipients": []}
        if pick is not None:
            return {"content": f"No hard info yet. To avoid a split vote I'll vote Player{pick} unless the Seer "
                               "speaks up. Please don't abstain.", "recipients": []}
        return {"content": "No hard info yet. Let's not abstain — three stalled days hands it to the wolves.",
                "recipients": []}


# ---------------------------------------------------------------- outgoing chat guard
_LEAK_ANY = re.compile(r"(?i)your role|fellow wol|investigations?:|you do not know|observation|reasoning_summary")
_SELF_WOLF = re.compile(r"(?i)\b(?:i am|i'm|im|we are|we're|us)\s+(?:a\s+|the\s+|both\s+)?(?:wolf|wolves|werewol)|"
                        r"\b(?:my|our)\s+(?:fellow|partner|ally|allies|teammate|pack|kill)|\bfellow\b|\bpartner\b|"
                        r"\bally\b|\ballies\b|\btonight\b|\bkill")
_SEER_CLAIM_OUT = re.compile(r"(?i)\b(?:i am|i'm|im)\s+(?:the\s+)?(?:real\s+|true\s+)?seer\b")
_RESULT_OUT = re.compile(r"(?i)player\d+\s+is\s+(?:a\s+)?(?:wolf|clear|villager)")
_WORD_CAP = 50


def chat_guard(msg, v: View, topic: str):
    """The message if it is safe to send, else None. Backstop for the templates above; it never edits a message."""
    if not isinstance(msg, dict):
        return None
    text = msg.get("content")
    rec = msg.get("recipients") or []
    if not isinstance(text, str) or not text.strip() or len(text.split()) > _WORD_CAP or _LEAK_ANY.search(text):
        return None
    if rec:
        # private messages: only a wolf, only to its one living ally
        if v.role != "wolf" or topic != "dm" or len(rec) != 1 or rec[0] not in v.allies or rec[0] not in v.alive:
            return None
        return {"content": text, "recipients": [rec[0]]}
    if v.role == "wolf":
        if _SELF_WOLF.search(text):
            return None
        mentioned = {int(x) for x in re.findall(r"(?i)player(\d+)", text)}
        if mentioned & set(v.allies) or re.search(rf"(?i)player{v.me}\s+is\s+(?:a\s+)?wolf", text):
            return None
        if _SEER_CLAIM_OUT.search(text) and topic != "counter":
            return None
    elif v.role == "villager":
        if _SEER_CLAIM_OUT.search(text) or (_RESULT_OUT.search(text) and topic != "follow"):
            return None                                 # villagers never fake-claim or report "results"
    elif v.role == "seer" and topic != "claim":
        if _SEER_CLAIM_OUT.search(text) or _RESULT_OUT.search(text):
            return None
    return {"content": text, "recipients": []}
