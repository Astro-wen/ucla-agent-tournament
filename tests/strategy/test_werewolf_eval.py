"""Local Werewolf evaluation tools: failure model of the engine, reference seats, paired eval, platform stand-in."""
import random
import time

from altruagent import TERMINATE_MESSAGING, SendMessage

from agent.agent import create_agent
from local import eval_werewolf as ev
from local import werewolf_env as we
from agent.werewolf.state import read_chat
from local.werewolf_bots import _QWEN_VARIANTS, _VARIANTS, HeuristicSeat, LLMQwenSeat, LLMSeat, LLMVariedSeat
from local.werewolf_platform import SeatAgent, play_table


class Boom(we.Seat):
    def choose(self, raw):
        raise RuntimeError("boom")


class Illegal(we.Seat):
    def choose(self, raw):
        return "99"


class Chatty(we.Seat):
    def choose(self, raw):
        return raw["legal_actions"]["actions"][0]["action_id"]

    def message(self, raw):
        return {"content": "word " * 60, "recipients": []}


def test_failure_model_auto_plays_a_broken_seat():
    for bad, key in ((Boom(), "crash"), (Illegal(), "illegal"), (Chatty(), "bad_chat")):
        seats = [we.OfficialBaseline() for _ in range(7)]
        seats[3] = bad
        g = we.Game(seats, random.Random(4))
        g.play()
        assert g.stats[key][3] >= 1 and g.broken[3]
        assert all(v["votes"][3] is None for v in g.votes if 3 in v["votes"])   # a broken seat abstains by day


def test_reference_seats_are_always_legal_and_deterministic_per_seed():
    for Seat in (HeuristicSeat, LLMSeat, LLMVariedSeat, LLMQwenSeat, we.RandomSeat):
        outs = []
        for _ in range(2):
            g = we.Game([Seat() for _ in range(7)], random.Random(11))
            g.play()
            assert sum(g.stats[k][i] for k in ("crash", "illegal", "bad_chat") for i in range(7)) == 0
            outs.append((g.winner, [d["seat"] for d in g.dead], len(g.messages)))
        assert outs[0] == outs[1]


def test_deal_is_shared_by_every_agent_and_eval_runs():
    assert ev.deal(1, 5, "mixed", 3) == ev.deal(1, 5, "mixed", 3)
    a = ev.play_one("agent", 1, 0, "llm", 2)
    b = ev.play_one("agent/champion", 1, 0, "llm", 2)
    assert a["roles"] == b["roles"] and len(a["wins"]) == 2
    assert sum(a["fail"].values()) == 0
    summary = ev.summarize({("agent", 1, "llm", 2): {0: a}, ("champion", 1, "llm", 2): {0: ev.play_one("champion", 1, 0, "llm", 2)}},
                           ["agent", "champion"], [("agent", "champion")])
    assert "1/llm/k2" in summary["conditions"] and "agent-champion" in summary["conditions"]["1/llm/k2"]
    lo, hi = ev.wilson(50, 100)
    assert lo < 0.5 < hi and ev.mean_ci([1, 0, 1, 0])[0] == 0.5


def test_varied_llm_stand_in_is_read_as_it_means():
    """Review finding: the llm stand-in only wrote in our reader's format. Every phrasing of the llmx stand-in must be
    read like its plain form (the first variant): claim-looking table talk is not a claim, no claim is dropped as an
    injection, the seats named wolf / clear are the same."""
    for variants_of in (_VARIANTS, _QWEN_VARIANTS):
        assert set(variants_of) == set(_VARIANTS)
        for form, variants in variants_of.items():
            plain = read_chat(_VARIANTS[form][0].format(3, 5, r=2), 0)
            for tpl in variants:
                for victim in ("Player2", "Someone"):
                    f = read_chat(tpl.format(3, 5, r=2, victim=victim), 0)
                    assert not f.injection, tpl
                    assert (f.claim, f.wolves, f.clears) == (plain.claim, plain.wolves, plain.clears), tpl


def test_qwen_stand_in_writes_skeptical_talk_that_is_not_a_verdict():
    """Review finding: no stand-in wrote the skeptical talk the extended reader misread ("that doesn't mean they're a
    wolf", "Seer claim: none yet", "It was the wolves"), so local games could not price a misread. llmq does; the test
    above checks each of its lines is read like the plain idle line (nothing)."""
    added = [t for t in _QWEN_VARIANTS["idle"] if t not in _VARIANTS["idle"]]
    assert len(added) >= 5
    assert any("doesn't mean" in t for t in added) and any(t.startswith("Seer claim:") for t in added)
    assert any("{victim}" in t and "It was the wolves" in t for t in added)


def test_mixed_background_draws_are_unchanged_and_llmx_runs():
    assert ev.MIXED == ("first", "heuristic", "random", "llm")          # llmx is not drawn: old deals stay valid
    for bg in ("llmx", "llmq"):
        rec = ev.play_one("agent", 1, 0, bg, 1)
        assert sum(rec["fail"].values()) == 0 and sum(rec["bg_fail"].values()) == 0
    assert ev.deal(1, 0, "llmq", 3) != ev.deal(1, 0, "llmx", 3)       # its own deals: no earlier run is changed


def _rec(g, win):
    return {"g": g, "roles": ["seer"], "winner": "villager" if win else "wolf", "wins": [win],
            "fail": {k: 0 for k in ev.FAIL_KEYS}, "bg_fail": {k: 0 for k in ev.FAIL_KEYS}, "max_ms": 1.0,
            "chats": 0, "dms": 0}


def test_verdict_lists_slice_regressions_that_pooling_hides():
    """Review finding: a real seer loss against the demo-like bot was averaged away by the pooled ship check."""
    win = {("agent", "heuristic"): lambda g: g % 10 < 3, ("champion", "heuristic"): lambda g: g % 10 < 6,
           ("agent", "llm"): lambda g: g % 10 < 9, ("champion", "llm"): lambda g: g % 10 < 4}
    results = {}
    for spec, like in (("agent", "agent"), ("champion", "champion"), ("champion/agent", "agent"),
                       ("agent/champion", "champion")):
        for bg in ("heuristic", "llm"):
            results[(spec, 7, bg, 1)] = {g: _rec(g, win[(like, bg)](g)) for g in range(100)}
    pairs = [("agent", "champion"), ("agent/champion", "champion"), ("champion/agent", "champion")]
    v = ev.verdict(ev.summarize(results, ["agent", "champion", "agent/champion", "champion/agent"], pairs), 0.01)
    assert v["seer"]["non_inferior"] and v["overall"]["better"]            # pooled, everything looks fine
    scopes = {(r["scope"], r["role"]) for r in v["regressions"]}
    assert {("heuristic", "seer"), ("heuristic/k<=3", "seer"), ("heuristic", "seat")} <= scopes
    assert not any(s.startswith("llm") for s, _ in scopes)
    assert v["ship"] is False


def test_platform_stand_in_runs_our_agent_through_the_real_runner():
    agents = [create_agent() for _ in range(4)] + [SeatAgent(LLMSeat(), random.Random(1)),
                                                   SeatAgent(HeuristicSeat(), random.Random(2)),
                                                   SeatAgent(we.OfficialBaseline(), random.Random(3))]
    out = play_table(agents, seed=3, idle_limit=120.0, timeout=60.0)
    assert out["terminal"] and not out["hung"] and not out["errors"]
    assert not [v for v in out["log"]["violations"] if v[0] < 4]
    assert not [a for a in out["log"]["auto"] if a[0] < 4]
    assert all(out["returns"][i] for i in range(4))
    assert max(out["max_s"][i] for i in range(4)) < 5.0


class Slow:
    """Sleeps past a tiny idle limit on its first vote: the platform must abstain for it."""

    def __init__(self):
        self.slept = False

    def choose_action(self, state, context):
        if not self.slept and (state.raw.get("game_state") or {}).get("phase") == "day_vote":
            self.slept = True
            time.sleep(0.6)
        return state.legal_actions[0]

    def choose_message(self, state, context):
        return TERMINATE_MESSAGING


def test_platform_stand_in_idle_limit_auto_plays():
    agents = [Slow()] + [SeatAgent(we.OfficialBaseline(), random.Random(i)) for i in range(6)]
    out = play_table(agents, seed=1, idle_limit=0.3, timeout=60.0)
    assert out["terminal"] and (0, "day_vote") in out["log"]["auto"]


def test_example_messaging_agent_shape_is_supported():
    from examples.messaging_agent import WerewolfAgent
    a = SeatAgent(LLMSeat(), random.Random(0))
    assert hasattr(a, "choose_message") and hasattr(WerewolfAgent(), "choose_message")
    assert isinstance(SendMessage("x"), SendMessage)
