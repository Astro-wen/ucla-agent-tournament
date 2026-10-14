from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, _CLAIMED_SEER


class BalancedNightPolicy(WerewolfPolicy):
    def __init__(self, general=False):
        super().__init__()
        self.general = general

    def _kill_plan(self, v, targets):
        original = super()._kill_plan(v, targets)
        if v.role != 'wolf' or original[1] == _CLAIMED_SEER or len(v.alive) < 4:
            return original
        if not self.general and (v.wolves_left != 1 or len(v.alive) != 4):
            return original
        wolves = {v.me, *v.allies} & set(v.alive)
        if len(wolves) != v.wolves_left:
            return original
        target = original[0]
        scores = self.suspicion(v, exclude=set(v.allies))
        if target not in scores:
            return original
        def exposed(kill):
            remaining = [s for s in v.alive if s != kill]
            return len({min(remaining), max(remaining)} & wolves)
        before = exposed(target)
        pool = [s for s in targets if s in v.others_alive and s not in v.allies
                and scores.get(s) == scores[target] and exposed(s) < before]
        if not pool:
            return original
        return min(pool, key=lambda s: (exposed(s), -s)), 'equal-trust target reduces wolves at the public seat extremes'


class BalancedNightContestant(Contestant):
    def __init__(self, general=False):
        self.general_night = general
        super().__init__()
        self.ww = BalancedNightPolicy(general)

    def _fresh(self):
        super()._fresh()
        self.ww = BalancedNightPolicy(self.general_night)


def balanced():
    return BalancedNightContestant()


def extremes():
    return BalancedNightContestant(True)
