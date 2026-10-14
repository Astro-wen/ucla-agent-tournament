from collections import Counter
from dataclasses import dataclass

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, _chats


@dataclass(frozen=True)
class Rules:
    order: str = "low"
    speech: str = "direct"
    claims: str = "hard"
    look: str = "low"


PROFILES = {
    "low_silent": Rules(speech="silent"),
    "low_seer": Rules(speech="seer"),
    "low_direct": Rules(),
    "high_direct": Rules(order="high"),
    "consensus_direct": Rules(order="consensus"),
    "firstclaim_direct": Rules(claims="first"),
    "firstclaim_highlook": Rules(claims="first", look="high"),
}


class MinimalPolicy(WerewolfPolicy):
    def __init__(self, profile="low_direct"):
        if profile not in PROFILES:
            raise ValueError(f"unknown minimal profile: {profile}")
        super().__init__()
        self.profile = profile
        self.rules = PROFILES[profile]

    def hard_knowledge(self, v):
        if v.role == "seer":
            wolves, clears = self.seer_knowledge(v)
            return set(wolves), set(clears)
        if v.role != "villager":
            return set(), set()
        claims = self.claims(v)
        confirmed = claims.trusted
        if v.dead.get(confirmed) != "seer" or confirmed in claims.liars:
            return set(), set()
        wolves = set(claims.trusted_wolves(v.alive))
        clears = claims.trusted_clears() & set(v.others_alive)
        unread = set(v.others_alive) - wolves - clears
        if unread and v.wolves_left - len(wolves) == len(unread):
            wolves |= unread
        return wolves, clears

    @staticmethod
    def support(v):
        latest = {}
        for _seq, sender, facts, day in _chats(v):
            if sender == v.me or sender not in v.alive or day != v.day or facts.injection:
                continue
            targets = (facts.sus | facts.wolves) & set(v.alive) - {sender}
            latest[sender] = next(iter(targets)) if len(targets) == 1 else None
        return dict(Counter(target for target in latest.values() if target is not None))

    def select(self, v, targets):
        if not targets:
            return None
        if self.rules.order == "high":
            return max(targets)
        if self.rules.order == "consensus":
            counts = self.support(v)
            return max(targets, key=lambda seat: (counts.get(seat, 0), -seat))
        return min(targets)

    def _day_vote(self, v, targets):
        pool = sorted(set(targets) & set(v.others_alive))
        if v.role == "wolf":
            pool = [seat for seat in pool if seat not in v.allies]
            return self.select(v, pool), "public vote plan"
        wolves, clears = self.hard_knowledge(v)
        known = [seat for seat in pool if seat in wolves]
        if known:
            return self.select(v, known), "verified result"
        pool = [seat for seat in pool if seat not in clears] or pool
        if self.rules.claims == "first" and v.role == "villager":
            claims = self.claims(v)
            live = claims.live(v)
            if live:
                first = live[0]
                named = [seat for seat in pool if seat in claims.wolves.get(first, ())]
                if named:
                    return self.select(v, named), "provisionally follow first seer claim"
                uncleared = [seat for seat in pool if seat != first and seat not in claims.clears.get(first, ())]
                pool = uncleared or pool
        return self.select(v, pool), "public vote plan"

    def _investigate(self, v, targets):
        pool = [seat for seat in targets if seat in v.others_alive and seat not in v.investigations]
        pool = pool or [seat for seat in targets if seat in v.others_alive]
        select = max if self.rules.look == "high" else min
        return (select(pool) if pool else None), "unread seat first"

    def _wolf_kill(self, v, targets):
        pool = [seat for seat in targets if seat in v.others_alive and seat not in v.allies]
        claims = self.claims(v, private=False)
        claimers = [seat for seat in claims.live(v) if seat in pool]
        return (min(claimers or pool) if pool else None), "public seer claim first"

    def _plan(self, v):
        if self.rules.speech == "silent":
            return []
        if v.role == "seer":
            wolves, clears = self.hard_knowledge(v)
            if wolves or clears:
                pick, _ = self._day_vote(v, v.others_alive)
                parts = ["I am the Seer."]
                parts.extend(f"Player{seat} is a WOLF." for seat in sorted(wolves))
                parts.extend(f"Player{seat} is clear." for seat in sorted(clears))
                if pick is not None:
                    parts.append(f"I vote Player{pick}.")
                return [("claim", {"content": " ".join(parts), "recipients": []})]
        if self.rules.speech != "direct":
            return []
        pick, _ = self._day_vote(v, v.others_alive)
        if pick is None:
            return []
        return [("open", {"content": f"I vote Player{pick}. Please vote together to avoid a tie.", "recipients": []})]


class MinimalContestant(Contestant):
    def __init__(self, profile="low_direct"):
        if profile != "reference" and profile not in PROFILES:
            raise ValueError(f"unknown minimal profile: {profile}")
        self.profile = profile
        super().__init__()
        self._install()

    def _install(self):
        self.ww = WerewolfPolicy() if self.profile == "reference" else MinimalPolicy(self.profile)

    def _fresh(self):
        super()._fresh()
        self._install()


def reference():
    return MinimalContestant("reference")


def low_silent():
    return MinimalContestant("low_silent")


def low_seer():
    return MinimalContestant("low_seer")


def low_direct():
    return MinimalContestant("low_direct")


def high_direct():
    return MinimalContestant("high_direct")


def consensus_direct():
    return MinimalContestant("consensus_direct")


def firstclaim_direct():
    return MinimalContestant("firstclaim_direct")


def firstclaim_highlook():
    return MinimalContestant("firstclaim_highlook")
