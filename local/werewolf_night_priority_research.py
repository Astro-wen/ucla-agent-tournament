from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, _CLAIMED_SEER


MODES = ('baseline', 'high', 'tie_high', 'keep_vote')


class NightPriorityPolicy(WerewolfPolicy):
    def __init__(self, mode='baseline'):
        if mode not in MODES:
            raise ValueError('unknown night-priority rule')
        super().__init__()
        self.mode = mode

    def _kill_plan(self, v, targets):
        original = super()._kill_plan(v, targets)
        if self.mode == 'baseline' or v.role != 'wolf' or original[1] == _CLAIMED_SEER:
            return original
        pool = [s for s in targets if s in v.others_alive and s not in v.allies]
        if not pool:
            return original
        if self.mode == 'high':
            return max(pool), 'public seer first; otherwise highest living legal target'
        scores = self.suspicion(v, exclude=set(v.allies))
        pool = [s for s in pool if s in scores]
        if not pool:
            return original
        if self.mode == 'tie_high':
            return min(pool, key=lambda s: (scores[s], -s)), 'same trust priority; high-seat tie break'
        daytime, _ = self._wolf_vote(v, list(pool))
        alternatives = [s for s in pool if s != daytime]
        return min(alternatives or pool, key=lambda s: (scores[s], s)), 'preserve the current soft daytime target'


class NightPriorityContestant(Contestant):
    def __init__(self, mode='baseline'):
        self.night_priority_mode = mode
        super().__init__()
        self.ww = NightPriorityPolicy(mode)

    def _fresh(self):
        super()._fresh()
        self.ww = NightPriorityPolicy(self.night_priority_mode)


def baseline():
    return NightPriorityContestant()


def high():
    return NightPriorityContestant('high')


def tie_high():
    return NightPriorityContestant('tie_high')


def keep_vote():
    return NightPriorityContestant('keep_vote')
