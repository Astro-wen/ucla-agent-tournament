"""Seer targeting / claiming and villager voting (agent/werewolf/policy.py)."""
from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import parse

NAMES = ["alice", "bob", "carl", "dave", "erin", "fran", "gus"]
PLAYERS = [{"position": i, "name": n, "agent_id": f"id{i}"} for i, n in enumerate(NAMES)]


def state(role_block, sub_phase, actions, me=3, day=1, alive=range(7), dead=(), votes=(), msgs=()):
    gs = {"day": day, "phase": sub_phase, "alive": [NAMES[i] for i in alive],
          "dead": [{"day": d, "player": NAMES[s], "role": r, "cause": c} for s, r, c, d in dead],
          "vote_history": [{"day": d, "votes": {NAMES[a]: (NAMES[b] if b is not None else "abstain") for a, b in vv.items()},
                            "eliminated": NAMES[e] if e is not None else None} for d, vv, e in votes],
          "winner": None}
    return {"observation": f"=== Werewolf ===\n{role_block}\nDay {day} | phase {sub_phase}",
            "your_position": me, "players": PLAYERS, "game_state": gs, "phase": "moving",
            "legal_actions": {"actions": [{"action_id": str(a)} for a in actions]} if actions else None,
            "new_messages": [{"seq": i, "sender": s, "recipients": [], "visibility": "broadcast", "type": "chat",
                              "content": t} for i, (s, t) in enumerate(msgs)]}


def test_seer_skips_predicted_lynch_and_never_rereads():
    # No information: the table of copies lynches the lowest seat (0) — don't spend the look there.
    st = state("Your role: SEER\nYour investigations: none yet", "night_seer", [0, 1, 2, 4, 5, 6])
    pol = WerewolfPolicy()
    assert WerewolfPolicy.predicted_lynch(parse(st)) == 0
    pick = int(pol.act(st)["action_id"])
    assert pick != 0 and pick != 3
    st2 = state(f"Your role: SEER\nYour investigations: Player{pick} is villager", "night_seer", [0, 1, 2, 4, 5, 6], day=2)
    assert int(WerewolfPolicy().act(st2)["action_id"]) not in (pick, 0)


def test_seer_claims_wolf_unambiguously():
    st = state("Your role: SEER\nYour investigations: Player6 is wolf, Player1 is villager", "day_vote", [])
    out = WerewolfPolicy().chat(st)
    assert out[0]["content"].startswith("I am the Seer. Player6 is a WOLF.")
    assert "Player1 is clear" in out[0]["content"]
    assert len(out) <= 5 and all(len(m["content"].split()) <= 50 for m in out)


def test_seer_deduces_wolves_by_elimination():
    # 4 alive: me(3), 1 and 5 cleared, one wolf dead -> Player6 must be the last wolf.
    st = state("Your role: SEER\nYour investigations: Player1 is villager, Player5 is villager", "day_vote",
               [1, 5, 6, 7], day=3, alive=[1, 3, 5, 6],
               dead=[(0, "wolf", "lynch", 1), (2, "villager", "night_kill", 2), (4, "villager", "lynch", 2)])
    assert WerewolfPolicy().act(st)["action_id"] == "6"
    assert "Player6 is a WOLF" in WerewolfPolicy().chat(st)[0]["content"]


def test_seer_claims_clears_when_a_clear_is_about_to_be_lynched():
    st = state("Your role: SEER\nYour investigations: Player0 is villager, Player5 is villager", "day_vote", [],
               day=2, alive=[0, 1, 3, 5, 6], dead=[(2, "villager", "lynch", 1), (4, "villager", "night_kill", 2)])
    out = WerewolfPolicy().chat(st)
    assert out and out[0]["content"].startswith("I am the Seer.") and "Player0 is clear" in out[0]["content"]


def test_villager_follows_uncontested_claim_over_vote_record():
    msgs = [(2, "I am the Seer. Player6 is a WOLF. Player0 is clear.")]
    votes = [(1, {0: 1, 1: 0, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1}, None)]
    st = state("Your role: VILLAGER", "day_vote", [0, 1, 2, 4, 5, 6, 7], day=1, votes=votes, msgs=msgs)
    assert WerewolfPolicy().act(st)["action_id"] == "6"
    chat = WerewolfPolicy().chat(st)[0]["content"]
    assert "Player6" in chat and "I am the" not in chat


def test_villager_never_votes_trusted_clear_and_never_abstains():
    msgs = [(2, "I am the Seer. Player0 is clear. Player1 is clear.")]
    st = state("Your role: VILLAGER", "day_vote", [0, 1, 2, 4, 5, 6, 7], msgs=msgs)
    pick = WerewolfPolicy().act(st)["action_id"]
    assert pick not in ("0", "1", "2", "7")


def test_claim_accusing_me_is_a_lie():
    msgs = [(5, "I am the Seer. Player3 is a wolf.")]
    st = state("Your role: VILLAGER", "day_vote", [0, 1, 2, 4, 5, 6, 7], msgs=msgs)
    assert WerewolfPolicy().act(st)["action_id"] == "5"


def test_confirmed_dead_seer_exposes_counterclaimer():
    msgs = [(1, "I am the Seer. Player4 is a wolf."), (2, "I am the seer. Player1 is a wolf.")]
    st = state("Your role: VILLAGER", "day_vote", [0, 2, 4, 5, 6, 7], day=2, alive=[0, 2, 3, 4, 5, 6],
               dead=[(1, "seer", "night_kill", 2)], msgs=msgs)
    pol = WerewolfPolicy()
    c = pol.claims(parse(st, [m for m in st["new_messages"]]))
    assert c.trusted == 1 and 2 in c.liars
    assert pol.act(st)["action_id"] == "4"      # the true seer's wolf first; the liar is next


def test_seer_votes_fake_seer():
    msgs = [(5, "I am the Seer. Player1 is a wolf.")]
    st = state("Your role: SEER\nYour investigations: Player0 is villager", "day_vote", [0, 1, 2, 4, 5, 6, 7], msgs=msgs)
    assert WerewolfPolicy().act(st)["action_id"] == "5"
    assert "Player5 is a WOLF" in WerewolfPolicy().chat(st)[0]["content"]


def test_malformed_payloads_never_raise():
    pol = WerewolfPolicy()
    bad = {"observation": None, "players": [None, {"name": "x"}], "game_state": {"dead": [None], "vote_history": [None]},
           "legal_actions": {"actions": [{"action_id": "7"}, {"action_id": "2"}]}, "new_messages": [None, {"seq": 1}]}
    assert pol.act(bad)["action_id"] == "2"
    assert isinstance(pol.chat(bad), list)
    assert pol.act({"legal_actions": {"actions": [{"action_id": "7"}]}})["action_id"] == "7"
