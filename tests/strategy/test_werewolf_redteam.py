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


def test_seer_votes_its_checked_wolf_the_table_is_on_over_a_deduced_one():
    """chaos3 seed 2407 game 125: on stall 2 the seer voted a deduced wolf while the table voted its checked wolf; the
    tie gave the wolves the game. A checked wolf comes first; among sure wolves, the one the table pushes."""
    # Living: 1, 3 (me), 4, 6. Dead: 0, 2, 5 villagers. Checked: 6 villager, 4 wolf; 1 is deduced (2 wolves left).
    st = state(SEER, "day_vote", [1, 4, 6], me=3, day=3, alive=[1, 3, 4, 6],
               dead=[(0, "villager", "night_kill", 2), (2, "villager", "vote", 1), (5, "villager", "night_kill", 3)],
               msgs=[(6, "I vote Player4."), (1, "vote Player4")])
    v = parse(st)
    assert set(WerewolfPolicy.seer_wolves(v)) == {1, 4} and WerewolfPolicy.seer_wolves(v)[0] == 4
    assert WerewolfPolicy().act(st)["action_id"] == "4"


def test_villager_joins_the_table_on_the_other_sure_wolf():
    """wolfpack split: a dead revealed seer named Player1; Player5 is an exposed fake seer. Both are sure wolves; the
    table votes Player5. Voting Player1 alone split the vote (no elimination) in 36 of 44 such days."""
    st = state("Your role: VILLAGER\nYou do not know anyone else's role.", "day_vote", [0, 1, 4, 5], me=3, day=2,
               alive=[0, 1, 3, 4, 5],
               dead=[(2, "seer", "night_kill", 2), (6, "villager", "vote", 1)],
               msgs=[(2, "I am the Seer. Player1 is a WOLF.", 1), (5, "I am the real Seer. Player2 is lying.", 1),
                     (0, "Player5 lied about being seer. Vote Player5.", 2), (4, "vote Player5", 2)])
    assert WerewolfPolicy().act(st)["action_id"] == "5"
