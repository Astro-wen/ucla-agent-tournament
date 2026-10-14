from collections import Counter
from dataclasses import replace

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, _chats
from .werewolf_coordination_research import CoordinationPolicy


MODES = ("plain", "neutral", "after_speakers", "after_coalition")
NEUTRAL = ("I want to hear everyone's reasons before choosing a vote. "
           "Please share any Seer results; let's avoid a split vote.")


def vote_message(target):
    return {"content": f"No hard info yet. To avoid a split vote I'll vote Player{target} unless the Seer "
                       "speaks up. Please don't abstain.", "recipients": []}


class AdaptationPolicy(WerewolfPolicy):
    def __init__(self, mode="neutral"):
        if mode not in MODES:
            raise ValueError(f"unknown adaptation mode: {mode}")
        super().__init__()
        self.mode = mode

    def opening_scope(self, v):
        return v.day == 1 and not v.votes and not v.dead and not self.claims(v, private=False).order

    @staticmethod
    def own_messages(v):
        return [m.get("content") for m in v.messages if isinstance(m, dict)
                and m.get("type", "chat") == "chat" and m.get("sender") == v.me
                and m.get("_day", m.get("day")) == v.day and not m.get("recipients")
                and m.get("visibility") != "direct"]

    def announced(self, v):
        messages = self.own_messages(v)
        if NEUTRAL not in messages:
            return None
        mapping = {vote_message(s)["content"]: s for s in v.others_alive}
        return next((mapping[text] for text in reversed(messages) if text in mapping), None)

    @staticmethod
    def heard(v):
        return {sender for _, sender, facts, day in _chats(v)
                if day == v.day and sender in v.alive and sender != v.me and not facts.injection
                and not facts.self_wolf and (facts.sus or facts.wolves or facts.clears or facts.claim)}

    def coalition_target(self, v, targets):
        excluded = {s for _, s, f, _ in _chats(v) if f.self_wolf}
        votes = CoordinationPolicy.commitments(v)
        support = Counter(t for s, t in votes.items() if s not in excluded)
        public = replace(v, role="villager", allies=[], investigations={}, evidence=None,
                         messages=[m for m in v.messages if isinstance(m, dict)
                                   and not m.get("recipients") and m.get("visibility") != "direct"])
        scores = WerewolfPolicy._scores(public, private=False)
        best = max((scores.get(t, 0) for t in targets), default=0)
        viable = [t for t in targets if support[t] >= 3 and scores.get(t, 0) >= best - 0.35]
        return max(viable, key=lambda t: (support[t], scores.get(t, 0), -t)) if viable else None

    def _day_vote(self, v, targets):
        original = super()._day_vote(v, targets)
        if self.mode != "after_coalition" or not self.opening_scope(v):
            return original
        wolves, clears = self.seer_knowledge(v) if v.role == "seer" else ([], [])
        if wolves:
            return original
        pool = [s for s in targets if s not in clears]
        declared = self.announced(v)
        target = declared if declared in pool else self.coalition_target(v, pool)
        return (target, "first-day public voting coalition; not a role result") if target is not None else original

    def _plan(self, v):
        if self.mode == "plain" or not self.opening_scope(v):
            return super()._plan(v)
        if NEUTRAL in self.own_messages(v):
            self._topics.add("open")
        if self.announced(v) is not None:
            self._topics.update(("adaptation-vote", "bus-adaptation-vote"))
        original = super()._plan(v)
        if any(topic not in ("open", "bus", "dm") for topic, _ in original):
            return original
        if "open" not in self._topics:
            return [(topic, message) for topic, message in original if topic == "dm"] + [
                ("open", {"content": NEUTRAL, "recipients": []})]
        if self.mode == "neutral" or self._topics & {"adaptation-vote", "bus-adaptation-vote"}:
            return original
        if self.mode == "after_speakers":
            ready = len(self.heard(v)) >= 3
        else:
            ready = self.coalition_target(v, v.others_alive) is not None
        if not ready:
            return original
        target, _ = self._day_vote(v, v.others_alive)
        if target is None:
            return original
        topic = "bus-adaptation-vote" if v.role == "wolf" and target in v.allies else "adaptation-vote"
        return original + [(topic, vote_message(target))]


class AdaptationContestant(Contestant):
    def __init__(self, mode="neutral"):
        self.adaptation_mode = mode
        super().__init__()
        self.ww = AdaptationPolicy(mode)

    def _fresh(self):
        super()._fresh()
        self.ww = AdaptationPolicy(self.adaptation_mode)


def plain():
    return AdaptationContestant("plain")


def neutral():
    return AdaptationContestant("neutral")


def after_speakers():
    return AdaptationContestant("after_speakers")


def after_coalition():
    return AdaptationContestant("after_coalition")
