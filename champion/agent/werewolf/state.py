"""Parse a Werewolf get_game_state payload into a structured view (docs/platform/werewolf.md §2–3), and read chat.

The public record (deaths, votes) comes from `game_state`, or from the observation's seat-labelled lines when that block
is missing or its display names are ambiguous. Both platform wordings are read: until 10-04 "lynch" (a death's cause
`lynch`, "(lynch)", "— Player1 lynched"), since then "eliminate" / "vote" (`vote`, "(eliminated by vote)", "(killed at
night)", "— Player1 eliminated by vote"); a death's cause is normalised to one value, `night_kill` or `vote`
(`norm_cause`). Both vote labels ("Vote to lynch PlayerN", "Vote to eliminate PlayerN") are day labels (`_DAY_LABEL`);
actions themselves are seat numbers. The seer's line before its first check, "You have not investigated anyone yet.",
is a seer cue like "Your investigations: ...".

Our role and allies are read from the documented observation lines ("Your role: WOLF", "Your fellow wolves:
Player4") and, only when those are missing, from reasonable variants ("Your role is: Werewolf", "You are the Seer",
"Your wolf ally: Player4", ...). Either way the result is cross-checked against what the platform does, which cannot
be worded differently: only wolves are offered night `Kill` actions (and never their ally as a target), only the seer
is offered `Investigate`, an engine role field if the payload carries one, and a revealed dead wolf is a wolf's ally.
When text and evidence disagree the evidence wins, and `RoleCheck` (one per match) logs one loud error per kind of
problem, so a payload drift shows up in the log instead of a silent misplay (a role line the parser missed used to
make a wolf play as a villager and vote its own ally). Weaker text cues (an ally line, an investigations line) are
used only when no role line is recognised; against a recognised one they are logged, never followed. Villagers get no
night action, but the absence of one is not used: a process restart, or a night answered by the watchdog's fallback,
would look the same.

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
sits between it and its referent, in reported speech ("Player2 says they are a villager", "Player3 keeps insisting
they're innocent", "According to Player2, they're a wolf"), in a hypothetical or a question, after a negation or doubt
("that doesn't mean they're a wolf", "I'm not convinced they're a wolf", "no proof they are wolves") or a hunch ("I
suspect they're a wolf", "chances are"), as a plural verdict about one seat, when its referent clause agrees with or
credits that seat ("Player2 is right. They're a wolf.") or when its referent sentence reports that seat's death
("Player3 was killed last night. It was the wolves."); the new direct patterns skip hypotheticals, reported speech,
attributions ("..., says Player2", "Player2: ...") and negations the same way. tests/strategy/data/ww_llm_chat.json is
the labelled regression set of real LLM chat, and the `llmq` stand-in of local/werewolf_bots.py plays that phrasing
(and that skeptical table talk) in local games.
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
# Two wordings of the public record (docs/platform/werewolf.md, changed 10-04/05; both are read, the server may lead
# or lag the docs): a death line ends "(lynch)" / "(night_kill)", or "(eliminated by vote)" / "(killed at night)";
# a vote line ends "— Player1 lynched", or "— Player1 eliminated by vote".
DEAD_LINE_RE = re.compile(r"^\s*day\s+(\d+):\s*Player(\d+)\s+was\s+(?:an?\s+)?(wolf|werewolf|seer|villager)\s*"
                          r"\(([^()\n]{1,40})\)", re.I | re.M)
VOTE_OUT_RE = re.compile(r"Player(\d+)\s+(?:was\s+)?(?:lynched|eliminated|voted\s+out|executed)\b", re.I)
# The seer's investigations line before its first check (since 10-05): "You have not investigated anyone yet."
NO_INVEST_RE = re.compile(r"\byou\s+have\s+not\s+(?:yet\s+)?investigated\s+anyone\b", re.I)

# The cause of a death, one internal value whatever the wording: game_state.dead[].cause is "night_kill" | "lynch"
# (until 10-04) or "night_kill" | "vote" (since), the observation says "(night_kill)" / "(lynch)" or "(killed at
# night)" / "(eliminated by vote)".
CAUSE_NIGHT = "night_kill"
CAUSE_VOTE = "vote"
_CAUSE_VOTE_RE = re.compile(r"\b(?:lynch(?:ed|ing)?|votes?|voted(?:\s+out)?|eliminat(?:ed|ion)\s+by\s+vote|"
                            r"day[\s_-]*vote|execut(?:ed|ion))\b")
_CAUSE_NIGHT_RE = re.compile(r"\bnight[\s_-]*kill(?:ed)?\b|\bkilled\s+(?:at|by|during|in\s+the)\s+(?:night|wol(?:f|ves))\b"
                             r"|\bwolf[\s_-]*kill\b")


def norm_cause(c) -> str:
    """CAUSE_NIGHT | CAUSE_VOTE for every documented wording (old and new), else the value lower-cased ("?" if none)."""
    if not isinstance(c, str) or not c.strip():
        return "?"
    t = " ".join(c.strip().lower().replace("_", " ").split())
    if t == "night kill" or _CAUSE_NIGHT_RE.search(t):
        return CAUSE_NIGHT
    if _CAUSE_VOTE_RE.search(t):
        return CAUSE_VOTE
    return c.strip().lower()


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
# Explicit ally / fellow-wolf phrasing followed by "is", "are" or a separator; it counts only when the rest of its line
# names a seat (not "Your teammates: everyone who is not a wolf", "Wolves remaining: 2").
ALLY_ANY_RE = re.compile(
    r"(?:\b(?:your\s+)?fellow\s+(?:were-?)?wol(?:f|ves)|\b(?:your\s+)?(?:were-?)?wolf\s+(?:allies|ally|partners?|"
    r"teammates?|pack(?:mates?)?|team)|\bother\s+(?:were-?)?wol(?:f|ves)|\byour\s+(?:allies|ally|partners?))"
    r"\s*(?:(?:is|are)\b\s*[:：=\-–—]?|[:：=\-–—])\s*(.*)$", re.I | re.M)
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
    if not isinstance(acts, list):
        return None, None
    acts = [a for a in acts if isinstance(a, dict) and str(a.get("action_id")) in {str(i) for i in range(7)}]
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
    then the weaker text cues (used only when no role line is recognised; against one they are logged, never
    followed), else villager. A wolf's allies: the seat a night_wolf target list left out and any revealed dead wolf;
    they replace the text's when they disagree. Only a wolf has allies. `night` / `night_allies` default to this
    state's."""
    night = night if night is not None else ev.night
    night_allies = night_allies if night_allies is not None else ev.night_allies
    problems: dict[str, str] = {}
    role = night or ev.engine or ev.text or ev.hint or "villager"
    if ev.observed and ev.text is None:
        problems["role-unparsed"] = "role line not recognised"
    seen = [(name, r) for name, r in (("night actions", night), ("engine role field", ev.engine),
                                      ("role line", ev.text)) if r]
    if len({r for _, r in seen}) > 1:
        problems["role-mismatch"] = ", ".join(f"{name}: {r}" for name, r in seen)
    elif ev.text and ev.hint and ev.hint != ev.text and not night and not ev.engine:
        problems["role-mismatch"] = f"role line: {ev.text}, other lines suggest: {ev.hint}"
    allies = list(ev.text_allies) if role == "wolf" else []
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
MAX_CHAT_CHARS = 1100           # the platform caps chat at 1024 characters (50 words, but a hyphen-packed "word" can be
#                                 long); anything longer is not read past this
# Quoted passages: double, curly, guillemet and low-9 quotes, and single quotes ('...' / ‘...’) whose opening quote
# does not follow a letter and whose closing quote is not followed by one, so apostrophes ("I'm", "p3's") never
# open or close a quote; an apostrophe between two letters inside the quote is kept ('I'm the seer').
_QUOTED = re.compile(r"\"[^\"]{0,300}\"|“[^”]{0,300}”|«[^»]{0,300}»|„[^“”]{0,300}[“”]"
                     r"|(?<!\w)'(?:[^'\n]|(?<=\w)'(?=\w)){1,300}'(?!\w)"
                     r"|‘(?:[^’\n]|(?<=\w)’(?=\w)){1,300}’(?!\w)")
_BLOCK_QUOTE = re.compile(r"^[ \t]*>[^\n]*", re.M)
# A copied transcript line keeps its speaker through sentence boundaries. Its actual sender can also label their
# own report this way; read_chat removes that header instead of attributing it to another seat.
_SPEAKER_LINE = re.compile(r"^[ \t]*player([0-6])\s*(?:(says|said)\s*)?:\s*([^\n]*)", re.M)
_SEER_LABEL_RESULT = re.compile(r"^\s*(?:the\s+)?(?:real\s+|true\s+|actual\s+)?seer\s*(?=$|[.!;,])")
_SEAT_TOKEN = re.compile(r"(?:\b(?:player|plyr|plyer|palyer|playr|pleyer|p|seat|agent|no\.?)\s*[#_\-]?\s*|#\s*)"
                         r"(?:\(\s*)?0*([0-6])(?:\s*\))?(?!\w)")    # also "Player05", "Plyer5", "Player(5)"
_SEAT_WORD = re.compile(r"\b(?:player|seat)(?:\s+|-)(zero|one|two|three|four|five|six)\b")  # "player five", "player-five"
_NUM_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
# Surface noise normalised before reading (red-team chaos, 10-08): markdown emphasis ("**I am the Seer.**",
# "**Player3**"), words packed with hyphens to fit the 50-word cap ("I-am-the-seer-and-I-investigated-Player3"),
# arrows ("checked Player3 -> wolf") and a few common misspellings.
_MARKUP = re.compile(r"\*+|`+|_{2,}|~{2,}")
_HYPHEN_WORDS = re.compile(r"(?<=[a-z0-9])-(?=[a-z0-9])")
_ARROW = re.compile(r"\s*(?:->|=>|→|⇒|⟶)\s*")
_TYPOS = {"teh": "the", "wolff": "wolf", "woolf": "wolf", "wolfe": "wolf", "werwolf": "werewolf",
          "vilager": "villager", "villlager": "villager", "villiger": "villager", "villagr": "villager",
          "seeer": "seer", "voet": "vote"}
_TYPO_RE = re.compile(r"\b(?:" + "|".join(_TYPOS) + r")\b")
# Runs of whitespace are collapsed to one character (a newline when the run had one) before anything is read: one chat
# of "result" and a thousand spaces, inside the platform's word and character caps, once took 2 s to read, because
# _OWN_QUOTE had three optional whitespace runs in a row.
_WS_RUN = re.compile(r"\s{2,}")


def _squeeze(t: str) -> str:
    return _WS_RUN.sub(lambda m: "\n" if "\n" in m.group() else " ", t)


# A seer quoting its own result ('My result: "Player3 is a wolf"') speaks in its own voice: that quote is kept. Each
# optional word or sign takes its own trailing whitespace, so no two optional whitespace runs touch.
_OWN_QUOTE = re.compile(r"((?:\bmy\s+(?:seer\s+)?|(?:^|[.!?:]\s*)(?:seer\s+)?)(?:results?|checks?|findings?|visions?|"
                        r"readings?)\s*(?:(?:is|was|are|were)\s*)?(?:[:=\-—–]\s*)?)[\"“«„]([^\"”»“]{1,200})[\"”»“]")
_W = r"(?:wolf|wolves|werewolf|werewolves|ww|wolfie)"
_P = r"player([0-6])"
_ADV = r"(?:(?:definitely|def|prob|probably|clearly|obviously|surely|totally|for sure|100%|confirmed|actually)\s+)?"
_CHAT_ACCUSE = [
    re.compile(_P + r"\s*(?:is|=|was|'s|’s|:)?\s*" + _ADV + r"(?:a\s+|an\s+|the\s+|one\s+of\s+the\s+)?" + _W + r"\b"),
    re.compile(r"\b([0-6])\s+(?:is|=|was)\s+" + _ADV + r"(?:a\s+|the\s+)?" + _W + r"\b"),
    re.compile(r"\b" + _W + r"\s*(?:is|=|:)\s*" + _P + r"\b"),
]
_CHAT_CLEAR = [
    re.compile(_P + r"\s*(?:is|=|was|'s|’s|:)?\s*" + _ADV + r"(?:a\s+|an\s+)?(?:villager|clear|cleared|innocent(?:\s+villager)?|good|"
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
# opening a sentence with a verdict on a seat or "I am the seer" right after it (not "Seer claim: none yet", not
# "Seer claim: Player2 says ..."), or a report of a check that carries a verdict on the checked seat ("I checked
# Player0: villager", "Player5 is a wolf, I checked them."; "I checked Player3's votes", "Player4 is a villager, I
# checked her votes", "Player5 is town, I checked them and their votes look fine" and "I looked at Player2 and they
# seem nervous" are not claims).
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
    re.compile(r"\bas\s+(?:the\s+)?(?:real\s+|true\s+|actual\s+)?seer\s*(?:[:—–]|-\s)"),      # "As the seer: P3 is a wolf"
]
# "My role is seer" (not "if my role is seer", not "my role is seer?")
_CLAIM_ROLE = re.compile(r"\bmy\s+(?:role|card|identity)\s+(?:is|=|:)\s*(?:the\s+|a\s+)?seer\b(?!\s*\?)")
_CHECK = re.compile(r"\bi\s+(?:have\s+|'ve\s+|’ve\s+)?(?:checked|investigated|inspected|scanned|peeked\s+at|looked\s+at)"
                    r"\s+player([0-6])\b(?!\s*['’]s\b)([^.!?]{0,60})")
_VERDICT = re.compile(r"\b(?:wolf|werewolf|villager|clear|cleared|clean|innocent|town)\b")

# ---- verdicts as real LLM seats phrase them (added to the patterns above; see the module docstring)
_W2 = r"(?:wolf|wolves|werewolf|werewolves|ww|wolfie|evil)"
_CLEARW = r"(?:villagers?|clear|cleared|clean|innocent|good|town|safe|human)"
_ADV2 = (r"(?:(?:definitely|def|prob|probably|clearly|obviously|surely|totally|confirmed|actually|indeed|also|"
         r"certainly|absolutely|really|undoubtedly|in\s+fact|for\s+sure|without\s+(?:a\s+)?doubt|100%|both)\s+)"
         r"{0,2}")   # not "likely": "Player4 is likely a wolf" is an opinion (the old patterns do not read it either)
_DET = (r"(?:(?:a|an|the|one\s+of\s+the(?:\s+two)?|our|my)\s+)?"
        r"(?:(?:real|true|actual|confirmed|known|other|second|last|remaining|fellow)\s+)?")
# a verdict word must end its phrase: "a wolf." / "a wolf too" / "a wolf, vote" but not "a wolf bluff" / "wolf's" /
# "a wolf or a scared villager"
_END = (r"(?!\s*,?\s*or\b)"
        r"(?=\s*(?:$|[.,!?;:)\]—–-]|(?:too|also|as\s+well|for\s+sure|for\s+certain|imo|tbh|then|now|confirmed|indeed|"
        r"and|but|so|because|since|last\s+night|tonight|today|yesterday|here|btw|lol)\b))")
_END_RE = re.compile(_END)
_SEATS = r"player[0-6](?:\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)player[0-6])*"
_LIST = r"player[0-6](?:\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)player[0-6])+"
# An alternative list identifies no individual: "Wolf is Player1, 5 or 6" must not bind its first seat, and
# "Player1 or Player5 is a wolf" must not bind its last. Mask only that reference, preserving other results in
# the same message and leaving conjunctive reports ("Player1 and Player5 are wolves") intact.
_SEAT_ALTERNATIVES = re.compile(
    r"\bplayer[0-6]\b(?:\s*,\s*(?:player)?[0-9]+\b)*\s*,?\s*or\s+(?:player)?[0-9]+\b"
    r"(?:\s*(?:,\s*(?:or\s+)?|or\s+)(?:player)?[0-9]+\b)*")
_IS = r"\s*(?:is|=|was|['’]s|:)\s+"
_CLEAR_PHRASE = r"(?:" + _DET + _CLEARW + r"|not\s+(?:a\s+|an\s+)?" + _W2 + r")"
_TURNED = (r"\s+(?:turned\s+out\s+to\s+be|came\s+back(?:\s+as)?|showed\s+up\s+as|"
           r"(?:was|is)\s+revealed\s+(?:as|to\s+be)|proved\s+to\s+be|tested\s+as|reads?\s+as)\s+")
_TEAM_W = r"on\s+(?:the\s+)?(?:wol(?:f|ves)|werewol(?:f|ves)|evil|bad)\s+(?:team|side)"
_TEAM_C = r"on\s+(?:the\s+)?(?:village|villagers?|town|good)\s+(?:team|side)"
_DIRECT = [   # (pattern with group "s" = the seat or seats, verdict)
    (re.compile(r"(?P<s>player[0-6])" + _IS + _ADV2 + _DET + r"(?:(?:evil|sneaky|lying|filthy|dirty)\s+)?" + _W2 + _END),
     "wolf"),
    (re.compile(r"(?P<s>player[0-6])\s*(?:is|was|['’]s)\s+" + _ADV2 + _TEAM_W + _END), "wolf"),
    (re.compile(r"(?P<s>player[0-6])\s*(?:is|was|['’]s)\s+" + _ADV2 + _TEAM_C + _END), "clear"),
    (re.compile(r"\bi\s+(?:(?:have|'ve|’ve|had|just|also|already)\s+)*cleared\s+(?P<s>" + _SEATS + r")\b(?!\s*['’]s\b)"),
     "clear"),
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
    (re.compile(r"\bplayer[0-6]" + _IS + _ADV2 + _DET + _CLEARW + r"\s*,?\s+(?:and\s+)?so\s+(?:is|was)\s+"
                r"(?P<s>player[0-6])" + _END), "clear"),
]
_PRON = r"(?:they|he|she|both(?:\s+of\s+them)?)"     # not "it": "It was the wolves" is about the killers
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
# Someone else's words, a hypothetical, or uncertainty right before a verdict: not read.
_CUE = re.compile(r"(?:\b(?P<who>\w+)\s+)?(?:said|says|say|claims?|claimed|claiming|thinks?|thought|believes?|"
                  r"believed|insists?|insisted|swears?|swore|admits?|admitted|denies|denied|deny|knows?|knew|wonders?|"
                  r"asks?|asked|pretends?|pretended|pretending|told\s+\w+|tells\s+\w+|if|whether|unless|maybe|perhaps|"
                  r"suppose|imagine|might|could|either|or)\s*[:,]?\s+(?:that\s+)?$")
# Another seat's stance or someone's attribution right before a verdict ("Player3 keeps insisting they're innocent",
# "Player3 is adamant", "acts like", "According to Player2, they're a wolf", "Per Player2,"): the subject is a third
# person, so "I'm sure they're a wolf" stays the sender's own verdict.
_REPORTED = re.compile(
    r"(?:\b(?:player[0-6]|he|she|they|someone|somebody|everyone|everybody)\s+(?:(?:keeps|kept|is|was|are|were|seems|"
    r"seemed|sounds|sounded|has\s+been|had\s+been|just|still|really)\s+)*(?:saying|insisting|swearing|claiming|"
    r"telling\s+\w+|adamant|convinced|sure|certain|acts?\s+like|acting\s+like|acted\s+like|"
    r"(?:trying|tried|tries)\s+to\s+(?:convince|tell|persuade)\s+\w+)"
    r"|\baccording\s+to\s+\w+|\bper\s+player[0-6]|\bin\s+player[0-6]['’]s\s+(?:view|opinion|words|eyes|mind))"
    r"\s*[:,]?\s+(?:that\s+)?$")
# A first-person hunch right before a verdict ("I suspect they're a wolf", "chances are they're a wolf"): an opinion,
# not a result (inside a seer claim it used to add wolves until the real seer "named too many" and looked a liar).
_HUNCH = re.compile(
    r"(?:\b(?:i|we)\s+(?:\w+\s+)?(?:suspect|guess|bet|fear|worry|reckon|feel\s+like|have\s+a\s+(?:hunch|feeling))|"
    r"\b(?:chances|odds)\s+are|\bmy\s+(?:guess|hunch|gut|feeling)\s+(?:is|says))\s*[:,]?\s+(?:that\s+)?$")
# The verdict is followed by its source ("Player0 and Player3 are wolves, says Player2."): not the sender's verdict
# (but "Player3 is a wolf - says he's a villager, but he lies" is).
_SOURCE_AFTER = re.compile(r"\s*[,—–-]?\s*(?:says|said|claims?|claimed|thinks?|according\s+to|per)\s+"
                           r"(?:player[0-6]|he|she|they|him|her|them)\s*(?=$|[.!?,;])")
_SPEAKER_LABEL = re.compile(r"^\s*player[0-6]\s*:\s*$")   # "Player2: Player0 and Player3 are wolves." (quoting Player2)
# A pronoun's referent clause that agrees with, thanks or credits that seat ("Player2 is right. They're a wolf.", "I
# agree with Player2: they're a wolf.", "Per Player2, ..."): the pronoun is about someone else.
_AGREE = re.compile(
    r"\bagree(?:s|d|ing)?\s+with\b|\b(?:player[0-6]|he|she|they|you)\s+(?:is|was|are|were|['’]re|['’]s)\s+"
    r"(?:so\s+|totally\s+|absolutely\s+|definitely\s+|100%\s+)?right\b|\b(?:good|fair|great|valid)\s+point\b|"
    r"\bwell\s+said\b|\b(?:nice|great|good)\s+(?:catch|call)\b|\bthanks?\b|\bthank\s+you\b|\baccording\s+to\b|"
    r"\bper\s+player[0-6]|\bplayer[0-6]['’]s\s+(?:view|opinion|point|case|logic|reasoning|take|theory|read)\b|"
    r"\b(?:pointed|points)\s+(?:it\s+|this\s+|that\s+)?out\b|"
    r"\bmade\s+a\s+(?:good\s+|strong\s+|fair\s+)?(?:case|point)\b|"
    r"\bwith\s+player[0-6]\s+on\b|\blisten\s+to\s+player[0-6]|"
    r"\bplayer[0-6]\s+(?:also\s+)?(?:noticed|vouched|vouches|defended|defends|backed|backs|supported|supports)\b")
# Negation or doubt before a verdict ("that doesn't mean they're a wolf", "I doubt they're a wolf", "I'm not convinced
# they're a wolf", "no proof they are wolves", "it's unlikely they're a wolf"): never a verdict, in the first person
# too. Up to four words may sit between the cue and the verdict ("no evidence at all that they're a wolf").
_NEG_CUE = re.compile(
    r"(?:(?<!\bno\s)(?<!\ba\s)\bdoubt(?:s|ed|ful)?|\bunlikely|\b(?:im)?possibl[ey]|\bunsure|\buncertain|"
    r"\bnot\s+(?:that|true|sure|convinced|certain|saying|necessarily|clear|positive)|\bfalse\s+that|"
    r"\bno\s+idea|\bwho\s+knows|"
    r"\bnever\s+(?:concluded|confirmed|established|proved|said|claimed|checked|investigated|inspected)|"
    r"\b(?:not|didn['’]t|haven['’]t|hadn['’]t)\s+(?:check(?:ed)?|investigat(?:e|ed)|inspect(?:ed)?)|"
    r"\bhard\s+to\s+(?:say|tell)|"
    r"\b(?:doesn['’]t|does\s+not|don['’]t|do\s+not|didn['’]t|did\s+not|won['’]t|wouldn['’]t|can['’]t|cannot|"
    r"isn['’]t|aren['’]t)\s+(?:\w+\s+)?(?:mean|means|prove|proves|make|makes|show|shows|think|believe|buy|say|"
    r"suggest|suggests|imply|implies)|"
    r"\b(?:no|not|without|any|zero|little|lack\s+of)\s+(?:\w+\s+)?(?:evidence|proof|reason|signs?|indication|basis))"
    r"(?:\s+\w+){0,4}?\s*[,:;—–-]?\s*(?:(?:that|if|whether)\s+)?$")
# The sentence a pronoun refers back to reports that seat's death ("Player3 was killed last night. They were ..."):
# not read (the game reveals a dead seat's role anyway).
_DEATH = re.compile(r"\b(?:died|dead|(?:was|were|got|been|is|are)\s+(?:\w+\s+)?(?:killed|lynched|eliminated|"
                    r"murdered|executed|voted\s+out|taken\s+out))\b")
_PLURAL_VERDICT = re.compile(r"\b(?:both|wolves|werewolves|villagers)\b")
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
# "Player5 is a wolf, I checked them." -- a seer's verdict word (not "good", "town", "safe", "human": table talk) and
# the object pronoun ends the sentence, at most followed by when; not "I checked her votes", not "Player5 is town, I
# checked them and their votes look fine" (a villager looking at the vote log)
_CLAIM_AFTER_VERDICT = re.compile(r"\bplayer[0-6]" + _IS + _ADV2 + _DET + r"(?:" + _W2 + r"|villagers?|clear|cleared|"
                                  r"clean|innocent)\b[^.!?]{0,20}?\bi\s+(?:have\s+|'ve\s+|’ve\s+)?(?:checked|"
                                  r"investigated|inspected)\s+(?:them|him|her)"
                                  r"(?=\s*(?:(?:last\s+night|yesterday|tonight)\s*)?(?:$|[.!?]))")
# "Seer claim: Player5 is a wolf" / "Seer claim: I am the seer" (not "Seer claim: none yet", "Seer claim - anyone?")
# Also "Seer claim. Result: Player5: wolf", "Seer result: Player5 is a wolf", "Claiming seer: ..." (a verdict on a seat
# must follow; "Seer results?" is a question).
_CLAIM_OPEN = re.compile(r"(?:^|[.!?]\s*)(?:hard\s+)?(?:seer\s+(?:claim|results?|checks?|readings?)|claiming\s+seer)"
                         r"\s*[:\-—–.!]\s*(?:(?:my\s+|the\s+)?results?\s*[:\-—–]?\s*)?(?:player[0-6]" + _IS + _ADV2 + _DET
                         + r"(?:" + _W2 + r"|" + _CLEARW + r"|not\s+(?:a\s+|an\s+)?" + _W2 + r")\b|"
                         r"i\s*(?:am|['’]m|m)\s+(?:the\s+)?(?:real\s+|true\s+)?seer\b)")
_TERM = re.compile(r"[.!?]")


def _claims_seer(t: str) -> bool:
    """Does normalized text `t` claim seer in the sender's own voice?"""
    for rx in (_CLAIM_IAM, _CLAIM_ROLE):
        for m in rx.finditer(t):
            start, _ = _sentence(t, m.start())
            if not (_question(t, m.end()) or _hedged(t[start:m.start()])
                    or _NOT_OWN_VOICE.search(t[max(start, m.start() - 40):m.start()])):
                return True
    for rx in _CHAT_CLAIM:
        for m in rx.finditer(t):
            start, _ = _sentence(t, m.start())
            if not (_question(t, m.end()) or _hedged(t[start:m.start()])
                    or _NOT_OWN_VOICE.search(t[max(start, m.start() - 40):m.start()])):
                return True
    for m in _CHECK.finditer(t):
        start, _ = _sentence(t, m.start())
        if (_question(t, m.end()) or _hedged(t[start:m.start()])
                or _NOT_OWN_VOICE.search(t[max(start, m.start() - 40):m.start()])):
            continue
        # A role word anywhere after "checked" is not enough: villagers discuss checks hypothetically, ask about
        # them, or inspect a seat's vote record. Require a verdict actually bound to this check's subject.
        wolves, clears = _verdicts(m.group(0))
        if int(m.group(1)) in wolves | clears:
            return True
    for m in _CLAIM_OPEN.finditer(t):
        if not _question(t, m.end()):
            return True
    for m in _CLAIM_AFTER_VERDICT.finditer(t):
        start, _ = _sentence(t, m.start())
        if not (_question(t, m.end()) or _NOT_OWN_VOICE.search(t[max(start, m.start() - 40):m.start()])
                or _hedged(t[start:m.start()])):
            return True
    return False


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
    """Reported speech, a hypothetical or uncertainty right before a verdict ("Player2 says", "if", "maybe", "Player3
    keeps insisting", "according to Player2", "I suspect"); a first-person "I think" / "I know" stays the sender's own
    voice."""
    # "Seer claim: Player3 is a wolf" is the sender's own result header, not reported speech by a person named Seer.
    if re.fullmatch(r"\s*(?:hard\s+)?seer\s+claim\s*[:—–-]\s*", before):
        return False
    m = _CUE.search(before)
    return (bool(m) and m.group("who") != "i") or bool(_REPORTED.search(before)) or bool(_HUNCH.search(before))


def _negated(before: str) -> bool:
    """Negation or doubt right before a verdict ("that doesn't mean", "I doubt", "no proof"), first person included."""
    return bool(_NEG_CUE.search(before))


def _hedged(prefix: str) -> bool:
    """The part of a sentence before a verdict makes it not the sender's verdict: a hypothetical opening ("If ..."),
    reported speech or uncertainty right before it, or a negation / doubt."""
    return bool(_IF_START.search(prefix)) or _cued(prefix) or _negated(prefix)


def _asserted_match(t: str, m: re.Match) -> bool:
    """Legacy short verdicts use the same attribution and uncertainty guards as the richer patterns.

    Without this check, a quoted-in-prose accusation such as "Player2 says Player3 is a wolf" bypasses all the
    newer guards. A claimed seer's table talk would then become a false investigation and discredit their genuine
    results. Requiring a phrase ending also rejects "a wolf or a villager" and "a wolf bluff".
    """
    start, end = _sentence(t, m.start())
    prefix = t[start:m.start()]
    return (not _question(t, m.end()) and not _hedged(prefix) and not _SPEAKER_LABEL.match(prefix)
            and not _SOURCE_AFTER.match(t, m.end(), max(end, m.end())) and bool(_END_RE.match(t, m.end())))


_NEG_ACTION = re.compile(r"\b(?:not|never|don['’]t|won['’]t|wouldn['’]t|can['’]t|cannot)\s*(?:\w+\s+){0,2}$")


def _asserted_suspicion(t: str, m: re.Match) -> bool:
    """A vote appeal needs the same own-voice guard as a result, plus action negation."""
    start, end = _sentence(t, m.start())
    prefix = t[start:m.start()]
    return (not _question(t, m.end()) and not _hedged(prefix) and not _NEG_ACTION.search(prefix)
            and not _SPEAKER_LABEL.match(prefix) and not _SOURCE_AFTER.match(t, m.end(), max(end, m.end())))


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
            start, end = _sentence(t, m.start())
            prefix = t[start:m.start()]
            if (not _question(t, m.end()) and not _hedged(prefix) and not _SPEAKER_LABEL.match(prefix)
                    and not _SOURCE_AFTER.match(t, m.end(), max(end, m.end()))):
                out[kind].update(seats_of(m.group("s")))
    # A check report: the verdict after the checked seat(s), up to the sentence end or the next seat named.
    for opener in (_CHECK_OPEN, _REVEAL_OPEN):
        for m in opener.finditer(t):
            start, _ = _sentence(t, m.start())
            if (_NOT_OWN_VOICE.search(t[max(start, m.start() - 40):m.start()]) or _question(t, m.end())
                    or _hedged(t[start:m.start()])):
                continue
            seats = seats_of(m.group("s"))
            win = t[m.end():m.end() + 80]
            cut = re.search(r"[.!?;]|\bplayer[0-6]", win)
            win = win[:cut.start()] if cut else win
            hit = None
            for rx, kind in _PV:
                for pm in rx.finditer(win):
                    if not _cued(win[:pm.start()]) and not _negated(win[:pm.start()]):
                        hit = (kind, pm.group(0))
                        break
                if hit:
                    break
            if hit is None:
                for rx in ((_WIN_IS, _WIN_LEAD, _WIN_CAME) if opener is _REVEAL_OPEN else (_WIN_LEAD, _WIN_CAME)):
                    vm = rx.search(win)
                    if vm and not _cued(win[:vm.start()]) and not _negated(win[:vm.start()]):
                        hit = (_kind(vm.group("v")), vm.group(0))
                        break
            if hit and (hit[0] == "clear" or _plural(seats, hit[1])):
                out[hit[0]].update(seats)
    # A pronoun (or a left-out subject) refers to the one seat its sentence names before it, or, when it opens the
    # sentence, to the one seat the previous sentence names; a plural verdict ("they're wolves") never refers to one
    # seat, and a sentence that reports the seat's death is not a referent.
    for rxs, vocative in ((_PV, False), (_PV_YOU, True)):
        for rx, kind in rxs:
            for m in rx.finditer(t):
                start, end = _sentence(t, m.start())
                if end < len(t) and t[end] == "?":
                    continue
                prefix = t[start:m.start()]
                if _hedged(prefix):
                    continue
                if vocative:
                    vm = re.match(r"\s*player([0-6])\s*,", prefix)
                    if vm and len(set(seats_of(prefix))) == 1:
                        out[kind].add(int(vm.group(1)))
                    continue
                if _PLURAL_VERDICT.search(m.group(0)) or _SOURCE_AFTER.match(t, m.end(), max(end, m.end())):
                    continue
                ref, seats = prefix, set(seats_of(prefix))
                if not seats and not prefix.strip() and start > 0:
                    pstart, _ = _sentence(t, start - 1)
                    prev = t[pstart:start - 1]
                    if t[start - 1] != "?" and not _IF_START.search(prev):
                        ref, seats = prev, set(seats_of(prev))
                if len(seats) == 1 and not _DEATH.search(ref) and not _AGREE.search(ref):
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
                        r"i|am|is|a|an|be|been|being|my|our|role|card|identity|it|we|us|he|they|them|"
                        r"to|of|as|for|from|with|this|that|these|those|so|but|or|if|when|then|because|since|"
                        r"definitely|def|probably|prob|really|clearly|obviously|surely|totally|actually|confirmed|"
                        r"innocent|evil|scum|suspicious|shady|unknown|"
                        r"yes|lol|idk|good|town|clear|safe|kill|day|night|all|any|one|two|out|sus)$")


@dataclass
class ChatFacts:
    claim: bool = False                              # the sender claims to be the seer
    wolves: set = field(default_factory=set)         # seats the sender calls wolf
    clears: set = field(default_factory=set)         # seats the sender calls villager / clear
    sus: set = field(default_factory=set)            # weaker: suspicious, vote-for, lynch
    injection: bool = False                          # tried to instruct the table; content not read
    conflicts: set = field(default_factory=set)      # incompatible assertions: wolf+clear, or claiming seer+self wolf
    self_wolf: bool = False                          # retain an asserted self role for earlier/later seer claims


def normalize(text, names: dict[int, str] | None = None) -> str:
    """Lower-case ASCII-ish text with seat mentions rewritten to 'playerN' and quoted passages removed."""
    if not isinstance(text, str):
        return ""
    t = _squeeze(unicodedata.normalize("NFKC", text[:MAX_CHAT_CHARS]).casefold())
    t = _MARKUP.sub("", t)
    t = _BLOCK_QUOTE.sub("", t)

    def own_quote(m):
        start, _ = _sentence(t, m.start())
        # "Player2 said my result: ..." is still Player2's report, not the sender's own quoted result.
        return m.group(0) if _question(t, m.end()) or _hedged(t[start:m.start()]) else f"{m.group(1)}{m.group(2)}. "

    t = _OWN_QUOTE.sub(own_quote, t)
    t = _QUOTED.sub(" ", t)
    # Protect complete seat references before an alias such as "five" or "seat" can rewrite part of one.
    t = _SEAT_WORD.sub(lambda m: f"player{_NUM_WORDS[m.group(1)]}", t)
    t = _SEAT_TOKEN.sub(lambda m: f"player{m.group(1)}", t)
    aliases = {seat: unicodedata.normalize("NFKC", str(name)).casefold().strip()
               for seat, name in (names or {}).items()}
    for seat, n in sorted(aliases.items(), key=lambda x: -len(x[1])):
        # Duplicate display names cannot identify a seat. Names that look like a documented seat label must never
        # rewrite that label to another seat (for example a player whose chosen name is "p5").
        if (len(n) >= 3 and sum(value == n for value in aliases.values()) == 1
                and not _SEAT_TOKEN.fullmatch(n) and not _SEAT_WORD.fullmatch(n)
                and not re.fullmatch(r"(?:player|seat|agent)?\s*\d+", n)
                and not any(_NAME_STOP.search(word) for word in re.split(r"[\W_]+", n)) and not _hedged(n + " ")
                and not _NEG_ACTION.search(n + " ")
                and not _CLAIM_IAM.fullmatch(n + " seer") and not _CLAIM_ROLE.fullmatch(n + " seer")):
            t = re.sub(r"(?<![\w])" + re.escape(n) + r"(?![\w])", f"player{seat}", t)
    t = _HYPHEN_WORDS.sub(" ", t)
    t = _ARROW.sub(" : ", t)
    t = _TYPO_RE.sub(lambda m: _TYPOS[m.group(0)], t)
    t = _SEAT_WORD.sub(lambda m: f"player{_NUM_WORDS[m.group(1)]}", t)
    return _SEAT_TOKEN.sub(lambda m: f"player{m.group(1)}", t)


def _self_claim(t: str, sender: int) -> bool:
    """The sender claims seer in the third person ("Player2 is the seer (that's me)", sent by Player2)."""
    for m in re.finditer(rf"\bplayer{sender}\b\s*(?:is|=|:)\s+(?:the\s+)?(?:real\s+|true\s+|actual\s+)?seer\b", t):
        start, _ = _sentence(t, m.start())
        if not (_question(t, m.end()) or _hedged(t[start:m.start()])):
            return True
    return False


# Verdict words read only inside a seer claim ("I am the Seer. Player5 is bad / scum."): as table talk they are opinions.
_CLAIM_ONLY_W = re.compile(r"(?P<s>player[0-6])" + _IS + _ADV2 + r"(?:a\s+)?(?:scum|scummy|bad|evil)" + _END)
_SELF_WOLF = re.compile(r"\b(?:i\s*am|i['’]m|im|my\s+(?:role|card|identity)\s+(?:is|=|:))\s+" + _DET + _W2 + _END)


def _claim_only_wolves(t: str) -> set[int]:
    out = set()
    for m in _CLAIM_ONLY_W.finditer(t):
        start, _ = _sentence(t, m.start())
        if not (_question(t, m.end()) or _hedged(t[start:m.start()])):
            out.add(int(m.group("s")[6:]))
    return out


def read_chat(text, sender: int | None = None, names: dict[int, str] | None = None) -> ChatFacts:
    """The facts one chat message asserts (never instructions). The sender's own seat is never its own target."""
    f = ChatFacts()
    raw = _squeeze(unicodedata.normalize("NFKC", text[:MAX_CHAT_CHARS]).casefold()) if isinstance(text, str) else ""
    t = normalize(text, names)
    if _INJECTION.search(raw) or _INJECTION.search(t):
        f.injection = True
        return f
    def speaker_line(m):
        body = m.group(3)
        # A compact result list is not a copied speaker turn: "Player5: wolf; Player4: clear". Keep the seat
        # attached to its result, including a sender's third-person role claim "Player3: Seer".
        if not m.group(2) and (_WIN_LEAD.match(body) or _SEER_LABEL_RESULT.match(body)):
            return m.group(0)
        return body if sender == int(m.group(1)) else ""

    t = _SEAT_ALTERNATIVES.sub("candidates", _SPEAKER_LINE.sub(speaker_line, t))
    f.claim = _claims_seer(t) or (isinstance(sender, int) and _self_claim(t, sender))
    for p in _CHAT_ACCUSE:
        f.wolves |= {int(m.group(1)) for m in p.finditer(t) if _asserted_match(t, m)}
    for p in _CHAT_CLEAR:
        f.clears |= {int(m.group(1)) for m in p.finditer(t) if _asserted_match(t, m)}
    for p in _CHAT_SUS:
        f.sus |= {int(m.group(1)) for m in p.finditer(t) if _asserted_suspicion(t, m)}
    wolves, clears = _verdicts(t)
    f.wolves |= wolves
    f.clears |= clears
    if f.claim:
        f.wolves |= _claim_only_wolves(t)
    f.conflicts = f.wolves & f.clears
    f.self_wolf = isinstance(sender, int) and (sender in f.wolves or any(
        _asserted_match(t, m) for m in _SELF_WOLF.finditer(t)))
    if f.claim and f.self_wolf:
        f.conflicts.add(sender)                     # a seer cannot also be a wolf; do not erase this with self-targets
    f.clears -= f.wolves                            # preserve verdict precedence, but expose conflicting evidence
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
    dead_cause: dict[int, str]                  # seat -> CAUSE_NIGHT ("night_kill") | CAUSE_VOTE ("vote"), norm_cause
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
        for m in ALLY_ANY_RE.finditer(obs):
            seats = sorted({int(x) for x in _SEAT_ANY.findall(m.group(1)) if int(x) <= 6})
            if seats:
                ally_line, allies = True, seats
                break

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
    if not invest_line and NO_INVEST_RE.search(obs):
        invest_line = True                      # the seer before its first check (a seer cue like the line itself)
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
            cause[s] = norm_cause(d.get("cause"))
    # The observation carries the same public record by seat label: use it when the structured block is missing
    # or its names do not map to seats.
    if not alive:
        m = LIVING_RE.search(obs)
        if m:
            alive = sorted({int(x) for x in SEAT_RE.findall(m.group(1))})
    if not dead:
        for _day, s, r, c in DEAD_LINE_RE.findall(obs):
            dead[int(s)] = "wolf" if r.lower() == "werewolf" else r.lower()
            cause[int(s)] = norm_cause(c)

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
            e = VOTE_OUT_RE.search(m.group(3) or "")
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
