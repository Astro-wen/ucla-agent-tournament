"""Bounded callback observability; no server text or behavior changes."""
from copy import deepcopy

import pytest

from agent.hok import policy as hok
from tests.hok.test_strategy import Clock, entries, payload, summary


def test_refusal_attributes_the_dispatched_order_and_safe_code():
    bot = hok.HokPolicy(clock=Clock())
    assert bot.decide(payload(version=17))[0] == "s0:macro_advance"
    bot.on_result({"error": "STALE_STATE", "detail": "private server message"})
    text = bot.summary_text()
    assert "refused=1" in text
    assert "refused_codes=(STALE_STATE:1)" in text
    assert "refused_macros=(advance:1)" in text
    assert "s0/advance@17:STALE_STATE" in text
    assert "private server message" not in text
    assert bot._sent == {}


@pytest.mark.parametrize("error", ["opaque_secret_value", {"code": "secret", "message": "secret"},
                                  ["secret"], True, 500])
def test_unknown_error_contents_are_never_retained(error):
    bot = hok.HokPolicy(clock=Clock())
    bot.decide(payload())
    result = {"error": error, "detail": "secret"}
    before = deepcopy(result)
    bot.on_result(result)
    assert result == before
    assert bot._refusal_codes == {"OTHER": 1}
    assert bot._refusal_examples == [(0, "advance", 10, "OTHER")]
    assert "secret" not in bot.summary_text()


def test_callback_does_not_read_nested_error_or_detail_objects():
    class Hidden:
        def __str__(self):
            raise AssertionError("must not stringify server content")

    bot = hok.HokPolicy(clock=Clock())
    bot.decide(payload())
    bot.on_result({"error": Hidden(), "detail": Hidden(), "hidden_state": Hidden()})
    assert bot._refusal_codes == {"OTHER": 1}
    assert "OTHER:1" in bot.summary_text()


def test_unattributed_callback_does_not_invent_a_seat_or_clear_team_orders():
    bot = hok.HokPolicy(clock=Clock())
    bot.decide(payload())
    bot.on_result({"ok": True})
    sent = dict(bot._sent)
    bot.on_result({"error": "NOT_YOUR_TURN"})
    assert bot._sent == sent
    assert bot._refusal_examples == [(None, "unattributed", None, "NOT_YOUR_TURN")]
    assert "s?/unattributed@?:NOT_YOUR_TURN" in bot.summary_text()


def test_refused_lockstep_raw_action_is_distinct_from_a_macro():
    bot = hok.HokPolicy(clock=Clock())
    raw = payload(seats=(0,), mode="lockstep", actions=entries(0, ()), summaries={0: summary(hp=0)})
    assert bot.decide(raw)[0] == "s0:button_0"
    bot.on_result({"error": "INVALID_ACTION"})
    assert bot._refusal_examples == [(0, "raw", 10, "INVALID_ACTION")]


def test_diagnostic_storage_and_summary_stay_bounded():
    bot = hok.HokPolicy(clock=Clock())
    for n in range(10000):
        bot.on_result({"error": "untrusted" + str(n), "detail": "x" * 1000})
    assert len(bot._refusal_examples) == 3
    assert bot._refusal_codes == {"OTHER": 10000}
    assert bot._refusal_macros == {"unattributed": 10000}
    assert len(bot.summary_text()) < 400
    fresh = hok.HokPolicy(clock=Clock())
    assert fresh._refusal_examples == [] and fresh._refusal_codes == {}


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_enabled_diagnostics_preserve_rejection_and_recovery_behavior(monkeypatch, mode):
    enabled_clock, disabled_clock = Clock(), Clock()
    enabled, disabled = hok.HokPolicy(clock=enabled_clock), hok.HokPolicy(clock=disabled_clock)
    for step in range(80):
        enabled_clock.now = disabled_clock.now = step * 0.3
        raw = payload(mode=mode, version=step,
                      standing={str(s): "macro_retreat" for s in range(5)},
                      summaries={s: summary(s, hp=200 if (s + step) % 4 == 0 else 2900) for s in range(5)})
        original = deepcopy(raw)
        monkeypatch.setattr(hok, "REFUSAL_DIAGNOSTICS", True)
        actual = enabled.decide(raw)
        enabled.on_result({"error": "INVALID_ACTION"} if step % 3 == 0 else {"ok": True})
        monkeypatch.setattr(hok, "REFUSAL_DIAGNOSTICS", False)
        expected = disabled.decide(raw)
        disabled.on_result({"error": "INVALID_ACTION"} if step % 3 == 0 else {"ok": True})
        assert actual == expected and raw == original
        for key in ("_retreating", "_healing_since", "_sent", "_acknowledged", "_deaths", "_rejected",
                    "_last_service", "_waiting_since", "orders", "by_macro", "idles", "refused", "views"):
            assert getattr(enabled, key) == getattr(disabled, key)
    assert disabled._refusal_codes == {} and disabled._refusal_examples == []
    assert "refused_codes=" not in disabled.summary_text()


def test_clean_match_summary_is_unchanged():
    bot = hok.HokPolicy(clock=Clock())
    assert bot.summary_text() == "hok views=0 orders=0 (-) idles=0 refused=0"


def test_diagnostic_failure_cannot_bypass_original_rejection_bookkeeping(monkeypatch):
    clock = Clock()
    bot = hok.HokPolicy(clock=clock)
    bot.decide(payload())
    def fail(_):
        raise RuntimeError("optional diagnostic failed")
    monkeypatch.setattr(bot, "_record_refusal", fail)
    bot.on_result({"error": "INVALID_ACTION"})
    assert bot.refused == 1 and bot._sent == {} and bot._inflight is None
    assert bot._rejected == {(0, "macro_advance"): (10, hok.RETRY_S)}


def test_large_error_text_is_bucketed_without_retaining_it():
    bot = hok.HokPolicy(clock=Clock())
    bot.on_result({"error": "x" * 1000000})
    assert bot._refusal_codes == {"OTHER": 1}
    assert len(bot.summary_text()) < 400
