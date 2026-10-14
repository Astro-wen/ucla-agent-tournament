from functools import partial
from types import FunctionType

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy
from .werewolf_belief_research import BeliefPolicy
from .werewolf_coordination_research import CoordinationPolicy
from .werewolf_simple import ExactPolicy


FACTORS = ("belief", "coalition", "information", "transmit", "night", "listen")


def with_followup_budget(method, budget):
    namespace = dict(method.__globals__, LISTEN_FOLLOWUPS=budget)
    function = FunctionType(method.__code__, namespace, method.__name__, method.__defaults__, method.__closure__)
    function.__kwdefaults__ = method.__kwdefaults__
    return function


class FactorPolicy(CoordinationPolicy, BeliefPolicy, ExactPolicy):
    def __init__(self, mask=0):
        if type(mask) is not int or not 0 <= mask < 64:
            raise ValueError("six binary factors require mask in 0..63")
        self.mask = mask
        self.profile = "tempered"
        CoordinationPolicy.__init__(self, "combined")

    def enabled(self, component):
        return component in FACTORS and bool(self.mask & (1 << FACTORS.index(component)))

    def _day_vote(self, v, targets):
        if v.role == "wolf":
            return WerewolfPolicy._day_vote(self, v, targets)
        original = BeliefPolicy._day_vote(self, v, targets) if self.enabled("belief") else WerewolfPolicy._day_vote(self, v, targets)
        return self._coalition_vote(v, targets, original) if self.enabled("coalition") else original

    def _wolf_vote(self, v, targets):
        return WerewolfPolicy._wolf_vote(self, v, targets)

    def _investigate(self, v, targets):
        if self.enabled("information"):
            return CoordinationPolicy._investigate(self, v, targets)
        if self.enabled("belief"):
            return BeliefPolicy._investigate(self, v, targets)
        return WerewolfPolicy._investigate(self, v, targets)

    def _seer_chat(self, v, c):
        if self.enabled("transmit"):
            return CoordinationPolicy._seer_chat(self, v, c)
        if self.enabled("belief"):
            return BeliefPolicy._seer_chat(self, v, c)
        return WerewolfPolicy._seer_chat(self, v, c)

    def _villager_chat(self, v, c):
        if self.enabled("belief"):
            return BeliefPolicy._villager_chat(self, v, c)
        return WerewolfPolicy._villager_chat(self, v, c)

    def _opening(self, v):
        return WerewolfPolicy._opening(self, v)

    def _kill_plan(self, v, targets):
        if self.enabled("night"):
            return ExactPolicy._kill_plan(self, v, targets)
        return WerewolfPolicy._kill_plan(self, v, targets)

    def _plan(self, v):
        out = WerewolfPolicy._plan(self, v)
        if self.enabled("coalition") and v.role in ("villager", "seer") and "open" in self._topics and not out:
            original = BeliefPolicy._day_vote(self, v, v.others_alive) if self.enabled("belief") else WerewolfPolicy._day_vote(self, v, v.others_alive)
            pick, _ = self._coalition_vote(v, v.others_alive, original)
            if pick != original[0]:
                out.append(("coordinate", {"content": f"I vote Player{pick}. Several living players have called "
                            "this vote; staying together avoids a tie. This is a vote plan, not a checked result.",
                            "recipients": []}))
        return out


class FactorContestant(Contestant):
    def __init__(self, mask=0):
        self.factor_mask = mask
        super().__init__()
        self._install()

    def _install(self):
        self.ww = FactorPolicy(self.factor_mask)
        for name in ("_choose_message", "_may_hold"):
            method = getattr(Contestant, name)
            if self.factor_mask & 32:
                method = with_followup_budget(method, 3)
            setattr(self, name, method.__get__(self, type(self)))

    def _fresh(self):
        super()._fresh()
        self._install()


for _mask in range(64):
    globals()[f"f{_mask:02d}"] = partial(FactorContestant, _mask)
