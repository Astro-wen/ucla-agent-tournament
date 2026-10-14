from copy import deepcopy
from collections import Counter
import math
import random
import time

from agent.agent import Contestant
from local import werewolf_planning_research as model


class FreePlanningPolicy(model.PlanningPolicy):
    def __init__(self):
        super().__init__('rollout_prior')

    def plan(self, v, worlds, choices, reference, key):
        start = time.perf_counter()
        rng = random.Random(key)
        values = {a: [] for a in choices}
        forecasts = {s: Counter() for s in v.alive if s != v.me}
        weights = {s: model.style_weights(v, s, False) for s in v.alive if s != v.me}
        memory = deepcopy(self.export_memory())
        for _ in range(model.PARTICLES):
            world = rng.choices([d for d, _ in worlds], [p for _, p in worlds])[0]
            styles = {s: rng.choices(model.STYLES, weights[s])[0] for s in v.alive if s != v.me}
            for s in range(7):
                styles.setdefault(s, 'informed')
            seed, predicted = rng.getrandbits(64), {}
            for action in choices:
                values[action].append(model.terminal_trial(v, world, styles, seed, action, memory,
                                                            forecast=predicted if action == reference else None))
            for voter, target in predicted.items():
                forecasts[voter][target] += 1
        means = {a: sum(values[a]) / model.PARTICLES for a in choices}
        pick = max(choices, key=lambda a: (means[a], a == reference, -a))
        delta = [a - b for a, b in zip(values[pick], values[reference])]
        mean = sum(delta) / model.PARTICLES
        variance = sum((d - mean) ** 2 for d in delta) / (model.PARTICLES - 1)
        self.planning_stats.update(calls=1, inner_games=model.PARTICLES * len(choices), changed=int(pick != reference))
        if len(self.planning_trace) < 30:
            self.planning_trace.append({'day': v.day, 'alive': len(v.alive), 'worlds': len(worlds), 'reference': reference,
                                        'selected': pick, 'model_advantage': mean, 'sampling_stability': mean - math.sqrt(variance / model.PARTICLES),
                                        'forecast': {s: dict(c) for s, c in forecasts.items()}, 'ungated': True,
                                        'ms': 1000 * (time.perf_counter() - start)})
        return pick


class FreePlanningContestant(Contestant):
    def __init__(self):
        super().__init__()
        self.ww = FreePlanningPolicy()

    def _fresh(self):
        super()._fresh()
        self.ww = FreePlanningPolicy()


def rollout_free():
    return FreePlanningContestant()
