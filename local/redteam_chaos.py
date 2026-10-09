"""Red-team Werewolf backgrounds "chaos*" (context-free challenger, robustness angle) and their driver.

    python -m local.redteam_chaos --bg chaos --k 1 --games 1000 --seed 2400            # agent vs six chaos seats
    python -m local.redteam_chaos --bg chaos --k 1 --games 5 --seed 2400 --show 5      # with transcripts
    python -m local.eval_werewolf --bg chaos,llmq --ks 1,3 --pairs '' --agents agent --seeds 2400

The seats here only use what a real seat knows (its role, its wolf ally, its own checks, the public record, the
broadcast chat and DMs sent to it) and only send what the platform accepts (a chat of 1..50 words and at most 1024
characters, broadcast or to one seat, at most 5 per window, legal actions only). They never imitate platform
text. Their peers understand each other's odd phrasing (an LLM table would): every chat a chaos seat sends is
written from a structured meaning, and the meaning is registered next to the text.

Kinds (each a subclass of ChaosSeat, all sharing one decision core):
  chaos      competent table play (seer claims its checks, villagers follow one claimant, wolves counter-claim and
             bus a lost partner) phrased chaotically: seat names in many formats (P3, seat 3, #3, player three,
             agent3, Player03), typos, many short chats, long hyphen-packed chats near 1024 characters, late claims.
  chaosx     chaos plus the extreme probes: quotes of other seats' claims, sarcasm, hypotheticals, negations,
             contradictory claims, out-of-range seats (Player7, Player9, Player12), dead-seat talk, abstentions.
  chaos1..5  the iterations below; chaos4 = chaos3's strategy with chaosx's noise.
The driver plays each deal with the agent in k seats and reports per-role win rates, the platform failure model
(crash / illegal / bad chat / timeout), and black-box anomaly checks on the agent's seats: a chat the platform would
reject (over 1024 characters, over 50 words), a villager-side abstention that hands the game to the wolves (third
day without elimination), a seer voting a seat it checked as villager or not voting a living wolf it checked, a
wolf voting its ally, chat naming an out-of-range or dead seat. Probe runs (CHAOS_PROBE, CHAOS_QUIET=1: only the probe
talks) measure one behaviour on otherwise identical deals: fmt:<claim>:<seat>[:<round>] (a true seer claim in one
wording), noise:<kind>[:<round>] (one kind of odd talk next to a plain true claim), wagon:<seat>[:<round>] (every chaos
seat pushes the agent's seat on day 1; the driver names the seat, a measurement hook only). --control replays each
deal with a chaos seat in the agent's seat; CHAOS_DEAL_AS=<bg> deals as another version (paired versions).

Iteration log (agent = our deployed player; win rates of the agent's seats, eval_werewolf seed 2400, 1000 games per
cell, rounds timing; baseline same seed: heuristic k1 53.0 / k3 52.7 per seat, llmq k1 61.2 / k3 57.8):
  v0 chaos   chaotic surface over plain play. k1 63.3 (V 67.4, S 90.7, W 41.3), k3 56.4. Transcripts: the agent opens
             day 1 with "To avoid a split vote I'll vote <lowest other seat>" and the table herds onto it; it ignores
             several seer-claim wordings. Format sweep (seed 2401/2402): clears worded "As the seer I cleared X",
             "checked X -> villager", "X is on the village team", 'My result: "X is a villager"', **bold**, or with
             the seat as "player three" / "Player03" / "Plyer3" / "Player(3)" / "**Player3**" are not read: the agent
             votes the cleared seat (its default call) 74% of the time, villager wins 74.4 -> 66.6 on paired deals.
  chaosx     k1 53.3 (V 50.5, S 64.7, W 52.5), k3 54.8. Noise sweep (seed 2403): sarcasm, hypotheticals, questions,
             negations, quotes, dead-seat talk, near-1024-character and 5-short-chat bursts, out-of-range seats
             (Player7/9/12/99999999999999999999, superscript, circled and fullwidth digits), JSON, zero-width spaces:
             no crash, no change in the agent's results. A villager copying the seer claim makes the agent seer vote a
             seat it checked as villager (seed 2403 games 461, 484, 558, 653, 697, 829, 856, noise:echo).
  v1 chaos1  no herding on unverified calls; the day-1 call tell (as villager the agent calls the lowest other seat
             100%, as wolf it skips its partner: 16% of wolf games name both wolves); wolves ride the first call,
             deflect it off a partner with a fake clear, kill the table's vote leader. k1 58.7 (W 38.6), k3 59.2.
  v2 chaos2  two-claim exploit. Between two seer claimants the agent votes the one the other accuses when only one
             accusation runs between them (else the later claimant if mutual, else the lower seat); the wolf a seer
             claim did not name counter-claims and accuses the seer. Villagers pick either claimant. The agent votes the
             true seer in 100% of clear+counter and 85% of wolf-named+counter spots (seed 2406). k1 51.5 (V 60.1,
             S 81.2, W 18.7): the lowest per-seat rate measured, level with heuristic (53.0, CIs overlap); k3 58.2
             (S 62.0, from 79.3 vs chaos1).
  v3 chaos3  sharper village (living counter-claimers are liars once the claimant they accused is revealed seer;
             shared tie order). Agent wolf k1 17.2, but its villager rate rises with its teammates (92.4): k1 67.1,
             k3 61.4. Agent seer stays weak (70.4 vs 98.5 for a chaos seat in the same seat, seed 2407).
  v4 chaos4  chaos3 + chaosx noise. k1 62.7 (V 82.4, S 62.7, W 24.2), k3 56.0. Runner-level (real run_game loop on
             local/werewolf_platform, 60 games, listening on/off): 0 errors, hangs, idle auto-actions or rejects.
  v5 chaos5  counter-claim held to the last rounds so the accused seer cannot answer (seed 2410, paired with chaos3
             deals): no change (k1 67.0 vs 67.4, k3 59.8 vs 60.0): the agent seer does not rebut a counter anyway.
             Bandwagon probe (seed 2409): pushed by "vote Player5" early, the agent seer claims 89% (100% with
             ARENA_WW_LISTEN=on) vs 46% unpushed; pushed in round >= 3, or as "vote player five", it does not react
             (46%); under rt timing with listening off it never reacts.
"""
from __future__ import annotations

import argparse
import os
import random
import re
import time
from collections import defaultdict

from .werewolf_bots import _legal, _private, _public
from .werewolf_env import ABSTAIN, N, SIM_HOLD, ContestantSeat, Game, Seat

MAX_CHARS = 1024
MAX_WORDS = 50
MAX_CHATS = 5

# ---------------------------------------------------------------- reading chat
_NUMW = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_SEATREF = (r"(?:\b(?:player|plyer|palyer|p|seat|agent|no\.?)|#)\s*[-_#]?\s*0*(\d+)"
            r"|\b(?:player|seat)\s+(zero|one|two|three|four|five|six)\b")
_SEAT_RX = re.compile(_SEATREF, re.I)


def seats_in(text: str) -> list[int]:
    out = []
    for m in _SEAT_RX.finditer(text):
        n = int(m.group(1)) if m.group(1) else _NUMW[m.group(2).lower()]
        out.append(n)
    return out


_MEANING: dict[str, dict] = {}     # text a chaos seat wrote -> the structured meaning it was written from
PROBE_TARGET: int | None = None    # wagon probe only: the seat the driver tells every chaos seat to push


def meaning_of(text: str, sender: int) -> dict:
    """What a chaos seat takes `text` to mean: its own peers' text exactly, anyone else's by plain patterns."""
    hit = _MEANING.get((sender, text))
    if hit is not None:
        return hit
    t = text.lower()
    m: dict = {"claim": None, "wolves": set(), "clears": set(), "vote": set()}
    if re.search(r"\b(?:i\s*(?:am|'m|m)\s+(?:the\s+)?(?:real\s+|true\s+)?seer|seer\s+here|as\s+(?:the\s+)?seer)\b", t):
        m["claim"] = "seer"
    for seg in re.split(r"[.;!?]", t):
        refs = seats_in(seg)
        if not refs:
            continue
        neg = re.search(r"\b(?:not|n't|no|never)\b", seg)
        if re.search(r"\b(?:wolf|werewolf|wolves)\b", seg) and not neg:
            m["wolves"] |= set(refs)
        elif re.search(r"\b(?:villager|clear|clean|innocent|town)\b", seg) and not neg:
            m["clears"] |= set(refs)
        if re.search(r"\bvot", seg):
            m["vote"] |= set(refs)
    for k in ("wolves", "clears", "vote"):
        m[k] = {s for s in m[k] if 0 <= s < N and s != sender}
    return m


# ---------------------------------------------------------------- writing chat
_WORDN = ["zero", "one", "two", "three", "four", "five", "six"]


def seat_name(s: int, rng: random.Random, style: str = "any") -> str:
    forms = [f"Player{s}", f"P{s}", f"p{s}", f"seat {s}", f"#{s}", f"player {_WORDN[s]}", f"agent{s}",
             f"Player0{s}", f"PLAYER{s}", f"player-{s}", f"Plyer{s}", f"seat#{s}", f"Player {s}"]
    if style == "plain":
        return f"Player{s}"
    return rng.choice(forms)


_TYPO = {"seer": ["seer", "seer", "seeer", "ser", "Seer", "SEER"], "wolf": ["wolf", "wolf", "wolff", "WOLF", "werewolf",
                                                                             "wolfie"],
         "villager": ["villager", "villager", "vilager", "villlager", "town"], "vote": ["vote", "vote", "voet", "VOTE"]}


def typo(word: str, rng: random.Random) -> str:
    return rng.choice(_TYPO.get(word, [word]))


def fit(text: str) -> str:
    """Trim to the platform's limits (50 words, 1024 characters); never empty."""
    words = text.split()[:MAX_WORDS] or ["hm"]
    out = " ".join(words)
    while len(out) > MAX_CHARS and len(words) > 1:
        words = words[:-1]
        out = " ".join(words)
    return out[:MAX_CHARS]


def _say(text: str, meaning: dict | None, to: int | None, sender) -> dict:
    """A chat dict within the platform's limits; its meaning is registered for the chaos peers under (sender, text)
    (no meaning = table noise the peers ignore)."""
    text = fit(text)
    meaning = meaning or {}
    _MEANING[(sender, text)] = {"claim": meaning.get("claim"), "wolves": set(meaning.get("wolves", ())),
                                "clears": set(meaning.get("clears", ())), "vote": set(meaning.get("vote", ())),
                                "fmt": meaning.get("fmt"), "noise": meaning.get("noise")}
    return {"content": text, "recipients": [to] if to is not None else []}


# Seer-claim formats for the probe runs (CHAOS_PROBE=fmt:<name>): (claim with a wolf, claim with clears only).
# {w} = the wolf's seat label, {c} = the cleared seats' labels; `seat` picks the label format.
_FILL = ("really-quite-honestly-and-after-a-lot-of-thinking-about-yesterday-and-the-way-people-talked-in-the-"
         "first-round-I-want-to-say-something-important")
CLAIM_FORMATS = {
    "plain":      ("I am the Seer. {w} is a wolf. Vote {w}.", "I am the Seer. {c} is a villager."),
    "im":         ("im the seer. {w} is a wolf. vote {w}", "im the seer. {c} is a villager"),
    "seerhere":   ("seer here: {w} is a wolf, vote them out", "seer here: {c} is clean"),
    "as":         ("As the seer: {w} is a wolf! vote {w}", "As the seer I cleared {c}. town."),
    "arrow":      ("I'm the Seer. checked {w} -> wolf. vote {w}", "I'm the Seer. checked {c} -> villager"),
    "eq":         ("I'm the Seer. {w}=wolf.", "I'm the Seer. {c}=clean."),
    "typo":       ("im teh seer. {w} is a wolff. voet {w}", "im teh seer. {c} is a vilager"),
    "caps":       ("I AM THE SEER. {w} IS A WOLF. VOTE {w}.", "I AM THE SEER. {c} IS A VILLAGER."),
    "werewolf":   ("I am the Seer. {w} is a werewolf.", "I am the Seer. {c} is innocent."),
    "evil":       ("I am the Seer and {w} came back evil. Vote {w}.", "I am the Seer and {c} came back good."),
    "colon":      ("Seer claim. Result: {w}: wolf", "Seer claim. Result: {c}: villager"),
    "role":       ("My role is seer. {w} is a wolf.", "My role is seer. {c} is a villager."),
    "hyphen":     ("Seer-claim: I-am-the-seer-and-I-investigated-{w}-and-the-result-was-WOLF-so-vote-{w}-today",
                   "Seer-claim: I-am-the-seer-and-I-investigated-{c}-and-the-result-was-VILLAGER"),
    "buried":     (_FILL + " " + " ".join([_FILL] * 4) + " I am the Seer. {w} is a wolf.",
                   _FILL + " " + " ".join([_FILL] * 4) + " I am the Seer. {c} is a villager."),
    "split":      (["I am the Seer.", "{w} is a wolf.", "Vote {w}."], ["I am the Seer.", "{c} is a villager."]),
    "thirdperson": ("{me} is the seer (that's me). {w} is a wolf.", "{me} is the seer (that's me). {c} is a villager."),
    "quoted":     ('I am the Seer. My result: "{w} is a wolf".', 'I am the Seer. My result: "{c} is a villager".'),
    "lower":      ("i am the seer. {w} is a wolf. vote {w}.", "i am the seer. {c} is a villager."),
    # phrasings an LLM seat writes
    "as_llm":     ("As the Seer, I checked {w} last night and they are a wolf.",
                   "As the Seer, I checked {c} last night and they are a villager."),
    "investigated": ("I'm the Seer. I investigated {w}: werewolf.", "I'm the Seer. I investigated {c}: villager."),
    "noclaim":    ("I checked {w} last night. {w} is a wolf.", "I checked {c} last night. {c} is a villager."),
    "notwolf":    ("I am the Seer. {w} is a wolf.", "I am the Seer. {c} is not a wolf."),
    "good":       ("I am the Seer. {w} is bad.", "I am the Seer. {c} is good."),
    "team":       ("I am the Seer. {w} is on the wolf team.", "I am the Seer. {c} is on the village team."),
    "town":       ("I am the Seer. {w} is scum.", "I am the Seer. {c} is town."),
    "emdash":     ("I am the Seer — {w} is a wolf.", "I am the Seer — {c} is a villager."),
    "they":       ("I am the Seer. I checked {w}, they're a wolf.", "I am the Seer. I checked {c}, they're a villager."),
    "confirm":    ("I'm the Seer and I can confirm {w} is a werewolf.", "I'm the Seer and I can confirm {c} is a villager."),
    "cleared":    ("I am the Seer. {w} is a wolf.", "I am the Seer. I cleared {c}."),
    "boldclaim":  ("**I am the Seer.** {w} is a **wolf**.", "**I am the Seer.** {c} is a **villager**."),
    "anwolf":     ("I am the Seer. {w} is an evil werewolf.", "I am the Seer. {c} is an innocent villager."),
    "seerresult": ("Seer result: {w} is a wolf.", "Seer result: {c} is a villager."),
}
SEAT_FORMATS = {
    "Player": "Player{s}", "P": "P{s}", "p": "p{s}", "seat": "seat {s}", "hash": "#{s}", "word": "player {word}",
    "agent": "agent{s}", "zero": "Player0{s}", "upper": "PLAYER{s}", "dash": "player-{s}", "typo": "Plyer{s}",
    "space": "Player {s}", "paren": "Player({s})", "bold": "**Player{s}**", "at": "@Player{s}",
}


def fmt_seat(s: int, style: str) -> str:
    return SEAT_FORMATS[style].format(s=s, word=_WORDN[s] if 0 <= s < len(_WORDN) else str(s))


# ---------------------------------------------------------------- the seat
class ChaosSeat(Seat):
    """A competent table player with a chaotic surface (see the module docstring)."""
    name = "chaos"
    EXTREME = False
    FAKE_CLAIM_D1 = 0.25       # a wolf fake-claims seer on day 1 before the real one speaks
    COUNTER = 0.9              # a wolf accused by a seer claim counter-claims
    LATE_CLAIM = 0.5           # the seer holds its claim to the last round
    ABSTAIN = 0.0              # a villager abstains when nothing points anywhere (never on stall 2)
    TWO_CLAIM = "later"        # villager facing two seer claimants: vote the later one ("later") or either ("random")
    KEEP_FAKE = True           # a wolf that fake-claimed keeps "checking" a seat every day

    def reset(self):
        self.msgs: dict[int, dict] = {}
        self.plan: list = []
        self.plan_day = None
        self.round = 0
        self.claimed = False
        self.fake: dict[int, str] = {}     # a wolf's fake check results
        self.said_claim_day = None
        # CHAOS_PROBE=fmt:<claim format>:<seat format>[:<round>]: the seer claims day 1 in exactly that format at that
        # round (default 0), wolves never fake-claim or counter-claim (a clean, uncontested true claim)
        self.probe = (os.environ.get("CHAOS_PROBE") or "").split(":")

    def say(self, text: str, meaning: dict | None = None, to: int | None = None) -> dict:
        return _say(text, meaning, to, self.me)

    def _probe_fmt(self):
        if len(self.probe) >= 3 and self.probe[0] == "fmt":
            rnd = int(self.probe[3]) if len(self.probe) > 3 else 0
            return self.probe[1], self.probe[2], rnd
        return None

    # -- reading -------------------------------------------------------------------------------------------------
    def _read(self, raw):
        for m in raw.get("new_messages") or []:
            if isinstance(m, dict) and m.get("seq") is not None:
                self.msgs[m["seq"]] = m

    def _claims(self, raw):
        """claimer -> {"order", "wolves", "clears"} from broadcasts; per-seat accusation pressure; vote calls."""
        me, alive, dead, _ = _public(raw)
        claims: dict[int, dict] = {}
        press: dict[int, float] = defaultdict(float)
        calls: dict[int, float] = defaultdict(float)
        for seq in sorted(self.msgs):
            m = self.msgs[seq]
            if m.get("type", "chat") != "chat" or m.get("recipients"):
                continue
            s = m.get("sender")
            mm = meaning_of(str(m.get("content") or ""), s)
            if mm["claim"] == "seer":
                c = claims.setdefault(s, {"order": len(claims), "wolves": set(), "clears": set()})
                c["wolves"] |= mm["wolves"]
                c["clears"] |= mm["clears"]
            for t in mm["wolves"]:
                press[t] += 1.0
            for t in mm["vote"]:
                calls[t] += 1.0
        # a claimer revealed as a wolf lied; a revealed villager who claimed seer lied too
        claims = {c: v for c, v in claims.items() if dead.get(c) in (None, "seer")}
        return claims, press, calls

    # -- table logic shared by every role -------------------------------------------------------------------------
    def _suspicion(self, raw, claims) -> dict[int, float]:
        me, alive, dead, votes = _public(raw)
        sc: dict[int, float] = defaultdict(float)
        for vv, elim in votes:
            er = dead.get(elim) if elim is not None else None
            for voter, tgt in vv.items():
                if tgt is None:
                    sc[voter] += 0.4
                elif tgt == elim:
                    sc[voter] += 1.0 if er in ("villager", "seer") else -1.0
                elif tgt is not None and dead.get(tgt) == "wolf":
                    sc[voter] -= 0.5
        for c, v in claims.items():                      # a claimer who called a revealed villager a wolf lied
            if any(dead.get(w) in ("villager", "seer") for w in v["wolves"]):
                sc[c] += 3.0
            if any(dead.get(w) == "wolf" for w in v["clears"]):
                sc[c] += 3.0
            if any(dead.get(w) == "wolf" for w in v["wolves"]):
                sc[c] -= 3.0
        return {s: sc[s] for s in alive if s != me}

    def _villager_target(self, raw, role, inv, targets):
        me, alive, dead, votes = _public(raw)
        claims, press, calls = self._claims(raw)
        sc = self._suspicion(raw, claims)
        if role == "seer":
            known = [t for t in targets if inv.get(t) == "wolf"]
            if known:
                return min(known)
            fakes = [c for c in claims if c != me and c in targets]
            if fakes:
                return min(fakes)
            pool = [t for t in targets if inv.get(t) != "villager"] or targets
            return max(pool, key=lambda s: (sc.get(s, 0) + 0.3 * press.get(s, 0) + 0.2 * calls.get(s, 0), -s))
        live = {c: v for c, v in claims.items() if c != me and (c in alive or dead.get(c) == "seer")}
        trusted = None
        if dead_seer := [c for c, v in live.items() if dead.get(c) == "seer"]:
            trusted = dead_seer[0]
        elif len(live) == 1:
            c = next(iter(live))
            if sc.get(c, 0) < 2.5:
                trusted = c
        elif len(live) >= 2:
            # two claimants: one is a wolf. Trust the one whose calls fit the record; else vote a claimant.
            ok = sorted(live, key=lambda c: (sc.get(c, 0), live[c]["order"]))
            if sc.get(ok[0], 0) + 1.0 < sc.get(ok[-1], 0):
                trusted = ok[0]
            else:
                both = [c for c in ok if c in targets]
                if both and self.TWO_CLAIM == "random":
                    return self.rng.choice(both)
                if both:
                    return max(both, key=lambda c: (sc.get(c, 0), live[c]["order"]))
        if trusted is not None:
            named = [t for t in live[trusted]["wolves"] if t in targets]
            if named:
                return min(named)
        cleared = set()
        if trusted is not None:
            cleared = live[trusted]["clears"] | {trusted}
        pool = [t for t in targets if t not in cleared] or targets
        return max(pool, key=lambda s: (sc.get(s, 0) + 0.4 * press.get(s, 0) + 0.5 * calls.get(s, 0), -s))

    def _wolf_target_vote(self, raw, allies, targets):
        me, alive, dead, votes = _public(raw)
        claims, press, calls = self._claims(raw)
        partner = [a for a in allies if a in alive]
        accusers = [c for c, v in claims.items() if c in targets and v["wolves"] & ({me} | set(allies))]
        if accusers:
            return min(accusers)
        # bus a partner who is already caught (named by a lone claimant, or carries the table's calls)
        pool = [t for t in targets if t not in allies]
        if not pool:
            return None
        hot = max(pool, key=lambda s: (calls.get(s, 0) + press.get(s, 0), -s))
        if calls.get(hot, 0) + press.get(hot, 0) > 0:
            return hot
        sc = self._suspicion(raw, claims)
        return max(pool, key=lambda s: (sc.get(s, 0), -s))

    def _kill_target(self, raw, allies, targets):
        me, alive, dead, votes = _public(raw)
        claims, press, calls = self._claims(raw)
        pool = [t for t in targets if t not in allies]
        seers = [c for c in claims if c in pool]
        if seers:
            return min(seers)
        # the most trusted, most talkative non-wolf: lowest suspicion, then lowest seat (both wolves agree)
        sc = self._suspicion(raw, claims)
        talk = defaultdict(int)
        for m in self.msgs.values():
            if not m.get("recipients"):
                talk[m.get("sender")] += 1
        return min(pool, key=lambda s: (sc.get(s, 0) - 0.05 * talk[s], s)) if pool else None

    # -- acting --------------------------------------------------------------------------------------------------
    def choose(self, raw: dict) -> str:
        self._read(raw)
        legal = _legal(raw)
        role, allies, inv = _private(raw)
        gs = raw.get("game_state") or {}
        sub = gs.get("phase")
        targets = [int(a) for a in legal if a != str(ABSTAIN)]
        pick = None
        if sub == "night_wolf":
            pick = self._kill_target(raw, allies, targets)
        elif sub == "night_seer":
            me, alive, dead, votes = _public(raw)
            claims, press, calls = self._claims(raw)
            sc = self._suspicion(raw, claims)
            pool = [t for t in targets if t not in inv and t not in dead] or targets
            pick = max(pool, key=lambda s: (sc.get(s, 0) + 0.3 * press.get(s, 0), -s))
        else:
            pick = self._vote(raw, role, allies, inv, targets)
            if self.probe and self.probe[0] == "wagon" and gs.get("day") == 1 and PROBE_TARGET in targets:
                pick = PROBE_TARGET
            if pick is None:
                return str(ABSTAIN) if str(ABSTAIN) in legal else legal[0]
        return str(pick) if pick is not None and str(pick) in legal else legal[0]

    def _stall(self, raw) -> int:
        n = 0
        for v in reversed((raw.get("game_state") or {}).get("vote_history") or []):
            if v.get("eliminated"):
                break
            n += 1
        return n

    def _vote(self, raw, role, allies, inv, targets):
        if role == "wolf":
            return self._wolf_target_vote(raw, allies, targets)
        if self.ABSTAIN and self._stall(raw) < 2 and self.rng.random() < self.ABSTAIN:
            return None
        return self._villager_target(raw, role, inv, targets)

    # -- talking -------------------------------------------------------------------------------------------------
    def message(self, raw: dict):
        self._read(raw)
        self.me = raw.get("your_position")
        gs = raw.get("game_state") or {}
        day = gs.get("day")
        if self.plan_day != day:
            self.plan_day, self.round, self.plan, self.sent = day, 0, None, 0
        else:
            self.round += 1
        if self.plan is None:
            self.plan = self._make_plan(raw)
        # plan: list of (round, message-builder or dict); hold until the round comes, terminate when empty
        while self.plan and self.sent < MAX_CHATS:
            rnd, item = self.plan[0]
            if rnd > self.round:
                return SIM_HOLD
            self.plan.pop(0)
            m = item(raw) if callable(item) else item
            if m:
                self.sent += 1
                return m
        return None

    def _make_plan(self, raw) -> list:
        """The day's talk as [(round, builder)], built from the state at the window's first ask."""
        rng = self.rng
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        plan = []
        if self.probe and self.probe[0] == "noise":
            return self._noise_plan(raw, self.probe[1], int(self.probe[2]) if len(self.probe) > 2 else 1)
        if self.probe and self.probe[0] == "wagon":
            # CHAOS_PROBE=wagon:<seat format>[:<round>]: on day 1 every chaos seat pushes the seat the DRIVER names
            # (PROBE_TARGET, a measurement hook: a real seat cannot know it) in that format, nothing else is said
            day = (raw.get("game_state") or {}).get("day") or 1
            t = PROBE_TARGET
            if day == 1 and t is not None and t != me and t in alive:
                rnd = int(self.probe[2]) if len(self.probe) > 2 else 1
                plan.append((rnd, self.say(f"vote {fmt_seat(t, self.probe[1])}", {"vote": {t}})))
            return plan
        pf = self._probe_fmt()
        if pf is not None:
            if role == "seer":
                fmt, sfmt, rnd = pf
                parts = self._probe_claim(raw, fmt, sfmt)
                for j, p in enumerate(parts):
                    plan.append((rnd + j, p))
            elif role == "wolf":
                plan.append((1, self._wolf_dm))
                if not os.environ.get("CHAOS_QUIET"):
                    plan.append((0, self._villager_open))
            elif not os.environ.get("CHAOS_QUIET"):
                plan.append((0, self._villager_open))
            if not os.environ.get("CHAOS_QUIET"):        # CHAOS_QUIET=1: only the seer talks (recognition test)
                plan.append((max(3, pf[2] + 1), self._push_vote))
            plan.sort(key=lambda x: x[0])
            return plan
        if role == "seer":
            claim_round = 4 if rng.random() < self.LATE_CLAIM else 0
            plan.append((claim_round, self._seer_claim))
        elif role == "wolf":
            plan.append((0, self._wolf_open))
            plan.append((1, self._wolf_dm))
        else:
            plan.append((0, self._villager_open))
        plan.append((2, self._react))
        plan.append((3, self._push_vote))
        return plan

    NOISE_P = 0.5               # share of the chaos villagers that inject the noise in a noise probe

    def _noise_plan(self, raw, kind: str, rnd: int) -> list:
        """CHAOS_PROBE=noise:<kind>[:<round>]: the seer claims in plain words at round 0 (true, uncontested by
        default) and chaos villagers add one kind of odd-but-legal talk at <round>, naming a random seat y.
        Kinds that change the claim order instead: wolffirst (a wolf fake-claims at round 0 naming a villager, the
        seer claims at round 1), wolflast (the seer claims at round 0, a wolf counter-claims at the last round)."""
        rng = self.rng
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        day = (raw.get("game_state") or {}).get("day") or 1
        plan = []
        if role == "seer":
            plan += [(1 if kind == "wolffirst" else 0, p) for p in self._probe_claim(raw, "plain", "Player")]
        elif role == "wolf":
            plan.append((0, self._villager_open))
            plan.append((1, self._wolf_dm))
            first = min(a for a in [me] + [x for x in allies if x in alive])
            if kind in ("wolffirst", "wolflast") and me == first and day == 1:
                pool = [s for s in alive if s != me and s not in allies]
                w = rng.choice(pool)
                self.claimed = True
                txt = self.say(f"I am the Seer. Player{w} is a wolf. Vote Player{w}.",
                               {"claim": "seer", "wolves": {w}, "vote": {w}, "fmt": f"fake:{kind}"})
                plan.append((0 if kind == "wolffirst" else 5, txt))
            if kind == "dmwolf":
                pool = [s for s in alive if s != me and s not in allies]
                if pool and rng.random() < self.NOISE_P:
                    to = rng.choice(pool)
                    y = rng.choice([s for s in pool if s != to] or pool)
                    plan.append((rnd, self.say(f"tonight Player{y}?", {"noise": (kind, y)}, to=to)))
        else:
            quiet = bool(os.environ.get("CHAOS_QUIET"))
            if not quiet:
                plan.append((0, self._villager_open))
            others = [s for s in alive if s != me]
            if others and rng.random() < self.NOISE_P and kind not in ("wolffirst", "wolflast", "dmwolf"):
                y = rng.choice(others)
                for j, item in enumerate(self._noise_items(raw, kind, y)):
                    plan.append((rnd + j, item))
        if not os.environ.get("CHAOS_QUIET"):
            plan.append((max(3, rnd + 2), self._push_vote))
        plan.sort(key=lambda x: x[0])
        return plan

    def _noise_items(self, raw, kind: str, y: int) -> list:
        me, alive, dead, votes = _public(raw)
        rng = self.rng
        tag = {"noise": (kind, y)}
        P = f"Player{y}"
        if kind == "quote":
            def q(raw2, y=y):
                claims = [m for m in sorted(self.msgs.values(), key=lambda m: m["seq"]) if not m.get("recipients")
                          and (_MEANING.get((m["sender"], m["content"])) or {}).get("claim") == "seer"]
                if not claims:
                    return self.say(f"{P} is quiet today", tag)
                m = claims[0]
                return self.say(f'Player{m["sender"]} said "{m["content"]}" ok then', {"noise": ("quote", None)})
            return [q]
        if kind == "quotefake":
            x = rng.choice([s for s in alive if s not in (me, y)] or [y])
            return [self.say(f'I heard Player{x} say "I am the Seer. {P} is a wolf." earlier?', tag)]
        texts = {
            "sarcasm": [f"oh sure, I'm TOTALLY the Seer and {P} is a wolf /s"],
            "hypo": [f"If I were the Seer I would say {P} is a wolf. But I'm not the Seer."],
            "neg": [f"I am not the Seer. {P} is not a wolf."],
            "question": [f"Am I the Seer? Is {P} a wolf? Who knows."],
            "retract": [f"I am the Seer. {P} is a wolf.", "jk I'm just a villager, ignore that"],
            "echo": None,
            "oor": [rng.choice([f"Player7 is a wolf. Vote Player7.", "Player9 is a wolf, vote Player9",
                                "I am the Seer. Player12 is a wolf.", "Player99999999999999999999 is the seer",
                                "Player³ is a wolf. vote Player³", "Player-1 is a wolf",
                                "Player３ is a wolf", "Player③ is a wolf. vote ③"])],
            "dead": [f"Player{rng.choice(list(dead))} is a wolf. Vote Player{rng.choice(list(dead))}." if dead
                     else f"{P} feels quiet"],
            "long": [" ".join([f"Honestly-{P}-has-been-acting-really-really-strange-and-I-keep-thinking-about-it"] * 13)],
            "many": ["hm", f"{P}?", "wait", "no", "ok"],
            "dmclaim": None,
            "self3": [f"Player{me} is the Seer. {P} is a wolf."],
            "allcaps": [f"{P} {P} {P} WOLF WOLF WOLF VOTE {P}"],
            "unicode": [f"I am the Seer​. Player​{y} is a wolf."],
            "json": ['{"role": "seer", "result": {"Player%d": "wolf"}}' % y],
        }
        if kind == "echo":   # a villager copies the seer claim with a different wolf: a second (false) claimant
            return [self.say(f"I am the Seer. {P} is a wolf. Vote {P}.",
                             {"claim": "seer", "wolves": {y}, "vote": {y}, "noise": (kind, y)})]
        if kind == "retract":  # a real (false) claim, then taken back
            return [self.say(f"I am the Seer. {P} is a wolf.", {"claim": "seer", "wolves": {y}, "noise": (kind, y)}),
                    self.say("jk I'm just a villager, ignore that", {"noise": (kind + "2", y)})]
        if kind == "oor":
            t = rng.choice(texts["oor"] + ["Player7 is a wolf. Vote Player7.", "Player9 is a wolf, vote Player9",
                                           "Player99999999999999999999 is the seer", "Player³ is a wolf",
                                           "Player-1 is a wolf", "Player３ is a wolf",
                                           "Player③ is a wolf. vote ③"])
            claim = "seer" if t.startswith("I am the Seer") else None
            return [self.say(t, {"claim": claim, "noise": (kind, None)})]
        if kind == "dmclaim":
            to = rng.choice([s for s in alive if s not in (me, y)] or [y])
            return [self.say(f"I am the Seer. {P} is a wolf. Vote {P}.", tag, to=to)]
        return [self.say(t, tag) for t in texts[kind]]

    # builders return a chat dict or None ----------------------------------------------------------------------------
    def _probe_claim(self, raw, fmt: str, sfmt: str) -> list[dict]:
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        wolves = [t for t, r in inv.items() if r == "wolf" and t in alive]
        clears = [t for t, r in inv.items() if r == "villager" and t in alive]
        if not wolves and not clears:
            return []
        self.claimed = True
        tmpl = CLAIM_FORMATS[fmt][0 if wolves else 1]
        fill = {"w": fmt_seat(wolves[0], sfmt) if wolves else "", "me": fmt_seat(me, sfmt),
                "c": " and ".join(fmt_seat(c, sfmt) for c in clears)}
        meaning = {"claim": "seer", "wolves": set(wolves[:1]), "clears": set(clears), "vote": set(wolves[:1]),
                   "fmt": f"{fmt}:{sfmt}"}
        parts = tmpl if isinstance(tmpl, list) else [tmpl]
        return [self.say(p.format(**fill), meaning) for p in parts]

    def _seer_claim(self, raw):
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        rng = self.rng
        wolves = [t for t, r in inv.items() if r == "wolf" and t in alive]
        clears = [t for t, r in inv.items() if r == "villager" and t in alive]
        if not wolves and not clears:
            return None
        self.claimed = True
        return self._claim_text(wolves[:1], clears, rng)

    def _claim_text(self, wolves, clears, rng):
        sn = lambda s: seat_name(s, rng)          # noqa: E731
        meaning = {"claim": "seer", "wolves": set(wolves), "clears": set(clears), "vote": set(wolves)}
        style = rng.randrange(4)
        if wolves:
            w = wolves[0]
            if style == 0:
                txt = f"im the {typo('seer', rng)}. checked {sn(w)} -> {typo('wolf', rng)}. {typo('vote', rng)} {sn(w)}"
            elif style == 1:
                txt = (f"Seer-claim: I-am-the-seer-and-last-night-I-investigated-{sn(w).replace(' ', '-')}-and-the-"
                       f"result-was-WOLF-so-everyone-please-vote-{sn(w).replace(' ', '-')}-today-no-abstaining")
            elif style == 2:
                txt = f"ok. seer here. {sn(w)}={typo('wolf', rng)}." + (
                    "".join(f" {sn(c)}=clean." for c in clears[:2]))
            else:
                txt = f"As the seer: {sn(w)} is a {typo('wolf', rng)}! vote {sn(w)}"
        else:
            cs = " and ".join(sn(c) for c in clears[:3])
            txt = [f"im the seer, {cs} {'is' if len(clears) == 1 else 'are'} clean (villager)",
                   f"seer here. checked {cs}: villager.",
                   f"As the seer I cleared {cs}. town."][style % 3]
        return self.say(txt, meaning)

    def _wolf_open(self, raw):
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        rng = self.rng
        claims, press, calls = self._claims(raw)
        day = (raw.get("game_state") or {}).get("day") or 1
        pool = [s for s in alive if s != me and s not in allies]
        if not pool:
            return None
        partner_claimed = any(a in claims for a in allies)
        if day == 1 and not claims and not partner_claimed and rng.random() < self.FAKE_CLAIM_D1:
            w = rng.choice(pool)
            self.fake[w] = "wolf"
            self.claimed = True
            return self._claim_text([w], [], rng)
        if self.claimed and self.fake and self.KEEP_FAKE:
            # keep the fake going: a new "check" each day
            fresh = [s for s in pool if s not in self.fake]
            if fresh:
                t = rng.choice(fresh)
                self.fake[t] = "wolf" if rng.random() < 0.5 else "villager"
            ws = [t for t, r in self.fake.items() if r == "wolf" and t in alive]
            cs = [t for t, r in self.fake.items() if r == "villager" and t in alive]
            return self._claim_text(ws[:1], cs, rng) if ws or cs else None
        return self._villager_open(raw)

    def _wolf_dm(self, raw):
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        partner = [a for a in allies if a in alive]
        if not partner:
            return None
        tgt = self._kill_target(raw, allies, [s for s in alive if s not in allies and s != me])
        if tgt is None:
            return None
        return self.say(f"tonight {seat_name(tgt, self.rng)}", {"vote": {tgt}}, to=partner[0])

    def _villager_open(self, raw):
        me, alive, dead, votes = _public(raw)
        rng = self.rng
        others = [s for s in alive if s != me]
        lines = ["hm", "ok who do we like today", "no info yet, watching votes", "hi. villager. lets not abstain",
                 f"{seat_name(rng.choice(others), rng)} feels quiet", "same as yesterday?"]
        return self.say(rng.choice(lines))

    def _react(self, raw):
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        rng = self.rng
        claims, press, calls = self._claims(raw)
        accused = [c for c, v in claims.items() if me in v["wolves"] and c != me]
        if accused:
            c = accused[0]
            if role == "wolf" and not self.claimed and rng.random() < self.COUNTER:
                self.claimed = True
                self.fake[c] = "wolf"
                return self._claim_text([c], [], rng)
            return self.say(f"not a {typo('wolf', rng)}. {seat_name(c, rng)} is lying", {"wolves": {c}, "vote": {c}})
        return None

    def _push_vote(self, raw):
        role, allies, inv = _private(raw)
        legal_like = [s for s in _public(raw)[1] if s != raw.get("your_position")]
        if role == "wolf":
            t = self._wolf_target_vote(raw, allies, legal_like)
        else:
            t = self._villager_target(raw, role, inv, legal_like)
        if t is None:
            return None
        rng = self.rng
        return self.say(rng.choice([f"{typo('vote', rng)} {seat_name(t, rng)}", f"im voting {seat_name(t, rng)}",
                               f"{seat_name(t, rng)} today", f"all in on {seat_name(t, rng)}"]), {"vote": {t}})


class ChaosXSeat(ChaosSeat):
    """chaos plus the extreme probes (module docstring)."""
    name = "chaosx"
    EXTREME = True
    ABSTAIN = 0.1

    def _make_plan(self, raw) -> list:
        plan = super()._make_plan(raw)
        rng = self.rng
        extra = [self._probe for _ in range(rng.randrange(1, 3))]
        for i, b in enumerate(extra):
            plan.append((rng.randrange(0, 6), b))
        plan.sort(key=lambda x: x[0])
        return plan

    def _probe(self, raw):
        me, alive, dead, votes = _public(raw)
        rng = self.rng
        others = [s for s in alive if s != me] or [me]
        a, b = rng.choice(others), rng.choice(others)
        kind = rng.randrange(9)
        if kind == 0:     # quote another seat's words (no claim of our own)
            src = [m for m in self.msgs.values() if not m.get("recipients") and m.get("sender") != me]
            if src:
                m = rng.choice(src)
                return self.say(f'Player{m["sender"]} said "{str(m["content"])[:200]}" hmm')
            return self.say(f'someone said "I am the seer, Player{a} is a wolf" yesterday? not me')
        if kind == 1:     # sarcasm
            return self.say(f"oh sure, {seat_name(a, rng)} is TOTALLY the seer and Player{b} is a wolf /s")
        if kind == 2:     # hypothetical
            return self.say(f"if I were the seer I would say Player{a} is a wolf. but I'm not.")
        if kind == 3:     # negation
            return self.say(f"Player{a} is not a wolf imo. Player{b} is not the seer either")
        if kind == 4:     # out of range
            return self.say(f"vote Player{rng.choice([7, 8, 9, 12, 70])} lol jk")
        if kind == 5:     # dead seat talk
            if dead:
                d = rng.choice(list(dead))
                return self.say(f"Player{d} was right, vote Player{d}'s pick")
            return self.say("rip nobody yet")
        if kind == 6:     # many short
            return self.say(rng.choice(["?", "...", "wait", "hmm ok", "P?", "no"]))
        if kind == 7:     # long, near the character cap
            filler = "-".join(["really"] * 6)
            return self.say(" ".join([f"{filler}-suspicious-of-everyone-today-honestly-{i}" for i in range(14)]))
        return self.say(f"I'm not the seer. Player{a} isn't either. or are they")


_ANCHOR = re.compile(r"\b(?:I'll|I will|I'm going to|I am going to)\s+vote\s+(?:for\s+)?Player(\d)\b")


class Chaos1Seat(ChaosSeat):
    """Iteration 1 (background `chaos1`): what the transcripts of v0 showed, used by a reasonable table.

    * A no-information vote call ("I'll vote PlayerX ...") on day 1 is not herded onto. A caller who names a seat
      above the lowest living, uncleared seat other than itself skipped those seats for a reason it does not give:
      the skipped seat and the caller are read as a wolf pair (the caller weighs most).
    * Wolves ride the first vote call that names a non-wolf (a table that already has a target needs no push),
      and when a call names their partner before any seer claim, one wolf (the lower seat of the pair) fake-claims
      seer clearing the partner, which moves a careful caller to the next seat.
    * Night kill: a claimed seer, else the seat whose vote calls the table followed (the leader), else the most
      trusted talker.
    """
    name = "chaos1"
    HERD = 0.0                 # weight of an unverified vote call (v0: 0.5)

    def _calls_and_skips(self, raw):
        """(first non-claim vote call of today: (caller, target) or None, wolf-pair suspicion from anchor skips)."""
        me, alive, dead, votes = _public(raw)
        day = (raw.get("game_state") or {}).get("day") or 1
        cleared: set = set()
        first = None
        skip_sc: dict[int, float] = defaultdict(float)
        claimed = False
        for seq in sorted(self.msgs):
            m = self.msgs[seq]
            if m.get("recipients") or m.get("type", "chat") != "chat" or m.get("day", day) != day:
                continue
            s = m.get("sender")
            text = str(m.get("content") or "")
            mm = meaning_of(text, s)
            if mm["claim"] == "seer":
                claimed = True
                cleared |= mm["clears"]
                continue
            hit = _ANCHOR.search(text)
            if not hit:
                continue
            t = int(hit.group(1))
            if first is None and t in alive:
                first = (s, t)
            if day == 1 and not claimed:
                pool = [i for i in alive if i != s and i not in cleared]
                if pool and t in pool and t != pool[0]:
                    skipped = [i for i in pool if i < t]
                    skip_sc[s] += 6.0
                    if skipped:
                        skip_sc[skipped[0]] += 4.0
        return first, skip_sc

    def _suspicion(self, raw, claims) -> dict[int, float]:
        sc = dict(super()._suspicion(raw, claims))
        _, skip_sc = self._calls_and_skips(raw)
        for s, x in skip_sc.items():
            if s in sc:
                sc[s] += x
        return sc

    def _villager_target(self, raw, role, inv, targets):
        me, alive, dead, votes = _public(raw)
        claims, press, calls = self._claims(raw)
        sc = self._suspicion(raw, claims)
        hot = [t for t in targets if sc.get(t, 0) >= 4.0 and inv.get(t) != "villager"]
        if hot and not any(inv.get(t) == "wolf" for t in targets):
            return max(hot, key=lambda s: (sc[s], -s))
        return super()._villager_target(raw, role, inv, targets)

    def _claims(self, raw):
        claims, press, calls = super()._claims(raw)
        return claims, press, defaultdict(float, {t: self.HERD * c for t, c in calls.items()})

    def _wolf_target_vote(self, raw, allies, targets):
        me, alive, dead, votes = _public(raw)
        claims, press, calls = ChaosSeat._claims(self, raw)
        accusers = [c for c, v in claims.items() if c in targets and v["wolves"] & ({me} | set(allies))]
        if accusers:
            return min(accusers)
        first, _ = self._calls_and_skips(raw)
        if first is not None and first[1] in targets and first[1] not in allies and first[0] not in allies:
            return first[1]
        return super()._wolf_target_vote(raw, allies, targets)

    def _kill_target(self, raw, allies, targets):
        me, alive, dead, votes = _public(raw)
        claims, press, calls = ChaosSeat._claims(self, raw)
        pool = [t for t in targets if t not in allies]
        seers = [c for c in claims if c in pool]
        if seers:
            return min(seers)
        # the leader: whose day vote call matched the day's elimination, most often
        lead: dict[int, int] = defaultdict(int)
        by_day: dict[int, list] = defaultdict(list)
        for m in self.msgs.values():
            if m.get("recipients"):
                continue
            hit = _ANCHOR.search(str(m.get("content") or ""))
            if hit:
                by_day[m.get("day")].append((m["seq"], m.get("sender"), int(hit.group(1))))
        gs = raw.get("game_state") or {}
        seat_of = {p["name"]: p["position"] for p in raw.get("players") or []}
        for vh in gs.get("vote_history") or []:
            el = seat_of.get(vh.get("eliminated"))
            for _, s, t in sorted(by_day.get(vh.get("day"), [])):
                if t == el and s in pool:
                    lead[s] += 1
        if lead:
            top = max(lead.values())
            return min(s for s in pool if lead.get(s) == top)
        return super()._kill_target(raw, allies, targets)

    def _make_plan(self, raw) -> list:
        plan = super()._make_plan(raw)
        role, allies, inv = _private(raw)
        if role == "wolf" and not self._probe_fmt() and not (self.probe and self.probe[0] == "noise"):
            plan.append((1, self._deflect))
            plan.sort(key=lambda x: x[0])
        return plan

    def _deflect(self, raw):
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        partner = [a for a in allies if a in alive]
        if not partner or self.claimed or me > partner[0]:
            return None
        claims, press, calls = ChaosSeat._claims(self, raw)
        if claims:
            return None
        first, _ = self._calls_and_skips(raw)
        if first is None or first[1] != partner[0] or first[0] in allies:
            return None
        self.claimed = True
        self.fake[partner[0]] = "villager"
        p = partner[0]
        return self.say(f"I am the Seer. Player{p} is a villager.", {"claim": "seer", "clears": {p}})


class Chaos2Seat(Chaos1Seat):
    """Iteration 2 (background `chaos2`): chaos1 plus the two-claim exploit seen in the transcripts.

    Between two seer claimants a reader of this table (the agent) votes the one accused by the other when only one
    accusation runs between them, the later one when they accuse each other, else the lower seat. So when a seer
    claim names a wolf, the wolf it did NOT name counter-claims seer at once and calls the claimant a wolf (a
    one-sided accusation); when a seer claim only clears, a wolf counter-claims the same way. A named wolf whose
    partner is gone stays quiet (any counter of its own makes it the accused or the later claimant). The fake claim
    is in plain words so every reader takes it as a claim.
    """
    name = "chaos2"
    FAKE_CLAIM_D1 = 0.0        # no unprompted fake claim: a pre-emptive claim becomes the accused one later
    TWO_CLAIM = "random"       # a neutral table (like local/werewolf_bots.LLMSeat): either claimant
    KEEP_FAKE = False

    def _make_plan(self, raw) -> list:
        plan = super()._make_plan(raw)
        role, allies, inv = _private(raw)
        if role == "wolf" and not self._probe_fmt() and not (self.probe and self.probe[0] == "noise"):
            for r in range(0, 6):
                plan.append((r, self._counter))
            plan.sort(key=lambda x: x[0])
        return plan

    def _counter(self, raw):
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        if self.claimed:
            return None
        claims, press, calls = ChaosSeat._claims(self, raw)
        live = {c: v for c, v in claims.items() if c in alive and c != me and c not in allies}
        if not live:
            return None
        partner = [a for a in allies if a in alive]
        for c, v in sorted(live.items(), key=lambda kv: kv[1]["order"]):
            named = v["wolves"]
            if me in named:
                continue                                   # the accused wolf never counters
            if named and not (named & set(partner)):
                continue                                   # it named a villager: let it stand
            if partner and any(p in named for p in partner) or not named:
                # the partner (or nobody) is named: counter, accusing the claimant only
                if not named and partner and me > partner[0]:
                    continue                               # a clear-only claim: the lower wolf counters
                self.claimed = True
                self.fake[c] = "wolf"
                return self.say(f"I am the Seer. Player{c} is a wolf. Vote Player{c}.",
                                {"claim": "seer", "wolves": {c}, "vote": {c}})
        return None


class Chaos3Seat(Chaos2Seat):
    """Iteration 3 (background `chaos3`): chaos2 with a sharper village.

    * Once a claimant is revealed seer at death, every other living seer claimant is a known wolf and is voted
      before anything else; a claimant revealed villager or wolf lied, and so did whoever it "cleared" if a check of
      the revealed seer contradicts it.
    * Ties are broken by an order every chaos seat shares but that is not the seat order (v0-v2 broke them toward the
      lowest seat, the same seat the agent's day-1 call names).
    * Wolves keep chaos2's counter-claim; they no longer ride a vote call once any seer claim is out.
    """
    name = "chaos3"

    def _tie(self, raw, s: int) -> int:
        day = (raw.get("game_state") or {}).get("day") or 1
        return (s * 5 + day * 3) % 7

    def _villager_target(self, raw, role, inv, targets):
        me, alive, dead, votes = _public(raw)
        claims, press, calls = ChaosSeat._claims(self, raw)
        all_claimers = set()
        for m in self.msgs.values():
            if not m.get("recipients") and meaning_of(str(m.get("content") or ""), m.get("sender"))["claim"] == "seer":
                all_claimers.add(m.get("sender"))
        true_seer = [c for c in all_claimers if dead.get(c) == "seer"]
        if true_seer and role != "seer":
            liars = [c for c in all_claimers if c in targets and c not in true_seer]
            if liars:
                return min(liars, key=lambda s: self._tie(raw, s))
        if role == "seer":
            liars = [c for c in all_claimers if c in targets and c != me]
            known = [t for t in targets if inv.get(t) == "wolf"]
            if known:
                return min(known, key=lambda s: self._tie(raw, s))
            if liars:
                return min(liars, key=lambda s: self._tie(raw, s))
        sc = self._suspicion(raw, claims)
        hot = [t for t in targets if sc.get(t, 0) >= 4.0 and inv.get(t) != "villager"]
        if hot:
            return max(hot, key=lambda s: (sc[s], -self._tie(raw, s)))
        pick = ChaosSeat._villager_target(self, raw, role, inv, targets)
        if pick is None:
            return None
        # re-break the base rule's tie toward the lowest seat with the shared order
        base = sc.get(pick, 0) + 0.4 * press.get(pick, 0)
        ties = [t for t in targets if abs(sc.get(t, 0) + 0.4 * press.get(t, 0) - base) < 1e-9
                and inv.get(t) != "villager"]
        named = set()
        for c, v in claims.items():
            named |= v["wolves"] | v["clears"]
        if pick in named or len(ties) <= 1:
            return pick
        cleared = set()
        for c, v in claims.items():
            cleared |= v["clears"] | {c}
        ties = [t for t in ties if t not in cleared] or ties
        return min(ties, key=lambda s: self._tie(raw, s))

    def _wolf_target_vote(self, raw, allies, targets):
        me, alive, dead, votes = _public(raw)
        claims, press, calls = ChaosSeat._claims(self, raw)
        accusers = [c for c, v in claims.items() if c in targets and v["wolves"] & ({me} | set(allies))]
        if accusers:
            return min(accusers)
        if claims:
            pool = [t for t in targets if t not in allies]
            seers = [c for c in claims if c in pool]
            if seers:
                return min(seers)
        return super()._wolf_target_vote(raw, allies, targets)


class Chaos5Seat(Chaos3Seat):
    """Iteration 5 (background `chaos5`): chaos3 with the counter-claim held to the window's last rounds.

    The agent answers pressure only early in a window (it stops listening after roughly the second round), and its
    two-claim rule turns a one-sided accusation into a vote on the accused. A counter-claim made in round 4 or 5
    cannot be answered: the true seer it accuses never accuses back, so the accusation stays one-sided.
    """
    name = "chaos5"
    LATE_COUNTER = 4

    def _counter(self, raw):
        if self.round < self.LATE_COUNTER:
            return None
        return super()._counter(raw)


class _Noisy:
    """Mixin: add chaosx's odd-but-legal talk (quotes, sarcasm, hypotheticals, negations, out-of-range seats, dead
    seats, many short and near-1024-character chats) on top of a chaos strategy."""

    def _make_plan(self, raw) -> list:
        plan = super()._make_plan(raw)
        rng = self.rng
        for _ in range(rng.randrange(1, 3)):
            plan.append((rng.randrange(0, 6), self._probe))
        plan.sort(key=lambda x: x[0])
        return plan

    _probe = ChaosXSeat._probe


class Chaos4Seat(_Noisy, Chaos3Seat):
    """Iteration 4 (background `chaos4`): chaos3's strategy with chaosx's noise on top (robustness at full strength)."""
    name = "chaos4"


CHAOS_BACKGROUNDS = {"chaos": ChaosSeat, "chaosx": ChaosXSeat, "chaos1": Chaos1Seat, "chaos2": Chaos2Seat,
                     "chaos3": Chaos3Seat, "chaos4": Chaos4Seat, "chaos5": Chaos5Seat}


# ---------------------------------------------------------------- driver
class ProbeSeat(ContestantSeat):
    """Our agent, with its raw answers recorded for the anomaly checks."""

    def __init__(self, factory, seat: int, log: list):
        super().__init__(factory)
        self.seat, self.log = seat, log

    def choose(self, raw: dict) -> str:
        a = super().choose(raw)
        gs = raw.get("game_state") or {}
        role, allies, inv = _private(raw)
        alive = set(_public(raw)[1])
        self.log.append({"seat": self.seat, "kind": "act", "day": gs.get("day"), "sub": gs.get("phase"), "a": a,
                         "role": role, "inv": dict(inv), "alive": sorted(alive)})
        return a

    def message(self, raw: dict):
        m = super().message(raw)
        if isinstance(m, dict):
            c = str(m.get("content", ""))
            self.log.append({"seat": self.seat, "kind": "chat", "day": (raw.get("game_state") or {}).get("day"),
                             "chars": len(c), "words": len(c.split()), "content": c, "to": m.get("recipients")})
        return m


def anomalies(game: Game, ours: list[int], log: list) -> list[str]:
    out = []
    roles = game.roles
    for e in log:
        if e["kind"] == "chat":
            if e["chars"] > MAX_CHARS:
                out.append(f"seat{e['seat']} chat over 1024 chars ({e['chars']})")
            if e["words"] > MAX_WORDS:
                out.append(f"seat{e['seat']} chat over 50 words ({e['words']})")
            for n in re.findall(r"Player(\d+)", e["content"]):
                if int(n) >= N:
                    out.append(f"seat{e['seat']} chat names Player{n}: {e['content'][:120]!r}")
                elif not game.alive[int(n)] and any(d["seat"] == int(n) and d["day"] < (e["day"] or 0)
                                                   for d in game.dead) and re.search(
                        rf"\bvote\s+Player{n}\b", e["content"]):
                    out.append(f"seat{e['seat']} day{e['day']} calls a vote on dead Player{n}: {e['content'][:120]!r}")
        if e["kind"] == "act" and e["sub"] == "day_vote" and e["role"] == "seer":
            a = int(e["a"])
            known = [t for t, r in e["inv"].items() if r == "wolf" and t in e["alive"]]
            if e["inv"].get(a) == "villager":
                out.append(f"day{e['day']} seat{e['seat']} seer voted Player{a} it checked as villager")
            if known and a not in known:
                out.append(f"day{e['day']} seat{e['seat']} seer did not vote its living checked wolf {known}")
    stall = 0
    for v in game.votes:
        for s in ours:
            if s not in v["votes"]:
                continue
            t = v["votes"][s]
            if roles[s] != "wolf" and t is None and stall == 2:
                out.append(f"day{v['day']} seat{s} ({roles[s]}) abstained on stall 2")
            if roles[s] == "wolf" and t is not None and roles[t] == "wolf":
                out.append(f"day{v['day']} seat{s} wolf voted its ally {t}")
        stall = stall + 1 if v["eliminated"] is None else 0
    return out


def claim_follow(game: Game, ours: list[int]) -> list[dict]:
    """For each day a background seat made a seer claim (registered meaning): the first claim of the day, and the
    vote of every one of our villager-side seats alive at that vote. follow = voted a seat the claim called wolf;
    hit_clear = voted a seat it cleared."""
    out = []
    for v in game.votes:
        d = v["day"]
        claim = None
        for m in game.messages:
            if m["day"] != d or m["sender"] in ours or m["recipients"]:
                continue
            mm = _MEANING.get((m["sender"], m["content"]))
            if mm and mm.get("claim") == "seer":
                claim = (m, mm)
                break
        if claim is None:
            continue
        m, mm = claim
        for s in ours:
            if s in v["votes"] and game.roles[s] != "wolf":
                t = v["votes"][s]
                ack = any(x["sender"] == s and x["day"] == d and f"Seer (Player{m['sender']})" in x["content"]
                          for x in game.messages)
                out.append({"day": d, "fmt": mm.get("fmt"), "true": game.roles[m["sender"]] == "seer",
                            "kind": "wolf" if mm["wolves"] else "clear", "role": game.roles[s],
                            "follow": t in mm["wolves"], "hit_clear": t in mm["clears"], "abstain": t is None,
                            "vote_claimer": t == m["sender"], "ack": ack})
    return out


def noise_effect(game: Game, ours: list[int]) -> list[dict]:
    """For each day with noise (a background chat registered with a noise tag) visible to one of our villager-side
    seats: did that seat vote the seat y the noise named?"""
    out = []
    for v in game.votes:
        d = v["day"]
        for m in game.messages:
            if m["day"] != d or m["sender"] in ours:
                continue
            mm = _MEANING.get((m["sender"], m["content"])) or {}
            nz = mm.get("noise")
            if not nz:
                continue
            kind, y = nz
            for s in ours:
                if s in v["votes"] and game.roles[s] != "wolf" and (not m["recipients"] or s in m["recipients"]):
                    out.append({"kind": kind + (":dm" if m["recipients"] else ""), "voted_y": y is not None
                                and v["votes"][s] == y, "y_wolf": y is not None and game.roles[y] == "wolf",
                                "voted_sender": v["votes"][s] == m["sender"]})
    return out


def two_claims(game: Game, ours: list[int]) -> list[dict]:
    """Days where the real seer (a background seat) and a wolf both claimed seer: our villager-side votes."""
    out = []
    for v in game.votes:
        d = v["day"]
        cl = {}
        for m in game.messages:
            if m["day"] == d and not m["recipients"] and m["sender"] not in ours:
                mm = _MEANING.get((m["sender"], m["content"])) or {}
                if mm.get("claim") == "seer":
                    cl.setdefault(m["sender"], mm)
        seers = [c for c in cl if game.roles[c] == "seer"]
        fakes = [c for c in cl if game.roles[c] == "wolf"]
        if not seers or not fakes:
            continue
        s0, f0 = seers[0], fakes[0]
        for s in ours:
            if s in v["votes"] and game.roles[s] != "wolf":
                t = v["votes"][s]
                out.append({"seer_named_wolf": bool(cl[s0]["wolves"]), "fake_accuses_seer": s0 in cl[f0]["wolves"],
                            "voted": "true seer" if t == s0 else "fake claimer" if t == f0 else
                            "seer's named wolf" if t in cl[s0]["wolves"] else "abstain" if t is None else "other"})
    return out


_AGENT_CLAIMANTS = [re.compile(r"Seer \((Player\d+)\)"),re.compile(r"Seer claims?: ((?:Player\d+(?:,| and| &)? ?)+)")]


def phantom_claimants(game: Game, ours: list[int], log: list) -> list[str]:
    """Seats our agent's chat treats as seer claimants ("Following the Seer (PlayerX)", "Two Seer claims: PlayerX
    and PlayerY") that never made a seer claim by then (background claims as registered, ours by our own words)."""
    out = []
    for e in log:
        if e["kind"] != "chat":
            continue
        named = set()
        for rx in _AGENT_CLAIMANTS:
            for g in rx.findall(e["content"]):
                named |= {int(x) for x in re.findall(r"Player(\d+)", g if isinstance(g, str) else g[0])}
        if not named:
            continue
        true = set()
        for m in game.messages:
            if m["day"] > e["day"] or m["recipients"]:
                continue
            if m["sender"] in ours:
                if re.search(r"\bI am the Seer\b", m["content"]):
                    true.add(m["sender"])
            elif (_MEANING.get((m["sender"], m["content"])) or {}).get("claim") == "seer":
                true.add(m["sender"])
        for s in sorted(named - true - {e["seat"]}):
            out.append(f"day{e['day']} seat{e['seat']} treats Player{s} as a seer claimant: {e['content'][:140]!r}")
    return out


def transcript(game: Game, ours: list[int]) -> str:
    lines = [f"roles: " + " ".join(f"{i}:{r[0].upper()}{'*' if i in ours else ''}" for i, r in enumerate(game.roles))
             + f" | winner {game.winner}"]
    days = sorted({m["day"] for m in game.messages} | {v["day"] for v in game.votes} | {d["day"] for d in game.dead})
    for d in days:
        for x in game.dead:
            if x["day"] == d and x["cause"] == "night_kill":
                lines.append(f" day{d} dawn: P{x['seat']} ({x['role']}) killed")
        for m in game.messages:
            if m["day"] == d:
                s = m["sender"]
                tag = f"P{s}{'*' if s in ours else ''}[{game.roles[s][0].upper()}]"
                to = f" ->P{m['recipients'][0]}" if m["recipients"] else ""
                lines.append(f"  {tag}{to}: {m['content']}")
        for v in game.votes:
            if v["day"] == d:
                vs = " ".join(f"{a}>{'x' if b is None else b}" for a, b in v["votes"].items())
                lines.append(f"  vote: {vs} => {v['eliminated']}")
        for x in game.dead:
            if x["day"] == d and x["cause"] != "night_kill":
                lines.append(f"  P{x['seat']} ({x['role']}) voted out")
    return "\n".join(lines)


def play(spec: str, seed: int, g: int, bg: str, k: int, control: bool = False):
    """One deal: the agent in k seats (control=True: background seats there instead). -> (game, ours, log)."""
    global PROBE_TARGET
    from .eval_werewolf import deal, factory, make_seat, parse_bg
    # CHAOS_DEAL_AS=<bg>: deal as that background (same roles and seats), to pair two chaos versions game by game
    gseed, ours, types = deal(seed, g, os.environ.get("CHAOS_DEAL_AS") or bg, k)
    base = parse_bg(bg)[1][0]
    types = {i: base for i in types}
    PROBE_TARGET = ours[0] if ours and (os.environ.get("CHAOS_PROBE") or "").startswith("wagon") else None
    log: list = []
    seats = []
    for i in range(N):
        if i in ours and not control:
            seats.append(ProbeSeat(factory(spec), i, log))
        else:
            seats.append(make_seat(types.get(i, base)))
    game = Game(seats, random.Random(gseed))
    game.play()
    return game, ours, log


def _chunk(args):
    spec, seed, bg, k, games, show, control = args
    os.environ["ARENA_RECORD"] = "0"
    out = []
    for g in games:
        game, ours, log = play(spec, seed, g, bg, k)
        rec = {"g": g, "roles": [game.roles[i] for i in ours], "winner": game.winner, "days": game.day,
               "fail": {key: sum(game.stats[key][i] for i in ours) for key in
                        ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate", "window_close")},
               "max_ms": max(game.stats["max_ms"][i] for i in ours),
               "anom": anomalies(game, ours, log) + phantom_claimants(game, ours, log),
               "follow": claim_follow(game, ours), "noise": noise_effect(game, ours),
               "two": two_claims(game, ours),
               "chats": [(game.roles[e["seat"]], e["day"], e["content"], e.get("to")) for e in log
                         if e["kind"] == "chat"]}
        if control:
            cg, _, _ = play(spec, seed, g, bg, k, control=True)
            rec["ctrl_winner"] = cg.winner
        if show and g < games[0] + show:
            rec["transcript"] = transcript(game, ours)
        out.append(rec)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg", default="chaos")
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--seed", type=int, default=2400)
    ap.add_argument("--spec", default="agent")
    ap.add_argument("--show", type=int, default=0, help="print the transcripts of the first N games")
    ap.add_argument("--only", default="", help="with --show: only games where our seats hold this role")
    ap.add_argument("--lost", action="store_true", help="with --show: only games our seats lost")
    ap.add_argument("--control", action="store_true", help="also play each deal with background seats in ours")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--tells", action="store_true", help="the agent's chat templates by role")
    a = ap.parse_args(argv)
    os.environ["ARENA_RECORD"] = "0"
    from .eval_werewolf import wilson
    chunk = 50
    tasks = [(a.spec, a.seed, a.bg, a.k, list(range(lo, min(a.games, lo + chunk))), a.games if a.show else 0,
              a.control) for lo in range(0, a.games, chunk)]
    t0 = time.time()
    recs = []
    if a.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(a.jobs) as ex:
            for out in ex.map(_chunk, tasks):
                recs += out
    else:
        for t in tasks:
            recs += _chunk(t)
    side = {"wolf": "wolf", "seer": "villager", "villager": "villager"}
    shown = 0
    for r in recs:
        if "transcript" not in r or shown >= a.show:
            continue
        if a.only and a.only not in r["roles"]:
            continue
        won = r["winner"] == side[r["roles"][0]]
        if a.lost and won:
            continue
        shown += 1
        print(f"--- game {r['g']} ours {r['roles']} {'WON' if won else 'lost'}")
        print(r["transcript"])
        if r["anom"]:
            print("  ANOMALIES:", r["anom"])
    by = defaultdict(list)
    ctrl = defaultdict(list)
    for r in recs:
        for role in set(r["roles"]):
            by[role].append(1.0 if r["winner"] == side[role] else 0.0)
            if "ctrl_winner" in r:
                ctrl[role].append(1.0 if r["ctrl_winner"] == side[role] else 0.0)
        by["seat"].append(sum(1.0 for role in r["roles"] if r["winner"] == side[role]) / len(r["roles"]))
    print(f"\n{a.spec} in {a.k} seat(s) vs {a.bg}, seed {a.seed}, {len(recs)} games ({time.time() - t0:.0f}s)")
    for role in ("villager", "seer", "wolf", "seat"):
        v = by.get(role, [])
        if v:
            lo, hi = wilson(sum(v), len(v))
            extra = ""
            if ctrl.get(role):
                c = ctrl[role]
                extra = f" | {a.bg} seat in the same role, same deals: {100 * sum(c) / len(c):5.1f}"
            print(f"  {role:8s} {100 * sum(v) / len(v):5.1f} [{100 * lo:5.1f},{100 * hi:5.1f}] (n={len(v)}){extra}")
    fails = defaultdict(int)
    for r in recs:
        for key, x in r["fail"].items():
            fails[key] += x
    print("  failures:", dict(fails), f"slowest {max(r['max_ms'] for r in recs):.1f} ms")
    kinds = defaultdict(list)
    for r in recs:
        for x in r["anom"]:
            kinds[re.sub(r"\d+", "N", x.split(":")[0])].append(r["g"])
    for key, gs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        print(f"  anomaly {key}: {len(gs)} (games {sorted(set(gs))[:12]})")
    if a.tells:
        tpl = defaultdict(lambda: defaultdict(int))
        tot = defaultdict(int)
        for r in recs:
            for role, day, content, to in r["chats"]:
                key = re.sub(r"Player\d+", "P#", content)
                key = ("DM " if to else "") + ("d1 " if day == 1 else "d2+ ") + key
                tpl[key][role] += 1
                tot[role] += 1
        print(f"  agent chat templates by role (share of that role's chats; totals {dict(tot)}):")
        for key, d in sorted(tpl.items(), key=lambda kv: -sum(kv[1].values()))[:40]:
            cells = " ".join(f"{role[0].upper()} {100 * d.get(role, 0) / max(1, tot[role]):5.1f}%"
                             for role in ("villager", "seer", "wolf"))
            print(f"    {cells} | {key[:150]}")
    two = defaultdict(lambda: defaultdict(int))
    for r in recs:
        for x in r["two"]:
            key = (f"true seer named a wolf={x['seer_named_wolf']}, wolf's counter-claim accuses the seer="
                   f"{x['fake_accuses_seer']}")
            two[key][x["voted"]] += 1
    for key, d in sorted(two.items()):
        n = sum(d.values())
        print(f"  two claims ({key}): n={n} our villager-side votes: "
              + ", ".join(f"{k} {100 * v / n:.0f}%" for k, v in sorted(d.items(), key=lambda kv: -kv[1])))
    nz = defaultdict(list)
    for r in recs:
        for x in r["noise"]:
            nz[x["kind"]].append(x)
    for kind, xs in sorted(nz.items()):
        n = len(xs)
        print(f"  noise {kind}: n={n} our villager-side seat voted the named seat {100 * sum(x['voted_y'] for x in xs) / n:.0f}%"
              f" (named seat was a wolf {100 * sum(x['y_wolf'] for x in xs) / n:.0f}%), voted the noise's sender "
              f"{100 * sum(x['voted_sender'] for x in xs) / n:.0f}%")
    fol = defaultdict(list)
    for r in recs:
        for x in r["follow"]:
            fol[(x["fmt"], x["kind"], x["true"], x["day"] == 1)].append(x)
    if fol:
        print("  our villager-side votes after a background seer claim (first claim of the day):")
        for key, xs in sorted(fol.items(), key=lambda kv: str(kv[0])):
            n = len(xs)
            f = sum(x["follow"] for x in xs)
            hc = sum(x["hit_clear"] for x in xs)
            ab = sum(x["abstain"] for x in xs)
            vc = sum(x["vote_claimer"] for x in xs)
            ak = sum(x["ack"] for x in xs)
            print(f"    fmt {key[0]} {key[1]} {'true' if key[2] else 'FAKE'} {'day1' if key[3] else 'later'}: n={n} "
                  f"voted named wolf {100 * f / n:.0f}% | voted cleared {100 * hc / n:.0f}% | voted claimer "
                  f"{100 * vc / n:.0f}% | abstain {100 * ab / n:.0f}% | chat says 'Seer (claimer)' {100 * ak / n:.0f}%")


if __name__ == "__main__":
    # run the package module's main: eval_werewolf imports local.redteam_chaos, and the seats it makes register
    # their meanings in that module's _MEANING, not in this __main__ copy's
    from local.redteam_chaos import main as _main
    _main()
