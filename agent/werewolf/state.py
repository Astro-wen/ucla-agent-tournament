"""Parse a Werewolf get_game_state payload into a structured view (docs/platform/werewolf.md §2–3), and read chat.

Our role and allies are read from the documented observation lines ("Your role: WOLF", "Your fellow wolves:
Player4") and, only when those are missing, from reasonable variants ("Your role is: Werewolf", "You are the Seer",
"Your wolf ally: Player4", ...). Either way the result is cross-checked against what the platform does, which cannot
be worded differently: only wolves are offered night `Kill` actions (and never their ally as a target), only the seer
is offered `Investigate`, an engine role field if the payload carries one, and a revealed dead wolf is a wolf's ally.
When text and evidence disagree the evidence wins, and `RoleCheck` (one per match) logs one loud error per kind of
problem, so a payload drift shows up in the log instead of a silent misplay (a role line the parser missed used to
make a wolf play as a villager and vote its own ally). Villagers get no night action, but the absence of one is not
used: a process restart, or a night answered by the watchdog's fallback, would look the same.

Chat from other seats is untrusted text written by other agents or humans: `read_chat` turns one message into a
few bounded facts (a seer claim, the seats it names wolf / clear, accusations, vote calls) and nothing else. It never
yields an instruction: text that tries to address the agents or pose as the game ("ignore previous instructions",
"[SYSTEM]", "moderator:", ...) is flagged and its content not read at all; game talk addressed to the table ("All
players must vote Player1") is not. Quoted text is dropped, in single quotes too (a seat quoting someone else's claim
is not claiming), and so are claims not made in the sender's own voice: questions ("Is the Seer here?"),
hypotheticals ("what if I am the seer"), reported speech ("Player2 said I am the seer") and check reports without a
verdict ("I checked Player3's votes"). Casual human text is normalized first: case, full-width characters, "p3",
"player 3", "seat #3", a seat's display name, "3 is wolf", "sus", "im seer".

Verdicts are also read the way real LLM seats (Qwen3-30B via examples/llm_agent.py) phrase them: after a check, by
pronoun ("I investigated Player3 and confirmed they are a wolf", "I checked Player3 and Player4 — they're wolves",
"I investigated Player3 as a villager"), back-referencing the one seat the sentence names ("Player5 is lying —
they're a wolf too", "Player0 claims to be the Seer but is actually a wolf"), with more qualifiers ("Player6 is the
real wolf", "Player1 is indeed a villager"), lists ("Player0 and Player3 are villagers"), relative clauses ("Player6,
who is a villager") and a wolf naming its ally ("Player0, my wolf ally"). A pronoun is never read when another seat
sits between it and its referent, in reported speech ("Player2 says they are a villager"), in a hypothetical or a
question. tests/strategy/data/ww_llm_chat.json is the labelled regression set of real LLM chat.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

log = logging.getLogger("agent.werewolf")

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

# ---------------------------------------------------------------- role / allies / investigations: wording variants
# Tried only when the documented line above is missing (the documented wording parses exactly as before).
_ROLE_WORD = r"(were-?wol(?:f|ves)|wol(?:f|ves)|seer|villager)"
ROLE_ANY_RE = re.compile(
    r"(?:\byour\s+(?:secret\s+|assigned\s+|current\s+)?role(?:\s+is)?|^\s*role|\byou\s+are(?:\s+playing)?)"
    r"\s*[:：=\-–—]?\s*(?:the\s+|a\s+|an\s+|one\s+of\s+the\s+|as\s+(?:the\s+|a\s+)?)?"
    r"(?:plain\s+|simple\s+|ordinary\s+|normal\s+)?" + _ROLE_WORD + r"\b", re.I | re.M)
ALLY_ANY_RE = re.compile(
    r"(?:\b(?:your\s+)?fellow\s+(?:were-?)?wol(?:f|ves)|\b(?:your\s+)?(?:were-?)?wolf\s+(?:allies|ally|partners?|"
    r"teammates?|pack(?:mates?)?|team)|\bother\s+(?:were-?)?wol(?:f|ves)|\byour\s+(?:allies|ally|partners?|teammates?|"
    r"pack)|^\s*(?:the\s+)?(?:were-?)?wolves)\s*(?:is|are)?\s*[:：=\-–—]?\s*(.*)$", re.I | re.M)
INVEST_LINE_ANY_RE = re.compile(
    r"(?:\byour\s+investigations?(?:\s+results?)?|\binvestigation\s+results?|\bseer\s+results?|"
    r"\byour\s+(?:checks|results|visions?))\s*[:：=\-]\s*(.+)", re.I)
INVEST_ANY_RE = re.compile(r"player\s*#?\s*(\d)\s*(?:is|=|:|-|–|—|\(|was)?\s*(?:a\s+|an\s+)?"
                           r"(were-?wolf|wolf|evil|villager|good|innocent|town)\b", re.I)
_SEAT_ANY = re.compile(r"(?:player|seat|p)\s*#?\s*(\d)\b", re.I)
_NO_INFO = re.compile(r"you do not know anyone else's role", re.I)
_ENGINE_ROLE_KEYS = ("role", "your_role", "my_role", "player_role", "private_role")
_KILL_LABEL = re.compile(r"\b(?:kill|attack|devour|bite|maul|murder|eat|hunt)\b", re.I)
_LOOK_LABEL = re.compile(r"\b(?:investigate|inspect|check|divine|peek|scan|examine|probe|see)\b", re.I)
_DAY_LABEL = re.compile(r"\b(?:vote|lynch|abstain|eliminate|execute)\b", re.I)


def _role_word(s) -> str | None:
    if not isinstance(s, str):
        return None
    m = re.fullmatch(r"\s*(?:the\s+|a\s+|an\s+)?" + _ROLE_WORD + r"\s*", s, re.I)
    if not m:
        return None
    w = m.group(1).lower()
    return "seer" if w == "seer" else "villager" if w == "villager" else "wolf"


@dataclass
class RoleEvidence:
    """What one state says about our role, from each source separately (resolved by `resolve_role`)."""
    observed: bool = False                  # the observation is a non-empty text
    text: str | None = None                 # the role line (documented or a variant); None: not recognised
    hint: str | None = None                 # weaker text cues: ally line (wolf), investigations line (seer), no-info
    engine: str | None = None               # an engine role field in the payload, if any
    night: str | None = None                # this state offers us a night action of this role (wolf / seer)
    night_allies: list[int] | None = None   # wolves left out of this night_wolf target list (None: no such list)
    text_allies: list[int] = field(default_factory=list)


def _engine_role(st: dict, gs: dict) -> str | None:
    """A role field of the payload (none is documented; read in case the engine adds one)."""
    for src, keys in ((st, _ENGINE_ROLE_KEYS), (gs, ("your_role", "my_role")),
                      *((st.get(k), _ENGINE_ROLE_KEYS) for k in ("private", "private_state", "private_info", "you"))):
        if isinstance(src, dict):
            for k in keys:
                r = _role_word(src.get(k))
                if r:
                    return r
    return None


def night_evidence(st: dict, sub: str | None, alive: list[int], me) -> tuple[str | None, list[int] | None]:
    """(role, allies) implied by the night action we are offered in `st`: `Kill` targets (or the night_wolf
    sub-phase) mean wolf, and the living non-target seat besides us is our ally; `Investigate` (or night_seer) means
    seer. (None, None) when the state offers no night action."""
    la = st.get("legal_actions")
    acts = la.get("actions") if isinstance(la, dict) else la
    acts = [a for a in acts or [] if isinstance(a, dict) and str(a.get("action_id")) in {str(i) for i in range(7)}]
    if not acts:
        return None, None
    labels = " ".join(str(a.get("label") or "") for a in acts)
    if _DAY_LABEL.search(labels):
        return None, None
    kill, look = bool(_KILL_LABEL.search(labels)), bool(_LOOK_LABEL.search(labels))
    if kill and look:
        return None, None
    role = "wolf" if kill else "seer" if look else {"night_wolf": "wolf", "night_seer": "seer"}.get(sub or "")
    allies = None
    if role == "wolf" and isinstance(me, int) and alive:
        targets = {int(a["action_id"]) for a in acts}
        living = set(alive)
        if targets <= living - {me}:
            missing = living - targets - {me}
            allies = sorted(missing) if len(missing) <= 1 else None     # two wolves: at most one ally left out
    return role, allies


def resolve_role(ev: RoleEvidence, me, dead: dict, night: str | None = None,
                 night_allies: list[int] | None = None) -> tuple[str, list[int], dict[str, str]]:
    """(role, allies, problems by kind). Evidence beats text: night actions, then an engine field, then the role line,
    then the weaker text cues, else villager; an ally line or an investigations line (only a wolf / the seer has one)
    also beats a role line that says villager. A wolf's allies: the seat a night_wolf target list left out and any
    revealed dead wolf; they replace the text's when they disagree. `night` / `night_allies` default to this state's."""
    night = night if night is not None else ev.night
    night_allies = night_allies if night_allies is not None else ev.night_allies
    problems: dict[str, str] = {}
    text = ev.hint if ev.text == "villager" and ev.hint in ("wolf", "seer") else ev.text
    role = night or ev.engine or text or ev.hint or "villager"
    if ev.observed and ev.text is None:
        problems["role-unparsed"] = "role line not recognised"
    seen = [(name, r) for name, r in (("night actions", night), ("engine role field", ev.engine),
                                      ("role line", ev.text)) if r]
    if len({r for _, r in seen}) > 1:
        problems["role-mismatch"] = ", ".join(f"{name}: {r}" for name, r in seen)
    elif ev.text and ev.hint and ev.hint != ev.text and not night and not ev.engine:
        problems["role-mismatch"] = f"role line: {ev.text}, other lines suggest: {ev.hint}"
    allies = list(ev.text_allies)
    if role == "wolf":
        e = set(night_allies or ()) | {s for s, r in (dead or {}).items() if r == "wolf" and s != me}
        if len(e) > 1:
            problems["allies"] = f"evidence names {sorted(e)} as allies (ignored)"
            e = set()
        if e and set(allies) != e:
            problems["allies"] = f"ally line: {sorted(allies) or 'none'}, platform evidence: {sorted(e)}"
            allies = sorted(e)
        elif not allies and not e:
            problems["allies"] = "wolf without a known ally"
    return role, allies, problems


class RoleCheck:
    """One per match: remembers the night evidence (a day state carries none) and applies it to every later view,
    and logs each kind of role / ally problem once."""

    def __init__(self):
        self.night: str | None = None
        self.night_allies: list[int] | None = None
        self.conflict = False
        self.reported: set[str] = set()

    def apply(self, v: View) -> View:
        ev = v.evidence
        if ev is None:
            return v
        if ev.night:
            if self.night not in (None, ev.night):
                self.conflict = True            # offered both kill and investigate in one match: trust neither
            self.night = self.night or ev.night
        if ev.night_allies:
            if self.night_allies and set(self.night_allies) != set(ev.night_allies):
                self.conflict = True
            self.night_allies = self.night_allies or list(ev.night_allies)
        night = None if self.conflict else self.night
        role, allies, problems = resolve_role(ev, v.me, v.dead, night or ev.night,
                                              None if self.conflict else self.night_allies)
        if self.conflict:
            problems["evidence"] = "conflicting night actions in one match"
        v.role, v.allies = role, allies
        new = {k: p for k, p in problems.items() if k not in self.reported}
        if new:
            self.reported |= set(new)
            log.error("werewolf role check (seat %s, day %s): %s; playing as %s%s", v.me, v.day,
                      "; ".join(new.values()), role, f" with allies {allies}" if role == "wolf" else "")
        return v


# ---------------------------------------------------------------- chat
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
# the seer", not a question), "seer here" opening a sentence (not "is the seer here?"), "as the seer, I", "Seer claim:"
# opening a sentence, or a report of a check that carries a verdict on the checked seat ("I checked Player0:
# villager", "Player5 is a wolf, I checked them"; "I checked Player3's votes" and "I looked at Player2 and they seem
# nervous" are not claims).
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
    re.compile(r"(?:^|[.!?]\s*)(?:hard\s+)?seer\s+claim\s*[:\-—–]"),
]
_CHECK = re.compile(r"\bi\s+(?:have\s+|'ve\s+|’ve\s+)?(?:checked|investigated|inspected|scanned|peeked\s+at|looked\s+at)"
                    r"\s+player([0-6])\b(?!\s*['’]s\b)([^.!?]{0,60})")
_VERDICT = re.compile(r"\b(?:wolf|werewolf|villager|clear|cleared|clean|innocent|town)\b")

# ---- verdicts as real LLM seats phrase them (added to the patterns above; see the module docstring)
_W2 = r"(?:wolf|wolves|werewolf|werewolves|ww|wolfie|evil)"
_CLEARW = r"(?:villagers?|clear|cleared|clean|innocent|good|town|safe|human)"
_ADV2 = (r"(?:(?:definitely|def|prob|probably|clearly|obviously|surely|totally|confirmed|actually|indeed|also|"
         r"certainly|absolutely|really|likely|undoubtedly|in\s+fact|for\s+sure|without\s+(?:a\s+)?doubt|100%|both)\s+)"
         r"{0,2}")
_DET = (r"(?:(?:a|an|the|one\s+of\s+the(?:\s+two)?|our|my)\s+)?"
        r"(?:(?:real|true|actual|confirmed|known|other|second|last|remaining|fellow)\s+)?")
# a verdict word must end its phrase: "a wolf." / "a wolf too" / "a wolf, vote" but not "a wolf bluff" / "wolf's"
_END = (r"(?=\s*(?:$|[.,!?;:)\]—–-]|(?:too|also|as\s+well|for\s+sure|for\s+certain|imo|tbh|then|now|confirmed|indeed|"
        r"and|but|so|or|last\s+night|tonight|today|yesterday|here|btw|lol)\b))")
_SEATS = r"player[0-6](?:\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)player[0-6])*"
_LIST = r"player[0-6](?:\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)player[0-6])+"
_IS = r"\s*(?:is|=|was|['’]s|:)\s+"
_CLEAR_PHRASE = r"(?:" + _DET + _CLEARW + r"|not\s+(?:a\s+|an\s+)?" + _W2 + r")"
_TURNED = (r"\s+(?:turned\s+out\s+to\s+be|came\s+back(?:\s+as)?|showed\s+up\s+as|"
           r"(?:was|is)\s+revealed\s+(?:as|to\s+be)|proved\s+to\s+be|tested\s+as|reads?\s+as)\s+")
_DIRECT = [   # (pattern with group "s" = the seat or seats, verdict)
    (re.compile(r"(?P<s>player[0-6])" + _IS + _ADV2 + _DET + _W2 + _END), "wolf"),
    (re.compile(r"(?P<s>player[0-6])" + _IS + _ADV2 + _CLEAR_PHRASE + _END), "clear"),
    (re.compile(r"(?P<s>player[0-6])\s+(?:isn['’]t|wasn['’]t)\s+" + _ADV2 + r"(?:a\s+|an\s+)?" + _W2 + _END), "clear"),
    (re.compile(r"(?P<s>" + _LIST + r")\s+(?:are|were|['’]re)\s+" + _ADV2 + r"(?:(?:both|all)\s+)?(?:the\s+)?"
                r"(?:two\s+)?(?:wolves|werewolves)" + _END), "wolf"),
    (re.compile(r"(?P<s>" + _LIST + r")\s+(?:are|were|['’]re)\s+" + _ADV2 + r"(?:(?:both|all)\s+)?(?:villagers|clear|"
                r"cleared|clean|innocent|good|town|safe|not\s+(?:wolves|werewolves))" + _END), "clear"),
    (re.compile(r"(?P<s>player[0-6])\s*,\s*who\s+(?:is|was|['’]s)\s+" + _ADV2 + _DET + _W2 + _END), "wolf"),
    (re.compile(r"(?P<s>player[0-6])\s*,\s*who\s+(?:is|was|['’]s)\s+" + _ADV2 + _CLEAR_PHRASE + _END), "clear"),
    (re.compile(r"(?P<s>player[0-6])\s*,\s*(?:my|our)\s+(?:fellow\s+)?" + _W2
                + r"(?:\s+(?:ally|allies|partner|teammate|buddy|friend))?" + _END), "wolf"),
    (re.compile(r"(?P<s>player[0-6])" + _TURNED + _ADV2 + _DET + _W2 + _END), "wolf"),
    (re.compile(r"(?P<s>player[0-6])" + _TURNED + _ADV2 + _CLEAR_PHRASE + _END), "clear"),
    (re.compile(r"\bplayer[0-6]" + _IS + _ADV2 + _DET + _W2 + r"\s*,?\s+(?:and\s+)?so\s+(?:is|was)\s+(?P<s>player[0-6])"
                + _END), "wolf"),
    (re.compile(r"\bplayer[0-6]" + _IS + _ADV2 + _CLEAR_PHRASE + r"\s*,?\s+(?:and\s+)?so\s+(?:is|was)\s+"
                r"(?P<s>player[0-6])" + _END), "clear"),
]
_PRON = r"(?:they|he|she|it|both(?:\s+of\s+them)?)"
_BE = (r"(?:\s*['’](?:re|s)|\s+(?:are|is|was|were|turned\s+out\s+to\s+be|came\s+back(?:\s+as)?|showed\s+up\s+as|"
       r"returned(?:\s+as)?|reads?\s+as|tested\s+as))")
_PV = [   # a pronoun verdict, or one whose subject is left out after "but" / "and"
    (re.compile(r"\b" + _PRON + _BE + r"\s+" + _ADV2 + _DET + _W2 + _END), "wolf"),
    (re.compile(r"\b" + _PRON + r"(?:" + _BE + r"\s+" + _ADV2 + _CLEAR_PHRASE + r"|\s+(?:aren['’]t|isn['’]t|wasn['’]t|"
                r"weren['’]t)\s+(?:a\s+|an\s+)?" + _W2 + r")" + _END), "clear"),
    (re.compile(r"\b(?:but|and|yet)\s+(?:is|are|was|were)\s+" + _ADV2 + _DET + _W2 + _END), "wolf"),
    (re.compile(r"\b(?:but|and|yet)\s+(?:is|are|was|were)\s+" + _ADV2 + _CLEAR_PHRASE + _END), "clear"),
]
_PV_YOU = [   # "Player3, you're a wolf": only when the sentence opens by addressing that one seat
    (re.compile(r"\byou(?:\s*['’]re|\s+are)\s+" + _ADV2 + _DET + _W2 + _END), "wolf"),
    (re.compile(r"\byou(?:\s*['’]re|\s+are)\s+" + _ADV2 + _CLEAR_PHRASE + _END), "clear"),
]
# Someone else's words, a hypothetical, or uncertainty right before a pronoun verdict: not read.
_CUE = re.compile(r"(?:\b(?P<who>\w+)\s+)?(?:said|says|say|claims?|claimed|claiming|thinks?|thought|believes?|"
                  r"believed|insists?|insisted|swears?|swore|admits?|admitted|denies|denied|deny|knows?|knew|wonders?|"
                  r"asks?|asked|pretends?|pretended|pretending|told\s+\w+|tells\s+\w+|if|whether|unless|maybe|perhaps|"
                  r"suppose|imagine|might|could)\s+(?:that\s+)?$")
_IF_START = re.compile(r"^\s*(?:if|what\s+if|even\s+if|suppose|supposing|unless|whether|imagine)\b")
_CHECK_OPEN = re.compile(
    r"(?:\bi\s+(?:(?:have|'ve|’ve|had|just|also|personally|already|first)\s+)*(?:checked|investigated|inspected|"
    r"scanned|peeked\s+at|looked\s+at|looked\s+into|tested|examined|probed|viewed|used\s+my\s+(?:vision|power|ability|"
    r"check)\s+on)|\bmy\s+(?:check|investigation|vision|result|reading|look|inspection)s?\s+(?:on|of|into))\s+"
    r"(?P<s>" + _SEATS + r")\b(?!\s*['’]s\b)")
_REVEAL_OPEN = re.compile(
    r"(?:\bi\s+(?:(?:have|'ve|’ve|had|just|also)\s+)*|\bmy\s+(?:check|investigation|vision|result|reading|power|"
    r"inspection)s?\s+(?:has\s+|have\s+)?)(?:revealed|showed|shown|proved|found|confirmed|confirms|reveals|shows|"
    r"identified|verified)\s+(?P<s>" + _SEATS + r")\b(?!\s*['’]s\b)")
_VERDICT_WORD = r"(?P<v>" + _W2 + r"|" + _CLEARW + r"|not\s+(?:a\s+|an\s+)?" + _W2 + r")"
_WIN_LEAD = re.compile(r"^\s*(?:last\s+night|tonight|yesterday)?\s*(?:[:=—–-]+|,)?\s*"
                       r"(?:and\s+(?=(?:the\s+|my\s+)?results?\b))?(?:(?:the\s+|my\s+)?results?\s*(?:is|was)?"
                       r"\s*[:=]?\s*)?(?:as\s+)?" + _DET + _VERDICT_WORD + _END)
_WIN_CAME = re.compile(r"\b(?:came\s+back|turned\s+out\s+to\s+be|showed\s+up\s+as|returned|was\s+revealed\s+as|"
                       r"revealed\s+as|reads?\s+as)\s+(?:as\s+)?" + _ADV2 + _DET + _VERDICT_WORD + _END)
_WIN_IS = re.compile(r"^\s*(?:to\s+be|as|is|are|was|were|=|:)\s*" + _ADV2 + _DET + _VERDICT_WORD + _END)
_CLAIM_AFTER_VERDICT = re.compile(r"\bplayer[0-6]" + _IS + _ADV2 + _DET + r"(?:" + _W2 + r"|" + _CLEARW + r")\b"
                                  r"[^.!?]{0,20}?\bi\s+(?:have\s+|'ve\s+|’ve\s+)?(?:checked|investigated|inspected)\s+"
                                  r"(?:them|him|her)\b")
_TERM = re.compile(r"[.!?]")


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
    return bool(_CLAIM_AFTER_VERDICT.search(t))


def _sentence(t: str, pos: int) -> tuple[int, int]:
    """(start, end) of the sentence around `pos`; t[end] is its terminator, or end == len(t)."""
    start = 0
    for m in _TERM.finditer(t, 0, pos):
        start = m.end()
    m = _TERM.search(t, pos)
    return start, m.start() if m else len(t)


def _question(t: str, pos: int) -> bool:
    _, end = _sentence(t, pos)
    return end < len(t) and t[end] == "?"


def _cued(before: str) -> bool:
    """Reported speech, a hypothetical or uncertainty right before a verdict ("Player2 says", "if", "maybe"); a
    first-person "I think" / "I know" stays the sender's own voice."""
    m = _CUE.search(before)
    return bool(m) and m.group("who") != "i"


def _kind(word: str) -> str:
    return "clear" if re.match(r"not\b|" + _CLEARW, word) else "wolf"


def _plural(seats: list, text: str) -> bool:
    return len(seats) <= 1 or bool(re.search(r"\b(?:both|wolves|werewolves|villagers)\b", text))


def _verdicts(t: str) -> tuple[set[int], set[int]]:
    """Seats normalized text `t` calls wolf / clear in the phrasings of the module docstring (beyond _CHAT_ACCUSE /
    _CHAT_CLEAR)."""
    out = {"wolf": set(), "clear": set()}

    def seats_of(s):
        return [int(x) for x in re.findall(r"player([0-6])", s)]

    for rx, kind in _DIRECT:
        for m in rx.finditer(t):
            if not _question(t, m.end()):
                out[kind].update(seats_of(m.group("s")))
    # A check report: the verdict after the checked seat(s), up to the sentence end or the next seat named.
    for opener in (_CHECK_OPEN, _REVEAL_OPEN):
        for m in opener.finditer(t):
            if _NOT_OWN_VOICE.search(t[max(0, m.start() - 40):m.start()]) or _question(t, m.end()):
                continue
            seats = seats_of(m.group("s"))
            win = t[m.end():m.end() + 80]
            cut = re.search(r"[.!?;]|\bplayer[0-6]", win)
            win = win[:cut.start()] if cut else win
            hit = None
            for rx, kind in _PV:
                for pm in rx.finditer(win):
                    if not _cued(win[:pm.start()]):
                        hit = (kind, pm.group(0))
                        break
                if hit:
                    break
            if hit is None:
                for rx in ((_WIN_IS, _WIN_LEAD, _WIN_CAME) if opener is _REVEAL_OPEN else (_WIN_LEAD, _WIN_CAME)):
                    vm = rx.search(win)
                    if vm and not _cued(win[:vm.start()]):
                        hit = (_kind(vm.group("v")), vm.group(0))
                        break
            if hit and (hit[0] == "clear" or _plural(seats, hit[1])):
                out[hit[0]].update(seats)
    # A pronoun (or a left-out subject) refers to the one seat its sentence names before it, or, when it opens the
    # sentence, to the one seat the previous sentence names.
    for rxs, vocative in ((_PV, False), (_PV_YOU, True)):
        for rx, kind in rxs:
            for m in rx.finditer(t):
                start, end = _sentence(t, m.start())
                if end < len(t) and t[end] == "?":
                    continue
                prefix = t[start:m.start()]
                if _IF_START.search(prefix) or _cued(prefix):
                    continue
                if vocative:
                    vm = re.match(r"\s*player([0-6])\s*,", prefix)
                    if vm and len(set(seats_of(prefix))) == 1:
                        out[kind].add(int(vm.group(1)))
                    continue
                seats = set(seats_of(prefix))
                if not seats and not prefix.strip() and start > 0:
                    pstart, _ = _sentence(t, start - 1)
                    prev = t[pstart:start - 1]
                    if t[start - 1] != "?" and not _IF_START.search(prev):
                        seats = set(seats_of(prev))
                if len(seats) == 1:
                    out[kind] |= seats
    return out["wolf"], out["clear"]


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
    wolves, clears = _verdicts(t)
    f.wolves |= wolves
    f.clears |= clears
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
    evidence: RoleEvidence | None = None                         # what role / allies were resolved from

    @property
    def others_alive(self) -> list[int]:
        return [s for s in self.alive if s != self.me]

    @property
    def wolves_left(self) -> int:
        return 2 - sum(1 for r in self.dead.values() if r == "wolf")


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _text_role(obs: str) -> tuple[str | None, str | None, list[int], bool, dict[int, str]]:
    """(role line, weaker role cue, allies, allies from the documented line, investigations) from the observation:
    the documented lines, or when one is missing, its wording variants."""
    m = ROLE_RE.search(obs)
    role = m.group(1).lower() if m else None
    if role is None:
        m = ROLE_ANY_RE.search(obs)
        role = _role_word(m.group(1)) if m else None

    allies: list[int] = []
    m = ALLY_RE.search(obs)
    ally_line = strict_allies = bool(m)
    if m:
        allies = [int(x) for x in SEAT_RE.findall(m.group(1))]
    else:
        m = ALLY_ANY_RE.search(obs)
        if m:
            ally_line = True
            allies = sorted({int(x) for x in _SEAT_ANY.findall(m.group(1)) if int(x) <= 6})

    invest: dict[int, str] = {}
    m = re.search(r"Your investigations:\s*(.+)", obs)
    invest_line = bool(m)
    if m:
        invest = {int(s): r.lower() for s, r in INVEST_RE.findall(m.group(1))}
    if not invest:
        m2 = m or INVEST_LINE_ANY_RE.search(obs)
        if m2:
            invest_line = True
            invest = {int(s): ("wolf" if r.lower() in ("wolf", "werewolf", "were-wolf", "evil") else "villager")
                      for s, r in INVEST_ANY_RE.findall(m2.group(1)) if int(s) <= 6}
    hint = "wolf" if ally_line else "seer" if invest_line else "villager" if _NO_INFO.search(obs) else None
    return role, hint, allies, strict_allies, invest


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

    text_role, hint, allies, strict_allies, invest = _text_role(obs)

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
    me = st.get("your_position") if isinstance(st.get("your_position"), int) else -1
    night, night_allies = night_evidence(st, sub, alive, me)
    ev = RoleEvidence(observed=bool(obs.strip()), text=text_role, hint=hint, engine=_engine_role(st, gs), night=night,
                      night_allies=night_allies,
                      text_allies=allies if strict_allies else [a for a in allies if a != me])
    role, allies, _problems = resolve_role(ev, me, dead)
    return View(
        me=me, role=role, day=day, sub_phase=sub, alive=alive, dead=dead, dead_cause=cause, allies=allies,
        investigations=invest, votes=votes, messages=msgs, names={p: n for n, p in name2seat.items()}, evidence=ev,
    )
