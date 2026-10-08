"""Werewolf red-team findings (10-08, four context-free challenger bots in local/redteam_*.py): what they made our
agent misread or misplay, pinned down as tests."""
import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import parse, read_chat
from local.redteam_chaos import CLAIM_FORMATS, SEAT_FORMATS, fmt_seat

NAMES = ["alice", "bob", "carl", "dave", "erin", "fran", "gus"]
PLAYERS = [{"position": i, "name": n, "agent_id": f"id{i}"} for i, n in enumerate(NAMES)]


def state(role_block, sub_phase, actions, me=3, day=1, alive=range(7), dead=(), votes=(), msgs=(), phase="moving"):
    gs = {"day": day, "phase": sub_phase, "alive": [NAMES[i] for i in alive],
          "dead": [{"day": d, "player": NAMES[s], "role": r, "cause": c} for s, r, c, d in dead],
          "vote_history": [{"day": d, "votes": {NAMES[a]: (NAMES[b] if b is not None else "abstain") for a, b in vv.items()},
                            "eliminated": NAMES[e] if e is not None else None} for d, vv, e in votes],
          "winner": None}
    new = [{"seq": i, "sender": m[0], "recipients": [], "visibility": "broadcast", "type": "chat", "content": m[1],
            "day": m[2] if len(m) > 2 else day} for i, m in enumerate(msgs)]
    return {"observation": f"=== Werewolf ===\n{role_block}\nDay {day} | phase {sub_phase}", "game_type": "werewolf",
            "your_position": me, "players": PLAYERS, "game_state": gs, "phase": phase, "terminated_messaging": [],
            "legal_actions": {"actions": [{"action_id": str(a), "label": f"Vote to eliminate Player{a}"}
                                          for a in actions]} if actions else None,
            "new_messages": new}


# ---------------------------------------------------------------- reading: claim wordings and seat labels (chaos)
@pytest.mark.parametrize("name", sorted(CLAIM_FORMATS))
@pytest.mark.parametrize("kind", ["wolf", "clear"])
def test_every_challenger_claim_wording_is_read(name, kind):
    """Seer claims and results in the wordings the chaos challenger used (bold, hyphen-packed, arrows, typos, "My role
    is seer", "Seer result:", third person, a quoted own result, "on the village team", "I cleared X", buried after
    900 characters): our agent used to miss 19 of the 58 and vote the cleared seat."""
    tmpl = CLAIM_FORMATS[name][0 if kind == "wolf" else 1]
    text = " ".join(tmpl) if isinstance(tmpl, list) else tmpl
    seat = 5 if kind == "wolf" else 3
    text = text.format(w=f"Player{seat}", c=f"Player{seat}", me="Player2")
    f = read_chat(text, 2, {})
    assert f.claim or name == "noclaim"
    assert seat in (f.wolves if kind == "wolf" else f.clears)
    assert seat not in (f.clears if kind == "wolf" else f.wolves)


@pytest.mark.parametrize("style", sorted(SEAT_FORMATS))
def test_every_seat_label_is_read(style):
    """"player three", "Player03", "Plyer3", "Player(3)", "**Player3**" and the rest name seat 3."""
    f = read_chat(f"I am the Seer. {fmt_seat(5, style)} is a wolf. {fmt_seat(3, style)} is a villager.", 2, {})
    assert f.claim and f.wolves == {5} and f.clears == {3}


@pytest.mark.parametrize("text", [
    "If my role is seer I would have told you already.",
    "Is my role seer? No, I'm a villager.",
    "Seer claim: none yet. Let's vote carefully.",
    "Seer results? Anyone?",
    "What would you do as the seer? I'd claim early.",
    "Player2 is the seer, I believe them.",            # sent by Player4: about someone else
    "Player5 is bad at this game, honestly.",
    "Player5 is bad.",                                  # an opinion outside a seer claim
])
def test_new_wordings_do_not_read_table_talk_as_claims(text):
    f = read_chat(text, 4, {})
    assert not f.claim and not f.wolves


def test_long_chat_is_read_to_the_platform_cap():
    filler = "really-quite-honestly-and-after-a-lot-of-thinking " * 19
    assert 900 < len(filler) < 1000
    f = read_chat(filler + "I am the Seer. Player5 is a wolf.", 2, {})
    assert f.claim and f.wolves == {5}


# ---------------------------------------------------------------- the seer's own votes (chaos)
SEER = "Your role: SEER\nYour investigations: Player6 is villager, Player4 is wolf"


def test_seer_never_votes_a_seat_it_cleared_for_a_fake_claim():
    """chaos seed 2403 game 461: our seer had checked Player6 = villager; Player6 fake-claimed seer naming Player1;
    our seer voted Player6 (any other claimer = liar) instead of its checked wolf or anyone else."""
    st = state("Your role: SEER\nYour investigations: Player6 is villager", "day_vote", [0, 1, 2, 4, 5, 6], me=3,
               msgs=[(6, "I am the Seer. Player1 is a wolf.")])
    pol = WerewolfPolicy()
    assert pol.act(st)["action_id"] != "6"


def test_seer_votes_the_sure_wolf_the_table_is_on():
    """chaos3 seed 2407 game 125: on stall 2 the seer voted a deduced wolf while the table voted its checked wolf; the
    tie gave the wolves the game. Among sure wolves (checked or deduced), the one the table pushes."""
    # Living: 1, 3 (me), 4, 6. Dead: 0, 2, 5 villagers. Checked: 6 villager, 4 wolf; 1 is deduced (2 wolves left).
    st = state(SEER, "day_vote", [1, 4, 6], me=3, day=3, alive=[1, 3, 4, 6],
               dead=[(0, "villager", "night_kill", 2), (2, "villager", "vote", 1), (5, "villager", "night_kill", 3)],
               msgs=[(6, "I vote Player4."), (1, "vote Player4")])
    assert WerewolfPolicy.seer_knowledge(parse(st))[0] == [1, 4]
    assert WerewolfPolicy().act(st)["action_id"] == "4"


def test_seer_and_the_villagers_following_it_vote_the_same_sure_wolf():
    """Without table pressure the seer votes the lowest of its sure wolves, the one it names first ("Everyone vote
    PlayerN") and the one villagers following its claim pick: a checked-first order split those votes (llmq k=5, 3 of
    1000 games lost, 10-08)."""
    st = state(SEER, "day_vote", [1, 4, 6], me=3, day=3, alive=[1, 3, 4, 6],
               dead=[(0, "villager", "night_kill", 2), (2, "villager", "vote", 1), (5, "villager", "night_kill", 3)])
    assert WerewolfPolicy().act(st)["action_id"] == "1"
    claim = WerewolfPolicy().chat(st | {"phase": "messaging"})[0]["content"]
    assert claim.startswith("I am the Seer. Player1 is a WOLF. Player4 is a WOLF.") and "Everyone vote Player1" in claim
    follower = state("Your role: VILLAGER", "day_vote", [1, 3, 4], me=6, day=3, alive=[1, 3, 4, 6],
                     dead=[(0, "villager", "night_kill", 2), (2, "villager", "vote", 1),
                           (5, "villager", "night_kill", 3)], msgs=[(3, claim)])
    assert WerewolfPolicy().act(follower)["action_id"] == "1"


def test_villager_joins_the_table_on_the_other_sure_wolf():
    """wolfpack split: a dead revealed seer named Player1; Player5 is an exposed fake seer. Both are sure wolves; the
    table votes Player5. Voting Player1 alone split the vote (no elimination) in 36 of 44 such days."""
    st = state("Your role: VILLAGER\nYou do not know anyone else's role.", "day_vote", [0, 1, 4, 5], me=3, day=2,
               alive=[0, 1, 3, 4, 5],
               dead=[(2, "seer", "night_kill", 2), (6, "villager", "vote", 1)],
               msgs=[(2, "I am the Seer. Player1 is a WOLF.", 1), (5, "I am the real Seer. Player2 is lying.", 1),
                     (0, "Player5 lied about being seer. Vote Player5.", 2), (4, "vote Player5", 2)])
    assert WerewolfPolicy().act(st)["action_id"] == "5"


# ---------------------------------------------------------------- the wolf's tells (detective, talker, chaos)
WOLF = "Your role: WOLF\nYour fellow wolves: Player5"


def test_wolf_follows_a_trusted_claim_onto_its_doomed_ally_like_a_villager():
    """detective / talker: our wolf never voted or followed a seer claim naming its partner (0 of 160), our villagers
    almost always did (772 of 782): the partner's flip then exposed our wolf. Now it votes the ally the table will
    vote out anyway and says so in the villagers' own words."""
    st = state(WOLF, "day_vote", [0, 1, 2, 4, 5, 6], me=3, msgs=[(1, "I am the Seer. Player5 is a wolf.")])
    assert WerewolfPolicy().act(st)["action_id"] == "5"
    chat = WerewolfPolicy().chat(st | {"phase": "messaging"})
    assert [m["content"] for m in chat] == [
        "Following the Seer (Player1): I vote Player5. Everyone vote Player5 so we do not split."]
    villager = state("Your role: VILLAGER", "day_vote", [0, 1, 2, 4, 5, 6], me=3,
                     msgs=[(1, "I am the Seer. Player5 is a wolf.")])
    assert WerewolfPolicy().chat(villager | {"phase": "messaging"}) == chat       # word for word


def test_wolf_named_with_its_ally_still_counter_claims_and_votes_the_claimer():
    claim = (1, "I am the Seer. Player5 is a wolf. Player3 is a wolf.")
    st = state(WOLF, "day_vote", [0, 1, 2, 4, 5, 6], me=3, msgs=[claim])
    chat = WerewolfPolicy().chat(st | {"phase": "messaging"})
    assert [m["content"] for m in chat] == ["I am the Seer. Player1 is a WOLF and is lying about being Seer. Vote Player1."]
    st = state(WOLF, "day_vote", [0, 1, 2, 4, 5, 6], me=3, msgs=[claim, (3, chat[0]["content"])])
    assert WerewolfPolicy().act(st)["action_id"] == "1"


def test_wolf_votes_an_ally_exposed_as_a_fake_seer_or_pressed_by_the_table():
    exposed = state(WOLF, "day_vote", [0, 1, 4, 5, 6], me=3, day=2, alive=[0, 1, 3, 4, 5, 6],
                    dead=[(2, "villager", "night_kill", 2)],
                    msgs=[(5, "I am the Seer. Player2 is a wolf.", 1)])
    assert WerewolfPolicy().act(exposed)["action_id"] == "5"
    pressed = state(WOLF, "day_vote", [0, 1, 2, 4, 5, 6], me=3,
                    msgs=[(0, "Player5 is a wolf, vote Player5."), (1, "vote Player5"), (6, "Player5 is sus")])
    assert WerewolfPolicy().act(pressed)["action_id"] == "5"
    two = state(WOLF, "day_vote", [0, 1, 2, 4, 5, 6], me=3, msgs=[(0, "vote Player5"), (1, "Player5 is sus")])
    assert WerewolfPolicy().act(two)["action_id"] != "5"             # two seats are not the whole table


@pytest.mark.parametrize("role", ["WOLF\nYour fellow wolves: Player5", "VILLAGER", "SEER\nYour investigations: none yet"])
def test_day_one_opening_never_cites_a_vote_record_that_does_not_exist(role):
    """detective: on day 1 our wolves wrote "My read from the vote record" (430 times, villagers and seer 0) whenever
    the table's chat gave them a suspect: a role tell before any vote exists."""
    st = state(f"Your role: {role}", "day_vote", [], me=3, phase="messaging",
               msgs=[(0, "Player2 is acting sus, vote Player2"), (1, "Player2 seems off")])
    chat = WerewolfPolicy().chat(st)
    assert len(chat) == 1 and chat[0]["content"].startswith("No hard info yet. To avoid a split vote I'll vote Player")
    later = state(f"Your role: {role}", "day_vote", [], me=3, day=2, phase="messaging", alive=[0, 1, 2, 3, 5, 6],
                  dead=[(4, "villager", "vote", 1)], votes=[(1, {0: 4, 1: 4, 2: 4, 3: 4, 5: 2, 6: 2}, 4)])
    assert WerewolfPolicy().chat(later)[0]["content"].startswith("My read from the vote record: Player")


# ---------------------------------------------------------------- frames and claim fights (wolfpack, talker, chaos)
def test_a_framed_villager_says_the_claimer_lies_and_votes_it():
    """wolfpack: a fake seer named our villager; it voted the claimer (108 of 108) but never said a word (1007 of 1007
    framed days) and was voted out 56% of those days."""
    st = state("Your role: VILLAGER", "day_vote", [0, 1, 2, 4, 5, 6], me=3, msgs=[(1, "I am the Seer. Player3 is a wolf.")])
    assert WerewolfPolicy().act(st)["action_id"] == "1"
    chat = WerewolfPolicy().chat(st | {"phase": "messaging"})
    assert [m["content"] for m in chat] == ["Player1 is lying: I am not a wolf, so Player1 is a fake Seer. Vote Player1."]


def test_a_claimant_calling_the_other_a_wolf_is_not_evidence():
    """chaos2 / talker: when a true seer named a wolf, the OTHER wolf claimed seer at once and called the true seer a
    wolf; we voted the claimant the other one accused (the true seer) every time. Neither claimed reactively here, so
    the tie goes to the lower seat, whoever accused whom."""
    for a, b in ((1, 5), (5, 1)):
        st = state("Your role: VILLAGER", "day_vote", [0, 1, 2, 4, 5, 6], me=3,
                   msgs=[(a, "I am the Seer. Player2 is a wolf."), (b, f"I am the Seer. Player{a} is a wolf.")])
        assert WerewolfPolicy().act(st)["action_id"] == "1"


def test_a_claimant_whose_named_wolf_flipped_wolf_is_believed_over_the_other():
    """Two live claimants; the wolf one of them named was voted out and revealed a wolf: vote the other one."""
    st = state("Your role: VILLAGER", "day_vote", [0, 1, 4, 5, 6], me=3, day=2, alive=[0, 1, 3, 4, 5, 6],
               dead=[(2, "wolf", "vote", 1)], votes=[(1, {0: 2, 1: 2, 2: 1, 3: 2, 4: 2, 5: 1, 6: 2}, 2)],
               msgs=[(1, "I am the Seer. Player2 is a wolf.", 1), (5, "I am the Seer. Player1 is a wolf.", 1)])
    assert WerewolfPolicy().act(st)["action_id"] == "5"


def test_wolf_votes_its_ally_in_the_allys_claim_fight_and_says_it_like_a_villager():
    """talker / chaos: in a claim fight between our wolf's ally and the true seer, our wolf voted the true seer; the
    reveal then exposed the ally and our wolf with it. Now it votes the ally, in the villagers' claim-fight words."""
    msgs = [(1, "I am the Seer. Player5 is a WOLF."), (5, "I am the Seer. Player1 is a WOLF and is lying.")]
    st = state(WOLF, "day_vote", [0, 1, 2, 4, 5, 6], me=3, msgs=msgs)
    assert WerewolfPolicy().act(st)["action_id"] == "5"
    chat = WerewolfPolicy().chat(st | {"phase": "messaging"})
    want = "Two Seer claims: Player1 and Player5. One is lying. I vote Player5; the reveal will show who lied."
    assert [m["content"] for m in chat] == [want]
    villager = state("Your role: VILLAGER", "day_vote", [0, 1, 2, 4, 5, 6], me=3, msgs=msgs)
    assert [m["content"] for m in WerewolfPolicy().chat(villager | {"phase": "messaging"})] == [want]
