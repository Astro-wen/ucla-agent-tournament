from __future__ import annotations

from collections import Counter
from functools import lru_cache
import json
from pathlib import Path

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, _CLAIMED_SEER, _chats


PHASES = ('villager_vote', 'seer_vote', 'seer_investigate', 'wolf_vote', 'wolf_night')
ROLE_PHASES = {'villager': (0,), 'seer': (1, 2), 'wolf': (3, 4)}
BASE = (0,) * 15
PREDICATES = 12
SELECTORS = 12
MODEL_PATH = Path(__file__).resolve().parents[1] / 'data/werewolf_evolution_model.json'


def validate(genome):
    genome = tuple(genome)
    if len(genome) != 15 or any(type(x) is not int or not 0 <= x < 12 for x in genome):
        raise ValueError('bounded15integer program required')
    return genome


def role_key(genome, role):
    return tuple(genome[i] for phase in ROLE_PHASES[role] for i in range(3 * phase, 3 * phase + 3))


def install_role(genome, role, genes):
    indices = [i for phase in ROLE_PHASES[role] for i in range(3 * phase, 3 * phase + 3)]
    if len(genes) != len(indices):
        raise ValueError('wrong role program width')
    out = list(genome)
    for i, value in zip(indices, genes):
        out[i] = value
    return validate(out)


class EvolutionPolicy(WerewolfPolicy):
    def __init__(self, genome=BASE):
        super().__init__()
        self.genome = validate(genome)
        self._wolf_context = False

    @staticmethod
    def support(v):
        latest = {}
        for _, sender, facts, day in _chats(v):
            if day != v.day or sender == v.me or sender not in v.alive or facts.injection:
                continue
            choices = (facts.sus | facts.wolves) & set(v.alive) - {sender}
            latest[sender] = next(iter(choices)) if len(choices) == 1 else None
        return Counter(target for target in latest.values() if target is not None)

    def predicate(self, index, v, claims, support):
        if index == 0:
            return True
        if index == 1:
            return v.day == 1
        if index in (2, 3):
            return len(v.alive) <= (3 if index == 2 else 5)
        live = claims.live(v)
        if index == 4:
            return len(live) == 1
        if index == 5:
            return len(live) >= 2
        if index == 6:
            return bool(set(claims.liars) & set(v.others_alive))
        if index == 7:
            return bool(v.votes and v.votes[-1][2] is None)
        if index == 8:
            return bool(v.votes and v.dead.get(v.votes[-1][2]) == 'wolf')
        if index == 9:
            return max(support.values(), default=0) >= 2
        if index == 10:
            if not v.votes:
                return False
            votes = v.votes[-1][1]
            others = {s: target for s, target in votes.items() if s != v.me}
            matches = sum(target == min(set(votes) - {s}) for s, target in others.items() if len(votes) > 1)
            return len(others) >= 3 and matches >= 0.8 * len(others)
        return v.role == 'wolf' and min(v.alive) in {v.me, *v.allies}

    def program(self, phase, v, pool, original):
        pool = sorted(set(pool))
        if not pool:
            return original
        predicate, yes, no = self.genome[3 * phase:3 * phase + 3]
        if yes == no == 0:
            return original
        claims = self.claims(v, private=v.role != 'wolf')
        support = self.support(v)
        choice = yes if self.predicate(predicate, v, claims, support) else no
        if choice == 0:
            return original
        scores = self._scores(v, private=v.role != 'wolf')
        pick = original[0] if original[0] in pool else min(pool)
        if choice == 1:
            pick = min(pool)
        elif choice == 2:
            pick = max(pool)
        elif choice == 3:
            pick = max(pool, key=lambda s: (scores.get(s, 0), -s))
        elif choice == 4:
            pick = min(pool, key=lambda s: (scores.get(s, 0), s))
        elif choice == 5:
            pick = max(pool, key=lambda s: (support.get(s, 0), scores.get(s, 0), -s))
        elif choice == 6:
            pick = min(pool, key=lambda s: (support.get(s, 0), scores.get(s, 0), s))
        elif choice in (7, 8, 9):
            live = [s for s in claims.live(v) if s in pool]
            if choice == 9:
                live = claims.live(v)
                named = [s for s in pool if live and s in claims.wolves.get(live[0], ())]
                if named:
                    pick = min(named)
            elif live:
                pick = live[0] if choice == 7 else live[-1]
        elif choice == 10:
            liars = [s for s in pool if s in claims.liars]
            if liars:
                pick = max(liars, key=lambda s: (scores.get(s, 0), -s))
        elif choice == 11 and v.votes:
            previous = v.votes[-1][1].get(v.me)
            if previous in pool:
                pick = previous
        return pick, 'fixed evolved observation-only decision program'

    def _day_vote(self, v, targets):
        original = super()._day_vote(v, targets)
        if v.role == 'wolf' or self._wolf_context:
            return original
        pool = set(targets) & set(v.others_alive)
        if v.role == 'seer':
            known, clear = self.seer_knowledge(v)
            if known or original[1] == 'fake seer claim':
                return original
            return self.program(1, v, pool - set(clear), original)
        claims = self.claims(v, private=True)
        if claims.trusted is not None and v.dead.get(claims.trusted) == 'seer':
            if claims.trusted_wolves(v.alive):
                return original
            pool -= claims.trusted_clears()
        return self.program(0, v, pool, original)

    def _wolf_vote(self, v, targets):
        previous, self._wolf_context = self._wolf_context, True
        try:
            original = super()._wolf_vote(v, targets)
        finally:
            self._wolf_context = previous
        if original[0] in v.allies:
            return original
        pool = set(targets) & set(v.others_alive) - set(v.allies)
        return self.program(3, v, pool, original)

    def _investigate(self, v, targets):
        original = super()._investigate(v, targets)
        pool = set(targets) & set(v.others_alive) - set(v.investigations)
        return self.program(2, v, pool, original) if v.role == 'seer' else original

    def _kill_plan(self, v, targets):
        original = super()._kill_plan(v, targets)
        if v.role != 'wolf':
            return original
        pool = set(targets) & set(v.others_alive) - set(v.allies)
        if original[1] == _CLAIMED_SEER:
            c = self.claims(v, private=False)
            pool &= {s for s in c.order if s not in c.liars}
        chosen = self.program(4, v, pool, original)
        return (chosen[0], _CLAIMED_SEER) if original[1] == _CLAIMED_SEER else chosen

    def _opening(self, v):
        previous = self._wolf_context
        self._wolf_context = previous or v.role == 'wolf'
        try:
            return super()._opening(v)
        finally:
            self._wolf_context = previous

    def _villager_chat(self, v, c):
        original = super()._villager_chat(v, c)
        if v.role != 'villager' or not original:
            return original
        baseline, _ = WerewolfPolicy._day_vote(self, v, v.others_alive)
        selected, _ = self._day_vote(v, v.others_alive)
        if selected == baseline or selected is None:
            return original
        return 'program-vote', {'content': f'My vote is Player{selected}. This is a vote plan, not a checked result. Please coordinate.',
                                'recipients': []}


class EvolutionContestant(Contestant):
    def __init__(self, genome=BASE):
        self.program_genome = validate(genome)
        super().__init__()
        self.ww = EvolutionPolicy(self.program_genome)

    def _fresh(self):
        super()._fresh()
        self.ww = EvolutionPolicy(self.program_genome)


def mirror():
    return EvolutionContestant()


def load_genome(path=MODEL_PATH):
    with path.open('rb') as handle:
        raw = handle.read(2049)
    if len(raw) > 2048:
        raise ValueError('oversized fixed program model')
    model = json.loads(raw)
    if set(model) != {'schema', 'genome'} or model['schema'] != 1:
        raise ValueError('invalid fixed program model schema')
    return validate(model['genome'])


@lru_cache(maxsize=1)
def frozen_genome():
    return load_genome()


def trained():
    return EvolutionContestant(frozen_genome())
