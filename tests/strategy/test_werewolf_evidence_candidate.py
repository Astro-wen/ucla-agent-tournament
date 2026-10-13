"""Keep table talk out of the factual evidence used to trust or discredit a seer."""
import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View, read_chat


@pytest.mark.parametrize("verdict", ["a wolf", "a villager", "innocent", "clear"])
@pytest.mark.parametrize("template", [
    "Player3 says Player5 is {verdict}.",
    "According to Player3, Player5 is {verdict}.",
    "Player3: Player5 is {verdict}.",
    "Player5 is {verdict}, says Player3.",
    "If Player5 is {verdict}, we should reconsider.",
    "Maybe Player5 is {verdict}, maybe not.",
    "There is no evidence Player5 is {verdict}.",
    "I doubt Player5 is {verdict}.",
    "Is Player5 {verdict}?",
])
def test_short_verdicts_require_the_senders_own_assertion(template, verdict):
    facts = read_chat(template.format(verdict=verdict), sender=0)
    assert not facts.claim
    assert not facts.wolves
    assert not facts.clears


@pytest.mark.parametrize("text", [
    "Player5 is a wolf or a scared villager.",
    "Player5 is a wolf bluff candidate.",
    "Player5 is a villager or a careful wolf.",
    "I am the Seer. Player3 says Player5 is a wolf.",
    "I am the Seer. No proof Player5 is innocent.",
    "I am the Seer. I doubt I checked Player5: wolf.",
    "I am the Seer. I never checked Player5: wolf.",
    "I am the Seer. I did not check Player5: wolf.",
    "I am the Seer. I have not checked Player5: wolf.",
    "I am the Seer. I haven't investigated Player5: wolf.",
])
def test_qualified_and_attributed_verdicts_are_not_investigation_results(text):
    facts = read_chat(text, sender=0)
    assert not facts.wolves
    assert not facts.clears


@pytest.mark.parametrize("text", [
    "Did I say I am the seer?",
    "Am I right that my role is seer?",
    "I checked Player5 and wonder whether they are a wolf.",
    "I checked Player5 and doubt they are a wolf.",
    "I checked Player5 and have no evidence they are a wolf.",
    "If I checked Player5 and found a wolf, I would reveal.",
    "What if I checked Player5 and he was a wolf?",
    "Player3 said I checked Player5: wolf.",
    "I checked Player5 for wolf tells in their voting.",
    "I checked Player5, but never concluded they were a wolf.",
])
def test_mentioning_an_investigation_is_not_itself_a_seer_claim(text):
    assert not read_chat(text, sender=0).claim


@pytest.mark.parametrize("text,wolves,clears", [
    ("Seer claim: Player5 is a wolf.", {5}, set()),
    ("I am the Seer. Player5 is an innocent villager.", set(), {5}),
    ("I checked Player5: wolf.", {5}, set()),
    ("I checked Player5: not a wolf.", set(), {5}),
    ("I inspected Player5 and they are a wolf.", {5}, set()),
    ("I checked Player5, he is clean.", set(), {5}),
    ("I investigated Player5 last night: Player5 is a wolf.", {5}, set()),
    ("Player5 is a wolf, I checked them.", {5}, set()),
    ("I am the Seer. Player5 is innocent because I checked him.", set(), {5}),
    ("I am the Seer. Player5 is a wolf because I investigated him.", {5}, set()),
    ("I am the Seer. Player5 is innocent since I investigated him.", set(), {5}),
])
def test_explicit_first_person_results_remain_readable(text, wolves, clears):
    facts = read_chat(text, sender=0)
    assert facts.claim
    assert facts.wolves == wolves
    assert facts.clears == clears


@pytest.mark.parametrize("later_message", [
    "Player3 says Player5 is a wolf.",
    "If Player5 is a wolf, we would know after the vote.",
    "I doubt Player5 is a wolf.",
    "Is Player5 a wolf?",
])
def test_reported_accusation_cannot_discredit_the_genuine_seer(later_message):
    view = View(me=0, role="villager", day=2, sub_phase="day_vote", alive=[0, 1, 2, 3, 4, 6],
                dead={5: "villager"}, dead_cause={5: "vote"}, messages=[
                    {"seq": 0, "sender": 2, "type": "chat", "content": "I am the Seer. Player4 is a wolf."},
                    {"seq": 1, "sender": 2, "type": "chat", "content": later_message},
                ])
    claims = WerewolfPolicy.claims(view)
    assert claims.trusted == 2
    assert 2 not in claims.liars
    assert claims.trusted_wolves(view.alive) == [4]


def test_speculative_check_does_not_create_a_rival_seer():
    view = View(me=0, role="villager", day=1, sub_phase="day_vote", alive=list(range(7)),
                dead={}, dead_cause={}, messages=[
                    {"seq": 0, "sender": 2, "type": "chat", "content": "I am the Seer. Player4 is a wolf."},
                    {"seq": 1, "sender": 3, "type": "chat",
                     "content": "I checked Player5 and wonder whether they are a wolf."},
                ])
    claims = WerewolfPolicy.claims(view)
    assert claims.order == [2]
    assert claims.trusted == 2


@pytest.mark.parametrize("names", [{1: "Robin", 5: "Robin"}, {1: "Robin", 5: "ＲＯＢＩＮ"}])
def test_ambiguous_display_names_do_not_identify_a_seat(names):
    facts = read_chat("I am the Seer. Robin is a wolf.", sender=0, names=names)
    assert facts.claim
    assert not facts.wolves


@pytest.mark.parametrize("alias", ["p5", "Seat #5", "player five", "Player(5)"])
def test_seat_label_cannot_be_rebound_by_another_players_display_name(alias):
    facts = read_chat(f"I am the Seer. {alias} is a wolf.", sender=0, names={1: alias})
    assert facts.wolves == {5}


def test_unique_unicode_display_name_remains_usable():
    facts = read_chat("I am the Seer. Robin is a wolf.", sender=0, names={5: "Ｒｏｂｉｎ"})
    assert facts.wolves == {5}
