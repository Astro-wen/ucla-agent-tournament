from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
import hashlib
import math
import random
import time

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, chat_guard
from agent.werewolf.state import normalize
from local.werewolf_belief_research import BeliefPolicy, role_of
from local.werewolf_coordination_research import CoordinationPolicy
from local.werewolf_counterfactual_research import canonical
from local.werewolf_env import Seat
from local.werewolf_research import ResearchGame


MODES = ('mirror', 'greedy', 'rollout_prior', 'rollout_observed')
STYLES = ('low', 'high', 'random', 'informed', 'pledge')
PRIOR = (0.2, 0.1, 0.2, 0.3, 0.2)
PARTICLES = 16
MIN_ADVANTAGE = 0.125


def public_messages(v):
    rows = []
    for message in v.messages:
        if (not isinstance(message, dict) or message.get('type', 'chat') != 'chat'
                or message.get('recipients') or message.get('visibility', 'broadcast') != 'broadcast'
                or type(message.get('sender')) is not int or message['sender'] not in range(7)
                or not isinstance(message.get('content'), str)):
            continue
        day = message.get('_day', message.get('day'))
        if type(day) is not int or not 1 <= day <= v.day:
            continue
        rows.append({'seq': len(rows), 'sender': message['sender'], 'content': normalize(message['content'], v.names),
                     'day': day, 'type': 'chat', 'visibility': 'broadcast', 'recipients': []})
    return rows


def signature(v, reference):
    return {'me': v.me, 'role': v.role, 'day': v.day, 'alive': sorted(v.alive), 'dead': v.dead,
            'dead_cause': v.dead_cause, 'votes': v.votes, 'checks': v.investigations if v.role == 'seer' else {},
            'public_messages': public_messages(v), 'reference': reference}


def distribution(v):
    belief = BeliefPolicy().beliefs(v)
    c = WerewolfPolicy.claims(v, private=True)
    known, clear = set(), set()
    if c.trusted is not None and v.dead.get(c.trusted) == 'seer' and c.trusted not in c.liars:
        known = set(c.trusted_wolves(v.alive))
        clear = set(c.trusted_clears())
    kept = [(d, p) for d, p in zip(belief.deals, belief.probabilities)
            if known <= set(d.wolves) and not clear & set(d.wolves)]
    total = sum(p for _, p in kept)
    return [(d, p / total) for d, p in kept] if total else []


def style_weights(v, voter, observed):
    if not observed:
        return PRIOR
    weights = list(PRIOR)
    if any(m['sender'] == voter for m in public_messages(v)):
        weights = [w * (0.35 if i < 3 else 1) for i, w in enumerate(weights)]
    for day, ballots, _ in v.votes[-2:]:
        if voter not in ballots:
            continue
        legal = sorted(s for s in ballots if s != voter) + [7]
        if not legal or len(legal) == 1:
            continue
        target = 7 if ballots[voter] is None else ballots[voter]
        intent = CoordinationPolicy.commitments(v, day).get(voter)
        chance = 1 / len(legal)
        low = 0.8 * (target == legal[0]) + 0.2 * chance
        high = 0.8 * (target == legal[-2]) + 0.2 * chance
        informed = 0.6 if v.dead.get(target) == 'wolf' else 0.15 if v.dead.get(target) in ('seer', 'villager') else chance
        pledge = 0.75 * (target == intent) + 0.25 * chance if intent is not None else chance
        weights = [w * max(0.03, likelihood) for w, likelihood in zip(weights, (low, high, chance, informed, pledge))]
    total = sum(weights)
    return tuple(0.8 * w / total + 0.2 * p for w, p in zip(weights, PRIOR))


class ModelPolicy(WerewolfPolicy):
    def fallback(self, raw):
        raise ValueError('unexpected hypothetical policy fallback')

    def chat(self, raw):
        v = self._view(raw)
        if v.day != self._said_day:
            self._said_day, self._topics = v.day, set()
        out = []
        for topic, message in self._plan(v):
            if topic in self._topics:
                continue
            self._topics.add(topic)
            message = chat_guard(message, v, topic)
            if message is not None:
                out.append(message)
        return out


class ModelSeat(Seat):
    def __init__(self, style='informed', root_day=None, intent=None, forced=None):
        self.style, self.root_day, self.intent, self.forced = style, root_day, intent, forced

    def reset(self):
        self.policy = ModelPolicy()
        self.queue = deque()
        self.spoken = 0
        self.chat_day = None
        self.used_force = False

    def choose(self, raw):
        legal = [int(a['action_id']) for a in raw['legal_actions']['actions']]
        gs = raw['game_state']
        if self.forced is not None and not self.used_force and gs['phase'] == 'day_vote' and gs['day'] == self.root_day:
            self.used_force = True
            if self.forced not in legal:
                raise ValueError('illegal hypothetical root action')
            return str(self.forced)
        if self.style == 'low':
            return str(min(legal))
        if self.style == 'high':
            return str(max(a for a in legal if a != 7) if any(a != 7 for a in legal) else 7)
        if self.style == 'random':
            return str(self.rng.choice(legal))
        if self.style == 'pledge' and gs['day'] == self.root_day and gs['phase'] == 'day_vote' and self.intent in legal:
            return str(self.intent)
        return self.policy.act(raw)['action_id']

    def message(self, raw):
        if self.style in ('low', 'high', 'random'):
            return None
        day = raw['game_state']['day']
        if day != self.chat_day:
            self.chat_day, self.spoken = day, 0
            self.queue.clear()
        if self.spoken >= 5:
            return None
        if not self.queue:
            self.queue.extend(self.policy.chat(raw))
        if not self.queue:
            return None
        self.spoken += 1
        return self.queue.popleft()


def deaths(v):
    out = []
    for seat, role in v.dead.items():
        cause = v.dead_cause.get(seat)
        if cause not in ('vote', 'night_kill'):
            raise ValueError('unsupported public death cause')
        if cause == 'vote':
            days = [day for day, _, eliminated in v.votes if eliminated == seat]
            if not days:
                raise ValueError('missing public elimination history')
            day = days[0]
        else:
            day = next((day for day, ballots, _ in v.votes if seat not in ballots), v.day)
        out.append({'day': day, 'seat': seat, 'role': role, 'cause': cause})
    return sorted(out, key=lambda d: (d['day'], d['cause'] == 'vote', d['seat']))


def hypothetical_game(v, world, styles, seed, action, memory):
    intents = CoordinationPolicy.commitments(v)
    seats = [ModelSeat('informed' if s == v.me else styles[s], v.day, intents.get(s), action if s == v.me else None)
             for s in range(7)]
    game = ResearchGame(seats, random.Random(seed), strict_messaging=True, timing='rounds', messaging=True, wording='new')
    game.roles = [role_of(world, s) for s in range(7)]
    game.alive = [s in v.alive for s in range(7)]
    game.day = v.day
    game.dead = deaths(v)
    game.votes = [{'day': day, 'votes': dict(ballots), 'eliminated': eliminated} for day, ballots, eliminated in v.votes]
    game.messages = public_messages(v)
    game.window_start_seq = len(game.messages)
    game.stall = 0
    for _, _, eliminated in reversed(v.votes):
        if eliminated is not None:
            break
        game.stall += 1
    public_memory = {m['seq']: dict(m, _day=m['day']) for m in game.messages}
    for seat in seats:
        seat.policy.transcript = deepcopy(public_memory)
    mine = deepcopy(memory)
    mine['transcript'] = deepcopy(public_memory)
    seats[v.me].policy.import_memory(mine)
    if world.seer in v.alive:
        if v.role == 'seer':
            game.invest = dict(v.investigations)
        else:
            c = WerewolfPolicy.claims(v, private=False)
            announced = sorted(c.wolves.get(world.seer, set()) | c.clears.get(world.seer, set()))
            pool = [s for s in announced if s != world.seer]
            rest = [s for s in range(7) if s != world.seer and s not in pool]
            rng = random.Random(f'unknown-seer-checks:{seed}')
            rng.shuffle(rest)
            for target in (pool + rest)[:min(v.day, 6)]:
                game.invest[target] = 'wolf' if target in world.wolves else 'villager'
    return game


def terminal_trial(v, world, styles, seed, action, memory, forecast=None):
    game = hypothetical_game(v, world, styles, seed, action, memory)
    game.vote()
    if forecast is not None:
        forecast.update({s: 7 if target is None else target for s, target in game.votes[-1]['votes'].items() if s != v.me})
    if not game.check():
        game.day += 1
        game.play()
    if any(sum(game.stats[k]) for k in ('crash', 'illegal', 'bad_chat', 'timeout', 'chat_after_terminate')):
        raise ValueError('hypothetical game protocol failure')
    return int(game.winner == 'villager')


class PlanningPolicy(WerewolfPolicy):
    def __init__(self, mode='rollout_prior'):
        if mode not in MODES:
            raise ValueError('unknown planning mode')
        super().__init__()
        self.mode = mode
        self.moving = False
        self.offered_abstain = False
        self.cache = {}
        self.planning_stats = Counter()
        self.planning_trace = []

    def act(self, raw):
        old = self.moving, self.offered_abstain
        self.moving, self.offered_abstain = raw.get('phase') == 'moving', '7' in self._legal(raw)
        try:
            return super().act(raw)
        finally:
            self.moving, self.offered_abstain = old

    def _day_vote(self, v, targets):
        original = super()._day_vote(v, targets)
        if (self.mode == 'mirror' or not self.moving or v.role not in ('seer', 'villager')
                or not 3 <= len(v.alive) <= 5 or v.me not in v.alive or v.day is None
                or len(v.alive) != len(set(v.alive)) or any(r not in ('wolf', 'seer', 'villager') for r in v.dead.values())
                or set(v.alive) & set(v.dead) or set(v.alive) | set(v.dead) != set(range(7))
                or len(v.messages) > 256 or not 0 < 2 * v.wolves_left < len(v.alive)
                or any(c not in ('vote', 'night_kill') for c in v.dead_cause.values())):
            return original
        try:
            deaths(v)
        except ValueError:
            self.planning_stats['unsupported_public_history'] += 1
            return original
        worlds = distribution(v)
        if not worlds:
            return original
        possible = set().union(*(set(d.wolves) for d, _ in worlds))
        offered = list(targets) + ([7] if self.offered_abstain else [])
        choices = sorted({a for a in offered if a == 7 or a in possible and a != v.me})
        if original[0] not in choices or len(choices) < 2:
            return original
        certain = set.intersection(*(set(d.wolves) for d, _ in worlds))
        if len(v.alive) == 3 and certain:
            return original
        if self.mode == 'greedy':
            probability = {s: sum(p for d, p in worlds if s in d.wolves) for s in choices if s != 7}
            pick = max(probability, key=lambda s: (round(probability[s], 10), s == original[0], -s))
            return pick, 'same information-set beliefs without rollout'
        key = hashlib.sha256(canonical({'view': signature(v, original[0]), 'legal': choices})).hexdigest()
        if key not in self.cache:
            self.cache[key] = self.plan(v, worlds, choices, original[0], key)
            if len(self.cache) > 64:
                self.cache.pop(next(iter(self.cache)))
        pick = self.cache[key]
        return (pick, 'sampled hidden-world terminal-outcome planning, not a role claim') if pick != original[0] else original

    def plan(self, v, worlds, choices, reference, key):
        start = time.perf_counter()
        rng = random.Random(key)
        values = {a: [] for a in choices}
        forecasts = {s: Counter() for s in v.alive if s != v.me}
        weights = {s: style_weights(v, s, self.mode == 'rollout_observed') for s in v.alive if s != v.me}
        memory = deepcopy(self.export_memory())
        for _ in range(PARTICLES):
            world = rng.choices([d for d, _ in worlds], [p for _, p in worlds])[0]
            styles = {s: rng.choices(STYLES, weights[s])[0] for s in v.alive if s != v.me}
            for s in range(7):
                styles.setdefault(s, 'informed')
            seed = rng.getrandbits(64)
            predicted = {}
            for action in choices:
                values[action].append(terminal_trial(v, world, styles, seed, action, memory,
                                                     forecast=predicted if action == reference else None))
            for voter, target in predicted.items():
                forecasts[voter][target] += 1
        advantages = {}
        for action in choices:
            delta = [a - b for a, b in zip(values[action], values[reference])]
            mean = sum(delta) / PARTICLES
            variance = sum((x - mean) ** 2 for x in delta) / (PARTICLES - 1)
            advantages[action] = mean, mean - math.sqrt(variance / PARTICLES)
        best = max(choices, key=lambda a: (advantages[a][0], a == reference, -a))
        pick = best if advantages[best][0] >= MIN_ADVANTAGE and advantages[best][1] > 0 else reference
        self.planning_stats.update(calls=1, inner_games=PARTICLES * len(choices), changed=int(pick != reference))
        if len(self.planning_trace) < 30:
            self.planning_trace.append({'day': v.day, 'alive': len(v.alive), 'worlds': len(worlds), 'reference': reference,
                                        'selected': pick, 'model_advantage': advantages[best][0],
                                        'sampling_stability': advantages[best][1], 'forecast': {s: dict(c) for s, c in forecasts.items()},
                                        'ms': 1000 * (time.perf_counter() - start)})
        return pick


class PlanningContestant(Contestant):
    def __init__(self, mode='rollout_prior'):
        self.planning_mode = mode
        super().__init__()
        self.ww = PlanningPolicy(mode)

    def _fresh(self):
        super()._fresh()
        self.ww = PlanningPolicy(self.planning_mode)


def mirror():
    return PlanningContestant('mirror')


def greedy():
    return PlanningContestant('greedy')


def rollout_prior():
    return PlanningContestant('rollout_prior')


def rollout_observed():
    return PlanningContestant('rollout_observed')
