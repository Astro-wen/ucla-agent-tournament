"""Revealed roles outrank claims, including a seer who died without claiming."""
from agent.werewolf.policy import WerewolfPolicy
from tests.strategy.test_werewolf_chat import state


def test_silent_dead_seer_discredits_later_claim_and_fake_clear():
    raw = state('Your role: VILLAGER', 'day_vote', [0, 1, 4, 5, 7], me=3, day=2,
                alive=[0, 1, 3, 4, 5], dead=[(2, 'seer', 'night_kill', 2), (6, 'villager', 'vote', 1)],
                msgs=[(4, 'I am the Seer. Player5 is a WOLF. Player0 is clear.')])
    policy = WerewolfPolicy()
    claims = policy.claims(policy._view(raw))
    assert claims.trusted == 2
    assert claims.liars == {4}
    assert not claims.trusted_wolves([0, 1, 3, 4, 5])
    assert 0 not in claims.trusted_clears()
    assert policy.act(raw)['action_id'] == '4'


def test_dead_claimed_seer_results_survive_competing_living_claim():
    raw = state('Your role: VILLAGER', 'day_vote', [0, 1, 4, 5, 7], me=3, day=2,
                alive=[0, 1, 3, 4, 5], dead=[(2, 'seer', 'night_kill', 2), (6, 'villager', 'vote', 1)],
                msgs=[(2, 'I am the Seer. Player5 is a WOLF.'),
                      (4, 'I am the Seer. Player0 is a WOLF.')])
    policy = WerewolfPolicy()
    claims = policy.claims(policy._view(raw))
    assert claims.trusted == 2 and claims.liars == {4}
    assert claims.trusted_wolves([0, 1, 3, 4, 5]) == [5]
    # Both a revealed-seer accusation and a false claimant are evidence; never
    # follow the false claimant's accusation against Player0.
    assert policy.act(raw)['action_id'] in {'4', '5'}


def test_unknown_dead_role_does_not_discredit_a_living_seer():
    raw = state('Your role: VILLAGER', 'day_vote', [0, 1, 4, 5, 6, 7], me=3, day=2,
                alive=[0, 1, 3, 4, 5, 6], dead=[(2, '?', 'night_kill', 2)],
                msgs=[(4, 'I am the Seer. Player5 is a WOLF.')])
    policy = WerewolfPolicy()
    assert policy.claims(policy._view(raw)).trusted == 4
    assert policy.act(raw)['action_id'] == '5'
