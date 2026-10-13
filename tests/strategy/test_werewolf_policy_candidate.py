"""Opt-in opening experiment: legality, claim precedence, unchanged ballots and safe rollback.

The neutral line is still a template-level tell, so these checks do not establish general strategic strength.
"""
from agent.werewolf.policy import WerewolfPolicy


def make_state(role='wolf', me=3, ally=0, day=1, messages=()):
    roles=f'Your role: {role.upper()}'
    if role=='wolf': roles+=f'\nYour fellow wolves: Player{ally}'
    elif role=='seer': roles+='\nYour investigations: Player6 is villager'
    return {
        'observation':f'=== Werewolf ===\n{roles}\nDay {day} | phase day_vote',
        'game_type':'werewolf','your_position':me,'phase':'messaging',
        'players':[{'position':i,'name':f'p{i}'} for i in range(7)],
        'game_state':{'day':day,'phase':'day_vote','alive':[f'p{i}' for i in range(7)],'dead':[],'vote_history':[]},
        'legal_actions':{'actions':[{'action_id':str(i)} for i in range(7) if i!=me]},
        'new_messages':[{'seq':i,'sender':sender,'content':text,'type':'chat','recipients':[],'day':day} for i,(sender,text) in enumerate(messages)],
    }

import pytest

from agent.werewolf import policy as P
from champion.agent.werewolf.policy import WerewolfPolicy as ChampionPolicy


@pytest.fixture(autouse=True)
def tactical_opening_on(monkeypatch):
    monkeypatch.setenv(P.TACTICS_ENV, 'on')
    monkeypatch.setenv(P.DM_ENV, 'off')


def test_wolf_does_not_publish_the_private_ally_exclusion():
    st = make_state(ally=0)
    pol = WerewolfPolicy()
    messages = pol.chat(st)
    assert len(messages) == 1
    assert messages[0]['recipients'] == []
    assert 'Player' not in messages[0]['content']
    assert 'abstain' in messages[0]['content']
    assert len(messages[0]['content'].split()) <= 50
    assert len(messages[0]['content'].encode('utf-8')) <= 1024
    # Concealing an unsupported opening read must not make the wolf sacrifice its ally.
    assert pol.act(st)['action_id'] == ChampionPolicy().act(st)['action_id'] == '1'


def test_ordinary_public_read_still_coordinates_the_table():
    st = make_state(ally=5)
    assert WerewolfPolicy().chat(st) == ChampionPolicy().chat(st)
    assert 'Player0' in WerewolfPolicy().chat(st)[0]['content']


@pytest.mark.parametrize('role', ['villager', 'seer'])
def test_good_roles_keep_their_opening_and_ballot(role):
    st = make_state(role=role)
    pol, old = WerewolfPolicy(), ChampionPolicy()
    assert pol.chat(st) == old.chat(st)
    assert pol.act(st)['action_id'] == old.act(st)['action_id']


def test_neutral_opening_is_only_an_uninformed_first_day_tactic():
    for st in [make_state(day=2), make_state(messages=[(6, 'I am the Seer. Player4 is clear.')])]:
        assert WerewolfPolicy().chat(st) == ChampionPolicy().chat(st)
        assert 'Player' in WerewolfPolicy().chat(st)[0]['content']


@pytest.mark.parametrize('accused', [0, 3])
def test_seer_claim_reactions_take_precedence_over_camouflage(accused):
    st = make_state(messages=[(6, f'I am the Seer. Player{accused} is a wolf.')])
    pol, old = WerewolfPolicy(), ChampionPolicy()
    assert pol.chat(st) == old.chat(st)
    assert pol.act(st)['action_id'] == old.act(st)['action_id']
    content = WerewolfPolicy().chat(st)[0]['content']
    assert content.startswith('Following the Seer' if accused == 0 else 'I am the Seer.')


def test_wolf_reacts_to_a_claim_after_its_neutral_opening():
    pol = WerewolfPolicy()
    first = make_state()
    opening = pol.chat(first)
    later = make_state(messages=[(3, opening[0]['content']), (6, 'I am the Seer. Player3 is a wolf.')])
    response = pol.chat(later)
    assert len(response) == 1
    assert response[0]['content'].startswith('I am the Seer. Player6 is a WOLF')
    assert response[0]['recipients'] == []


def test_neutral_opening_survives_a_restart_without_duplicate_chat():
    st = make_state()
    original = WerewolfPolicy()
    assert len(original.chat(st)) == 1
    restored = WerewolfPolicy()
    restored.import_memory(original.export_memory())
    assert restored.chat(st) == []


@pytest.mark.parametrize('setting', ['off', '0', 'false', 'no', 'disabled', 'unknown'])
def test_tactics_rollback_restores_baseline_opening(monkeypatch, setting):
    monkeypatch.setenv(P.TACTICS_ENV, setting)
    st = make_state()
    assert not P.tactics_enabled()
    assert WerewolfPolicy().chat(st) == ChampionPolicy().chat(st)
    assert 'Player1' in WerewolfPolicy().chat(st)[0]['content']


def test_experimental_tactic_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv(P.TACTICS_ENV)
    assert not P.tactics_enabled()
    st = make_state()
    assert WerewolfPolicy().chat(st) == ChampionPolicy().chat(st)


def test_named_experimental_tactic_is_explicitly_enabled(monkeypatch):
    monkeypatch.setenv(P.TACTICS_ENV, 'no_skip')
    assert P.tactics_enabled()
    assert 'Player' not in WerewolfPolicy().chat(make_state())[0]['content']


@pytest.mark.parametrize('setting', ['on', 'off'])
def test_tactic_never_changes_the_night_action(monkeypatch, setting):
    monkeypatch.setenv(P.TACTICS_ENV, setting)
    st = make_state(day=2)
    st['game_state']['phase'] = 'night_wolf'
    st['observation'] = st['observation'].replace('day_vote', 'night_wolf')
    st['phase'] = 'moving'
    st['legal_actions']['actions'] = [{'action_id': str(i)} for i in [1, 2, 4, 5, 6]]
    assert WerewolfPolicy().act(st)['action_id'] == ChampionPolicy().act(st)['action_id']
