from __future__ import annotations

import itertools
import math
import re
from collections import Counter
from dataclasses import dataclass

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy


ABSTAIN = 7
_COMMIT = re.compile(r"(?:I (?:will |shall )?vote(?: for)?|I'll vote(?: for)?|My vote is) Player([0-6])", re.I)
_ABSTAIN = re.compile(r"I (?:will )?abstain", re.I)
_BALLOT_WORD = re.compile(r"\b(?:vote|voting|abstain|withdraw|retract|cancel|change)\b", re.I)
_UNCERTAIN = re.compile(r"\b(?:not|never|unless|if|maybe|might|would|could|withdraw|retract|cancel|example|quote)\b|n't", re.I)
_RESULT = re.compile(r"(?:I (?:checked|investigated) Player([0-6]):|Player([0-6]) is(?: a)?) (wolf|villager|clear)", re.I)


@dataclass(frozen=True)
class Recommendation:
    action: int
    lower_advantage: float = 0.0
    profiles: int = 0
    reason: str = "reference retained"


def plurality(ballots):
    tally = Counter(target for target in ballots if target != ABSTAIN and target is not None)
    leaders = [s for s, count in tally.items() if count == max(tally.values())] if tally else []
    return leaders[0] if len(leaders) == 1 else None


def dawn_features(alive, wolves, eliminated, stall=0):
    remaining = set(alive) - ({eliminated} if eliminated is not None else set())
    nw = len(set(wolves) & remaining)
    if nw == 0:
        return 0.0, 0.0
    if 2 * nw >= len(remaining) or (eliminated is None and stall >= 2):
        return 1.0, 0.0
    if 2 * nw >= len(remaining) - 1:
        return 1.0, 0.0
    return 0.0, 1.0


def _assertive(text):
    normalized = re.sub(r"\bI'(?:ll|m)\b", "I", text, flags=re.I)
    return not (_UNCERTAIN.search(text) or any(mark in normalized for mark in ('"', "'", "“", "”", "‘", "’", "?")))


def _envelopes(view):
    accepted, conflicting = {}, set()
    for item in view.messages:
        if not isinstance(item, dict):
            continue
        seq, sender = item.get("seq"), item.get("sender")
        day = item.get("day", item.get("_day", view.day))
        text = item.get("content")
        if (type(seq) is not int or seq < 0 or type(sender) is not int or not 0 <= sender < 7
                or type(day) is not int or view.day is None or not 1 <= day <= view.day
                or item.get("type", "chat") != "chat" or item.get("recipients", []) != []
                or item.get("visibility", "broadcast") != "broadcast" or not isinstance(text, str)
                or not 0 < len(text.split()) <= 50 or len(text) > 1024):
            continue
        value = day, sender, text
        if seq in accepted and accepted[seq] != value:
            conflicting.add(seq)
        accepted[seq] = value
    return [accepted[seq] for seq in sorted(accepted) if seq not in conflicting]


def commitments(view, day=None):
    day = view.day if day is None else day
    result = {}
    for when, sender, text in _envelopes(view):
        if when != day or sender == view.me or sender not in view.alive:
            continue
        if not _BALLOT_WORD.search(text):
            continue
        result.pop(sender, None)
        if not _assertive(text):
            continue
        targets = set()
        for sentence in re.split(r"[.!]", text):
            hit = _COMMIT.fullmatch(sentence.strip())
            if hit:
                targets.add(int(hit.group(1)))
            elif _ABSTAIN.fullmatch(sentence.strip()):
                targets.add(ABSTAIN)
        if len(targets) == 1:
            target = targets.pop()
            if target != sender and (target == ABSTAIN or target in view.alive or day != view.day):
                result[sender] = target
    return result


def hard_facts(view):
    known = dict(view.investigations) if view.role == "seer" else {}
    for _, sender, text in _envelopes(view):
        if view.dead.get(sender) != "seer" or not _assertive(text):
            continue
        for sentence in re.split(r"[.!]", text):
            hit = _RESULT.fullmatch(sentence.strip())
            if hit:
                target = int(hit.group(1) or hit.group(2))
                verdict = "wolf" if hit.group(3).lower() == "wolf" else "villager"
                if target in known and known[target] != verdict:
                    return None
                known[target] = verdict
    return known


def role_worlds(view):
    alive = tuple(sorted(view.alive))
    if (len(alive) != len(set(alive)) or not 3 <= len(alive) <= 5 or view.me not in alive
            or any(type(s) is not int or not 0 <= s < 7 for s in alive)
            or set(alive) & set(view.dead) or any(r not in ("wolf", "seer", "villager") for r in view.dead.values())):
        return ()
    remaining = 2 - sum(r == "wolf" for r in view.dead.values())
    if remaining <= 0 or remaining * 2 >= len(alive):
        return ()
    if view.role == "wolf":
        wolves = tuple(s for s in alive if s == view.me or s in view.allies)
        return (wolves,) if len(wolves) == remaining else ()
    if view.role not in ("villager", "seer") or len(alive) > 4:
        return ()
    facts = hard_facts(view)
    if facts is None or facts.get(view.me) == "wolf":
        return ()
    eligible = [s for s in alive if s != view.me and facts.get(s) != "villager"]
    required = {s for s in alive if facts.get(s) == "wolf"}
    return tuple(world for world in itertools.combinations(eligible, remaining) if required <= set(world))


def _reliability(view, voter, level):
    kept = broken = 0
    for day, ballots, _ in view.votes:
        if type(day) is not int or not 1 <= day < (view.day or 1):
            continue
        intent = commitments(view, day).get(voter)
        if intent is not None and voter in ballots:
            target = ABSTAIN if ballots[voter] is None else ballots[voter]
            kept += target == intent
            broken += target != intent
    adjustment = 0.1 * (kept - broken) / (kept + broken + 2)
    return min(0.85, max(0.30, level + adjustment))


def recommend(view, legal, reference, *, side="both", min_advantage=0.04, max_profiles=8000):
    keep = Recommendation(reference)
    if (view.sub_phase != "day_vote" or side not in ("wolf", "good", "both")
            or (side == "wolf" and view.role != "wolf") or (side == "good" and view.role == "wolf")
            or len(view.messages) > 256 or min_advantage <= 0 or not math.isfinite(min_advantage)):
        return keep
    worlds = role_worlds(view)
    legal = tuple(sorted({s for s in legal if type(s) is int and (s == ABSTAIN or s in view.alive and s != view.me)}))
    if not worlds or reference not in legal:
        return keep
    intents = commitments(view)
    if len(intents) < 2:
        return keep
    if view.role == "wolf":
        if sum(s not in worlds[0] for s in intents) < 2:
            return keep
        candidates = [s for s in legal if s == ABSTAIN or s not in worlds[0]]
    else:
        sure_wolves = set.intersection(*(set(world) for world in worlds))
        possible_wolves = set.union(*(set(world) for world in worlds))
        candidates = [s for s in legal if s in sure_wolves] if sure_wolves else [
            s for s in legal if s == ABSTAIN or s in possible_wolves]
    candidates = [s for s in candidates if s != reference]
    if not candidates:
        return keep
    voters = tuple(s for s in sorted(view.alive) if s != view.me)
    options = tuple(tuple(s for s in sorted(view.alive) if s != voter) + (ABSTAIN,) for voter in voters)
    profiles = math.prod(map(len, options))
    work = sum(profiles * 2 * (1 + sum(s in intents and s not in world for s in voters)) for world in worlds)
    if work > max_profiles:
        return Recommendation(reference, profiles=0, reason="enumeration budget exceeded")
    stall = 0
    for day, _, eliminated in reversed(view.votes):
        if type(day) is not int or not 1 <= day < (view.day or 1):
            continue
        if eliminated is not None:
            break
        stall += 1
    if any(cause == "resigned" for cause in view.dead_cause.values()):
        return keep
    lower = {action: float("inf") for action in candidates}
    evaluations = 0
    for world in worlds:
        wolf_indices = [i for i, s in enumerate(voters) if s in world]
        good_indices = [i for i, s in enumerate(voters) if s not in world]
        dropouts = [None] + [s for s in voters if s in intents and s not in world]
        tables = []
        for ballots in itertools.product(*options):
            baseline = dawn_features(view.alive, world, plurality((*ballots, reference)), stall)
            deltas = []
            sign = 1 if view.role == "wolf" else -1
            for action in candidates:
                outcome = dawn_features(view.alive, world, plurality((*ballots, action)), stall)
                deltas.append(tuple(sign * ((outcome[0] - baseline[0]) + continuation * (outcome[1] - baseline[1]))
                                    for continuation in (0.25, 0.75)))
            tables.append((ballots, tuple(ballots[i] for i in wolf_indices), deltas))
        for level in (0.45, 0.80):
            reliability = {s: _reliability(view, s, level) for s in voters}
            for dropout in dropouts:
                groups = {}
                for ballots, wolf_ballots, deltas in tables:
                    probability = 1.0
                    for i in good_indices:
                        voter = voters[i]
                        confidence = reliability[voter] if voter in intents and voter != dropout else 0.0
                        probability *= (1 - confidence) / len(options[i]) + confidence * (ballots[i] == intents.get(voter))
                    values = groups.setdefault(wolf_ballots, [[0.0, 0.0] for _ in candidates])
                    for index, delta in enumerate(deltas):
                        values[index][0] += probability * delta[0]
                        values[index][1] += probability * delta[1]
                    evaluations += 1
                for values in groups.values():
                    for action, value in zip(candidates, values):
                        lower[action] = min(lower[action], *value)
    best = max(candidates, key=lambda action: (lower[action], action != ABSTAIN, -action))
    if lower[best] + 1e-12 < min_advantage:
        return Recommendation(reference, profiles=evaluations, reason="insufficient conservative advantage")
    return Recommendation(best, lower[best], evaluations, "conditional noisy-ballot dawn utility; not a win guarantee")


class EndgamePolicy(WerewolfPolicy):
    side = "both"

    def act(self, raw):
        original = super().act(raw)
        if (raw.get("phase") != "moving" or raw.get("eliminated") or raw.get("is_terminal")
                or raw.get("is_current_actor") is False):
            return original
        legal = tuple(int(a) for a in self._legal(raw) if str(a).isdigit())
        reference = original.get("action_id")
        if not isinstance(reference, str) or not reference.isdigit():
            return original
        result = recommend(self._view(raw), legal, int(reference), side=self.side)
        if result.action == int(reference) or result.action not in legal:
            return original
        return {"action_id": str(result.action), "reasoning_summary": result.reason}


class EndgameContestant(Contestant):
    side = "both"

    def __init__(self):
        super().__init__()
        self._install()

    def _install(self):
        self.ww = EndgamePolicy()
        self.ww.side = self.side

    def _fresh(self):
        super()._fresh()
        self._install()


class WolfContestant(EndgameContestant):
    side = "wolf"


class GoodContestant(EndgameContestant):
    side = "good"


def wolf():
    return WolfContestant()


def good():
    return GoodContestant()


def combined():
    return EndgameContestant()
