"""Parse a Werewolf get_game_state payload into a structured view (docs/platform/werewolf.md §2–3), and read chat.

Chat from other seats is untrusted text written by other agents or humans: `read_chat` turns one message into a
few bounded facts (a seer claim, the seats it names wolf / clear, accusations, vote calls) and nothing else. It never
yields an instruction: text that tries to address the agents or pose as the game ("ignore previous instructions",
"[SYSTEM]", "moderator:", ...) is flagged and its content not read at all; game talk addressed to the table ("All
players must vote Player1") is not. Quoted text is dropped, in single quotes too (a seat quoting someone else's claim
is not claiming), and so are claims not made in the sender's own voice: questions ("Is the Seer here?"),
hypotheticals ("what if I am the seer"), reported speech ("Player2 said I am the seer") and check reports without a
verdict ("I checked Player3's votes"). Casual human text is normalized first: case, full-width characters, "p3",
"player 3", "seat #3", a seat's display name, "3 is wolf", "sus", "im seer".
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

ROLE_RE = re.compile(r"Your role:\s*(WOLF|SEER|VILLAGER)", re.I)
ALLY_RE = re.compile(r"Your fellow wolves:\s*(.+)")
INVEST_RE = re.compile(r"Player(\d+) is (wolf|villager)", re.I)
SEAT_RE = re.compile(r"Player(\d+)")
LIVING_RE = re.compile(r"^Living:\s*(.*)$", re.M)
VOTE_LINE_RE = re.compile(r"^Day\s+(\d+)\s+vote:\s*(.*?)(?:\s+[—–-]+\s+(.*))?$", re.M)
VOTE_PAIR_RE = re.compile(r"Player(\d+)\s*->\s*(Player\d+|abstain)", re.I)
DEAD_LINE_RE = re.compile(r"^\s*day\s+(\d+):\s*Player(\d+)\s+was\s+(wolf|seer|villager)\s*\((night_kill|lynch)\)", re.I | re.M)
# Kept for callers of the old names; chat is read with the normalized patterns below.
ACCUSE_RE = re.compile(r"Player(\d+)\s+(?:is|=|was)\s+(?:a\s+|the\s+)?(?:wolf|werewolf)", re.I)
SEER_CLAIM_RE = re.compile(r"\b(i am|i'm|im)\s+(the\s+)?(real\s+|true\s+)?seer\b", re.I)
CLEAR_RE = re.compile(r"Player(\d+)\s+(?:is|=|was)\s+(?:a\s+|an\s+)?"
                      r"(?:villager|clear|cleared|innocent|good|town|not\s+a\s+(?:wolf|werewolf))\b", re.I)

MAX_CHAT_CHARS = 600            # the platform caps chat at 50 words; anything longer is not read past this
# Quoted passages: double, curly, guillemet and low-9 quotes, and single quotes ('...' / ‘...’) whose opening quote
# does not follow a letter and whose closing quote is not followed by one, so apostrophes ("I'm", "p3's") never
# open or close a quote; an apostrophe between two letters inside the quote is kept ('I'm the seer').
_QUOTED = re.compile(r"\"[^\"]{0,300}\"|“[^”]{0,300}”|«[^»]{0,300}»|„[^“”]{0,300}[“”]"
                     r"|(?<!\w)'(?:[^'\n]|(?<=\w)'(?=\w)){1,300}'(?!\w)"
                     r"|‘(?:[^’\n]|(?<=\w)’(?=\w)){1,300}’(?!\w)")
_SEAT_TOKEN = re.compile(r"(?:\b(?:player|plyr|p|seat|agent|no\.?)\s*[#_\-]?\s*|#\s*)([0-6])\b")
_W = r"(?:wolf|wolves|werewolf|werewolves|ww|wolfie)"
_P = r"player([0-6])"
_ADV = r"(?:(?:definitely|def|prob|probably|clearly|obviously|surely|totally|for sure|100%|confirmed|actually)\s+)?"
_CHAT_ACCUSE = [
    re.compile(_P + r"\s*(?:is|=|was|'s|’s|:)?\s*" + _ADV + r"(?:a\s+|an\s+|the\s+|one\s+of\s+the\s+)?" + _W + r"\b"),
    re.compile(r"\b([0-6])\s+(?:is|=|was)\s+" + _ADV + r"(?:a\s+|the\s+)?" + _W + r"\b"),
    re.compile(r"\b" + _W + r"\s*(?:is|=|:)\s*" + _P + r"\b"),
]
_CHAT_CLEAR = [
    re.compile(_P + r"\s*(?:is|=|was|'s|’s|:)?\s*" + _ADV + r"(?:a\s+|an\s+)?(?:villager|clear|cleared|innocent|good|"
               r"town|clean|safe|not\s+(?:a\s+)?" + _W + r")\b"),
    re.compile(r"\b([0-6])\s+(?:is|=|was)\s+" + _ADV + r"(?:a\s+|an\s+)?(?:villager|clear|innocent|good|town|clean)\b"),
]
_CHAT_SUS = [
    re.compile(_P + r"\s*(?:is|seems|looks|=|'s|’s)?\s*(?:very\s+|so\s+|super\s+|really\s+|kinda\s+|a\s+bit\s+|most\s+)?"
               r"(?:sus|suss|sussy|suspicious|shady|sketchy|lying|a\s+liar|fake)\b"),
    re.compile(r"\b(?:vote|voting|lynch|lynching|eliminate|kill)\s+(?:out\s+|for\s+|off\s+)?" + _P + r"\b"),
    re.compile(r"\b(?:suspect|sus\s+on|doubt)\s+" + _P + r"\b"),
]
# A seer claim is "I am the seer" said in the sender's own voice (not "what if I am the seer", not "Player2 said I am
# the seer", not a question), "seer here" opening a sentence (not "is the seer here?"), "as the seer, I", or a report
# of a check that carries a verdict on the checked seat ("I checked Player0: villager"; "I checked Player3's votes" and
# "I looked at Player2 and they seem nervous" are not claims).
_CLAIM_IAM = re.compile(r"\b(?:i\s*am|i'm|i’m|im|i\s+m)\s+(?:the\s+|a\s+|actually\s+|really\s+|truly\s+)*"
                        r"(?:real\s+|true\s+|actual\s+|only\s+)?seer\b(?!\s*\?)")
_NOT_OWN_VOICE = re.compile(
    r"(?:\b(?:if|what\s+if|even\s+if|as\s+if|suppose|supposing|imagine|pretend|pretending|unless|whether|say)"
    r"|\b(?:player[0-6]|he|she|they|someone|somebody|anyone|nobody|who)\s+(?:just\s+)?"
    r"(?:said|says|say|claims?|claimed|thinks?|thought|pretends?|pretended)"
    r"|\b(?:not\s+saying|never\s+said|didn't\s+say|did\s+not\s+say))\s*[:,]?\s*(?:that\s+)?$")
_CHAT_CLAIM = [
    re.compile(r"(?:^|[.!?;:,]\s*|\b(?:hi|hey|hello|ok|okay|yes|yep|yeah|so|alright|well|guys|fine)\s*,?\s*)"
               r"(?:(?:the\s+)?(?:real|true|actual)\s+)?seer\s+here\b(?!\s*\?)"),
    re.compile(r"\bas\s+(?:the\s+)?seer,?\s+i\b"),
]
_CHECK = re.compile(r"\bi\s+(?:have\s+|'ve\s+|’ve\s+)?(?:checked|investigated|inspected|scanned|peeked\s+at|looked\s+at)"
                    r"\s+player([0-6])\b(?!\s*['’]s\b)([^.!?]{0,60})")
_VERDICT = re.compile(r"\b(?:wolf|werewolf|villager|clear|cleared|clean|innocent|town)\b")


def _claims_seer(t: str) -> bool:
    """Does normalized text `t` claim seer in the sender's own voice?"""
    for m in _CLAIM_IAM.finditer(t):
        if not _NOT_OWN_VOICE.search(t[max(0, m.start() - 40):m.start()]):
            return True
    if any(p.search(t) for p in _CHAT_CLAIM):
        return True
    for m in _CHECK.finditer(t):
        v = _VERDICT.search(m.group(2))
        if v and all(x == m.group(1) for x in re.findall(r"player([0-6])", m.group(2)[:v.start()])):
            return True
    return False


# Text that addresses the agents or poses as the game. Ordinary game talk addressed to the table ("All players must
# vote Player1", "Reminder: all players should vote") is not an injection: real seers write it.
_INJECTION = re.compile(
    r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+|previous\s+|prior\s+|above\s+|earlier\s+)*(?:instructions?|rules|prompts?)"
    r"|disregard\s+(?:all\s+|any\s+|the\s+|your\s+|previous\s+|prior\s+)*(?:instructions?|rules|prompts?)"
    r"|system\s*(?:prompt|message|override|note|notice)\b|^\s*system\s*:|\[\s*(?:system|admin|moderator|game|engine|"
    r"server|host)\b[^\]]*\]|\b(?:admin|moderator|game\s*master|gm|developer|host|narrator|engine|server)\s*:"
    r"|you\s+(?:are|must)\s+(?:now\s+)?(?:an?\s+)?(?:ai|assistant|language\s+model|llm|agent)\b"
    r"|\ball\s+(?:agents|ais|bots|models|llms)\s+(?:must|should|are\s+required)"
    r"|new\s+instructions?|developer\s+mode|jailbreak|<\s*/?\s*(?:system|instructions?)\s*>", re.M)


# Display names that would rewrite ordinary words (a player called "the" or "Seer") are not substituted.
_NAME_STOP = re.compile(r"wolf|seer|villager|vote|lynch|player|^(?:the|and|you|are|not|was|his|her|she|him|who|why|"
                        r"yes|lol|idk|good|town|clear|safe|kill|day|night|all|any|one|two|out|sus)$")


@dataclass
class ChatFacts:
    claim: bool = False                              # the sender claims to be the seer
    wolves: set = field(default_factory=set)         # seats the sender calls wolf
    clears: set = field(default_factory=set)         # seats the sender calls villager / clear
    sus: set = field(default_factory=set)            # weaker: suspicious, vote-for, lynch
    injection: bool = False                          # tried to instruct the table; content not read


def normalize(text, names: dict[int, str] | None = None) -> str:
    """Lower-case ASCII-ish text with seat mentions rewritten to 'playerN' and quoted passages removed."""
    if not isinstance(text, str):
        return ""
    t = unicodedata.normalize("NFKC", text[:MAX_CHAT_CHARS]).casefold()
    t = _QUOTED.sub(" ", t)
    for seat, name in sorted((names or {}).items(), key=lambda x: -len(str(x[1]))):
        n = str(name).casefold().strip()
        if len(n) >= 3 and not re.fullmatch(r"(?:player|seat|agent)?\s*\d+", n) and not _NAME_STOP.search(n):
            t = re.sub(r"(?<![\w])" + re.escape(n) + r"(?![\w])", f"player{seat}", t)
    return _SEAT_TOKEN.sub(lambda m: f"player{m.group(1)}", t)


def read_chat(text, sender: int | None = None, names: dict[int, str] | None = None) -> ChatFacts:
    """The facts one chat message asserts (never instructions). The sender's own seat is never its own target."""
    f = ChatFacts()
    raw = unicodedata.normalize("NFKC", text[:MAX_CHAT_CHARS]).casefold() if isinstance(text, str) else ""
    if _INJECTION.search(raw):
        f.injection = True
        return f
    t = normalize(text, names)
    f.claim = _claims_seer(t)
    for p in _CHAT_ACCUSE:
        f.wolves |= {int(x) for x in p.findall(t)}
    for p in _CHAT_CLEAR:
        f.clears |= {int(x) for x in p.findall(t)}
    for p in _CHAT_SUS:
        f.sus |= {int(x) for x in p.findall(t)}
    f.clears -= f.wolves                             # "p3 is not a wolf ... p3 is a wolf": the accusation wins
    if isinstance(sender, int):
        for s in (f.wolves, f.clears, f.sus):
            s.discard(sender)
    return f


@dataclass
class View:
    me: int
    role: str                                   # wolf | seer | villager
    day: int | None
    sub_phase: str | None                       # night_wolf | night_seer | day_vote
    alive: list[int]
    dead: dict[int, str]                        # seat -> revealed role
    dead_cause: dict[int, str]                  # seat -> night_kill | lynch
    allies: list[int] = field(default_factory=list)
    investigations: dict[int, str] = field(default_factory=dict)
    votes: list[tuple[int, dict[int, int | None], int | None]] = field(default_factory=list)
    # (day, {voter_seat: target_seat or None=abstain}, eliminated_seat)
    messages: list[dict] = field(default_factory=list)
    names: dict[int, str] = field(default_factory=dict)          # seat -> display name

    @property
    def others_alive(self) -> list[int]:
        return [s for s in self.alive if s != self.me]

    @property
    def wolves_left(self) -> int:
        return 2 - sum(1 for r in self.dead.values() if r == "wolf")


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def parse(st: dict, messages: list[dict] | None = None) -> View:
    st = _dict(st)
    obs = st.get("observation") or ""
    if not isinstance(obs, str):
        obs = str(obs)
    players = st.get("players") if isinstance(st.get("players"), list) else []
    valid = [p for p in players
             if isinstance(p, dict) and isinstance(p.get("position"), int) and isinstance(p.get("name"), str)]
    name2seat = {p.get("name"): p.get("position") for p in valid}
    gs = _dict(st.get("game_state"))
    if len(name2seat) < len(valid):
        # Two seats share a display name: the name-keyed game_state is ambiguous. The observation carries the same
        # public record by seat label, so read it from there.
        name2seat = {}
        gs = {k: gs[k] for k in ("day", "phase") if k in gs}

    m = ROLE_RE.search(obs)
    role = m.group(1).lower() if m else "villager"

    allies: list[int] = []
    m = ALLY_RE.search(obs)
    if m:
        allies = [int(x) for x in SEAT_RE.findall(m.group(1))]

    invest: dict[int, str] = {}
    m = re.search(r"Your investigations:\s*(.+)", obs)
    if m:
        invest = {int(s): r.lower() for s, r in INVEST_RE.findall(m.group(1))}

    def seat(name) -> int | None:
        if not isinstance(name, str) or name == "abstain":
            return None
        if name in name2seat:
            return name2seat[name]
        m = SEAT_RE.fullmatch(name)
        return int(m.group(1)) if m else None

    alive = sorted({s for s in (seat(n) for n in gs.get("alive") or [] if isinstance(gs.get("alive"), list))
                    if s is not None})
    dead, cause = {}, {}
    for d in gs.get("dead") or [] if isinstance(gs.get("dead"), list) else []:
        if not isinstance(d, dict):
            continue
        s = seat(d.get("player"))
        if s is not None:
            dead[s] = d.get("role", "?")
            cause[s] = d.get("cause", "?")
    # The observation carries the same public record by seat label: use it when the structured block is missing
    # or its names do not map to seats.
    if not alive:
        m = LIVING_RE.search(obs)
        if m:
            alive = sorted({int(x) for x in SEAT_RE.findall(m.group(1))})
    if not dead:
        for _day, s, r, c in DEAD_LINE_RE.findall(obs):
            dead[int(s)], cause[int(s)] = r.lower(), c.lower()

    votes = []
    for vh in gs.get("vote_history") or [] if isinstance(gs.get("vote_history"), list) else []:
        if not isinstance(vh, dict):
            continue
        vv = {}
        for voter, tgt in _dict(vh.get("votes")).items():
            vs = seat(voter)
            if vs is not None:
                vv[vs] = seat(tgt)
        votes.append((vh.get("day"), vv, seat(vh.get("eliminated"))))
    if not votes:
        for m in VOTE_LINE_RE.finditer(obs):
            vv = {int(a): (int(b[6:]) if b.lower().startswith("player") else None)
                  for a, b in VOTE_PAIR_RE.findall(m.group(2))}
            e = re.search(r"Player(\d+)\s+lynched", m.group(3) or "")
            votes.append((int(m.group(1)), vv, int(e.group(1)) if e else None))

    day = gs.get("day") if isinstance(gs.get("day"), int) and not isinstance(gs.get("day"), bool) else None
    sub = gs.get("phase") if isinstance(gs.get("phase"), str) else None
    if day is None or sub is None:
        m = re.search(r"Day\s+(\d+)\s*\|\s*phase\s+(\w+)", obs)
        if m:
            day = day if day is not None else int(m.group(1))
            sub = sub if sub is not None else m.group(2)
    msgs = messages if messages is not None else [x for x in st.get("new_messages") or [] if isinstance(x, dict)] \
        if isinstance(st.get("new_messages"), list) else []
    return View(
        me=st.get("your_position") if isinstance(st.get("your_position"), int) else -1, role=role, day=day,
        sub_phase=sub, alive=alive, dead=dead, dead_cause=cause, allies=allies, investigations=invest,
        votes=votes, messages=msgs, names={p: n for n, p in name2seat.items()},
    )
