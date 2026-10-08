"""Werewolf listening (ARENA_WW_LISTEN): keep the discussion window open after the opening chat, read it through the
runner's own session, send at most one follow-up a day, and terminate before any limit.

The runner asks choose_message after every change while a window is open and needs a chat or a terminate each time;
without listening we terminate at the first call with nothing to say (usually right after the first other seat
spoke), so a seer claim or an accusation made later can no longer be answered. Listening holds that terminate,
reading with read-only wait_for_update calls on the runner's session (found on the call stack), until there is
something to answer or a stop rule fires: every other living seat terminated, the window about to close, a quiet
spell, the follow-up used, the quota used. The vote reads everything heard either way.
"""
import contextlib
import io
import random

import pytest
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage
from altruagent.mcp_transport import MCPToolError
from altruagent.runner import run_game

import agent.agent as A
from agent.agent import HOLD, Contestant, create_agent, listen_enabled
from agent.werewolf import policy as P
from local import werewolf_env as we
from local.werewolf_bots import LLMQwenSeat
from local.werewolf_platform import SeatAgent, play_table

ROLES = ["villager", "villager", "seer", "wolf", "villager", "wolf", "villager"]   # seat 3 (ours) is a wolf, ally 5


def ctx():
    return DecisionContext("sid", None, "werewolf", "me")


def _game(roles=ROLES, clock=0.0):
    g = we.Game([we.OfficialBaseline() for _ in range(7)], random.Random(1))
    g.roles = list(roles)
    g.clock = clock
    g.terminated = []
    return g


def _chat(g, sender, text):
    g.messages.append({"seq": len(g.messages), "sender": sender, "recipients": [], "visibility": "broadcast",
                       "type": "chat", "content": text, "day": g.day})


def _gs(g, me=3, phase="messaging"):
    return GameState.from_mcp_state(g.state(me, phase, "day_vote", None if phase == "messaging" else [0, 1, 2, 4, 5, 6, 7]))


def _listener(on=True):
    c = Contestant()
    c.listen = on
    return c


@pytest.fixture(autouse=True)
def _no_dm(monkeypatch):
    monkeypatch.setenv(P.DM_ENV, "off")


# ---------------------------------------------------------------- the switch
@pytest.mark.parametrize("value,on", [("on", True), ("ON ", True), ("yes", True), ("1", True), ("off", False),
                                      ("0", False), ("no", False), ("", A.LISTEN_DEFAULT == "on")])
def test_listen_switch(value, on):
    assert listen_enabled({A.LISTEN_ENV: value}) is on
    assert listen_enabled({}) is (A.LISTEN_DEFAULT == "on")


# ---------------------------------------------------------------- the decision step
def test_off_never_holds():
    c, g = _listener(False), _game()
    first = c.message_step(_gs(g), ctx())
    assert isinstance(first, SendMessage)
    _chat(g, 3, first.content)
    g.clock = 5
    _chat(g, 1, "Is the Seer here? Please speak up before we vote.")
    assert c.message_step(_gs(g), ctx()) is TERMINATE_MESSAGING


def test_holds_after_the_opening_while_others_still_talk():
    c, g = _listener(), _game()
    first = c.message_step(_gs(g), ctx())
    assert isinstance(first, SendMessage)
    _chat(g, 3, first.content)
    g.clock = 5
    _chat(g, 1, "Is the Seer here? Please speak up before we vote.")
    assert c.message_step(_gs(g), ctx()) is HOLD
    assert not c._chat_done


def _opened(c, g):
    first = c.message_step(_gs(g), ctx())
    _chat(g, 3, first.content)
    return first


@pytest.mark.parametrize("change", ["others_done", "closing", "eliminated", "moving", "listed_terminated", "quota"])
def test_stop_rules(change):
    c, g = _listener(), _game()
    _opened(c, g)
    g.clock = 10
    raw = g.state(3, "messaging", "day_vote")
    assert c.message_step(GameState.from_mcp_state(raw), ctx()) is HOLD
    if change == "others_done":
        raw["terminated_messaging"] = [0, 1, 2, 4, 5, 6]
    elif change == "closing":
        raw["messaging_seconds_left"] = 14                  # the margin is W/8 = 15 s of a 120 s window
    elif change == "eliminated":
        raw["eliminated"] = True
    elif change == "moving":
        raw["phase"] = "moving"
    elif change == "listed_terminated":
        raw["terminated_messaging"] = [3]
    else:
        c._chat_sent = A.MAX_CHATS_PER_WINDOW
    assert c.message_step(GameState.from_mcp_state(raw), ctx()) is TERMINATE_MESSAGING
    assert c._chat_done
    _chat(g, 2, "I am the Seer. Player3 is a wolf.")      # done for the day: never a chat or a hold again
    assert c.message_step(_gs(g), ctx()) is TERMINATE_MESSAGING


def test_quiet_spell_ends_listening_and_a_new_chat_restarts_it():
    c, g = _listener(), _game()
    _opened(c, g)                                         # first sight at 120 s left: quiet = W/5 = 24 s
    g.clock = 5
    _chat(g, 1, "Is the Seer here? Please speak up before we vote.")
    assert c.message_step(_gs(g), ctx()) is HOLD          # last chat by another seat at 115 s left
    g.clock = 25
    assert c.message_step(_gs(g), ctx()) is HOLD          # 20 s quiet
    g.clock = 26
    _chat(g, 4, "Reminder: all players should vote, abstaining helps the wolves.")
    assert c.message_step(_gs(g), ctx()) is HOLD          # a new chat restarts the quiet clock
    g.clock = 49
    assert c.message_step(_gs(g), ctx()) is HOLD          # 23 s
    g.clock = 51
    assert c.message_step(_gs(g), ctx()) is TERMINATE_MESSAGING


def test_a_countdown_that_stops_moving_cannot_keep_us_listening():
    c, g = _listener(), _game(clock=20.0)                  # the state says 100 s left, and keeps saying it
    now = [0.0]
    c._clock = lambda: now[0]
    _opened(c, g)
    for t, out in ((30.0, HOLD), (60.0, HOLD), (90.0, TERMINATE_MESSAGING)):
        now[0] = t
        _chat(g, 1 + int(t) % 5, f"Reminder {t}: all players should vote, abstaining helps the wolves.")
        assert c.message_step(_gs(g), ctx()) is out


def test_one_follow_up_a_day_then_terminate():
    c, g = _listener(), _game()
    _opened(c, g)
    g.clock = 30
    _chat(g, 2, "I am the Seer. Player3 is a wolf.")
    counter = c.message_step(_gs(g), ctx())
    assert isinstance(counter, SendMessage) and counter.content.startswith("I am the Seer. Player2 is a WOLF")
    _chat(g, 3, counter.content)
    g.clock = 40
    _chat(g, 6, "I am the Seer. Player3 is a wolf. Vote Player3.")
    assert c.message_step(_gs(g), ctx()) is TERMINATE_MESSAGING
    assert c.listen_stats["followups"] == 1
    g.day = 2                                             # a new day: a new opening and a new follow-up
    g.messages, g.window_start_seq, g.clock = g.messages, len(g.messages), 0
    assert isinstance(c.message_step(_gs(g), ctx()), SendMessage)


def test_follow_up_cap_survives_a_restart():
    c, g = _listener(), _game()
    _opened(c, g)
    g.clock = 30
    _chat(g, 2, "I am the Seer. Player3 is a wolf.")
    follow_up = c.message_step(_gs(g), ctx())
    assert isinstance(follow_up, SendMessage)
    _chat(g, 3, follow_up.content)                        # it reached the server (the next state shows it)
    saved = c._export("werewolf", None)
    d = _listener()
    d._import(saved)
    g.clock = 40
    _chat(g, 6, "I am the Seer. Player3 is a wolf. Vote Player3.")
    assert d.message_step(_gs(g), ctx()) is TERMINATE_MESSAGING


def test_a_follow_up_that_never_landed_is_not_counted_across_a_restart():
    """v4.2.4: the follow-up's send failed (the runner re-reads instead of ending the match) and the worker restarted
    from the state saved with it in flight: the state it is asked with does not show it, so it is not counted (the
    follow-up budget, the quota) and is sent again, once."""
    c, g = _listener(), _game()
    _opened(c, g)
    g.clock = 30
    _chat(g, 2, "I am the Seer. Player3 is a wolf.")
    follow_up = c.message_step(_gs(g), ctx())
    assert isinstance(follow_up, SendMessage)
    d = _listener()
    d._import(c._export("werewolf", None))                # saved with the follow-up counted and in flight
    g.clock = 35
    again = d.message_step(_gs(g), ctx())
    assert isinstance(again, SendMessage) and again.content == follow_up.content
    assert d.undelivered == 1 and d._followups == 1 and d._chat_sent == 2
    _chat(g, 3, again.content)
    g.clock = 40
    _chat(g, 6, "I am the Seer. Player3 is a wolf. Vote Player3.")
    assert d.message_step(_gs(g), ctx()) is TERMINATE_MESSAGING   # the budget is used now: no third chat


def test_without_a_runner_session_listening_ends_in_a_terminate():
    c, g = _listener(), _game()
    first = c.choose_message(_gs(g), ctx())
    assert isinstance(first, SendMessage)
    _chat(g, 3, first.content)
    g.clock = 5
    _chat(g, 1, "Is the Seer here? Please speak up before we vote.")
    out = c.choose_message(_gs(g), ctx())                 # no altruagent.runner.run_game on the stack
    assert out is TERMINATE_MESSAGING and c.listen_stats["no_game"] == 1
    g.clock = 30
    _chat(g, 2, "I am the Seer. Player3 is a wolf.")
    assert c.choose_message(_gs(g), ctx()) is TERMINATE_MESSAGING     # terminated: never a chat afterwards


# ---------------------------------------------------------------- through the real runner
class ScriptedGame:
    """The MCPGameSession surface run_game uses, over a werewolf_env Game seen from seat `me`: every wait_for_update
    applies the next scripted event (another seat's chat or terminate, the clock moving) and returns the new state;
    send_message follows the platform (WRONG_PHASE outside the window, the window closes when every living seat has
    terminated); the vote ends the game."""

    def __init__(self, g, me, events, fail_wait=None):
        self.g, self.me, self.events = g, me, list(events)
        self.phase, self.version, self.voted = "messaging", 1, None
        self.sends, self.waits, self.fail_wait = [], [], fail_wait
        self.term_clock = None                            # the window's clock when our first terminate arrived
        self.inside = None                                # set by the test: the contestant, to see if it was listening

    def _raw(self):
        if self.voted is not None:
            raw = self.g.state(self.me, "moving", None)
            raw.update(is_terminal=True, status="completed", legal_actions=None, is_current_actor=False)
        elif self.phase == "messaging":
            raw = self.g.state(self.me, "messaging", "day_vote")
        else:
            raw = self.g.state(self.me, "moving", "day_vote", [s for s in self.g.living() if s != self.me] + [7])
            raw["new_messages"] = []
        raw["state_version"] = self.version
        if raw.get("legal_actions"):
            raw["legal_actions"]["state_version"] = self.version
        return raw

    def get_state(self):
        return GameState.from_mcp_state(self._raw())

    def wait_for_update(self, **kw):
        self.waits.append(dict(kw, listening=bool(self.inside and self.inside._listening_now)))
        if self.fail_wait and self.inside and self.inside._listening_now:
            raise self.fail_wait
        if self.events:
            self.events.pop(0)(self)
            self.version += 1
        return self.get_state()

    def get_legal_actions(self):
        return self._raw().get("legal_actions") or {"actions": [], "state_version": self.version}

    def send_message(self, *, message_type, content=None, recipients=None):
        if self.phase != "messaging":
            raise MCPToolError("WRONG_PHASE: window closed", status_code=None, error_code="WRONG_PHASE")
        self.sends.append((message_type, content))
        if message_type == "chat":
            assert self.me not in self.g.terminated, "a chat after our terminate"
            _chat(self.g, self.me, content)
        elif self.me not in self.g.terminated:
            self.g.terminated.append(self.me)
            self.term_clock = self.g.clock
        if all(s in self.g.terminated for s in self.g.living()):
            self.phase = "moving"
        return {"phase": self.phase}

    def play_action(self, *, action_id=None, action=None, state_version=None, **_kw):
        assert self.phase == "moving" and state_version == self.version
        self.voted = action_id
        return {"status": "completed", "state": None}

    def get_result(self):
        return {"is_terminal": True, "status": "completed", "returns": {}, "termination_reason": "completed"}


def _ev_chat(sender, text, clock):
    def ev(sg):
        sg.g.clock = clock
        _chat(sg.g, sender, text)
    return ev


def _ev_clock(clock):
    def ev(sg):
        sg.g.clock = clock
    return ev


def _ev_others_done(clock):
    def ev(sg):
        sg.g.clock = clock
        sg.g.terminated = sorted(set(sg.g.terminated) | {s for s in sg.g.living() if s != sg.me})
        if all(s in sg.g.terminated for s in sg.g.living()):
            sg.phase = "moving"
    return ev


def _ev_window_closes(sg):
    sg.g.terminated = list(sg.g.living())
    sg.phase = "moving"


class Probe(Contestant):
    """Contestant that marks when it is inside its listening loop (for the scripted game's bookkeeping)."""
    _listening_now = False

    def _listen(self, state, context):
        self._listening_now = True
        try:
            return super()._listen(state, context)
        finally:
            self._listening_now = False


def _play(events, fail_wait=None):
    g = _game()
    sg = ScriptedGame(g, 3, events, fail_wait=fail_wait)
    c = Probe()
    c.listen = True
    sg.inside = c
    final = run_game(sg, ctx(), c, wait_seconds=0.0, sleep=lambda s: None)
    return sg, c, final


def test_runner_listens_and_counters_a_late_accusation():
    sg, c, final = _play([
        _ev_chat(1, "Is the Seer here? Please speak up before we vote.", 5),     # nothing to answer: hold
        _ev_clock(9), _ev_clock(13),                                              # quiet polls
        _ev_chat(2, "I am the Seer. Player3 is a wolf.", 20),                    # a late accusation
        _ev_others_done(40),
    ])
    assert final.is_terminal
    kinds = [k for k, _ in sg.sends]
    assert kinds == ["chat", "chat", "terminate"]
    assert sg.sends[1][1].startswith("I am the Seer. Player2 is a WOLF")
    assert c.listen_stats["holds"] == 1 and c.listen_stats["followups"] == 1 and c.listen_stats["errors"] == 0
    held = [w for w in sg.waits if w["listening"]]
    assert held and all(0 < w["timeout_seconds"] <= 5.0 + 1e-9 for w in held)
    assert all(w["since_phase"] == "messaging" for w in held)
    assert sg.voted == "2"                                # the claim fight: vote the rival claimant


def test_runner_listening_stops_on_a_quiet_spell_without_a_follow_up():
    sg, c, final = _play([_ev_chat(1, "Is the Seer here? Please speak up before we vote.", 5)]
                         + [_ev_clock(t) for t in range(10, 60, 5)] + [_ev_others_done(60)])
    kinds = [k for k, _ in sg.sends]
    assert final.is_terminal and kinds[0] == "chat" and set(kinds[1:]) == {"terminate"}   # then the runner's no-ops
    assert c.listen_stats["followups"] == 0 and c.listen_stats["holds"] == 1
    assert sg.term_clock == 30                            # 24 s after the last other chat (115 s left): the quiet rule


def test_runner_listening_error_terminates_and_the_match_goes_on():
    err = MCPToolError("boom", status_code=None, error_code="INTERNAL")
    sg, c, final = _play([_ev_chat(1, "Is the Seer here? Please speak up before we vote.", 5),
                          _ev_others_done(30)], fail_wait=err)
    assert final.is_terminal and [k for k, _ in sg.sends] == ["chat", "terminate"]
    assert c.listen_stats["errors"] == 1 and sg.voted is not None


def test_runner_window_closing_while_listening_is_a_race_the_runner_rereads():
    sg, c, final = _play([_ev_chat(1, "Is the Seer here? Please speak up before we vote.", 5), _ev_window_closes])
    assert final.is_terminal and sg.voted is not None     # our terminate met WRONG_PHASE: re-read, then voted
    assert [k for k, _ in sg.sends] == ["chat"]


# ---------------------------------------------------------------- the simulators
def _alive_at_window(game, seat, day) -> bool:
    for d in game.dead:
        if d["seat"] == seat and (d["day"] < day or (d["day"] == day and d["cause"] == "night_kill")):
            return False
    return True


def test_rt_timing_puts_our_opening_first_and_listening_ends_in_time(monkeypatch):
    monkeypatch.setenv(A.LISTEN_ENV, "on")
    checked = 0
    for g in range(8):
        seats = [we.ContestantSeat(create_agent) if i == g % 7 else LLMQwenSeat() for i in range(7)]
        game = we.Game(seats, random.Random(100 + g), timing="rt")
        game.play()
        me = g % 7
        firsts = {}
        for m in game.messages:
            firsts.setdefault(m["day"], m)
        for day, m in firsts.items():
            if _alive_at_window(game, me, day):
                assert m["sender"] == me                  # we answer in milliseconds: always the first to speak
                checked += 1
        assert sum(game.stats[k][me] for k in ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate",
                                               "window_close")) == 0
    assert checked >= 8


def test_rt_timing_off_matches_the_rounds_free_runner_model(monkeypatch):
    """Listening off: every window, our seat says its opening at t = 0 and is done after at most one more call
    with something to say (no follow-up after the first other seat's event without a reaction)."""
    monkeypatch.setenv(A.LISTEN_ENV, "off")
    seats = [we.ContestantSeat(create_agent)] + [LLMQwenSeat() for _ in range(6)]
    game = we.Game(seats, random.Random(7), timing="rt")
    game.play()
    assert game.stats["holds"][0] == 0 and game.stats["window_close"][0] == 0


def test_platform_stand_in_with_slow_seats_and_listening(monkeypatch):
    """The official runner path (threads, real time, compressed 8 s window, bots that take ~0.3 s per move): no
    error, no idle auto-action, no rejected chat, no WRONG_PHASE except the runner's own no-op re-terminate race,
    and listening ends before the window does."""
    monkeypatch.setenv(A.LISTEN_ENV, "on")
    monkeypatch.setenv("ARENA_RECORD", "0")
    agents, ours = [], [0, 3]
    for i in range(7):
        if i in ours:
            agents.append(create_agent())
        else:
            agents.append(SeatAgent(LLMQwenSeat(), random.Random(i), think=0.3, think_rng=random.Random(50 + i)))
    with contextlib.redirect_stdout(io.StringIO()):
        out = play_table(agents, 4242, window_limit=8.0, timeout=240)
    assert out["terminal"] and not out["hung"] and not out["errors"]
    assert not [a for a in out["log"]["auto"] if a[0] in ours]
    assert not [v for v in out["log"]["violations"] if v[0] in ours]
    assert not [e for e in out["log"]["errors"] if e[0] in ours and e[1] != "WRONG_PHASE_NOOP"]
    assert sum(agents[i].listen_stats["holds"] for i in ours) >= 1
    assert all(out["listen_s"][i] < 8.0 - 1.0 for i in ours)


def test_listening_is_on_by_default(monkeypatch):
    """v4.3 (10-08): on by default. Against the red team's framing wolves our agent answered 0 of 1007 frames under
    the official runner's timing; listening measured +3 villager / +5 seer points there and +0.2 / +0.5 against the
    stand-ins (docs/progress.md "v4.3")."""
    monkeypatch.delenv(A.LISTEN_ENV, raising=False)
    assert A.LISTEN_DEFAULT == "on" and listen_enabled() and create_agent().listen is True
    monkeypatch.setenv(A.LISTEN_ENV, "off")
    assert create_agent().listen is False


# ---------------------------------------------------------------- switched off: listening code never runs
@pytest.fixture
def tripwire(monkeypatch):
    """With ARENA_WW_LISTEN=off, every listening entry point (the frame walk, the read loop, the hold and clock rules)
    is replaced by a tripwire that records the call and raises."""
    monkeypatch.setenv(A.LISTEN_ENV, "off")
    calls = []

    def trip(name):
        def f(*_a, **_kw):
            calls.append(name)
            raise AssertionError(f"listening code reached with {A.LISTEN_ENV}=off: {name}")
        return f
    monkeypatch.setattr(A, "_runner_game", trip("_runner_game"))
    for name in ("_listen", "_listen_wait", "_may_hold"):
        monkeypatch.setattr(Contestant, name, trip(name))
    return calls


def test_off_never_reaches_listening_through_the_runner(tripwire):
    sg = ScriptedGame(_game(), 3, [_ev_chat(1, "Is the Seer here? Please speak up before we vote.", 5),
                                   _ev_chat(2, "I am the Seer. Player3 is a wolf.", 20), _ev_others_done(40)])
    c = create_agent()
    assert c.listen is False
    final = run_game(sg, ctx(), c, wait_seconds=0.0, sleep=lambda s: None)
    kinds = [k for k, _ in sg.sends]
    assert final.is_terminal and sg.voted is not None and tripwire == []
    assert kinds[0] == "chat" and "terminate" in kinds        # ScriptedGame fails on a chat after our terminate
    assert c.listen_stats["holds"] == 0


@pytest.mark.parametrize("timing", ["rounds", "rt"])
def test_off_never_reaches_listening_in_the_simulator(tripwire, timing):
    for g in range(6):
        ours = {g % 7, (g + 3) % 7}
        seats = [we.ContestantSeat(create_agent) if i in ours else LLMQwenSeat() for i in range(7)]
        game = we.Game(seats, random.Random(300 + g), timing=timing)
        game.play()
        for me in ours:
            assert sum(game.stats[k][me] for k in ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate",
                                                   "window_close", "holds")) == 0
    assert tripwire == []


def test_off_never_reaches_listening_on_the_platform_stand_in(tripwire, monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")
    agents, ours = [], [1, 4]
    for i in range(7):
        if i in ours:
            agents.append(create_agent())
        else:
            agents.append(SeatAgent(LLMQwenSeat(), random.Random(i), think=0.1, think_rng=random.Random(70 + i)))
    with contextlib.redirect_stdout(io.StringIO()):
        out = play_table(agents, 4343, window_limit=8.0, timeout=240)
    assert out["terminal"] and not out["hung"] and not out["errors"] and tripwire == []
    assert not [e for e in out["log"]["errors"] if e[0] in ours and e[1] != "WRONG_PHASE_NOOP"]
    assert all(agents[i].listen_stats["holds"] == 0 and out["listen_s"][i] == 0.0 for i in ours)
