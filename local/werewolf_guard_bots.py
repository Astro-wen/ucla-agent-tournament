"""Cheap, observation-only Werewolf opponents for robustness checks.

These are deliberately small probes, not claims about real opponent strength.
Use ordinary tables with one swapped probe, e.g. ``heuristic+1guard_repeat``.
``heuristic+1guard_accuse`` uses exactly the same deals/attacker seats and differs
only in sending one accusation instead of five; the evaluator's existing swap
syntax does this without giving either bot its opponents' types or roles.

Fake claims are made by wolves only; an actual seer reports its own checks and
villagers stay silent in those variants. Homogeneous probe tables are available
as stress cases but should be reported separately from one-attacker tables.
There are no Game references, agent imports, hidden-role inputs, shared meaning
registries, environment switches, network calls, or simulator seed reads here.
"""
from __future__ import annotations

import re

from .werewolf_bots import _legal, _private
from .werewolf_env import ABSTAIN, OfficialBaseline, Seat

_SEAT = re.compile(r"Player([0-6])\b", re.I)
_VOTE = re.compile(r"\b(?:I(?:'ll| will)?\s+)?vote\s+(?:for\s+)?Player([0-6])\b", re.I)


def _alive(raw: dict) -> list[int]:
    """Seat labels are public and remain unambiguous when display names collide."""
    line = re.search(r"^Living:\s*(.*)$", raw.get("observation") or "", re.M)
    if line:
        return sorted({int(s) for s in _SEAT.findall(line.group(1))})
    names = {p.get("name"): p.get("position") for p in raw.get("players") or []}
    return sorted({names[n] for n in (raw.get("game_state") or {}).get("alive") or []
                   if n in names and isinstance(names[n], int) and 0 <= names[n] < 7})


class GuardSeat(Seat):
    """Legal first-target play; only subclasses' chat/vote rule varies."""
    name = "guard_silent"
    chats = 0
    high_target = False

    def reset(self):
        self._day = None
        self._sent = 0
        self._done = False
        self._messages: dict[int, dict] = {}

    def _read(self, raw):
        day = (raw.get("game_state") or {}).get("day")
        if day != getattr(self, "_day", None):
            self.reset()
            self._day = day
        for msg in raw.get("new_messages") or []:
            if not isinstance(msg, dict) or msg.get("type", "chat") != "chat":
                continue
            if msg.get("recipients") or msg.get("visibility", "broadcast") != "broadcast":
                continue
            sender, seq = msg.get("sender"), msg.get("seq")
            if not isinstance(sender, int) or not 0 <= sender < 7 or not isinstance(seq, int):
                continue
            if msg.get("day", day) == day:
                self._messages.setdefault(seq, dict(msg))

    def _target(self, raw, targets):
        role, allies, inv = _private(raw)
        known = [t for t in targets if role == "seer" and inv.get(t) == "wolf"]
        pool = [t for t in targets if t not in allies and (role != "seer" or inv.get(t) != "villager")]
        pool = known or pool or targets
        return (max(pool) if self.high_target else min(pool)) if pool else None

    def choose(self, raw: dict) -> str:
        self._read(raw)
        legal = _legal(raw)
        if not legal:
            raise ValueError("guard seat was asked to act without legal actions")
        targets = [int(a) for a in legal if str(a).isdigit() and 0 <= int(a) < 7]
        sub = (raw.get("game_state") or {}).get("phase")
        if sub in ("night_wolf", "night_seer"):
            # Keep all variants' night behavior identical. Only own checks are visible.
            _, _, inv = _private(raw)
            pool = [t for t in targets if t not in inv] if sub == "night_seer" else targets
            pick = min(pool or targets) if targets else None
        else:
            pick = self._target(raw, targets)
        return str(pick) if pick is not None and str(pick) in legal else legal[0]

    def _text(self, raw, target):
        return f"I will vote Player{target}."

    def message(self, raw: dict):
        self._read(raw)
        me = raw.get("your_position")
        if raw.get("phase") != "messaging" or raw.get("messaging_enabled") is False or raw.get("eliminated"):
            return None
        if me in (raw.get("terminated_messaging") or []):
            self._done = True
        if self._done or self._sent >= self.chats:
            self._done = True
            return None
        target = self._target(raw, [s for s in _alive(raw) if s != me])
        text = self._text(raw, target) if target is not None else None
        if text is None:
            self._done = True
            return None
        self._sent += 1
        return {"content": text, "recipients": []}


class AbstainSeat(GuardSeat):
    """The legal idle proxy: abstain by day, first available action at night."""
    name = "guard_abstain"

    def choose(self, raw):
        if (raw.get("game_state") or {}).get("phase") == "day_vote" and str(ABSTAIN) in _legal(raw):
            return str(ABSTAIN)
        return super().choose(raw)


class FixedVoteSeat(GuardSeat):
    name = "guard_fixed"
    chats = 1


class VoteEchoSeat(FixedVoteSeat):
    """Follow the first public vote call for a legal, non-partner target."""
    name = "guard_echo"

    def _target(self, raw, targets):
        role, allies, inv = _private(raw)
        # Private checked wolves still take precedence over this deliberately gullible rule.
        if role == "seer" and any(inv.get(t) == "wolf" for t in targets):
            return super()._target(raw, targets)
        for seq in sorted(self._messages):
            msg = self._messages[seq]
            if msg["sender"] == raw.get("your_position"):
                continue
            hit = _VOTE.search(str(msg.get("content") or ""))
            target = int(hit.group(1)) if hit else None
            if target in targets and target not in allies and (role != "seer" or inv.get(target) != "villager"):
                return target
        return super()._target(raw, targets)


class AccuseSeat(GuardSeat):
    name = "guard_accuse"
    chats = 1
    high_target = True

    def _text(self, raw, target):
        return f"Player{target} is a wolf. Vote Player{target}."


class RepeatAccuseSeat(AccuseSeat):
    """Same choice and same statement as AccuseSeat, using the full five-message quota."""
    name = "guard_repeat"
    chats = 5


class FakeSeerSeat(AccuseSeat):
    name = "guard_fake"

    def _text(self, raw, target):
        role, _, inv = _private(raw)
        if role == "wolf":
            return f"I am the Seer. I checked Player{target}: wolf. Vote Player{target}."
        if role == "seer" and self._sent == 0:
            checks = sorted((t, verdict) for t, verdict in inv.items() if t in _alive(raw))
            if checks:
                t, verdict = checks[0]
                return f"I am the Seer. I checked Player{t}: {verdict}."
        return None


class ContradictSeerSeat(FakeSeerSeat):
    name = "guard_contradict"
    chats = 2

    def _text(self, raw, target):
        if _private(raw)[0] == "wolf" and self._sent == 1:
            return f"I am the Seer. I checked Player{target}: villager. Player{target} is clear."
        return super()._text(raw, target)


class BaitSeat(AccuseSeat):
    """Quoted and hypothetical verdicts: neither is a real assertion of a result."""
    name = "guard_bait"
    chats = 2

    def _text(self, raw, target):
        if self._sent == 0:
            return f'Example: "I am the Seer. Player{target} is a wolf." That is a quote, not a role claim.'
        return f"If I were the Seer, I might suspect Player{target} is a wolf; this is only a hypothetical."


class FirstHighNightSeat(OfficialBaseline):
    name = "first_high_night"

    def choose(self, raw):
        if (raw.get("game_state") or {}).get("phase") == "night_wolf":
            return max(_legal(raw), key=int)
        return super().choose(raw)


class FirstRandomNightSeat(OfficialBaseline):
    name = "first_random_night"

    def choose(self, raw):
        if (raw.get("game_state") or {}).get("phase") == "night_wolf":
            return self.rng.choice(_legal(raw))
        return super().choose(raw)


class FirstSwitchSeat(OfficialBaseline):
    name = "first_switch"

    def choose(self, raw):
        gs = raw.get("game_state") or {}
        if gs.get("phase") == "day_vote" and gs.get("day", 1) >= 2:
            return max((a for a in _legal(raw) if a != str(ABSTAIN)), key=int)
        return super().choose(raw)


GUARD_BACKGROUNDS = {cls.name: cls for cls in (
    FirstHighNightSeat, FirstRandomNightSeat, FirstSwitchSeat,
    GuardSeat, AbstainSeat, FixedVoteSeat, VoteEchoSeat, AccuseSeat, RepeatAccuseSeat,
    FakeSeerSeat, ContradictSeerSeat, BaitSeat,
)}
