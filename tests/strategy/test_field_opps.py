"""local/field_opps.py: the organizers' example agents behind the official runner's rules and the platform's clocks,
plus the pokemon_env hooks they use (TIMER_DEFAULT, RESIGN). Showdown-backed games skip without local/js/node_modules."""
import math
import random
import time

import pytest

from altruagent import RESIGN, WithReasoning
from altruagent.models import LegalAction
from local import field_opps as fo
from local import pokemon_env as pe
from local.showdown import JS_DIR

needs_showdown = pytest.mark.skipif(not (JS_DIR / "node_modules").exists(), reason="npm install not run in local/js")


# ---------------------------------------------------------------- the VGC timer
def test_a_seat_that_never_answers_gets_preview_plus_seven_defaults_then_forfeits():
    c = fo.VGCClock()
    verdicts = [c.request(math.inf) for _ in range(9)]
    assert verdicts == ["default"] * 8 + ["forfeit"]                   # 510 -> 420 -> 365 ... -> 35 -> 0


def test_a_fast_seat_never_loses_bank_and_a_slow_one_runs_out():
    fast = fo.VGCClock()
    assert all(fast.request(0.05) == "ok" for _ in range(200))
    slow = fo.VGCClock()
    got = [slow.request(50.0) for _ in range(10)]
    assert got[:9] == ["ok"] * 9 and got[9] == "forfeit"            # 510 - 50 -> capped 420 -> 370 ... 20 < 50
    late_preview = fo.VGCClock()
    assert late_preview.request(120.0) == "default"                    # an LLM retry (2 x 60 s) misses the 90 s preview
    assert late_preview.request(54.9) == "ok" and late_preview.request(55.0) == "default"


# ---------------------------------------------------------------- what the platform accepts
def _tmpl():
    return {"type": "doubles_turn", "slots": [
        {"slot": 0, "options": [{"type": "move", "move_id": "fakeout", "targets": [1, 2, -2]},
                                {"type": "switch", "species": "gholdengo"}]},
        {"slot": 1, "options": [{"type": "move", "move_id": "protect", "targets": [0]},
                                {"type": "switch", "species": "gholdengo"}, {"type": "pass"}]}]}


def _moving(tmpl=None):
    return {"phase": "moving", "legal_actions": {"actions": [{"action_id": "doubles_turn",
                                                              "input": {"action": tmpl or _tmpl()}}]}}


def test_platform_accepts_mirrors_the_env_rules():
    ok = {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "fakeout", "target": 2},
          "slot_1": {"type": "move", "move_id": "protect", "target": 0}}
    assert fo.platform_accepts(_moving(), ok)
    for bad in ({**ok, "slot_0": {"type": "move", "move_id": "fakeout", "target": 0}},
                {**ok, "slot_0": {"type": "switch", "species": "gholdengo"},
                 "slot_1": {"type": "switch", "species": "gholdengo"}},
                {**ok, "slot_0": {"type": "pass"}},
                {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "fakeout", "target": 1}},
                _tmpl(), "doubles_turn", None):
        assert not fo.platform_accepts(_moving(), bad)
    preview = {"phase": "team_preview", "observation": {"your_roster": [{"species": s} for s in "abcdef"]}}
    assert fo.platform_accepts(preview, {"type": "select_lineup", "bring": list("abcd"), "leads": list("ba")})
    assert not fo.platform_accepts(preview, {"type": "select_lineup", "bring": list("abcz"), "leads": list("ab")})
    assert not fo.platform_accepts(preview, {"type": "select_lineup", "bring": [1, 2, 3, 4], "leads": [1, 2]})
    draft = {"phase": "draft", "observation": {"available_card_ids": ["x", "y"]}}
    assert fo.platform_accepts(draft, {"type": "draft_pick", "card_id": "y"})
    assert not fo.platform_accepts(draft, {"type": "draft_pick", "card_id": "z"})


def test_templates_are_told_apart_from_finished_moves():
    lineup = LegalAction(action_id="select_lineup", label="", input={"action": {"type": "select_lineup",
                                                                                 "roster": [], "instructions": "x"}})
    turn = LegalAction(action_id="doubles_turn", label="", input={"action": _tmpl()})
    pick = LegalAction(action_id="draft_pick:c1", label="", input={"action": {"type": "draft_pick", "card_id": "c1"}})
    assert fo.is_template(lineup) and fo.is_template(turn) and not fo.is_template(pick)


# ---------------------------------------------------------------- a seat on synthetic states
def _state(phase, actions, obs=None):
    return {"session_id": "local", "game_type": pe.GAME, "status": "in_progress", "state_version": 0, "phase": phase,
            "observation": obs or {}, "is_terminal": False, "is_current_actor": True,
            "current_actor": {"agent_id": "agent-a", "position": 0},
            "legal_actions": {"session_id": "local", "state_version": 0, "actions": actions}}


ROSTER = ["incineroar", "rillaboom", "urshifu", "amoonguss", "tornadus", "fluttermane"]
DRAFT = _state("draft", [{"action_id": f"draft_pick:{c}", "label": "", "input": {"action": {"type": "draft_pick",
                                                                                           "card_id": c}}}
                         for c in ("c1", "c2")], {"available_card_ids": ["c1", "c2"]})
PREVIEW = _state("team_preview", [{"action_id": "select_lineup", "label": "", "input": {"action": {
    "type": "select_lineup", "roster": ROSTER, "instructions": "bring 4"}}}],
    {"your_roster": [{"species": s} for s in ROSTER]})
MOVING = _state("moving", [{"action_id": "doubles_turn", "label": "", "input": {"action": _tmpl()}}])


def _seat(fn, **kw):
    s = fo.PlatformSeat(lambda seat: fn, "t", **kw)
    s.reset()
    return s


def test_basic_agent_goes_down_at_team_preview_and_the_timer_plays_for_it():
    s = fo.make("basic")
    s.reset()
    assert s.choose(DRAFT) == {"type": "draft_pick", "card_id": "c1"}
    assert s.choose(PREVIEW) is pe.TIMER_DEFAULT
    assert s.stats["crashes"] == 1 and s.stats["rejected"] == 1 and "select_lineup" in s.crash_reason
    got = [s.choose(MOVING) for _ in range(8)]
    assert got == [pe.TIMER_DEFAULT] * 7 + [RESIGN] and s.stats["forfeit"] == 1
    assert s.stats["crashes"] == 1                                     # down, not called again


def test_messaging_agent_is_basic_in_pokemon():
    s = fo.make("messaging")
    s.reset()
    assert s.choose(DRAFT) == {"type": "draft_pick", "card_id": "c1"}
    assert s.choose(PREVIEW) is pe.TIMER_DEFAULT and s.stats["crashes"] == 1


def test_smoke_agent_answers_everything_in_time():
    s = fo.make("smoke")
    s.reset()
    assert s.choose(DRAFT) == {"type": "draft_pick", "card_id": "c1"}
    assert s.choose(PREVIEW) == {"type": "select_lineup", "bring": ROSTER[:4], "leads": ROSTER[:2]}
    assert s.choose(MOVING) == {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "fakeout", "target": 1},
                                "slot_1": {"type": "move", "move_id": "protect", "target": 0}}
    assert s.stats["crashes"] == s.stats["timer_defaults"] == 0


def test_llm_fallback_stand_in_plays_the_real_agents_fallback():
    s = fo.make("llm_fallback")
    s.reset()
    smoke = fo.make("smoke")
    smoke.reset()
    for st in (DRAFT, PREVIEW, MOVING):
        assert s.choose(st) == smoke.choose(st)
    assert s.stats["fallbacks"] == 3 and s.stats["model_calls"] == 6 and s.stats["crashes"] == 0
    assert s.sim.t == 0


def test_llm_fallback_is_not_the_missing_key_case(monkeypatch):
    # review 2 finding 4: without a key the real create_agent() raises before any decision - that seat never plays
    import examples.llm_agent as llm
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm, "load_dotenv", lambda *a, **k: None)      # never read a .env here
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        llm.create_agent()


def test_llm_timeout_stand_in_is_late_everywhere_but_the_draft():
    # modelling assumption (module docstring): the answer that arrives 120 s late is discarded, never lands later
    s = fo.make("llm_timeout")
    s.reset()
    assert s.choose(DRAFT) == {"type": "draft_pick", "card_id": "c1"}  # no timer in the draft: 120 s late is fine
    assert s.choose(PREVIEW) is pe.TIMER_DEFAULT
    assert [s.choose(MOVING) for _ in range(8)] == [pe.TIMER_DEFAULT] * 7 + [RESIGN]
    assert s.stats["late"] == 8 and s.stats["crashes"] == 0 and s.stats["model_calls"] == 2 * 10
    assert s.sim.t == pytest.approx(60.0 * 20)


class _FakeModel:
    """Answers every schema the real LLMAgent sends, choosing differently from the fallback, 'taking' `secs`."""
    model = "fake"

    def __init__(self, secs=0.0):
        self.secs = secs

    def complete_structured(self, messages, schema_name, schema):
        time.sleep(self.secs)
        props = schema["properties"]
        if "action_id" in props:
            return {"action_id": props["action_id"]["enum"][-1], "reasoning_summary": "last card"}
        if "bring" in props:
            roster = props["bring"]["items"]["enum"]
            return {"bring": roster[2:6], "leads": roster[2:4], "reasoning_summary": "the back four"}
        return {"slot_0": {"option": 0, "target": 2}, "slot_1": {"option": 0, "target": 0}, "reasoning_summary": "hit B"}


def test_llm_live_plays_a_real_models_answers_on_its_real_latency(monkeypatch):
    from examples.llm import providers
    monkeypatch.setattr(providers, "provider_from_env", lambda: _FakeModel(0.01))
    s = fo.make("llm_live")
    s.reset()
    assert s.choose(DRAFT) == {"type": "draft_pick", "card_id": "c2"}
    assert s.choose(PREVIEW) == {"type": "select_lineup", "bring": ROSTER[2:6], "leads": ROSTER[2:4]}
    assert s.choose(MOVING) == {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "fakeout", "target": 2},
                                "slot_1": {"type": "move", "move_id": "protect", "target": 0}}
    assert s.stats["model_calls"] == 3 and s.stats["fallbacks"] == s.stats["crashes"] == s.stats["timer_defaults"] == 0
    assert 0.03 <= s.sim.t < 5                                         # real seconds, charged to the seat's clock
    slow = fo.WallTimed(_FakeModel(0.02), fo.SimClock())
    slow.complete_structured([], "x", {"properties": {"action_id": {"enum": ["a"]}}})
    assert slow.calls == 1 and slow.sim.t >= 0.02 and slow.model == "fake"


def test_llm_live_needs_a_key_and_is_never_in_a_group(monkeypatch):
    from local import panel
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        fo.make("llm_live").reset()
    with pytest.raises(SystemExit):
        panel.resolve_opps("smoke,llm_live")
    assert "llm_live" not in fo.FIELD and all("llm_live" not in g for g in panel.GROUPS.values())
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-not-a-key")
    assert panel.resolve_opps("smoke,llm_live") == ["smoke", "llm_live"]
    with pytest.raises(ValueError):
        fo.make("gpt5")


def test_a_seat_without_the_timer_is_random_played_like_the_documented_300s_rule():
    s = fo.make("basic", clock="none")
    s.reset()
    random.seed(1)
    out = s.choose(PREVIEW)
    random.seed(1)
    assert out == pe.random_action(PREVIEW) and s.stats["random_played"] == 1
    assert pe.lineup_ok(out, ROSTER)


def test_exceptions_illegal_payloads_and_resign_follow_the_runner():
    def boom(state, ctx):
        raise ValueError("bug")
    s = _seat(boom)
    assert s.choose(MOVING) is pe.TIMER_DEFAULT and s.stats["crashes"] == 1 and "ValueError" in s.crash_reason
    s = _seat(lambda st, ctx: {"type": "doubles_turn", "slot_0": {"type": "pass"}, "slot_1": {"type": "pass"}})
    assert s.choose(MOVING) is pe.TIMER_DEFAULT and s.stats["rejected"] == 1
    s = _seat(lambda st, ctx: "not-an-action")
    assert s.choose(DRAFT) is None and s.stats["draft_stalls"] == 1   # the real draft would wait forever
    s = _seat(lambda st, ctx: RESIGN)
    assert s.choose(MOVING) is RESIGN and s.stats["resigned"] == 1
    s = _seat(lambda st, ctx: WithReasoning(st.legal_actions[1], "second card"))
    assert s.choose(DRAFT) == {"type": "draft_pick", "card_id": "c2"}


# ---------------------------------------------------------------- the candidate on the VGC timer in real time
class _Timed(pe.Player):
    """Answers like `inner`, 'taking' secs[phase] on a fake wall clock (a list the seat's timer reads)."""
    name = "timed"

    def __init__(self, secs, inner=None):
        self.secs, self.inner, self.now = secs, inner, [0.0]

    def reset(self):
        self.n = 0
        if self.inner is not None:
            self.inner.reset()

    def choose(self, raw):
        if raw["phase"] == "moving":
            self.n += 1
        self.now[0] += self.secs.get(raw["phase"], 0.0)
        return self.inner.choose(raw) if self.inner is not None else f"answer-{raw['phase']}"


def _wall(p):
    return fo.WallClockSeat(p, timer=lambda: p.now[0])


def test_a_slow_candidate_is_timer_played_and_forfeits_like_on_the_platform():
    # review 2 finding 2: the panel's candidate had no clock, so "timer_cand == 0" held by construction
    p = _Timed({"draft": 300.0, "team_preview": 70.0, "moving": 54.0})
    s = _wall(p)
    s.reset()
    assert s.choose(DRAFT) == "answer-draft"                          # no idle timer in the draft: only measured
    assert s.choose(PREVIEW) == "answer-team_preview"                 # 70 s < 90 s: in time (bank 510 -> 440)
    assert s.choose(MOVING) == "answer-moving"                        # 54 s < 55 s (bank 420 -> 370)
    p.secs["moving"] = 70.0
    got = [s.choose(MOVING) for _ in range(7)]
    assert got == [pe.TIMER_DEFAULT] * 6 + [RESIGN]                    # 370 -> 315 ... -> 40 -> 0: lost on inactivity
    assert s.stats == {"requests": 9, "timer_defaults": 6, "forfeit": 1, "max_s": 70.0, "draft_max_s": 300.0,
                       "bank_left": 0.0}
    fast = _wall(_Timed({"team_preview": 6.0, "moving": 2.5}))
    fast.reset()
    assert [fast.choose(st) for st in (PREVIEW, *[MOVING] * 60)] == ["answer-team_preview"] + ["answer-moving"] * 60
    assert fast.stats["timer_defaults"] == fast.stats["forfeit"] == 0 and fast.stats["bank_left"] == 420.0


@needs_showdown
def test_a_slow_candidate_loses_a_whole_game_on_the_timer():
    from local.showdown import Showdown
    p = _Timed({"moving": 70.0}, inner=pe.RandomPlayer())
    s = _wall(p)
    fast = _wall(_Timed({"team_preview": 1.0, "moving": 1.0}, inner=pe.RandomPlayer()))
    with Showdown() as sd:
        r = pe.play_match(sd, s, pe.RandomPlayer(), seed=41)
        assert r.invalid == [0, 0] and r.timer_defaults == [min(p.n, 7), 0]
        assert (r.forfeit == 0) == (p.n >= 8) and s.stats["timer_defaults"] == min(p.n, 7) and p.n >= 1
        r = pe.play_match(sd, pe.RandomPlayer(), fast, seed=41)
        assert r.timer_defaults == [0, 0] and r.forfeit is None and fast.stats["max_s"] == 1.0


# ---------------------------------------------------------------- whole games on Showdown
class _Scripted(pe.Player):
    """TIMER_DEFAULT everywhere after the draft, or RESIGN at the n-th battle request."""
    name = "scripted"

    def __init__(self, resign_at=None):
        self.resign_at = resign_at

    def reset(self):
        self.n = 0

    def choose(self, raw):
        if raw["phase"] == "draft":
            return raw["legal_actions"]["actions"][0]["action_id"]
        if raw["phase"] == "moving":
            self.n += 1
            if self.resign_at == self.n:
                return RESIGN
        return pe.TIMER_DEFAULT


@needs_showdown
def test_env_plays_showdowns_default_and_forfeits():
    from local.showdown import Showdown
    with Showdown() as sd:
        r = pe.play_match(sd, _Scripted(), pe.RandomPlayer(), seed=11)
        assert r.invalid == [0, 0] and r.timer_defaults[0] >= 2 and r.timer_defaults[1] == 0 and r.forfeit is None
        assert r.brought[0] == "team 1234"
        r = pe.play_match(sd, pe.RandomPlayer(), _Scripted(resign_at=2), seed=12)
        assert r.forfeit == 1 and r.winner == 0 and r.invalid == [0, 0]


@needs_showdown
def test_basic_without_timer_reproduces_official_baseline_game_for_game():
    from local.showdown import Showdown
    with Showdown() as sd:
        for seed in (21, 22):
            a = pe.play_match(sd, fo.make("basic", clock="none"), pe.RandomPlayer(), seed=seed)
            b = pe.play_match(sd, pe.OfficialBaseline(), pe.RandomPlayer(), seed=seed)
            assert (a.winner, a.turns, a.invalid, a.brought, a.rosters) == (b.winner, b.turns, b.invalid, b.brought,
                                                                             b.rosters)


@needs_showdown
def test_field_opponents_play_whole_games_legally():
    from local.showdown import Showdown
    with Showdown() as sd:
        smoke = fo.make("smoke")
        llm = fo.make("llm_fallback")
        for seed in (31, 32):
            r1 = pe.play_match(sd, smoke, pe.RandomPlayer(), seed=seed)
            assert r1.invalid == [0, 0] and r1.timer_defaults == [0, 0] and smoke.stats["crashes"] == 0
            r2 = pe.play_match(sd, llm, pe.RandomPlayer(), seed=seed)
            assert (r2.winner, r2.turns, r2.brought) == (r1.winner, r1.turns, r1.brought)   # same choices as smoke
            assert llm.stats["fallbacks"] == llm.stats["requests"] and llm.stats["crashes"] == 0
        basic = fo.make("basic")
        timeout = fo.make("llm_timeout")
        for seed in (33, 34):
            rb = pe.play_match(sd, pe.RandomPlayer(), basic, seed=seed)
            rt = pe.play_match(sd, pe.RandomPlayer(), timeout, seed=seed)
            for r, s in ((rb, basic), (rt, timeout)):
                assert r.invalid == [0, 0] and r.timer_defaults[0] == 0
                assert r.timer_defaults[1] == s.stats["timer_defaults"] >= 1
                assert (r.forfeit == 1) == bool(s.stats["forfeit"])
                if r.forfeit == 1:
                    assert r.winner == 0 and r.timer_defaults[1] == 8
            assert basic.stats["crashes"] == 1 and timeout.stats["crashes"] == 0
            assert (rb.winner, rb.turns, rb.brought, rb.forfeit) == (rt.winner, rt.turns, rt.brought, rt.forfeit)


@needs_showdown
def test_llm_live_path_plays_whole_games_legally(monkeypatch):
    # a "model" that answers every request (sometimes illegally: the agent retries, then falls back) on real templates
    from examples.llm import providers
    from local.showdown import Showdown
    monkeypatch.setattr(providers, "provider_from_env", lambda: _FakeModel())
    live = fo.make("llm_live")
    with Showdown() as sd:
        for seed in (35, 36):
            r = pe.play_match(sd, live, pe.RandomPlayer(), seed=seed)
            assert r.invalid == [0, 0] and r.timer_defaults == [0, 0] and r.forfeit is None
            assert live.stats["crashes"] == 0 and live.stats["model_calls"] >= live.stats["requests"]
            assert r.brought[0] == "team 3456"                          # the model's lineup, not the fallback's
