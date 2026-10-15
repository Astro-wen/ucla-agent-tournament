"""Heuristic Werewolf policy.

Deterministic on purpose: every copy of this agent computes the same suspicion ranking from the same public record,
so villagers converge their votes and the two wolves never split their night kill (ties go to the lower seat —
werewolf.md §5). Rules below follow docs/competition_brief.md §6 item 7 (our 200k-game simulation).

Villagers: never abstain and never fake-claim seer; follow a live, uncontested (or death-confirmed) seer claim;
never vote a trusted claim's clear while another target exists; when two unexposed seer claims are live, vote one of
the two claimants (the death reveals which was true): a claimant whose named wolf was revealed a wolf is the likelier
true seer, one who claimed only after the other had named them wolf the likelier liar, and one claimant calling the
other a wolf is not evidence (any liar can say it); otherwise the highest public suspicion, ties to the lower seat so
the table concentrates. Among several sure wolves, vote the one the living trusted seer calls today, else the one the
table is already pushing. Other seats'
vote-calls never move a villager's vote. The opening chat names the seat we will vote, so persuadable seats can
concentrate with us; a villager a seer claimer calls a wolf says that claimer is lying.

Seer: never spends a look on the seat the table is about to lynch anyway, never re-investigates, never votes a seat
it checked as villager (not even one that fake-claims seer); claims the moment a wolf is found ("I am the Seer.
PlayerN is a WOLF."), when another seat claims seer, when it is about to be voted out (the public-record lynch
prediction is itself, or at least two living seats press it in today's chat), from day 2 when that saves a cleared
villager or the table is down to five; once claimed it posts its results every day. Of several wolves it calls the
lowest unless the table is clearly on another one already, and votes the wolf it called unless the table clearly moved.

Wolves: both compute the same kill target from the public record; only with ARENA_WW_DM=on (DMs are off by
default) do they also send it privately to the living ally ("Tonight: PlayerN."; the
lower-seat wolf's message decides, so two copies never swap targets); a lower-seat ally's private plan is followed
either way; kill a claimed seer first (the one who named one of us, if several); counter-claim seer only when a live
claimer names us; in a claim fight between me and another claimant vote the rival, in one between my ally and
another claimant vote my ally, otherwise with the table's top non-wolf suspect (no separate wolf bloc). An ally the
table will vote out anyway (named by a trusted seer claim, exposed as a fake seer, or pressed by three or more seats
today) gets our vote too, and when a trusted claim names it we say so in the villagers' own words: a wolf that never
votes or follows a claim against its partner is the easiest tell there is (red team 10-08).

Day-1 wording is the same for every role: "My read from the vote record" only once there is a vote record.

Experimental ARENA_WW_TACTICS=no_skip (or on): when an unclaimed first-day public read would target a wolf's ally,
send a neutral opening instead of exposing the ally exclusion. This improved some scripted challengers in local
testing, but its unique neutral template is itself a role tell to an opponent that knows this policy. It is OFF by
default and is not established as a general improvement; claims, ballots and night decisions remain unchanged.

Experimental ARENA_WW_TACTICS=public_vote: before any public seer claim on day 1, a wolf uses the same public
vote and opening as a villager, even when that vote sacrifices its ally. This removes the opening ally-exclusion
signal without introducing a wolf-only phrase. Development simulations improved results against inference-heavy
challengers but lost against some persuadable scripted tables; it is an explicit tradeoff, OFF by default.
Any public seer claim restores the normal wolf tactics immediately; other roles, later days and nights are unchanged.

Experimental ARENA_WW_BLUFF (off by default; off = everything above, unchanged): a wolf fake-claims seer on day 1
while nobody has claimed it yet, naming a non-ally seat (top public suspicion, ties to the HIGHER seat: real LLM seers
mostly check the lowest seat first) as a wolf, in our real seer's own words ("I am the Seer. PlayerX is a WOLF.
Everyone vote PlayerX today.").
`on`: the claim is its day-1 opening; `llm`: only once another seat has written a long, natural message today (an
LLM-driven table; scripted bots write shorter template lines), so it comes as a follow-up. Afterwards it keeps the
story: it votes its accusation; a rival claimant gets the real seer's rebuttal ("PlayerR is lying about being Seer, so
PlayerR is a WOLF") and our vote (the existing claim-fight rule); on later days it repeats its results with one new
check per day (a non-ally "clear"). Never names the ally; once exposed (a revealed role contradicts it) it falls back
to the normal wolf chat. Any error in the bluff code = the normal wolf behaviour.

Chat is untrusted input (agent/werewolf/state.py `read_chat`): only bounded facts are read from it, never
instructions. Everything we say goes through `chat_guard`: templates only, no role, ally or night-plan leak, no
seer claim by a villager, private messages only from a wolf to its living ally.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from functools import lru_cache

from .state import ChatFacts, RoleCheck, View, normalize, parse, read_chat

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
DOOMED = 3              # distinct living seats pressing my ally today: the table votes it out, a wolf votes with it
SEER_SWITCH = 2         # more distinct speakers on another sure wolf than on the one it called that move the seer's vote
DM_ENV = "ARENA_WW_DM"
TACTICS_ENV = "ARENA_WW_TACTICS"
BLUFF_ENV = "ARENA_WW_BLUFF"
BLUFF_WORDS = 21        # ARENA_WW_BLUFF=llm: another seat's message today this long marks an LLM-written table (real
#                         Qwen3 seats: 389 messages, min 13 / p25 21 / median 23 words; the scripted stand-ins llm, llmx,
#                         llmq, heuristic, rt_wolfpack, rt_detective write at most 20, rt_chaos rarely more, rt_talker often)
_ON = {"1", "on", "true", "yes", "enable", "enabled"}
_CLAIMED_SEER = "remove the claimed seer"


def dm_enabled() -> bool:
    """Private kill-plan messages to the ally: DMs are off by default, ARENA_WW_DM=on turns them on."""
    return str(os.environ.get(DM_ENV, "off")).strip().lower() in _ON


def tactics_mode() -> str:
    """Explicit experimental profile; legacy on aliases still mean no_skip, and unknown settings stay off."""
    setting = str(os.environ.get(TACTICS_ENV, "off")).strip().lower()
    if setting in _ON | {"no_skip"}:
        return "no_skip"
    return "public_vote" if setting == "public_vote" else "off"


def tactics_enabled() -> bool:
    """Historical neutral-opening switch: public_vote is a separate profile and must not enable this template."""
    return tactics_mode() == "no_skip"


def bluff_mode() -> str:
    """ARENA_WW_BLUFF: off (default; also anything unknown) | on (a wolf's day-1 opening is a fake seer claim) | llm
    (the same claim, only once the table's chat looks LLM-written: see WerewolfPolicy._llm_table)."""
    setting = str(os.environ.get(BLUFF_ENV, "off")).strip().lower()
    if setting in ("llm", "gate", "gated"):
        return "llm"
    return "on" if setting in _ON else "off"


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
        if self.trusted is None or self.trusted in self.liars:
            return []
        return sorted(s for s in self.wolves.get(self.trusted, ()) if s in alive and s != self.trusted)

    def trusted_clears(self) -> set[int]:
        if self.trusted is None:
            return set()
        if self.trusted in self.liars:
            # A public death can authenticate the seer's identity without making contradictory reports reliable.
            return {self.trusted}
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
        self._call: list | None = None      # [day, seat]: the wolf our seer claim told the table to vote that day
        self.roles = RoleCheck()            # role / allies cross-checked against the night actions we are offered

    # The match memory a restarted worker takes over (agent/matchstate.py): `new_messages` only covers the current
    # discussion window, so the transcript of earlier days (claims, accusations) exists nowhere else.
    MATCH_MEMORY = ("transcript", "_said_day", "_topics", "_dm_target", "_dm_day", "_call")

    def export_memory(self) -> dict:
        return {k: getattr(self, k) for k in self.MATCH_MEMORY}

    def import_memory(self, mem: dict) -> None:
        for k in self.MATCH_MEMORY:
            if k in mem:
                setattr(self, k, mem[k])

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
        return self.roles.apply(parse(st, [self.transcript[k] for k in keys]))

    def seats(self, st: dict) -> tuple[int, list[int]]:
        """(my seat, the living seats) as this policy reads the state (agent.Contestant's listening)."""
        v = self._view(st)
        return v.me, list(v.alive)

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
        self_wolves: set[int] = set()               # retain role contradictions even before a later seer claim
        for _seq, sender, f, _day in _chats(v):
            if f.injection:
                continue
            if getattr(f, "self_wolf", False):
                self_wolves.add(sender)
            if not f.claim:
                if sender in c.wolves:                # a claimer's later messages keep reporting results
                    c.wolves[sender] |= f.wolves
                    c.clears[sender] |= f.clears - f.wolves
                    if getattr(f, "conflicts", ()) or getattr(f, "self_wolf", False):
                        c.liars.add(sender)
                    named_by_claimer |= f.wolves
                continue
            if sender not in c.wolves:
                c.wolves[sender], c.clears[sender] = set(), set()
                c.order.append(sender)
                if sender in named_by_claimer:
                    c.reactive.add(sender)
            c.wolves[sender] |= f.wolves
            c.clears[sender] |= f.clears - f.wolves
            if getattr(f, "conflicts", ()) or getattr(f, "self_wolf", False):
                c.liars.add(sender)
            named_by_claimer |= f.wolves
        c.liars |= self_wolves & set(c.order)
        dead_wolves = sum(1 for r in v.dead.values() if r == "wolf")
        for s in c.order:
            wolves, clears = c.wolves[s], c.clears[s]
            if wolves & clears or s in wolves:
                c.liars.add(s)          # immutable roles cannot be both wolf and clear; the seer cannot be a wolf
            elif v.dead.get(s) in ("wolf", "villager"):
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
        # Death reveals the unique seer even if they never spoke. A later claim
        # cannot become trustworthy simply because the real seer stayed silent.
        confirmed = sorted(s for s, role in v.dead.items() if role == "seer")
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
        # Repeating an accusation is not another witness. Count each speaker/target once across the transcript,
        # taking the stronger of their accusation and vote suggestion. Otherwise five cheap messages can outweigh
        # the actual vote record; rephrasing or echoing the same assertion must not increase its evidence weight.
        accusations, suggestions, injectors = set(), set(), set()
        for _seq, sender, f, _day in _chats(v):
            if f.injection:
                injectors.add(sender)
                continue
            if sender in claimers:
                continue                  # claim results are weighed below
            for s in f.wolves:
                accusations.add((sender, s))
            for s in f.sus - f.wolves:
                suggestions.add((sender, s))
        for sender in injectors:
            score[sender] += INJECT
        for _sender, target in accusations:
            score[target] += ACCUSE
        for _sender, target in suggestions - accusations:
            score[target] += sus_w
        live_claimers = [s for s in c.order if s not in c.liars and (s in v.alive or v.dead.get(s) == "seer")]
        for s in c.liars:
            score[s] += LIAR
            if s != c.trusted:
                for t in c.clears.get(s, ()):
                    score[t] += LIAR_CLEAR
        if c.trusted is not None:
            for t in c.trusted_wolves(v.alive):
                score[t] += TRUSTED_WOLF
            for t in c.trusted_clears():
                score[t] += TRUSTED_CLEAR
        else:
            # Contested: at least one of them is lying. Evidence, not seat order or who accuses whom: a claimant whose
            # named wolf was revealed a wolf is the likelier true seer; one claimant calling another a wolf adds
            # nothing (a fake seer says it as easily as the real one, red team 10-08).
            proven = {s for s in live_claimers if any(v.dead.get(t) == "wolf" for t in c.wolves.get(s, ()))}
            for s in live_claimers:
                if s in proven and len(proven) < len(live_claimers):
                    score[s] += TRUSTED_CLEAR
                    continue
                score[s] += CONTESTED + (REACTIVE if s in c.reactive else 0.0)
                for t in c.wolves.get(s, ()):
                    if t not in live_claimers:
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
        claimers = [s for s in c.order if s in targets and s not in c.liars]
        if claimers:
            real = [s for s in claimers if c.wolves.get(s, set()) & wolfset]     # named one of us: the real seer
            pool = real or claimers
            return min(pool), _CLAIMED_SEER
        # Kill whoever the table trusts most (lowest suspicion) — trusted villagers steer votes.
        sus = self.suspicion(v, exclude=set(v.allies))
        pool = [t for t in targets if t in sus]
        if not pool:
            return (min(targets) if targets else None), "default"
        return min(pool, key=lambda s: (sus[s], s)), "remove the most trusted villager"

    @classmethod
    def _converge(cls, v: View, cands: list[int], margin: int = 1) -> int:
        """Of several seats we are sure are wolves (in our order of preference), the one the table is already pushing:
        another candidate replaces the first only with at least `margin` more distinct speakers naming it today. Voting
        our own first choice while the table votes another sure wolf splits the vote and gives the wolves a free day."""
        best, top = cands[0], cls.pressure_on(v, cands[0])
        for s in cands[1:]:
            p = cls.pressure_on(v, s)
            if p >= top + margin:
                best, top = s, p
        return best

    @staticmethod
    def _trusted_call(v: View, c: Claims, cands: list[int]) -> int | None:
        """The one of `cands` the living trusted seer told the table to vote today ("Everyone vote PlayerN"), from its
        latest message that names exactly one of them: villagers following a seer vote what it calls."""
        if c.trusted is None or c.trusted not in v.alive:
            return None
        call = None
        for _seq, sender, f, day in _chats(v):
            if day == v.day and sender == c.trusted and not f.injection:
                hit = [s for s in cands if s in f.sus]
                if len(hit) == 1:
                    call = hit[0]
        return call

    def _called(self, v: View) -> int | None:
        """The wolf our seer claim told the table to vote today, if any."""
        call = self._call
        if isinstance(call, (list, tuple)) and len(call) == 2 and call[0] == v.day and isinstance(call[1], int):
            return call[1]
        return None

    def _day_vote(self, v: View, targets: list[int]) -> tuple[int | None, str]:
        if v.role == "seer":
            wolves, clears = self.seer_knowledge(v)
            known = [s for s in wolves if s in targets]
            if known:
                # The wolf it called (else the lowest), unless the table has clearly moved to another sure wolf: the
                # call is what the villagers following us vote (10-08: a seer that called one wolf and voted another
                # one a single seat had pushed split the vote it had organised; chaos3 seed 2407: a seer that ignored a
                # table of two on its checked wolf tied the vote).
                called = self._called(v)
                first = called if called in known else known[0]
                return self._converge(v, [first] + [s for s in known if s != first], SEER_SWITCH), "seer result: wolf"
            c = self.claims(v, private=True)
            liars = [s for s in sorted(c.liars) if s in targets and s not in clears]   # a cleared bluffer is no wolf
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
            if c.trusted is not None and v.dead.get(c.trusted) == "seer":
                known += [s for s in sorted(c.liars) if s in targets and s not in known and s != c.trusted]
            call = self._trusted_call(v, c, known)
            return (call if call is not None else self._converge(v, known)), "follow the seer: named wolf"
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
        if tactics_mode() == "public_vote" and v.day == 1 and not c.live(v):
            # Match a villager's actual decision as well as its opening. The ally can be the legal target:
            # excluding it would recreate the very public/private discrepancy this profile accepts a cost to hide.
            public = replace(v, role="villager", allies=[], investigations={}, evidence=None,
                             messages=[m for m in v.messages if isinstance(m, dict)
                                       and m.get("visibility") != "direct" and not m.get("recipients")])
            return self._day_vote(public, targets)
        pool = [t for t in targets if t not in wolfset]
        live = [s for s in c.order if s in v.alive and s not in c.liars]
        if v.me in live and bluff_mode() != "off":
            try:
                t = self._bluff_target(v, c)                         # vote what our seer bluff told the table
            except Exception:
                t = None
            if t is not None and t in pool:
                return t, "my seer claim"
        if any(s in wolfset for s in live):                          # a claim fight involving one of us
            rivals = [s for s in live if s in pool]
            mine = [s for s in live if s in v.allies and s in targets]
            if rivals and mine and v.me not in live:
                # My ally's claim against another claimant: the reveal settles it within a day or two, and a wolf that
                # voted the true seer next to a fake one is exposed with it. Vote the ally (measured 10-08: about +10 /
                # +3 points of wolf win rate against the red team / the stand-ins over voting the rival).
                return mine[0], "claim fight: vote the claimant the reveal will expose"
            if rivals:
                return min(rivals), "claim fight"
        ally = self._doomed_ally(v, c, targets)
        if ally is not None:
            return ally, "the table votes my ally out anyway: vote with it"
        sus = self.suspicion(v, exclude=set(v.allies))
        return self._top(sus, pool), "vote with the table"

    def _doomed_ally(self, v: View, c: Claims, targets: list[int]) -> int | None:
        """A living ally the table will vote out anyway, whom a wolf votes too (measured 10-08: never voting a doomed
        partner is the tell vote-record readers catch a wolf by): one exposed as a fake seer, one DOOMED or more other
        seats press today, or (when the claim does not also name me) one a trusted seer claim names."""
        live_allies = [a for a in v.allies if a in targets and a in v.alive]
        if not live_allies:
            return None
        a = live_allies[0]
        if a in c.liars or self.pressure_on(v, a) >= DOOMED:
            return a
        if c.trusted is None or c.trusted in (v.me, *v.allies):
            return None
        named = set(c.trusted_wolves(v.alive))
        if v.me in named:
            return None
        allies = [x for x in live_allies if x in named]
        return allies[0] if allies else None

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
        # With ARENA_WW_DM=on a wolf's first message of the day is the private kill plan (DMs are off by default).
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
            pick, msg = self._opening(v)
            # (a wolf whose pick is its doomed ally says it in the same words as anyone else: chat_guard's "bus")
            out.append(("bus" if v.role == "wolf" and pick in v.allies else "open", msg))
        return out

    def _seer_chat(self, v: View, c: Claims):
        wolves, clears = self.seer_knowledge(v)
        fakes = [s for s in c.order if s in c.liars and s in v.alive and s != v.me and s not in clears]
        clear_txt = " ".join(f"Player{x} is clear." for x in clears[:3])
        if wolves:
            # The wolf to vote, fixed for the day: the lowest, unless the table is clearly on another one already.
            called = self._called(v)
            w = called if called in wolves else self._converge(v, wolves, SEER_SWITCH)
            self._call = [v.day, w]
            rest = [x for x in wolves if x != w]
            txt = f"I am the Seer. Player{w} is a WOLF."
            if rest:
                txt += " " + " ".join(f"Player{x} is a WOLF." for x in rest[:2])
            if clears:
                txt += " " + clear_txt
            return "claim", {"content": txt + f" Everyone vote Player{w} today.", "recipients": []}
        if fakes:
            f = fakes[0]
            if "claim" in self._topics and not c.wolves.get(v.me):
                # Our earlier clear-only claim used today's normal claim topic. A new opposing claimant is new
                # information, so allow one bounded rebuttal; the remembered topic also deduplicates a restart.
                # Being the real seer proves the rival's role claim false, not that a possibly bluffing villager
                # is a wolf. Keep that distinction and follow the same ballot as our private-information policy.
                pick, _ = self._day_vote(v, v.others_alive)
                f = pick if pick in fakes else f
                txt = f"I am the Seer. Player{f} is not the Seer; their claim is false."
                if clears:
                    txt += " " + clear_txt
                return "claim-rebuttal", {"content": txt + f" Vote Player{f}.", "recipients": []}
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
        """Counter-claim seer when a live claimer names me; when a trusted claim names my ally (not me) and the table
        will vote it out, follow the claim in the villagers' words (_follow_msg). ARENA_WW_BLUFF: the seer bluff first."""
        c = self.claims(v, private=False)
        mode = bluff_mode()
        if mode != "off":
            try:
                bluff = self._bluff_chat(v, c, mode)
            except Exception:
                bluff = None                                          # any error: the normal wolf chat
            if bluff is not None:
                return bluff
        if v.me in c.order:
            return None
        accusers = [s for s in c.order if s in v.alive and s != v.me and s not in c.liars
                    and v.me in c.wolves.get(s, set())]
        if not accusers:
            others = [s for s in v.alive if s != v.me]
            ally = self._doomed_ally(v, c, others)
            if ally is not None and c.trusted is not None and ally in c.wolves.get(c.trusted, ()):
                return "bus", self._follow_msg(c, ally)
            live = c.live(v)
            if c.trusted is None and len(live) >= 2 and any(a in live for a in v.allies):
                pick, _ = self._wolf_vote(v, others)          # my ally's claim fight: the villagers' words for it
                if pick is not None:
                    return "bus-contest", self._contest_msg(live, pick)
            return None
        a = accusers[0]
        return "counter", {"content": f"I am the Seer. Player{a} is a WOLF and is lying about being Seer. Vote Player{a}.",
                           "recipients": []}

    # ---- ARENA_WW_BLUFF: a wolf's proactive fake seer claim ---------------------------------------------------------
    @staticmethod
    def _claim_day(v: View) -> int | None:
        """The day of my first broadcast seer claim, or None."""
        for _seq, sender, f, day in _chats(v):
            if sender == v.me and f.claim and not f.injection:
                return day
        return None

    @staticmethod
    def _llm_table(v: View) -> bool:
        """Another living seat has written a message of BLUFF_WORDS or more words today (ARENA_WW_BLUFF=llm)."""
        for m in v.messages:
            if not isinstance(m, dict) or m.get("type", "chat") != "chat" or m.get("visibility") == "direct" \
                    or m.get("recipients") or m.get("_day", m.get("day")) != v.day:
                continue
            s, text = m.get("sender"), m.get("content")
            if isinstance(s, int) and not isinstance(s, bool) and s != v.me and s in v.alive and isinstance(text, str) \
                    and len(text.split()) >= BLUFF_WORDS:
                return True
        return False

    def _bluff_pick(self, v: View) -> int | None:
        """The seat the claim names a wolf: the living non-ally seat with the highest public suspicion, ties to the
        HIGHER seat (never a seat that claimed seer). Real LLM seers mostly investigate the first legal option, the
        lowest other seat, on night 1 (pilot 10-14: 5 of 9 counter-claims said "I confirmed Player0 is a villager"
        right after we named Player0), so the lowest seat is the one most likely already cleared. Our own opening's
        "I'll vote PlayerN" (the lowest seat) is not evidence and is left out."""
        c = self.claims(v, private=False)
        pool = [s for s in v.alive if s != v.me and s not in v.allies and s not in c.order]
        heard = replace(v, messages=[m for m in v.messages if not isinstance(m, dict) or m.get("sender") != v.me])
        sus = self.suspicion(heard, exclude=set(v.allies))
        pool = [s for s in pool if s in sus]
        return max(pool, key=lambda s: (round(sus[s], 6), s)) if pool else None

    @staticmethod
    def _bluff_target(v: View, c: Claims) -> int | None:
        """The seat our live seer bluff tells the table to vote: a rival claimant (the claim-fight rule: vote the
        rival), else the lowest living non-ally seat we named a wolf."""
        others = [s for s in v.alive if s != v.me and s not in v.allies]
        rivals = [s for s in c.order if s in others]
        if rivals:
            return min(rivals)
        named = sorted(s for s in c.wolves.get(v.me, ()) if s in others)
        return named[0] if named else None

    def _bluff_chat(self, v: View, c: Claims, mode: str):
        """(topic, message) of the seer bluff, or None (then the normal wolf chat). Day 1 with no seer claim yet: the
        claim. After it, while no revealed role has exposed it: a rival claimant gets the real seer's rebuttal once (on
        the day it appears); every later day opens with our "results" (the wolves we named, one new non-ally clear per
        day, the seat to vote)."""
        if v.me not in v.alive:
            return None
        if v.me not in c.order:
            if v.day != 1 or c.order or "seer" in v.dead.values():
                return None
            if mode == "llm" and not self._llm_table(v):
                return None
            x = self._bluff_pick(v)
            if x is None:
                return None
            return "bluff-claim", {"content": f"I am the Seer. Player{x} is a WOLF. Everyone vote Player{x} today.",
                                   "recipients": []}
        if v.me in c.liars:
            return None                                       # exposed: nothing left to defend
        others = [s for s in v.alive if s != v.me and s not in v.allies]
        named = c.wolves.get(v.me, set())
        fresh = [s for s in c.order if s in others and s not in named]
        target = self._bluff_target(v, c)
        if fresh:
            r = target if target in fresh else fresh[0]
            return "bluff-rebut", {"content": f"I am the Seer. Player{r} is lying about being Seer, so Player{r} is a "
                                              f"WOLF. Vote Player{r}.", "recipients": []}
        claimed = self._claim_day(v)
        if claimed is None or v.day is None or v.day <= claimed or target is None:
            return None
        wolves = [target] + [s for s in sorted(named) if s in others and s != target][:1]
        clears = [s for s in sorted(c.clears.get(v.me, ())) if s in others and s not in wolves]
        need = max(0, (v.day - claimed + 1) - len(named) - len(c.clears.get(v.me, ())))
        if need:
            sus = self.suspicion(v, exclude=set(v.allies))
            pool = [s for s in others if s not in wolves and s not in clears and s not in c.order and s in sus]
            clears += sorted(pool, key=lambda s: (sus[s], s))[:need]   # the most trusted unnamed seats
        txt = "I am the Seer. " + " ".join(f"Player{s} is a WOLF." for s in wolves)
        if clears:
            txt += " " + " ".join(f"Player{s} is clear." for s in clears[:2])
        return "bluff-result", {"content": txt + f" Everyone vote Player{target} today.", "recipients": []}

    def extra_followups(self, st: dict) -> int:
        """Listening follow-ups allowed on top of agent.LISTEN_FOLLOWUPS today: 1 on the day our seer bluff went out
        (so the claim never uses up the one reaction a rival's counter-claim needs), else 0. 0 with ARENA_WW_BLUFF off."""
        try:
            if bluff_mode() == "off":
                return 0
            v = self._view(st)
            return 1 if v.role == "wolf" and v.day is not None and self._claim_day(v) == v.day else 0
        except Exception:
            return 0

    @staticmethod
    def _follow_msg(c: Claims, t: int) -> dict:
        return {"content": f"Following the Seer (Player{c.trusted}): I vote Player{t}. "
                           f"Everyone vote Player{t} so we do not split.", "recipients": []}

    @staticmethod
    def _contest_msg(live: list[int], pick: int) -> dict:
        return {"content": f"Two Seer claims: Player{live[0]} and Player{live[1]}. One is lying. "
                           f"I vote Player{pick}; the reveal will show who lied.", "recipients": []}

    def _villager_chat(self, v: View, c: Claims):
        if v.role == "villager":
            # A seer claimer calls me a wolf: I know it lies (claims() already treats it as a liar); say so.
            framers = [s for s in c.order if s in v.alive and s != v.me and v.me in c.wolves.get(s, set())]
            if framers:
                f = framers[0]
                return "deny", {"content": f"Player{f} is lying: I am not a wolf, so Player{f} is a fake Seer. "
                                           f"Vote Player{f}.", "recipients": []}
        known = c.trusted_wolves(v.alive)
        if known and c.trusted != v.me:
            call = self._trusted_call(v, c, known)                             # the seat our vote goes to
            return "follow", self._follow_msg(c, call if call is not None else self._converge(v, known))
        live = c.live(v)
        if c.trusted is None and len(live) >= 2 and v.role == "villager":
            others = [s for s in v.alive if s != v.me]
            pick, _ = self._day_vote(v, others)
            if pick is not None:
                return "contest", self._contest_msg(live, pick)
        return None

    def _opening(self, v: View) -> tuple[int | None, dict]:
        """(the seat we will vote, the opening line naming it). The same words for every role: "My read from the vote
        record" once there is a vote record, else "No hard info yet" (a wolf citing a record that does not exist yet
        on day 1 was the clearest role tell the red team found, 10-08)."""
        others = [s for s in v.alive if s != v.me]
        if tactics_enabled() and v.role == "wolf" and v.day == 1 and not self.claims(v, private=False).order:
            # With no seer claim, skipping the public vote target solely because it is our ally reveals a pair.
            # Announcing that ally instead pushes persuadable villagers onto it. Keep this opening neutral;
            # subsequent claims, reactions and the actual ballot still follow their usual evidence rules.
            public = replace(v, role="villager", allies=[], investigations={})
            public_pick, _ = self._day_vote(public, others)
            if public_pick in v.allies:
                return None, {"content": "No hard info yet. Let's hear from everyone and keep our votes together. "
                                         "Please don't abstain.", "recipients": []}
        pick = self._day_vote(v, others)[0] if others else None
        if pick is not None and v.votes:
            return pick, {"content": f"My read from the vote record: Player{pick} is most suspicious. "
                                     f"I'll vote Player{pick} unless someone has better info.", "recipients": []}
        if pick is not None:
            return pick, {"content": f"No hard info yet. To avoid a split vote I'll vote Player{pick} unless the Seer "
                                     "speaks up. Please don't abstain.", "recipients": []}
        return None, {"content": "No hard info yet. Let's not abstain — three stalled days hands it to the wolves.",
                      "recipients": []}


# ---------------------------------------------------------------- outgoing chat guard
_LEAK_ANY = re.compile(r"(?i)your role|fellow wol|investigations?:|you do not know|observation|reasoning_summary")
_SELF_WOLF = re.compile(r"(?i)\b(?:i am|i'm|im|we are|we're|us)\s+(?:a\s+|the\s+|both\s+)?(?:wolf|wolves|werewol)|"
                        r"\b(?:my|our)\s+(?:fellow|partner|ally|allies|teammate|pack|kill)|\bfellow\b|\bpartner\b|"
                        r"\bally\b|\ballies\b|\btonight\b|\bkill")
_SEER_CLAIM_OUT = re.compile(r"(?i)\b(?:i am|i'm|im)\s+(?:the\s+)?(?:real\s+|true\s+)?seer\b")
_RESULT_OUT = re.compile(r"(?i)player\d+\s+is\s+(?:a\s+)?(?:wolf|clear|villager)")
# The only words in which a wolf may name its ally in public: a villager's own templates for voting a doomed ally
# (following a trusted seer claim, the opening line naming the seat we will vote, a claim fight).
_BUS_OUT = re.compile(r"Following the Seer \(Player\d\): I vote (Player\d)\. Everyone vote \1 so we do not split\."
                      r"|My read from the vote record: (Player\d) is most suspicious\. I'll vote \2 unless someone has "
                      r"better info\."
                      r"|No hard info yet\. To avoid a split vote I'll vote Player\d unless the Seer speaks up\. "
                      r"Please don't abstain\."
                      r"|Two Seer claims: Player\d and Player\d\. One is lying\. I vote Player\d; the reveal will show "
                      r"who lied\.")
# The only seer claims a wolf may make besides the counter-claim: ARENA_WW_BLUFF's templates (topics "bluff-*", which
# only WerewolfPolicy._bluff_chat produces, and only with the switch on). They name seats as above: never the ally.
_BLUFF_OUT = re.compile(r"I am the Seer\. (Player\d) is a WOLF\.(?: Player\d is a WOLF\.)?(?: Player\d is clear\.){0,2} "
                        r"Everyone vote \1 today\."
                        r"|I am the Seer\. (Player\d) is lying about being Seer, so \2 is a WOLF\. Vote \2\.")
_WORD_CAP = 50
_CHAR_CAP = 1024        # MESSAGE_TOO_LONG over 1024 characters (werewolf.md §8); counted in UTF-8 bytes, which is
#                         never fewer than the characters or UTF-16 units the server might count


def chat_guard(msg, v: View, topic: str):
    """The message if it is safe to send, else None. Backstop for the templates above; it never edits a message."""
    if not isinstance(msg, dict):
        return None
    text = msg.get("content")
    rec = msg.get("recipients") or []
    if not isinstance(text, str) or not text.strip() or len(text.split()) > _WORD_CAP \
            or len(text.encode("utf-8", "replace")) > _CHAR_CAP or _LEAK_ANY.search(text):
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
        bus = topic.startswith("bus") and _BUS_OUT.fullmatch(text)      # voting a doomed ally, in villager words
        if (mentioned & set(v.allies) and not bus) or re.search(rf"(?i)player{v.me}\s+is\s+(?:a\s+)?wolf", text):
            return None
        if _SEER_CLAIM_OUT.search(text) and topic != "counter" \
                and not (topic.startswith("bluff") and _BLUFF_OUT.fullmatch(text)):
            return None
    elif v.role == "villager":
        if _SEER_CLAIM_OUT.search(text) or (_RESULT_OUT.search(text) and topic != "follow"):
            return None                                 # villagers never fake-claim or report "results"
    elif v.role == "seer" and topic not in ("claim", "claim-rebuttal"):
        if _SEER_CLAIM_OUT.search(text) or _RESULT_OUT.search(text):
            return None
    return {"content": text, "recipients": []}
