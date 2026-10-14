"""Independent observation-only population challenges; not a strength estimate.

Only Seat observations are inputs. No candidate imports, simulator objects,
identity dispatch, meaning registry, network access or shared mutable memory.
Factories are exported for the evaluator to register explicitly.
"""
from __future__ import annotations

import re
from collections import Counter

from .werewolf_bots import _private, _public
from .werewolf_env import SIM_HOLD, Seat


_PLAYER = re.compile(r"Player([0-6])\b", re.I)
_CLAIM = re.compile(r"I(?: am|'m) (?:the )?(?:real )?Seer", re.I)
_CHECK = re.compile(r"(?:I (?:checked|investigated) Player([0-6]):|Player([0-6]) is(?: a)?) (wolf|villager)", re.I)
_VOTE = re.compile(r"(?:I (?:will |shall )?)?vote(?: for)? Player([0-6])", re.I)
_KILL = re.compile(r"Tonight kill Player([0-6])\.", re.I)


def _seat(value):
    return type(value) is int and 0 <= value < 7


def _own(raw):
    role, allies, checks = _private(raw)
    return (role, [s for s in allies if _seat(s)] if role == "wolf" else [],
            {s: v for s, v in checks.items() if _seat(s)} if role == "seer" else {})


def _table(raw):
    observation = raw.get("observation") or ""
    living = re.search(r"^Living:[ \t]*(.*)$", observation, re.M)
    if not living:
        return _public(raw)
    alive = sorted({int(s) for s in _PLAYER.findall(living.group(1))})
    dead = {int(s): role.lower() for s, role in re.findall(
        r"^\s*day \d+: Player([0-6]) was (wolf|seer|villager)\b", observation, re.M | re.I)}
    history = []
    for line in re.findall(r"^Day \d+ vote: (.*)$", observation, re.M):
        ballots = {int(s): None if t == "abstain" else int(t[-1]) for s, t in re.findall(
            r"Player([0-6]) -> (Player[0-6]\b|abstain\b)", line)}
        hit = re.search(r"— Player([0-6]) (?:eliminated by vote|lynched)", line)
        history.append((ballots, int(hit.group(1)) if hit else None))
    return raw.get("your_position"), alive, dead, history


def _sentences(text):
    return [part.strip() for part in re.split(r"[.!?]", text) if part.strip()]


class PopulationSeat(Seat):
    name = "population_base"
    prompt_results = False

    def reset(self):
        self._day = None
        self._sent = 0
        self._done = False
        self._announced = set()
        self._reported = set()
        self._messages = {}
        self._adapted = False
        self._kill_plan = None
        self._fake_result = None
        self._holds = 0

    def _read(self, raw):
        if not hasattr(self, "_messages"):
            self.reset()
        day = (raw.get("game_state") or {}).get("day")
        if day != self._day:
            self._day, self._sent, self._done = day, 0, False
            self._announced = set()
            self._holds = 0
        me, alive, _, _ = _table(raw)
        for message in raw.get("new_messages") or []:
            if not isinstance(message, dict) or message.get("type") != "chat":
                continue
            sender, seq = message.get("sender"), message.get("seq")
            text, recipients = message.get("content"), message.get("recipients")
            when = message.get("day", day)
            if (not _seat(sender) or type(seq) is not int or seq < 0
                    or not isinstance(text, str) or not 0 < len(text.split()) <= 50 or len(text) > 1024
                    or type(when) is not int or type(day) is not int or not 1 <= when <= day
                    or not isinstance(recipients, list) or len(recipients) > 1
                    or any(not _seat(s) or s == sender for s in recipients)):
                continue
            if when == day and sender not in alive:
                continue
            visibility = message.get("visibility")
            if recipients:
                if visibility != "direct" or (me not in recipients and sender != me):
                    continue
            elif visibility != "broadcast":
                continue
            self._messages.setdefault(seq, (when, sender, text, tuple(recipients)))
        self._observe(raw)

    def _observe(self, raw):
        pass

    def _intents(self, raw):
        me, alive, _, _ = _table(raw)
        intents = {}
        for seq in sorted(self._messages):
            day, sender, text, recipients = self._messages[seq]
            if day != self._day or recipients or sender == me or sender not in alive:
                continue
            for sentence in _sentences(text):
                hit = _VOTE.fullmatch(sentence)
                if hit and int(hit.group(1)) in alive and int(hit.group(1)) != sender:
                    intents[sender] = int(hit.group(1))
        return intents

    def _claims(self, raw):
        me, alive, dead, _ = _table(raw)
        claims, contradictions = {}, set()
        for seq in sorted(self._messages):
            _, sender, text, recipients = self._messages[seq]
            if recipients or sender == me or (sender not in alive and dead.get(sender) != "seer"):
                continue
            sentences = _sentences(text)
            if any(_CLAIM.fullmatch(sentence) for sentence in sentences):
                claims.setdefault(sender, {})
            if sender not in claims:
                continue
            for sentence in sentences:
                hit = _CHECK.fullmatch(sentence)
                if not hit:
                    continue
                target, verdict = int(hit.group(1) or hit.group(2)), hit.group(3).lower()
                if target == sender or (target in claims[sender] and claims[sender][target] != verdict):
                    contradictions.add(sender)
                else:
                    claims[sender][target] = verdict
        return {s: results for s, results in claims.items() if s not in contradictions}, contradictions

    def _known(self, raw):
        role, _, checks = _own(raw)
        if role == "seer":
            return checks
        _, _, dead, _ = _table(raw)
        claims, _ = self._claims(raw)
        return {target: verdict for sender, results in claims.items() if dead.get(sender) == "seer"
                for target, verdict in results.items()}

    def _history_scores(self, raw):
        _, alive, dead, history = _table(raw)
        scores = {s: 0.0 for s in alive}
        for ballots, eliminated in history:
            for voter, target in ballots.items():
                if voter not in scores:
                    continue
                if target is None:
                    scores[voter] += 0.25
                elif target == eliminated and eliminated in dead:
                    scores[voter] += -2.0 if dead[eliminated] == "wolf" else 1.0
        return scores

    def _scores(self, raw):
        scores = self._history_scores(raw)
        weight = 0.0 if self._adapted else 0.75
        for target, count in Counter(self._intents(raw).values()).items():
            scores[target] = scores.get(target, 0.0) + weight * count
        claims, contradictions = self._claims(raw)
        for sender in contradictions:
            scores[sender] = scores.get(sender, 0.0) + 3.0
        if len(claims) == 1:
            for results in claims.values():
                for target, verdict in results.items():
                    scores[target] = scores.get(target, 0.0) + (2.0 if verdict == "wolf" else -1.0)
        return scores

    def _vote_pool(self, raw, targets):
        role, allies, _ = _own(raw)
        if role == "wolf":
            return [s for s in targets if s not in allies]
        known = self._known(raw)
        wolves = [s for s in targets if known.get(s) == "wolf"]
        return wolves or [s for s in targets if known.get(s) != "villager"]

    def _vote_target(self, raw, targets):
        pool = self._vote_pool(raw, targets)
        scores = self._scores(raw)
        return max(pool, key=lambda s: (scores.get(s, 0.0), -s)) if pool else None

    def _night_target(self, raw, targets):
        claims, _ = self._claims(raw)
        scores = self._history_scores(raw)
        return max(targets, key=lambda s: (s in claims, -scores.get(s, 0.0), -s)) if targets else None

    def choose(self, raw):
        self._read(raw)
        if (raw.get("phase") != "moving" or raw.get("eliminated") or raw.get("is_terminal")
                or raw.get("is_current_actor") is False):
            return None
        actions = (raw.get("legal_actions") or {}).get("actions") or []
        legal = [str(a["action_id"]) for a in actions if isinstance(a, dict)
                 and str(a.get("action_id")) in {str(s) for s in range(8)}]
        me, alive, _, _ = _table(raw)
        role, allies, checks = _own(raw)
        sub = (raw.get("game_state") or {}).get("phase")
        targets = sorted({int(a) for a in legal if a != "7" and int(a) in alive and int(a) != me})
        if sub == "night_wolf" and role == "wolf":
            targets = [s for s in targets if s not in allies]
            pick = self._night_target(raw, targets)
        elif sub == "night_seer" and role == "seer":
            pool = [s for s in targets if s not in checks] or targets
            scores = self._scores(raw)
            pick = max(pool, key=lambda s: (scores.get(s, 0.0), -s)) if pool else None
        elif sub == "day_vote":
            pick = self._vote_target(raw, targets)
            if pick is None and "7" in legal:
                return "7"
        else:
            return None
        return str(pick) if pick is not None and str(pick) in legal else (str(targets[0]) if targets else None)

    def _ordinary_message(self, raw):
        me, alive, _, _ = _table(raw)
        role, _, checks = _own(raw)
        if role == "seer":
            available = [(s, v) for s, v in checks.items() if (s, v) not in self._reported
                         and (self.prompt_results or v == "wolf" or (self._day or 1) >= 2 or len(alive) <= 5)]
            if available:
                target, verdict = min(available, key=lambda item: (item[1] != "wolf", item[0]))
                self._reported.add((target, verdict))
                return {"content": f"I am the Seer. I checked Player{target}: {verdict}.", "recipients": []}
        target = self._vote_target(raw, [s for s in alive if s != me])
        if target is not None and target not in self._announced:
            self._announced.add(target)
            return {"content": f"I will vote Player{target}.", "recipients": []}
        return None

    def _next_message(self, raw):
        return self._ordinary_message(raw)

    def message(self, raw):
        self._read(raw)
        if (raw.get("phase") != "messaging" or raw.get("messaging_enabled") is False
                or raw.get("eliminated") or raw.get("is_terminal")
                or (raw.get("game_state") or {}).get("phase") != "day_vote"):
            return None
        seconds = raw.get("messaging_seconds_left")
        if (raw.get("your_position") in (raw.get("terminated_messaging") or [])
                or (isinstance(seconds, (int, float)) and seconds <= 0) or self._sent >= 5):
            self._done = True
        if self._done:
            return None
        output = self._next_message(raw)
        if output is SIM_HOLD:
            return output
        if output is None:
            self._done = True
            return None
        self._sent += 1
        return output


class CoordinatedSeat(PopulationSeat):
    name = "pop_coordinated"

    def _night_target(self, raw, targets):
        me, _, _, _ = _table(raw)
        _, allies, _ = _own(raw)
        leader = min([me] + allies)
        if leader == me and self._kill_plan:
            day, target = self._kill_plan
            if day >= (self._day or 1) - 1 and target in targets:
                return target
        for seq in sorted(self._messages, reverse=True):
            day, sender, text, recipients = self._messages[seq]
            hit = _KILL.fullmatch(text)
            if (sender == leader and sender in allies and recipients == (me,) and hit
                    and day >= (self._day or 1) - 1 and int(hit.group(1)) in targets):
                return int(hit.group(1))
        return super()._night_target(raw, targets)

    def _next_message(self, raw):
        me, alive, _, _ = _table(raw)
        role, allies, _ = _own(raw)
        partner = [s for s in allies if s in alive]
        if role == "wolf" and partner and me < min(partner) and self._sent == 0:
            target = super()._night_target(raw, [s for s in alive if s != me and s not in allies])
            if target is not None:
                self._kill_plan = self._day, target
                return {"content": f"Tonight kill Player{target}.", "recipients": [min(partner)]}
        return super()._next_message(raw)


class PromptSeerSeat(PopulationSeat):
    name = "pop_prompt_seer"
    prompt_results = True


class LateClaimSeat(PopulationSeat):
    name = "pop_late_claim"

    def _vote_target(self, raw, targets):
        if _own(raw)[0] == "wolf" and self._fake_result in self._vote_pool(raw, targets):
            return self._fake_result
        return super()._vote_target(raw, targets)

    def _next_message(self, raw):
        if _own(raw)[0] != "wolf" or self._fake_result is not None:
            return super()._next_message(raw)
        if self._sent == 0:
            return {"content": "I want to compare claims before committing. What did the Seer find?", "recipients": []}
        me, alive, _, _ = _table(raw)
        heard = {s for day, s, _, rec in self._messages.values()
                 if day == self._day and s != me and s in alive and not rec}
        seconds = raw.get("messaging_seconds_left")
        late = isinstance(seconds, (int, float)) and seconds <= 40
        if len(heard) >= 2 or late:
            pool = self._vote_pool(raw, [s for s in alive if s != me])
            claims, _ = self._claims(raw)
            target = next((s for s in sorted(claims) if s in pool), None)
            if target is None:
                target = super()._vote_target(raw, pool)
            if target is not None:
                self._fake_result = target
                return {"content": f"I am the Seer. I checked Player{target}: wolf.", "recipients": []}
        if self._holds < 3 and not late:
            self._holds += 1
            return SIM_HOLD
        return super()._next_message(raw)


class PressureBusSeat(PopulationSeat):
    name = "pop_pressure_bus"

    def _vote_target(self, raw, targets):
        role, allies, _ = _own(raw)
        _, alive, _, _ = _table(raw)
        if role == "wolf":
            pressure = Counter(t for s, t in self._intents(raw).items() if s not in allies)
            for ally in sorted(set(allies) & set(targets)):
                alternative = max((n for t, n in pressure.items() if t != ally), default=0)
                if pressure[ally] >= max(2, len(alive) // 2) and pressure[ally] >= alternative + 2:
                    return ally
        return super()._vote_target(raw, targets)


class DistanceSeat(PopulationSeat):
    name = "pop_distance"

    def _vote_target(self, raw, targets):
        role, allies, _ = _own(raw)
        _, alive, _, _ = _table(raw)
        intents = self._intents(raw)
        pressure = Counter(intents.values())
        if role == "wolf" and len(alive) > 4 and max(pressure.values(), default=0) < 3:
            partners_targets = {t for s, t in intents.items() if s in allies}
            separate = [s for s in self._vote_pool(raw, targets) if s not in partners_targets]
            if partners_targets and separate:
                return super()._vote_target(raw, separate)
        return super()._vote_target(raw, targets)


class ObservedSwitchSeat(PopulationSeat):
    name = "pop_observed_switch"

    def _observe(self, raw):
        _, _, dead, history = _table(raw)
        if any(dead.get(eliminated) in ("villager", "seer")
               and sum(t == eliminated for t in ballots.values()) >= 3 for ballots, eliminated in history):
            self._adapted = True


class SurvivorKillSeat(PopulationSeat):
    name = "pop_survivor_kill"

    def _night_target(self, raw, targets):
        _, _, dead, history = _table(raw)
        claims, _ = self._claims(raw)
        correct = Counter(voter for ballots, _ in history for voter, target in ballots.items()
                          if dead.get(target) == "wolf")
        survived = Counter(target for ballots, eliminated in history for target in ballots.values()
                           if target is not None and target != eliminated)
        return max(targets, key=lambda s: (s in claims, correct[s], survived[s], -s)) if targets else None


class AntiMajoritySeat(PopulationSeat):
    name = "pop_anti_majority"

    def _vote_target(self, raw, targets):
        pool = self._vote_pool(raw, targets)
        role = _own(raw)[0]
        known = self._known(raw) if role != "wolf" else {}
        if any(known.get(s) == "wolf" for s in pool):
            return super()._vote_target(raw, pool)
        intents = self._intents(raw)
        pressure = Counter(intents.values())
        if pressure:
            leader = max(pressure, key=lambda s: (pressure[s], -s))
            advocates = [s for s, target in intents.items() if target == leader and s in pool]
            if pressure[leader] >= 3 and advocates:
                scores = self._history_scores(raw)
                return max(advocates, key=lambda s: (scores.get(s, 0.0), -s))
        return super()._vote_target(raw, targets)


POPULATION_BACKGROUNDS = {cls.name: cls for cls in (
    CoordinatedSeat, PromptSeerSeat, LateClaimSeat, PressureBusSeat,
    DistanceSeat, ObservedSwitchSeat, SurvivorKillSeat, AntiMajoritySeat,
)}
