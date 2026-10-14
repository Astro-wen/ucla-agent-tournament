from collections import defaultdict
import re

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, _chats
from agent.werewolf.state import normalize, read_chat


MODES = ("transmit", "coalition", "information", "combined")
_VOTE = re.compile(r"\b(?:i\s+(?:(?:will|'ll)\s+)?vote|i'm\s+voting|i\s+am\s+voting|"
                   r"(?:everyone\s+)?vote)\s+(?:for\s+)?player([0-6])\b")
_WITHDRAW = re.compile(r"\s*i\s+(?:will not|won't|do not|don't|no longer)\s+vote\s+(?:for\s+)?"
                       r"player([0-6])\s*[.!;]?\s*")


class CoordinationPolicy(WerewolfPolicy):
    def __init__(self, mode="combined"):
        if mode not in MODES:
            raise ValueError(f"unknown coordination mode: {mode}")
        super().__init__()
        self.mode = mode

    def enabled(self, component):
        return self.mode in (component, "combined")

    @staticmethod
    def commitments(v, day=None):
        day = v.day if day is None else day
        latest = {}
        for message in v.messages:
            if not isinstance(message, dict) or message.get("type", "chat") != "chat":
                continue
            sender = message.get("sender")
            if (type(sender) is not int or sender not in v.alive or sender == v.me
                    or message.get("visibility") == "direct" or message.get("recipients")
                    or message.get("_day", message.get("day")) != day):
                continue
            text = message.get("content")
            facts = read_chat(text, sender, v.names)
            if facts.injection:
                continue
            choices, withdrawn = set(), set()
            clean = normalize(text, v.names)
            for clause in re.findall(r"[^.!?;\n]+[.!?;]?", clean):
                withdrawal = _WITHDRAW.fullmatch(clause)
                if withdrawal:
                    withdrawn.add(int(withdrawal.group(1)))
                local = read_chat(clause, sender)
                for hit in _VOTE.finditer(clause):
                    target = int(hit.group(1))
                    if target in local.sus and target in facts.sus and (target in v.alive or day != v.day):
                        choices.add(target)
            choices -= withdrawn
            if latest.get(sender) in withdrawn:
                latest.pop(sender)
            if choices:
                latest[sender] = next(iter(choices)) if len(choices) == 1 else None
        return {s: t for s, t in latest.items() if t is not None}

    def supporters(self, v, day=None):
        c = self.claims(v, private=True)
        excluded = set(c.liars)
        if v.role == "seer":
            excluded.update(self.seer_knowledge(v)[0])
        elif v.role == "villager":
            excluded.update(c.trusted_wolves(v.alive))
        out = defaultdict(set)
        for sender, target in self.commitments(v, day).items():
            if sender not in excluded:
                out[target].add(sender)
        return out

    def credible(self, v):
        c = self.claims(v, private=True)
        clear = set(c.trusted_clears())
        if v.role == "seer":
            clear.update(self.seer_knowledge(v)[1])
        kept = set()
        for day, votes, _ in v.votes[-2:]:
            for sender, target in self.commitments(v, day).items():
                if votes.get(sender) == target:
                    kept.add(sender)
        return (clear | kept) & set(v.alive) - c.liars - {v.me}

    @staticmethod
    def emergency(v):
        stalled = bool(v.votes and v.votes[-1][2] is None)
        return stalled or len(v.alive) <= 2 * v.wolves_left + 2

    def _coalition_vote(self, v, targets, original):
        pick, _ = original
        if not self.enabled("coalition") or v.role == "wolf" or pick is None:
            return original
        c = self.claims(v, private=True)
        known, clears = self.seer_knowledge(v) if v.role == "seer" else (c.trusted_wolves(v.alive), [])
        if known or any(s in targets and s not in clears for s in c.liars) or len(c.live(v)) >= 2:
            return original
        safe = set(clears) | c.trusted_clears()
        support = self.supporters(v)
        credible = self.credible(v)
        score = self.suspicion(v)
        viable = []
        for target in targets:
            voters = support[target]
            if target == pick or target in safe or score.get(target, 0) < score.get(pick, 0) - 0.35:
                continue
            enough = len(voters) >= 2 and len(voters) >= len(support[pick]) + 2
            final_clear = len(v.alive) == 3 and v.wolves_left == 1 and bool(voters & safe)
            reliable = len(voters) >= 3 or bool(voters & credible)
            if (enough and reliable and (self.emergency(v) or len(voters) >= 3)) or final_clear:
                viable.append(target)
        if not viable:
            return original
        chosen = max(viable, key=lambda t: (len(support[t]), score.get(t, 0), -t))
        return chosen, "conditional living vote coalition; not a verified role"

    def _day_vote(self, v, targets):
        return self._coalition_vote(v, targets, super()._day_vote(v, targets))

    def _plan(self, v):
        out = super()._plan(v)
        if self.enabled("coalition") and v.role in ("villager", "seer") and "open" in self._topics:
            original = super()._day_vote(v, v.others_alive)
            pick, _ = self._coalition_vote(v, v.others_alive, original)
            if pick != original[0] and not out:
                out.append(("coordinate", {"content": f"I vote Player{pick}. Several living players have called "
                            "this vote; staying together avoids a tie. This is a vote plan, not a checked result.",
                            "recipients": []}))
        return out

    def _seer_chat(self, v, c):
        if not self.enabled("transmit"):
            return super()._seer_chat(v, c)
        wolves, clears = self.seer_knowledge(v)
        fakes = [s for s in c.order if s in c.liars and s in v.alive and s != v.me and s not in clears]
        picked, _ = self._day_vote(v, v.others_alive)
        support = self.supporters(v)
        threatened_clear = any(len(support[t]) >= 3 or
                               (len(support[t]) >= 2 and bool(support[t] & self.credible(v))) for t in clears)
        disclosed = v.me in c.order
        routine = bool(clears) and (disclosed or self.pressure_on(v, v.me) >= 2
                                   or self.predicted_lynch(v) == v.me
                                   or (v.day is not None and v.day >= 2 and
                                       (self.predicted_lynch(v) in clears or len(v.alive) <= 5)))
        if not (wolves or fakes or routine or threatened_clear):
            return None
        topic = "claim"
        if topic in self._topics:
            new_rival = bool(fakes) and not c.wolves.get(v.me)
            switched_wolf = picked in wolves and self._called(v) not in (None, picked)
            if "claim-rebuttal" in self._topics or not (new_rival or switched_wolf):
                return None
            topic = "claim-rebuttal"
        parts = ["I am the Seer."]
        if picked in wolves:
            ordered = [picked] + [s for s in wolves if s != picked]
            for target in ordered:
                if v.investigations.get(target) == "wolf":
                    parts.append(f"I checked Player{target}: wolf.")
                else:
                    parts.append(f"The remaining roles imply Player{target} is a WOLF.")
            self._call = [v.day, picked]
        elif fakes:
            rival = picked if picked in fakes else fakes[0]
            parts.append(f"Player{rival} is not the Seer; their claim is false.")
        for target in clears:
            if len(" ".join(parts + [f"I checked Player{target}: villager."]).split()) <= 42:
                parts.append(f"I checked Player{target}: villager.")
        if picked is not None and picked not in clears:
            parts.append(f"Everyone vote Player{picked} today.")
        return topic, {"content": " ".join(parts), "recipients": []}

    def _investigate(self, v, targets):
        if not self.enabled("information"):
            return super()._investigate(v, targets)
        unread = [t for t in targets if t not in v.investigations and t != v.me]
        if not unread:
            return super()._investigate(v, targets)
        c = self.claims(v, private=True)
        pool = [t for t in unread if t not in c.liars] or unread
        lynch = self.predicted_lynch(v)
        pool = [t for t in pool if t != lynch] or pool
        followers = defaultdict(set)
        for day, votes, _ in v.votes[-2:]:
            for sender, target in self.commitments(v, day).items():
                if votes.get(sender) == target:
                    followers[sender].update(s for s, vote in votes.items()
                                             if s in v.alive and s not in (sender, v.me) and vote == target)
        active = {s for _, s, f, _ in _chats(v) if s in v.alive and not f.injection}
        scores = self._scores(v, private=True)
        pick = max(pool, key=lambda t: (len(followers[t]) >= 2, min(len(followers[t]), 3),
                                       round(scores.get(t, 0), 3), t in active, t))
        return pick, "investigate an unresolved voting pivot; repeated speech adds no influence"


class CoordinationContestant(Contestant):
    def __init__(self, mode="combined"):
        self.coordination_mode = mode
        super().__init__()
        self.ww = CoordinationPolicy(mode)

    def _fresh(self):
        super()._fresh()
        self.ww = CoordinationPolicy(self.coordination_mode)


def transmit():
    return CoordinationContestant("transmit")


def coalition():
    return CoordinationContestant("coalition")


def information():
    return CoordinationContestant("information")


def combined():
    return CoordinationContestant("combined")
