from collections import Counter
from functools import lru_cache

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, _chats


def fixed_actions(alive, wolves, me, night, targets, stall=0):
    values = {}
    wolfset = set(wolves)
    for action in targets:
        if night:
            kills = [action if s == me else min(t for t in alive if t not in wolfset)
                     for s in alive if s in wolfset]
            victim = min(kills)
            values[action] = fixed_value(tuple(s for s in alive if s != victim), wolves, me, False, stall)
        else:
            votes = Counter(action if s == me else min(t for t in alive if t != s) for s in alive)
            votes.pop(7, None)
            leaders = [s for s, count in votes.items() if count == max(votes.values())] if votes else []
            victim = leaders[0] if len(leaders) == 1 else None
            values[action] = fixed_value(tuple(s for s in alive if s != victim), wolves, me, True,
                                         0 if victim is not None else stall + 1)
    return values


@lru_cache(maxsize=32768)
def fixed_value(alive, wolves, me, night, stall=0):
    nw = sum(s in wolves for s in alive)
    if nw == 0:
        return int(me not in wolves)
    if nw * 2 >= len(alive) or stall >= 3:
        return int(me in wolves)
    if night:
        targets = tuple(s for s in alive if s not in wolves) if me in alive and me in wolves else (7,)
    else:
        targets = tuple(s for s in alive if s != me) + (7,) if me in alive else (7,)
    return max(fixed_actions(alive, wolves, me, night, targets, stall).values())


class SimplePolicy(WerewolfPolicy):
    mode = "combined"

    def _simple_table(self, v):
        if not v.votes or self.claims(v, private=False).order:
            return None
        if any(sender != v.me and (f.claim or f.wolves or f.clears or f.injection)
               for _, sender, f, _ in _chats(v)):
            return None
        observed = []
        for _, votes, _ in v.votes:
            voters = set(votes)
            for voter, target in votes.items():
                if voter == v.me or (v.role == "wolf" and voter in v.allies):
                    continue
                others = voters - {voter}
                if not others:
                    return None
                observed.append((target, min(others)))
        if len(observed) < 4:
            return None
        if all(target is None for target, _ in observed):
            return "abstain"
        if all(target == first for target, first in observed):
            return "first"
        return None

    def _kill_plan(self, v, targets):
        pick, why = super()._kill_plan(v, targets)
        if self.mode in ("night", "combined") and targets and self._simple_table(v) == "first":
            return max(targets), "preserve likely daytime vote targets"
        return pick, why

    def _day_vote(self, v, targets):
        pick, why = super()._day_vote(v, targets)
        if self.mode not in ("inference", "combined") or v.role == "wolf" or not self._simple_table(v):
            return pick, why
        wolves, clears = self.seer_knowledge(v) if v.role == "seer" else ([], [])
        if wolves:
            return pick, why
        night_deaths = [s for s in v.dead if v.dead_cause.get(s) == "night_kill"]
        if not night_deaths:
            return pick, why
        victim = night_deaths[-1]
        skipped = [s for s in targets if s < victim and s not in clears]
        if skipped:
            return min(skipped), "possible low-target night pattern; not a verified role"
        return pick, why


class ExactPolicy(SimplePolicy):
    def _select(self, v, targets, original, night):
        pick, why = original
        if v.role != "wolf" or not targets or self._simple_table(v) != "first":
            return original
        if self.mode == "exact_night" and not night or self.mode == "exact_vote" and night:
            return original
        stall = 0
        for _, _, eliminated in reversed(v.votes):
            if eliminated is not None:
                break
            stall += 1
        values = fixed_actions(tuple(sorted(v.alive)), tuple(sorted({v.me, *v.allies})),
                               v.me, night, tuple(targets), stall)
        best = max(values, key=lambda s: (values[s], s == pick, -s))
        return (best, "conditional fixed-vote continuation") if values[best] > values.get(pick, -1) else original

    def _kill_plan(self, v, targets):
        return self._select(v, targets, WerewolfPolicy._kill_plan(self, v, targets), True)

    def _wolf_vote(self, v, targets):
        return self._select(v, targets, super()._wolf_vote(v, targets), False)

    def _day_vote(self, v, targets):
        return WerewolfPolicy._day_vote(self, v, targets)


class SimpleContestant(Contestant):
    mode = "combined"
    policy_class = SimplePolicy

    def __init__(self):
        super().__init__()
        self._install()

    def _install(self):
        self.ww = self.policy_class()
        self.ww.mode = self.mode

    def _fresh(self):
        super()._fresh()
        self._install()


class NightContestant(SimpleContestant):
    mode = "night"


class InferenceContestant(SimpleContestant):
    mode = "inference"


def night():
    return NightContestant()


def inference():
    return InferenceContestant()


def combined():
    return SimpleContestant()


class ExactContestant(SimpleContestant):
    policy_class = ExactPolicy


def exact():
    return ExactContestant()


class ExactNightContestant(ExactContestant):
    mode = "exact_night"


class ExactVoteContestant(ExactContestant):
    mode = "exact_vote"


def exact_night():
    return ExactNightContestant()


def exact_vote():
    return ExactVoteContestant()
