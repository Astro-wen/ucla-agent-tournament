from dataclasses import dataclass
from itertools import combinations
import math

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy


@dataclass(frozen=True)
class Deal:
    wolves: tuple[int, int]
    seer: int


DEALS = tuple(Deal(wolves, seer) for wolves in combinations(range(7), 2)
              for seer in range(7) if seer not in wolves)


@dataclass(frozen=True)
class Beliefs:
    deals: tuple[Deal, ...]
    probabilities: tuple[float, ...]
    wolf: tuple[float, ...]
    seer: tuple[float, ...]
    hard_wolves: frozenset[int]
    hard_clears: frozenset[int]

    @property
    def consistent(self):
        return bool(self.deals)


def role_of(deal, seat):
    return "wolf" if seat in deal.wolves else "seer" if seat == deal.seer else "villager"


def feasible(deal, v):
    if v.me in range(7) and v.role in ("wolf", "seer", "villager") and role_of(deal, v.me) != v.role:
        return False
    if any(role in ("wolf", "seer", "villager") and role_of(deal, s) != role for s, role in v.dead.items()
           if s in range(7)):
        return False
    if v.role == "wolf" and any(s not in deal.wolves for s in v.allies if s in range(7)):
        return False
    if v.role == "seer":
        for s, result in v.investigations.items():
            if s in range(7) and result in ("wolf", "villager") and (s in deal.wolves) != (result == "wolf"):
                return False
    return True


class BeliefPolicy(WerewolfPolicy):
    profile = "tempered"

    def _soft_log_weight(self, v, deal, claims, ballots):
        claim_score = 0.0
        for sender in sorted(set(claims.order) - {v.me}):
            correct = sender not in claims.liars and all(t in deal.wolves for t in claims.wolves[sender]) \
                and all(t not in deal.wolves for t in claims.clears[sender])
            role = role_of(deal, sender)
            likelihood = (0.95 if correct else 0.04) if role == "seer" else 0.25 if role == "wolf" else 0.12
            claim_score += math.log((0.25 * 0.5 + 0.75 * likelihood) / 0.3)
        vote_score = 0.0
        for voter, targets in ballots.items():
            score = 0.0
            for target in targets:
                if v.dead.get(target) == "wolf":
                    factor = 0.85 if voter in deal.wolves else 1.1
                elif v.dead.get(target) in ("villager", "seer"):
                    factor = 1.1 if voter in deal.wolves else 0.95
                else:
                    factor = 0.85 if voter in deal.wolves and target in deal.wolves else 1.0
                score += math.log(factor)
            vote_score += max(-0.5, min(0.5, score))
        return max(-3.0, min(3.0, claim_score + vote_score))

    def beliefs(self, v):
        deals = tuple(deal for deal in DEALS if feasible(deal, v))
        if not deals:
            return Beliefs((), (), (0.0,) * 7, (0.0,) * 7, frozenset(), frozenset())
        hard_wolves = frozenset(s for s in range(7) if all(s in deal.wolves for deal in deals))
        hard_clears = frozenset(s for s in range(7) if all(s not in deal.wolves for deal in deals))
        scores = [0.0] * len(deals)
        if self.profile != "constraints":
            claims = self.claims(v, private=True)
            named = set().union(*claims.wolves.values()) if claims.wolves else set()
            ballots = {}
            for _, votes, _ in v.votes:
                for voter, target in votes.items():
                    if voter != v.me and voter in range(7) and target in range(7) and voter != target and target not in named:
                        ballots.setdefault(voter, set()).add(target)
            scores = [self._soft_log_weight(v, deal, claims, ballots) for deal in deals]
        maximum = max(scores)
        weights = [math.exp(score - maximum) for score in scores]
        total = math.fsum(weights)
        probabilities = tuple(0.8 * w / total + 0.2 / len(deals) for w in weights)
        wolf = tuple(1.0 if s in hard_wolves else 0.0 if s in hard_clears else
                     math.fsum(p for deal, p in zip(deals, probabilities) if s in deal.wolves) for s in range(7))
        seer = tuple(min(1.0, math.fsum(p for deal, p in zip(deals, probabilities) if deal.seer == s))
                     for s in range(7))
        return Beliefs(deals, probabilities, wolf, seer, hard_wolves, hard_clears)

    def _day_vote(self, v, targets):
        if v.role == "wolf":
            return super()._day_vote(v, targets)
        b = self.beliefs(v)
        private_wolves = {s for s, r in v.investigations.items() if r == "wolf"} if v.role == "seer" else set()
        private_clears = {s for s, r in v.investigations.items() if r == "villager"} if v.role == "seer" else set()
        known = sorted(set(targets) & (b.hard_wolves | private_wolves))
        if known:
            called = self._called(v) if v.role == "seer" else None
            if called in known:
                known.remove(called)
                known.insert(0, called)
            return self._converge(v, known, 2 if v.role == "seer" else 1), "hard role constraints"
        pool = [s for s in targets if s not in b.hard_clears and s not in private_clears] or list(targets)
        if not pool:
            return None, "no legal target"
        if not b.consistent:
            return min(pool), "inconsistent role record; no probabilistic inference"
        return max(pool, key=lambda s: (round(b.wolf[s], 12), -s)), "bounded role hypothesis, not a verified result"

    def _wolf_vote(self, v, targets):
        return WerewolfPolicy()._wolf_vote(v, targets)

    def _opening(self, v):
        if v.role == "wolf":
            return WerewolfPolicy()._opening(v)
        return super()._opening(v)

    def _investigate(self, v, targets):
        unread = [t for t in targets if t not in v.investigations]
        if not unread:
            return (min(targets) if targets else None), "nothing left to read"
        b = self.beliefs(v)
        if not b.consistent:
            return max(unread), "investigate an unread seat"
        if self.profile == "inquisitive":
            def value(s):
                p = b.wolf[s]
                return -sum(x * math.log(x) for x in (p, 1 - p) if 0 < x < 1)
        else:
            def value(s):
                return b.wolf[s]
        pick = max(unread, key=lambda s: (round(value(s), 12), s))
        return pick, "check uncertainty" if self.profile == "inquisitive" else "check a likely wolf, including unverified claimants"

    def _seer_chat(self, v, c):
        wolves, clears = self.seer_knowledge(v)
        fakes = [s for s in c.order if s in v.alive and s != v.me]
        if fakes and not wolves:
            topic = "claim-rebuttal"
            text = f"I am the Seer. Player{fakes[0]}'s claim is false; that alone does not prove a wolf."
            text += "".join(f" Player{s} is clear." for s in clears[:3])
            pick, _ = self._day_vote(v, v.others_alive)
            if pick is not None:
                text += f" I vote Player{pick}."
            return topic, {"content": text, "recipients": []}
        return super()._seer_chat(v, c)

    def _villager_chat(self, v, c):
        if v.role == "wolf":
            return super()._villager_chat(v, c)
        pick, _ = self._day_vote(v, v.others_alive)
        if pick is None:
            return None
        framers = [s for s in c.order if s in v.alive and s != v.me and v.me in c.wolves[s]]
        if v.role == "villager" and framers:
            return "deny", {"content": f"Player{framers[0]}'s claim about me is false. I am not a wolf. "
                                        f"I vote Player{pick}; claims are not verified roles.", "recipients": []}
        if c.order and self.profile != "constraints":
            return "belief", {"content": f"Claims can be false, including village bluffs. My current vote is Player{pick}. "
                                          "Please keep votes together; this is a read, not a verified result.", "recipients": []}
        return None


class SeerAuditPolicy(BeliefPolicy):
    def _day_vote(self, v, targets):
        if v.role == "villager":
            return WerewolfPolicy._day_vote(self, v, targets)
        return super()._day_vote(v, targets)

    def _villager_chat(self, v, c):
        if v.role == "villager":
            return WerewolfPolicy._villager_chat(self, v, c)
        return super()._villager_chat(v, c)


class BeliefContestant(Contestant):
    profile = "tempered"
    policy_class = BeliefPolicy

    def __init__(self):
        super().__init__()
        self._install()

    def _install(self):
        self.ww = self.policy_class()
        self.ww.profile = self.profile

    def _fresh(self):
        super()._fresh()
        self._install()


class ConstraintsContestant(BeliefContestant):
    profile = "constraints"


class InquisitiveContestant(BeliefContestant):
    profile = "inquisitive"


def constraints():
    return ConstraintsContestant()


def tempered():
    return BeliefContestant()


def inquisitive():
    return InquisitiveContestant()


class SeerAuditContestant(BeliefContestant):
    profile = "seer_audit"
    policy_class = SeerAuditPolicy


def seer_audit():
    return SeerAuditContestant()
