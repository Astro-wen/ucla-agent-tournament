from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy
from local.werewolf_night_priority_research import NightPriorityPolicy


class ShieldedNightPolicy(NightPriorityPolicy):
    def __init__(self):
        super().__init__('tie_high')

    def _kill_plan(self, v, targets):
        original = WerewolfPolicy._kill_plan(self, v, targets)
        proposed = super()._kill_plan(v, targets)
        if v.role != 'wolf' or proposed[0] == original[0]:
            return original
        wolves = {v.me, *v.allies} & set(v.alive)
        if len(wolves) != v.wolves_left:
            return original
        def risks(target):
            live = sorted(s for s in v.alive if s != target)
            return int(live[0] in wolves), int(live[-1] in wolves)
        return original if any(new > old for new, old in zip(risks(proposed[0]), risks(original[0]))) else proposed


class ShieldedNightContestant(Contestant):
    def __init__(self):
        super().__init__()
        self.ww = ShieldedNightPolicy()

    def _fresh(self):
        super()._fresh()
        self.ww = ShieldedNightPolicy()


def shielded():
    return ShieldedNightContestant()
