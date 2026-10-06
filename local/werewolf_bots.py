"""Reference seats for the local Werewolf evaluation (local/eval_werewolf.py).

None of these import our agent: they are independent stand-ins for what we expect at a real table.

* `HeuristicSeat` -- modelled on the platform's demo `werewolf_agent.py` as docs/platform/werewolf.md section 14
  describes it: parses its role (wolf ally, seer results) from the observation, scores suspicion from the public
  death and vote record only (it does not read chat), wolves rank kill targets by the same deterministic key so they
  never split, never abstains, one bland chat line per day.
* `LLMSeat` -- a cheap stand-in for `examples/llm_agent.py` (gpt-4o-mini class): reads the broadcast chat and is
  easily persuaded by it, over-shares as seer, sometimes fake-claims seer as a wolf when accused, follows its wolf
  ally's private suggestion, is noisy (a random legal action now and then, including abstain) and does not
  coordinate unless told. Parameters are guesses, not measurements; the evaluation reports each background
  separately so no single guess decides the verdict.
* `LLMVariedSeat` -- the same decisions as `LLMSeat`, but phrased freely (casual forms, claim-looking table talk such
  as "Is the Seer here?", "All players must vote PlayerN") and less pulled toward the seat named most in chat: `LLMSeat`
  only ever writes in the format our chat reader was built on, which flatters us.
* `LLMQwenSeat` -- `LLMVariedSeat` writing the way the real Qwen3 seats of examples/llm_agent.py do
  (tests/strategy/data/ww_llm_chat.json): seer results by pronoun after a check, "the real wolf", "PlayerN is lying --
  they're a wolf", and the skeptical table talk that says nothing about anyone's role but looks like a verdict to a
  careless reader: hedges and negations about a seat ("that doesn't mean they're a wolf", "no proof they are wolves"),
  "Seer claim: none yet", last night's victim ("It was the wolves"). It measures both what reading real LLM chat gains
  and what misreading it costs.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .werewolf_env import ABSTAIN, Seat

_ROLE = re.compile(r"Your role:\s*(WOLF|SEER|VILLAGER)", re.I)
_ALLY = re.compile(r"Your fellow wolves:\s*(.+)")
_INV = re.compile(r"Player(\d+) is (wolf|villager)", re.I)
_SEAT = re.compile(r"Player(\d+)")


def _private(raw: dict) -> tuple[str, list[int], dict[int, str]]:
    obs = raw.get("observation") or ""
    m = _ROLE.search(obs)
    role = m.group(1).lower() if m else "villager"
    m = _ALLY.search(obs)
    allies = [int(x) for x in _SEAT.findall(m.group(1))] if m else []
    m = re.search(r"Your investigations:\s*(.+)", obs)
    inv = {int(s): r.lower() for s, r in _INV.findall(m.group(1))} if m else {}
    return role, allies, inv


def _public(raw: dict):
    """(me, alive seats, {seat: role} of the dead, [(votes {voter: target|None}, eliminated)])."""
    seat_of = {p["name"]: p["position"] for p in raw.get("players") or []}
    gs = raw.get("game_state") or {}
    alive = sorted(seat_of[n] for n in gs.get("alive") or [] if n in seat_of)
    dead = {seat_of[d["player"]]: d["role"] for d in gs.get("dead") or [] if d.get("player") in seat_of}
    votes = []
    for vh in gs.get("vote_history") or []:
        vv = {seat_of[a]: (seat_of.get(b) if b != "abstain" else None) for a, b in (vh.get("votes") or {}).items()
              if a in seat_of}
        votes.append((vv, seat_of.get(vh.get("eliminated"))))
    return raw.get("your_position"), alive, dead, votes


def _legal(raw: dict) -> list[str]:
    return [a["action_id"] for a in (raw.get("legal_actions") or {}).get("actions", [])]


class HeuristicSeat(Seat):
    name = "heuristic"

    @staticmethod
    def suspicion(raw: dict) -> dict[int, float]:
        me, alive, dead, votes = _public(raw)
        sc: dict[int, float] = defaultdict(float)
        for vv, elim in votes:
            er = dead.get(elim) if elim is not None else None
            for voter, tgt in vv.items():
                if tgt is None:
                    sc[voter] += 0.5
                elif tgt == elim:
                    sc[voter] += 1.0 if er in ("villager", "seer") else -1.0
        return {s: sc[s] for s in alive if s != me}

    def choose(self, raw: dict) -> str:
        legal = _legal(raw)
        targets = [int(a) for a in legal if a != str(ABSTAIN)]
        role, allies, inv = _private(raw)
        sub = (raw.get("game_state") or {}).get("phase")
        sc = self.suspicion(raw)
        top = lambda pool: max(pool, key=lambda s: (sc.get(s, 0.0), -s)) if pool else None   # noqa: E731
        if sub == "night_wolf":
            pool = [t for t in targets if t not in allies]
            pick = min(pool, key=lambda s: (sc.get(s, 0.0), s)) if pool else None   # the most trusted villager
        elif sub == "night_seer":
            pick = top([t for t in targets if t not in inv]) or (targets[0] if targets else None)
        else:
            known = [t for t in targets if inv.get(t) == "wolf"]
            if known:
                pick = min(known)
            else:
                pool = [t for t in targets if t not in allies and inv.get(t) != "villager"] or targets
                pick = top(pool)
        return str(pick) if pick is not None and str(pick) in legal else legal[0]

    def message(self, raw: dict):
        day = (raw.get("game_state") or {}).get("day")
        if getattr(self, "_spoke", None) == day:
            return None
        self._spoke = day
        return {"content": "No strong read yet. Let's not abstain; the vote record will tell us more.", "recipients": []}


# ---------------------------------------------------------------- LLM-like stand-in
_T = r"(?:player|p|seat)\s*#?\s*(\d)"
_CLAIM = re.compile(r"\bi\s*(?:am|'m|m)\s+(?:the\s+)?(?:real\s+|true\s+|actual\s+)?seer\b", re.I)
_WOLF_OF = re.compile(_T + r"\s*(?:is|was|=)\s*(?:a\s+|the\s+)?(?:wolf|werewolf)", re.I)
_CLEAR_OF = re.compile(_T + r"\s*(?:is|was)\s*(?:a\s+)?(?:villager|clear|innocent)", re.I)
_VOTE_FOR = re.compile(r"\bvot\w*\s+(?:for\s+|out\s+)?" + _T, re.I)
_SUS = re.compile(_T + r"\s*(?:is|seems|looks)\s+(?:very\s+|most\s+|really\s+)?(?:sus|suspicious|shady|lying)", re.I)


class LLMSeat(Seat):
    name = "llm"
    EPS = 0.12            # a random legal action (abstain included)
    ABSTAIN_D1 = 0.10     # abstains on day 1 without a claim to follow
    CLAIM_WOLF = 0.85     # seer claims a found wolf
    CLAIM_CLEAR = 0.35    # seer claims with only clears
    COUNTER = 0.5         # accused wolf counter-claims seer (else denies)
    PRE_FAKE = 0.05       # wolf fake-claims seer unprompted on day 1
    FOLLOW_DM = 0.8       # follows the wolf ally's private target
    FOLLOW_PRESSURE = 1.0  # a villager without a claim to follow votes the seat named most in chat

    def reset(self):
        self.msgs: dict[int, dict] = {}
        self.day_sent: dict = {}

    # -- reading ---------------------------------------------------------------
    def _read(self, raw):
        for m in raw.get("new_messages") or []:
            if isinstance(m, dict) and m.get("seq") is not None:
                self.msgs[m["seq"]] = m

    @staticmethod
    def _meaning(text: str, s) -> tuple[bool, set, set, set]:
        """(claims seer, seats called wolf, seats called clear, seats pressed) as this stand-in reads `text`."""
        wolves = {int(x) for x in _WOLF_OF.findall(text)} - {s}
        clears = {int(x) for x in _CLEAR_OF.findall(text)} - {s}
        press = (wolves | {int(x) for x in _VOTE_FOR.findall(text)} | {int(x) for x in _SUS.findall(text)}) - {s}
        return bool(_CLAIM.search(text)), wolves, clears, press

    def _claims(self, raw):
        """claimer -> (order, wolves named, clears named), broadcast only; and chat pressure per seat."""
        me, alive, dead, _ = _public(raw)
        claims, pressure = {}, defaultdict(float)
        for seq in sorted(self.msgs):
            m = self.msgs[seq]
            if m.get("type", "chat") != "chat" or m.get("recipients"):
                continue
            s, text = m.get("sender"), str(m.get("content") or "")
            claim, wolves, clears, press = self._meaning(text, s)
            if claim:
                c = claims.setdefault(s, [len(claims), set(), set()])
                c[1] |= wolves
                c[2] |= clears
            for t in press:
                pressure[t] += 1.0
        live = {c: v for c, v in claims.items() if c in alive or dead.get(c) == "seer"}
        # a dead "seer" claimer revealed as a wolf is a liar; one revealed as seer is the truth
        live = {c: v for c, v in live.items() if dead.get(c) != "wolf"}
        return live, pressure

    def _ally_dm(self, raw, allies):
        for seq in sorted(self.msgs, reverse=True):
            m = self.msgs[seq]
            if m.get("recipients") and m.get("sender") in allies:
                hit = re.search(_T, str(m.get("content") or ""), re.I)
                if hit:
                    return int(hit.group(1))
        return None

    # -- acting ----------------------------------------------------------------
    def choose(self, raw: dict) -> str:
        self._read(raw)
        legal = _legal(raw)
        rng = self.rng
        if rng.random() < self.EPS:
            return rng.choice(legal)
        me, alive, dead, votes = _public(raw)
        role, allies, inv = _private(raw)
        sub = (raw.get("game_state") or {}).get("phase")
        targets = [int(a) for a in legal if a != str(ABSTAIN)]
        claims, pressure = self._claims(raw)
        pick = None
        if sub == "night_wolf":
            seers = [c for c in claims if c in targets]
            dm = self._ally_dm(raw, allies)
            if seers:
                pick = rng.choice(seers)
            elif dm in targets and rng.random() < self.FOLLOW_DM:
                pick = dm
            else:
                pick = rng.choice(targets) if targets else None
        elif sub == "night_seer":
            pool = [t for t in targets if t not in inv]
            pool_p = [t for t in pool if pressure.get(t)]
            pick = rng.choice(pool_p or pool or targets)
        else:
            pick = self._vote(rng, me, role, allies, inv, targets, claims, pressure, raw)
            if pick is None:
                return str(ABSTAIN) if str(ABSTAIN) in legal else legal[0]
        return str(pick) if pick is not None and str(pick) in legal else rng.choice(legal)

    def _vote(self, rng, me, role, allies, inv, targets, claims, pressure, raw):
        day = (raw.get("game_state") or {}).get("day") or 1
        if role == "seer":
            known = [t for t in targets if inv.get(t) == "wolf"]
            if known:
                return known[0]
            fakes = [c for c in claims if c != me and c in targets]
            if fakes:
                return fakes[0]
        if role == "wolf":
            accusers = [c for c, v in claims.items() if c in targets and (v[1] & ({me} | set(allies)))]
            if accusers:
                return accusers[0]
            pool = [t for t in targets if t not in allies]
            hot = sorted(pool, key=lambda t: -pressure.get(t, 0.0))
            return hot[0] if hot and pressure.get(hot[0]) else (rng.choice(pool) if pool else None)
        live = [c for c in claims if c != me]
        if len(live) == 1:
            named = [t for t in claims[live[0]][1] if t in targets]
            if named:
                return named[0]
        elif len(live) >= 2:
            both = [c for c in live if c in targets]
            if both:
                return rng.choice(both)
        cleared = set()
        for c, v in claims.items():
            cleared |= v[2]
        pool = [t for t in targets if t not in cleared and inv.get(t) != "villager"] or targets
        hot = [t for t in pool if pressure.get(t)]
        if hot and (self.FOLLOW_PRESSURE >= 1.0 or rng.random() < self.FOLLOW_PRESSURE):
            top = max(pressure[t] for t in hot)
            return rng.choice([t for t in hot if pressure[t] == top])
        if day == 1 and rng.random() < self.ABSTAIN_D1:
            return None
        return rng.choice(pool) if pool else None

    def message(self, raw: dict):
        self._read(raw)
        gs = raw.get("game_state") or {}
        day = gs.get("day")
        n = self.day_sent.get(day, 0)
        if n >= 2:
            return None
        self.day_sent[day] = n + 1
        rng = self.rng
        me, alive, dead, _ = _public(raw)
        role, allies, inv = _private(raw)
        claims, pressure = self._claims(raw)
        others = [s for s in alive if s != me]
        if n == 0:
            if role == "seer":
                wolves = [t for t, r in inv.items() if r == "wolf" and t in alive]
                clears = [t for t, r in inv.items() if r == "villager" and t in alive]
                if wolves and rng.random() < self.CLAIM_WOLF:
                    return {"content": f"I am the Seer. Player{wolves[0]} is a wolf. Vote Player{wolves[0]}.", "recipients": []}
                if clears and rng.random() < self.CLAIM_CLEAR:
                    return {"content": f"I am the Seer. Player{clears[0]} is a villager.", "recipients": []}
            if role == "wolf":
                partner = [a for a in allies if a in alive]
                if partner and rng.random() < 0.5:
                    pool = [s for s in others if s not in allies]
                    if pool:
                        return {"content": f"Let's kill Player{rng.choice(pool)} tonight.", "recipients": [partner[0]]}
                pool = [s for s in others if s not in allies]
                if day == 1 and pool and rng.random() < self.PRE_FAKE:
                    return {"content": f"I am the Seer. Player{rng.choice(pool)} is a wolf.", "recipients": []}
            hot = [t for t in others if pressure.get(t) and t not in allies]
            if hot and rng.random() < 0.6:
                t = max(hot, key=lambda s: pressure[s])
                return {"content": f"I think Player{t} is suspicious. I'll vote Player{t}.", "recipients": []}
            return {"content": "I'm a villager. Let's hear from everyone before we vote.", "recipients": []}
        # second message: react
        accused_me = [c for c, v in claims.items() if me in v[1] and c != me]
        if accused_me:
            if role == "wolf" and me not in claims and rng.random() < self.COUNTER:
                return {"content": f"I am the real Seer. Player{accused_me[0]} is a wolf.", "recipients": []}
            return {"content": f"I'm not a wolf. Player{accused_me[0]} is lying.", "recipients": []}
        live = [c for c in claims if c != me]
        if len(live) == 1 and role != "wolf":
            named = [t for t in claims[live[0]][1] if t in alive]
            if named:
                return {"content": f"I trust Player{live[0]}. Vote Player{named[0]}.", "recipients": []}
        return None


# ---------------------------------------------------------------- the same stand-in, phrased freely
_FORMS = [
    (re.compile(r"I am the Seer\. Player(\d) is a wolf\.(?: Vote Player\d\.)?"), "seer_wolf"),
    (re.compile(r"I am the Seer\. Player(\d) is a villager\."), "seer_clear"),
    (re.compile(r"I am the real Seer\. Player(\d) is a wolf\."), "counter"),
    (re.compile(r"I think Player(\d) is suspicious\. I'll vote Player\d\."), "sus"),
    (re.compile(r"I'm a villager\. Let's hear from everyone before we vote\."), "idle"),
    (re.compile(r"I'm not a wolf\. Player(\d) is lying\."), "deny"),
    (re.compile(r"I trust Player(\d)\. Vote Player(\d)\."), "trust"),
]
_VARIANTS = {
    "seer_wolf": ["I am the Seer. Player{0} is a wolf. Vote Player{0}.",
                  "seer here: p{0} is a wolf, vote them out",
                  "I'm the seer. I investigated Player{0} last night: Player{0} is a wolf. All players must vote "
                  "Player{0}.",
                  "As the seer, I checked Player{0}: wolf. Everyone vote Player{0} today.",
                  "im the real seer!! player {0} is a werewolf"],
    "seer_clear": ["I am the Seer. Player{0} is a villager.", "Seer here: p{0} is clean.",
                   "I'm the seer, I checked Player{0}: villager."],
    "counter": ["I am the real Seer. Player{0} is a wolf.", "No, I'm the real seer! p{0} is the wolf, not me.",
                "Player{0} is lying, I am the Seer and Player{0} is a wolf."],
    "sus": ["I think Player{0} is suspicious. I'll vote Player{0}.", "p{0} kinda sus tbh, voting p{0}",
            "I checked Player{0}'s votes, very shady. Voting Player{0}."],
    # table talk that only looks like a seer claim (or like an instruction)
    "idle": ["I'm a villager. Let's hear from everyone before we vote.",
             "Is the Seer here? Please speak up before we vote.",
             "We need the seer here to guide us.",
             "Reminder: all players should vote, abstaining helps the wolves.",
             "What if I am the seer? Kidding, just a villager.",
             "I looked at Player{r} and they seem nervous."],
    "deny": ["I'm not a wolf. Player{0} is lying.", "not a wolf lol, p{0} is lying"],
    "trust": ["I trust Player{0}. Vote Player{1}.", "Agree with p{0}. All players should vote Player{1}."],
}
_SAID: dict[tuple, tuple] = {}   # (sender, text) an LLMVariedSeat wrote -> what it meant (LLMSeat._meaning of the plain form)


class LLMVariedSeat(LLMSeat):
    """`llm` with free phrasing and a weaker pull toward the seat named most in chat (background `llmx`).

    The `llm` stand-in writes eight fixed sentences, all in the format our chat reader was built on, and always votes
    the seat named most in chat. This one decides exactly like `llm` but says it in varied words (casual forms,
    "seer here: p3 is a wolf", "All players must vote Player3"), adds table talk that only looks like a seer claim
    ("Is the Seer here?", "We need the seer here", "I checked Player3's votes", "What if I am the seer?"), and follows
    chat pressure only FOLLOW_PRESSURE of the time. Its peers understand each other's paraphrases (an LLM would); other
    seats' text is read with the `llm` patterns.
    """
    name = "llmx"
    FOLLOW_PRESSURE = 0.6
    VARIANTS = _VARIANTS

    def _meaning(self, text: str, s):
        hit = _SAID.get((s, text))
        return hit if hit is not None else LLMSeat._meaning(text, s)

    def message(self, raw: dict):
        m = super().message(raw)
        if not isinstance(m, dict) or m.get("recipients"):
            return m
        text = m.get("content", "")
        for rx, form in _FORMS:
            hit = rx.fullmatch(text)
            if hit:
                me, alive, dead, _ = _public(raw)
                others = [s for s in alive if s != me] or [0]
                victim = f"Player{list(dead)[-1]}" if dead else "Someone"      # the latest death, if any
                out = self.rng.choice(self.VARIANTS[form]).format(*hit.groups(), r=self.rng.choice(others),
                                                                  victim=victim)
                _SAID[(me, out)] = LLMSeat._meaning(text, me)
                return {"content": out, "recipients": []}
        return m


# ---------------------------------------------------------------- the same stand-in, phrased like the real LLM seats
# Seer results as the real Qwen3 seats word them (the forms the reader of agent/werewolf/state.py was extended for),
# and their skeptical table talk: every added idle line says nothing about anyone's role, so a reader that takes one
# for a verdict or a claim pays for it in these games. {victim} is the latest dead seat ("Someone" before any death).
_QWEN_VARIANTS = {
    "seer_wolf": ["I am the Seer. I investigated Player{0} and confirmed they are a wolf. Trust my result and vote "
                  "Player{0} tonight.",
                  "I'm the Seer. I checked Player{0} and confirmed they're a wolf. Trust me.",
                  "I'm the Seer. Player{0} is the real wolf. Trust me.",
                  "I am the Seer and I confirm Player{0} is a wolf. Vote Player{0} to protect the village."],
    "seer_clear": ["I am the Seer. I investigated Player{0} and confirmed they are a villager. Let's focus on "
                   "suspicious players.",
                   "I'm the Seer. I checked Player{0} and confirmed they're a villager."],
    "counter": ["I'm the real Seer. I checked Player{0} and they're a werewolf. Trust me.",
                "I'm the Seer too. Player{0} is lying — they're a wolf. Trust me."],
    "sus": _VARIANTS["sus"],
    "idle": _VARIANTS["idle"] + [
        "Player{r} voted against me, but that doesn't mean they're a wolf.",
        "Player{r} is quiet, but I doubt they're a wolf.",
        "Player{r} is loud, but I'm not convinced they're a wolf. No proof they are werewolves.",
        "Player{r} is quiet, which means they are a wolf or a scared villager.",
        "Seer claim: none yet. Let's vote carefully.",
        "{victim} was killed last night. It was the wolves, obviously.",
    ],
    "deny": _VARIANTS["deny"],
    "trust": _VARIANTS["trust"],
}


class LLMQwenSeat(LLMVariedSeat):
    """`llmx` phrased like the real Qwen3 seats (background `llmq`): same decisions, same peers' understanding of each
    other's words; seer results in the real seats' wording and skeptical table talk that is not a verdict."""
    name = "llmq"
    VARIANTS = _QWEN_VARIANTS
