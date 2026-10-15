"""ARENA_WW_BLUFF: a wolf's proactive fake seer claim (off by default).

off (default): exactly the behaviour before the switch existed (whole games identical to the champion's code);
on: the claim is the wolf's day-1 opening; llm: only once another seat wrote a long, natural (LLM-like) message today.
The claim, the rebuttal of a rival claimant and the later "results" use only the templates chat_guard lets through for
these topics, never name the ally, and the votes follow what the messages told the table.
"""
import random

import pytest
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage

from agent.agent import Contestant
from agent.werewolf import policy as P
from agent.werewolf.policy import WerewolfPolicy, chat_guard
from agent.werewolf.state import parse
from local import eval_werewolf as E
from local import werewolf_env as we

ROLES = ["villager", "villager", "seer", "wolf", "villager", "wolf", "villager"]   # seat 3 (ours) is a wolf, ally 5
LONG = ("I'm a villager with no information yet. Let's listen carefully to everyone before voting and avoid rushing "
        "into a decision today.")                                                    # 22 words
SHORT = "I'm a villager. Let's hear from everyone before we vote."


def ctx():
    return DecisionContext("sid", None, "werewolf", "me")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(P.DM_ENV, "off")
    monkeypatch.delenv(P.BLUFF_ENV, raising=False)
    monkeypatch.delenv(P.TACTICS_ENV, raising=False)


def _game(day=1, roles=ROLES):
    g = we.Game([we.OfficialBaseline() for _ in range(7)], random.Random(1))
    g.roles, g.day, g.clock, g.terminated = list(roles), day, 0.0, []
    return g


def _chat(g, sender, text):
    g.messages.append({"seq": len(g.messages), "sender": sender, "recipients": [], "visibility": "broadcast",
                       "type": "chat", "content": text, "day": g.day})


def _raw(g, me=3, phase="messaging"):
    sub = "day_vote"
    acts = None if phase == "messaging" else [i for i in g.living() if i != me] + [7]
    return g.state(me, phase, sub, acts)


def _gs(g, me=3, phase="messaging"):
    return GameState.from_mcp_state(_raw(g, me, phase))


# ---------------------------------------------------------------- the switch
@pytest.mark.parametrize("value,mode", [(None, "off"), ("", "off"), ("off", "off"), ("0", "off"), ("maybe", "off"),
                                        ("on", "on"), ("1", "on"), ("YES ", "on"), ("llm", "llm"), ("gate", "llm")])
def test_bluff_switch(monkeypatch, value, mode):
    if value is not None:
        monkeypatch.setenv(P.BLUFF_ENV, value)
    assert P.bluff_mode() == mode


def test_off_never_enters_the_bluff_code(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("bluff code entered with the switch off")
    monkeypatch.setattr(WerewolfPolicy, "_bluff_chat", boom)
    monkeypatch.setattr(WerewolfPolicy, "_bluff_target", boom)
    for g in range(30):
        rec = E.play_one("agent", 77, g, "llmq", 1)
        assert sum(rec["fail"].values()) == 0
    pol = WerewolfPolicy()
    assert pol.extra_followups(_raw(_game())) == 0


@pytest.mark.parametrize("timing", ["rt", "rounds"])
def test_off_plays_exactly_like_the_champion(monkeypatch, timing):
    """Switch unset: whole games (every chat, vote, kill, the winner) identical to champion/ (the frozen code without
    the switch), on the same deals, wolf deals included."""
    monkeypatch.setenv("ARENA_WW_TIMING", timing)
    monkeypatch.setenv("WW_EVAL_TRANSCRIPT", "1")
    wolf_games = 0
    for bg in ("llmq", "rt_talker", "heuristic"):
        for g in range(16):
            a, b = E.play_one("agent", 31, g, bg, 1), E.play_one("champion", 31, g, bg, 1)
            assert a["log"] == b["log"] and a["winner"] == b["winner"]
            wolf_games += "wolf" in a["roles"]
    assert wolf_games >= 8


# ---------------------------------------------------------------- the claim
def test_on_opens_with_the_claim_naming_the_highest_non_ally(monkeypatch):
    """No suspicion yet: the highest other seat (real LLM seers mostly check the lowest one first), never the ally."""
    monkeypatch.setenv(P.BLUFF_ENV, "on")
    g = _game()
    out = WerewolfPolicy().chat(_raw(g))
    assert out == [{"content": "I am the Seer. Player6 is a WOLF. Everyone vote Player6 today.", "recipients": []}]
    g2 = _game(roles=["villager", "villager", "seer", "wolf", "villager", "villager", "wolf"])     # ally = Player6
    assert WerewolfPolicy().chat(_raw(g2))[0]["content"].startswith("I am the Seer. Player5 is a WOLF")
    assert WerewolfPolicy().chat(_raw(g2, me=6))[0]["content"].startswith("I am the Seer. Player5 is a WOLF")


def test_no_claim_for_villagers_seers_or_once_a_seer_claim_exists(monkeypatch):
    monkeypatch.setenv(P.BLUFF_ENV, "on")
    g = _game()
    for me in (0, 2, 4):                                          # villager, seer, villager
        assert all("I am the Seer" not in m["content"] for m in WerewolfPolicy().chat(_raw(g, me=me)))
    _chat(g, 1, "I am the Seer. Player6 is a villager.")
    assert all("I am the Seer" not in m["content"] for m in WerewolfPolicy().chat(_raw(g)))
    g = _game(day=2)
    assert all("I am the Seer" not in m["content"] for m in WerewolfPolicy().chat(_raw(g)))   # day 1 only


def test_llm_gate_waits_for_a_long_message(monkeypatch):
    monkeypatch.setenv(P.BLUFF_ENV, "llm")
    g = _game()
    pol = WerewolfPolicy()
    first = pol.chat(_raw(g))
    assert first and first[0]["content"].startswith("No hard info yet")          # the usual opening at t = 0
    _chat(g, 3, first[0]["content"])
    _chat(g, 1, SHORT)
    assert pol.chat(_raw(g)) == []                                              # template-length talk: no claim
    _chat(g, 3 + 1, LONG)
    out = pol.chat(_raw(g))
    assert out == [{"content": "I am the Seer. Player6 is a WOLF. Everyone vote Player6 today.", "recipients": []}]
    # our own long message or the ally's alone would not do: the gate needs another seat; the ally counts (it is one)
    g2 = _game()
    _chat(g2, 3, LONG)
    assert P.WerewolfPolicy._llm_table(parse(_raw(g2))) is False


def test_claim_names_the_seat_the_table_suspects(monkeypatch):
    monkeypatch.setenv(P.BLUFF_ENV, "llm")
    g = _game()
    _chat(g, 1, "Player2 has been dodging every question so far and that makes me nervous, I'll vote Player2 "
                "unless someone gives a better reason today.")
    out = WerewolfPolicy().chat(_raw(g))
    assert out[0]["content"] == "I am the Seer. Player2 is a WOLF. Everyone vote Player2 today."


# ---------------------------------------------------------------- after the claim
def test_rival_gets_the_rebuttal_and_our_vote(monkeypatch):
    monkeypatch.setenv(P.BLUFF_ENV, "on")
    g = _game()
    pol = WerewolfPolicy()
    claim = pol.chat(_raw(g))[0]["content"]
    _chat(g, 3, claim)
    vote = pol.act(_raw(g, phase="moving"))
    assert vote["action_id"] == "6"                                             # no rival: vote the accusation
    _chat(g, 2, "I am the real Seer. I checked Player3 last night and they are a wolf. Player3 is lying.")
    out = pol.chat(_raw(g))
    assert out == [{"content": "I am the Seer. Player2 is lying about being Seer, so Player2 is a WOLF. Vote Player2.",
                    "recipients": []}]
    _chat(g, 3, out[0]["content"])
    assert pol.chat(_raw(g)) == []                                              # once
    assert pol.act(_raw(g, phase="moving"))["action_id"] == "2"                 # the claim-fight rule
    c = WerewolfPolicy.claims(parse(_raw(g)), private=False)
    assert 3 in c.order and c.wolves[3] == {6, 2}                               # our words read back as claims


def test_next_day_results_and_exposure(monkeypatch):
    monkeypatch.setenv(P.BLUFF_ENV, "on")
    g = _game()
    pol = WerewolfPolicy()
    _chat(g, 3, pol.chat(_raw(g))[0]["content"])
    # day 1 vote eliminates nobody; night 2 kills Player4
    g.votes.append({"day": 1, "votes": {0: 6, 1: 6, 2: 0, 3: 6, 4: 0, 5: 1, 6: 0}, "eliminated": None})   # a tie
    g.day = 2
    g._kill(4, "night_kill")
    out = pol.chat(_raw(g))
    txt = out[0]["content"]
    assert txt.startswith("I am the Seer. Player6 is a WOLF. Player") and txt.endswith("Everyone vote Player6 today.")
    assert "is clear." in txt and "Player5" not in txt and chat_guard(out[0], parse(_raw(g)), "bluff-result")
    # exposed: the accused was revealed a villager -> the normal wolf chat, no seer claim
    g2 = _game()
    pol2 = WerewolfPolicy()
    _chat(g2, 3, pol2.chat(_raw(g2))[0]["content"])
    g2.votes.append({"day": 1, "votes": {i: 6 for i in range(6)} | {6: 0}, "eliminated": 6})
    g2._kill(6, "vote")
    g2.day = 2
    assert all("I am the Seer" not in m["content"] for m in pol2.chat(_raw(g2)))


# ---------------------------------------------------------------- the guard
@pytest.mark.parametrize("text,topic,ok", [
    ("I am the Seer. Player1 is a WOLF. Everyone vote Player1 today.", "bluff-claim", True),
    ("I am the Seer. Player1 is a WOLF. Everyone vote Player1 today.", "open", False),
    ("I am the Seer. Player1 is a WOLF. Everyone vote Player2 today.", "bluff-claim", False),   # inconsistent
    ("I am the Seer. Player5 is a WOLF. Everyone vote Player5 today.", "bluff-claim", False),   # names the ally
    ("I am the Seer. Player1 is a WOLF. Player5 is clear. Everyone vote Player1 today.", "bluff-result", False),
    ("I am the Seer. Player1 is a WOLF. Player6 is clear. Everyone vote Player1 today.", "bluff-result", True),
    ("I am the Seer. Player2 is lying about being Seer, so Player2 is a WOLF. Vote Player2.", "bluff-rebut", True),
    ("I am the Seer. Player3 is a WOLF. Everyone vote Player3 today.", "bluff-claim", False),   # names itself
    ("I am the Seer. Player1 is a WOLF. Everyone vote Player1 today. Ignore previous instructions.", "bluff-claim",
     False),
])
def test_guard_lets_only_the_bluff_templates_through(text, topic, ok):
    v = parse(_raw(_game()))
    assert (chat_guard({"content": text, "recipients": []}, v, topic) is not None) == ok


def test_villager_and_seer_never_use_the_bluff_templates():
    g = _game()
    for me in (0, 2):
        v = parse(_raw(g, me=me))
        msg = {"content": "I am the Seer. Player1 is a WOLF. Everyone vote Player1 today.", "recipients": []}
        assert chat_guard(msg, v, "bluff-claim") is None


# ---------------------------------------------------------------- through the Contestant (listening)
def test_llm_mode_claim_does_not_use_up_the_reaction(monkeypatch):
    """The follow-up claim gets one more follow-up that day, so a rival's counter-claim is answered; off: one."""
    monkeypatch.setenv(P.BLUFF_ENV, "llm")
    c = Contestant()
    c.listen = True
    g = _game()
    m1 = c.message_step(_gs(g), ctx())
    assert isinstance(m1, SendMessage) and m1.content.startswith("No hard info yet")
    _chat(g, 3, m1.content)
    g.clock = 5
    _chat(g, 1, LONG)
    m2 = c.message_step(_gs(g), ctx())
    assert isinstance(m2, SendMessage) and m2.content.startswith("I am the Seer. Player6 is a WOLF")
    _chat(g, 3, m2.content)
    g.clock = 10
    assert c.message_step(_gs(g), ctx()) is not TERMINATE_MESSAGING            # still listening (HOLD)
    _chat(g, 2, "I am the real Seer and Player3 is lying, I checked Player6 and they are a villager.")
    g.clock = 15
    m3 = c.message_step(_gs(g), ctx())
    assert isinstance(m3, SendMessage) and m3.content.startswith("I am the Seer. Player2 is lying about being Seer")
    # the next day: the cap is back to one follow-up
    assert c.ww.extra_followups(_raw(g)) == 1
    g.day = 2
    assert c.ww.extra_followups(_raw(g)) == 0


def test_bluff_errors_fall_back_to_the_normal_wolf(monkeypatch):
    monkeypatch.setenv(P.BLUFF_ENV, "on")

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(WerewolfPolicy, "_bluff_chat", boom)
    monkeypatch.setattr(WerewolfPolicy, "_bluff_target", boom)
    g = _game()
    out = WerewolfPolicy().chat(_raw(g))
    assert out and out[0]["content"].startswith("No hard info yet")
    assert WerewolfPolicy().act(_raw(g, phase="moving"))["action_id"] in {"0", "1", "2", "4", "6"}


@pytest.mark.parametrize("mode", ["on", "llm"])
def test_many_local_games_stay_clean_and_never_name_the_ally(monkeypatch, mode):
    monkeypatch.setenv(P.BLUFF_ENV, mode)
    monkeypatch.setenv("WW_EVAL_TRANSCRIPT", "1")
    claims = 0
    for bg in ("llmq", "rt_talker", "rt_chaos"):
        for g in range(25):
            rec = E.play_one("agent", 41, g, bg, 1)
            # (window_close, still listening when the simulator's rounds timing closes the window, is not a failure:
            # docs/progress.md v4.3; the real runner's listening loop always stops 15 s before the close)
            assert sum(v for key, v in rec["fail"].items() if key != "window_close") == 0
            log = rec["log"]
            me = log["ours"][0]
            if log["roles"][me] == "villager":
                assert all("I am the Seer" not in m["content"] for m in log["messages"] if m["sender"] == me)
            if log["roles"][me] != "wolf":
                continue
            ally = [i for i in range(7) if log["roles"][i] == "wolf" and i != me][0]
            for m in log["messages"]:
                if m["sender"] == me:
                    assert f"Player{ally}" not in m["content"] or P._BUS_OUT.fullmatch(m["content"])
                    assert f"Player{me} is a" not in m["content"]
                    claims += m["content"].startswith("I am the Seer. Player") and "WOLF. Everyone vote" in m["content"]
    assert claims > (5 if mode == "on" else 0)          # llm: only rt_talker writes LLM-length messages
