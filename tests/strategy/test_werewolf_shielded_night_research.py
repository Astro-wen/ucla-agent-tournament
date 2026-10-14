from itertools import combinations

import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View
from local.werewolf_night_priority_research import NightPriorityPolicy
from local.werewolf_shielded_night_research import ShieldedNightPolicy, shielded


def view(alive, wolves):
    dead = {s: 'villager' for s in range(7) if s not in alive}
    if len(wolves) == 1:
        dead[min(dead)] = 'wolf'
    return View(me=wolves[0], role='wolf', day=3, sub_phase='night_wolf', alive=list(alive),
                allies=list(wolves[1:]), dead=dead, dead_cause={s: 'vote' for s in dead})


def risks(alive, wolves, target):
    live = sorted(s for s in alive if s != target)
    return int(live[0] in wolves), int(live[-1] in wolves)


@pytest.mark.parametrize('wolf_count', [1, 2])
def test_guard_reduces_neither_endpoint_safety_to_improve_the_other(wolf_count, monkeypatch):
    for size in range(2 * wolf_count + 1, 7 if wolf_count == 1 else 8):
        for alive in combinations(range(7), size):
            for wolves in combinations(alive, wolf_count):
                v = view(alive, wolves)
                monkeypatch.setattr(WerewolfPolicy, 'suspicion', lambda self, v, **kw: {s: 0 for s in v.alive})
                legal = [s for s in alive if s not in wolves]
                old = WerewolfPolicy()._kill_plan(v, legal)
                high = NightPriorityPolicy('tie_high')._kill_plan(v, legal)
                new = ShieldedNightPolicy()._kill_plan(v, legal)
                before, after = risks(alive, wolves, old[0]), risks(alive, wolves, new[0])
                assert new[0] in legal and all(a <= b for a, b in zip(after, before))
                allowed = all(a <= b for a, b in zip(risks(alive, wolves, high[0]), before))
                assert new == (high if allowed and high[0] != old[0] else old)


def test_does_not_trade_a_low_exposed_ally_for_a_high_exposed_self(monkeypatch):
    v = view([0, 1, 2, 3, 4, 5], (4, 1))
    monkeypatch.setattr(WerewolfPolicy, 'suspicion', lambda self, v, **kw: {s: 0 for s in v.alive})
    assert NightPriorityPolicy('tie_high')._kill_plan(v, [0, 2, 3, 5])[0] == 5
    assert ShieldedNightPolicy()._kill_plan(v, [0, 2, 3, 5])[0] == 0


def test_equal_endpoint_risks_keep_the_positive_high_tie_rule(monkeypatch):
    v = view([0, 1, 2, 4, 5, 6], (1, 4))
    scores = {0: 1, 2: 0, 5: 0, 6: 1}
    monkeypatch.setattr(WerewolfPolicy, 'suspicion', lambda self, v, **kw: scores)
    assert ShieldedNightPolicy()._kill_plan(v, [0, 2, 5, 6])[0] == 5


def test_public_seer_and_priority_not_overridden(monkeypatch):
    v = view([0, 1, 2, 3, 4, 5], (1, 4))
    v.messages = [{'sender': 5, 'content': 'I am the Seer. Player1 is a WOLF.',
                   'day': 2, 'seq': 0, 'visibility': 'broadcast', 'recipients': []}]
    assert ShieldedNightPolicy()._kill_plan(v, [0, 2, 3, 5]) == WerewolfPolicy()._kill_plan(v, [0, 2, 3, 5])
    v.messages = []
    monkeypatch.setattr(WerewolfPolicy, 'suspicion', lambda self, v, **kw: {0: -1, 2: 0, 3: 0, 5: 0})
    assert ShieldedNightPolicy()._kill_plan(v, [0, 2, 3, 5]) == WerewolfPolicy()._kill_plan(v, [0, 2, 3, 5])


def test_fresh_reinstalls_the_same_guarded_policy():
    agent = shielded()
    before = agent.ww
    agent._fresh()
    assert isinstance(agent.ww, ShieldedNightPolicy) and agent.ww is not before
