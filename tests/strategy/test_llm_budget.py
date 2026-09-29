"""The spend caps of local/llm_client.py across processes: the global NEBIUS_BUDGET_USD (never above the hard
ceiling), the per-run NEBIUS_RUN_BUDGET_USD, reserve-before-send under fcntl.flock, worst-case charging.

No network and no real key: every client talks to an httpx.MockTransport with FAKE_KEY, and every ledger is a file
under tmp_path (NEBIUS_LEDGER / ledger_path); worker processes are forked from the test and never import a .env."""
import json
import multiprocessing as mp
import os
import random
import time

import httpx
import pytest

from local import llm_client as lc

from .test_llm_opponents import FAKE_KEY, FakeNebius, client, ledger_lines, no_network

MSGS = [{"role": "user", "content": "y" * 600}]
MAXTOK = 300
can_fork = pytest.mark.skipif(lc.fcntl is None or "fork" not in mp.get_all_start_methods(),
                              reason="needs fcntl.flock and fork (Linux / WSL)")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for k in lc.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEBIUS_DOTENV", str(tmp_path / "no.env"))
    monkeypatch.setenv("NEBIUS_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("NEBIUS_CACHE", "off")
    monkeypatch.setattr(lc, "_SHARED", {})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)    # only MockTransport answers here
    yield


def worst_case(model=lc.DEFAULT_MODEL, messages=MSGS, max_tokens=MAXTOK):
    """(estimated prompt tokens, USD) of one request's reservation."""
    prompt, _, usd = lc.LLMClient(None, model=model, ledger_path=None, cache_path=None).estimate(messages, max_tokens)
    return prompt, usd


def replay(path):
    """Replays the ledger line by line: (highest total the caps ever saw, most reservations open at once, ledger)."""
    led, peak, most_open = lc.Ledger(None), 0.0, 0
    for line in path.read_text().splitlines():
        led._add(json.loads(line))
        peak, most_open = max(peak, led.totals["usd"]), max(most_open, len(led.pending))
    return peak, most_open, led


# ------------------------------------------------------------------------------------------ one process
def test_an_over_cap_request_is_refused_and_never_sent(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text(json.dumps({"run": "other-run", "rid": "x1", "status": "ok", "cost_usd": 0.999,
                             "prompt_tokens": 1, "completion_tokens": 1}) + "\n")     # spent by another run
    fake = FakeNebius()
    c = client(tmp_path, fake, budget_usd=1.0, run_id="me")
    before = p.read_bytes()
    with pytest.raises(lc.BudgetExhausted, match="NEBIUS_BUDGET_USD") as e:
        c.chat(MSGS, max_tokens=4000)                                  # worst case ~$0.0012 > the $0.001 left
    assert isinstance(e.value, lc.LLMUnavailable) and FAKE_KEY not in str(e.value)
    assert fake.requests == [] and p.read_bytes() == before            # nothing sent, nothing reserved
    with pytest.raises(lc.BudgetExhausted):                            # complete_json passes the signal on
        c.complete_json(MSGS, schema={"type": "object"}, max_tokens=4000)
    assert fake.requests == [] and c.stats.refused == 2
    c.chat(MSGS, max_tokens=100)                                       # a request that fits still goes
    assert len(fake.requests) == 1


def test_the_hard_ceiling_holds_whatever_the_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBIUS_BUDGET_USD", "1000")
    assert lc.LLMClient.from_env().budget_usd == lc.HARD_CEILING_USD == 20.0
    assert lc.LLMClient(FAKE_KEY, budget_usd=1e9, ledger_path=None, cache_path=None).budget_usd == 20.0
    p = tmp_path / "ledger.jsonl"
    p.write_text(json.dumps({"run": "old", "rid": "o", "status": "ok", "cost_usd": 19.9995}) + "\n")
    fake = FakeNebius()
    monkeypatch.setenv("NEBIUS_API_KEY", FAKE_KEY)
    c = lc.LLMClient.from_env(transport=httpx.MockTransport(fake))
    with pytest.raises(lc.BudgetExhausted, match=r"\$20\.00"):
        c.chat(MSGS, max_tokens=4000)
    assert fake.requests == []


def test_the_per_run_sub_cap_refuses_while_the_global_budget_has_room(tmp_path, monkeypatch):
    prompt, worst = worst_case()
    fake = FakeNebius(usage=(prompt, MAXTOK))                          # billed exactly the worst case
    monkeypatch.setenv("NEBIUS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("NEBIUS_BUDGET_USD", "5")
    monkeypatch.setenv("NEBIUS_RUN_ID", "sub")
    monkeypatch.setenv("NEBIUS_RUN_BUDGET_USD", repr(2.5 * worst))
    w1 = lc.LLMClient.from_env(transport=httpx.MockTransport(fake))
    w2 = lc.LLMClient.from_env(transport=httpx.MockTransport(fake))   # a second worker process of the same run
    assert w1.run_budget_usd == pytest.approx(2.5 * worst)
    w1.chat(MSGS, max_tokens=MAXTOK)
    w2.chat(MSGS, max_tokens=MAXTOK)
    with pytest.raises(lc.BudgetExhausted, match="NEBIUS_RUN_BUDGET_USD"):
        w1.chat(MSGS, max_tokens=MAXTOK)
    assert w1.spent()["total_usd"] == pytest.approx(2 * worst) and 2 * worst < w1.budget_usd   # global has room
    other = client(tmp_path, fake, run_id="another-run")               # other runs only answer to the global cap
    other.chat(MSGS, max_tokens=MAXTOK)
    assert len(fake.requests) == 3


@pytest.mark.parametrize("raw, want", [("junk", 0.0), ("nan", 0.0), ("inf", 0.0), ("-1", 0.0), ("0.3", 0.3),
                                       ("30", 5.0)])
def test_a_run_sub_cap_that_cannot_be_read_refuses_rather_than_disappears(monkeypatch, raw, want):
    monkeypatch.setenv("NEBIUS_RUN_BUDGET_USD", raw)
    assert lc.LLMClient.from_env().run_budget_usd == want              # never above the global budget
    assert lc.LLMClient.from_env(environ={}).run_budget_usd is None     # unset: no sub-cap


def test_an_unknown_model_is_reserved_and_charged_at_the_ceiling_price(tmp_path):
    ceiling = (max(p[0] for p in lc.PRICES.values()), max(p[1] for p in lc.PRICES.values()))
    assert lc.UNKNOWN_PRICE == ceiling
    model = "some-lab/Brand-New-Model-9"
    assert lc.known_price(model) is None and lc.price_of(model) == ceiling
    fake = FakeNebius(usage=(1000, 100))
    c = client(tmp_path, fake, model=model)
    prompt, worst = worst_case(model)
    assert worst == pytest.approx((prompt * ceiling[0] + MAXTOK * ceiling[1]) / 1e6)
    comp = c.chat(MSGS, max_tokens=MAXTOK)
    res, fin = ledger_lines(tmp_path / "ledger.jsonl", final_only=False)
    assert res["reserve_usd"] == pytest.approx(worst, abs=1e-8)
    charged = (1000 * ceiling[0] + 100 * ceiling[1]) / 1e6
    assert fin["cost_usd"] == pytest.approx(charged) and comp.cost_usd == pytest.approx(charged)


def test_a_reply_without_usage_keeps_the_worst_case_reservation(tmp_path):
    c = client(tmp_path, FakeNebius(usage=None, content='{"a": 1}'))
    prompt, worst = worst_case()
    comp = c.chat(MSGS, max_tokens=MAXTOK)
    res, fin = ledger_lines(tmp_path / "ledger.jsonl", final_only=False)
    assert res["reserve_usd"] == pytest.approx(worst, abs=1e-8) and fin["cost_usd"] == pytest.approx(worst, abs=1e-8)
    assert (fin["prompt_tokens"], fin["completion_tokens"]) == (prompt, MAXTOK) and fin["estimated"] is True
    assert comp.cost_usd == pytest.approx(worst) and c.spent()["total_usd"] == pytest.approx(worst, abs=1e-8)


def test_cached_answers_cost_nothing_and_say_so(tmp_path):
    fake = FakeNebius(usage=(100, 20))
    c = client(tmp_path, fake, cache_path=tmp_path / "cache.sqlite")
    a = c.chat(MSGS)
    ledger_before, spent_before = (tmp_path / "ledger.jsonl").read_text(), c.spent()["total_usd"]
    b = c.chat(MSGS)
    assert not a.cached and a.cost_usd > 0
    assert b.cached and b.cost_usd == 0 and b.latency_s == 0 and b.text == a.text and len(fake.requests) == 1
    assert (tmp_path / "ledger.jsonl").read_text() == ledger_before and c.spent()["total_usd"] == spent_before
    assert c.stats.cost_usd == pytest.approx(a.cost_usd) and c.stats.cache_hits == 1
    broke = client(tmp_path, fake, cache_path=tmp_path / "cache.sqlite", budget_usd=0.0)
    assert broke.chat(MSGS).cached                                     # free, so even an exhausted budget serves it
    with pytest.raises(lc.BudgetExhausted):
        broke.chat(MSGS, max_tokens=7)                                 # anything uncached is refused
    assert len(fake.requests) == 1


def test_summing_cost_usd_over_the_ledger_gives_the_settled_spend(tmp_path):
    """The misreading behind the 09-28 '$3.25': the old reservation lines carried their worst case in cost_usd, so
    a plain column sum counted every request twice. Reservations now carry it in reserve_usd only."""
    c = client(tmp_path, FakeNebius(usage=(100, 20)))
    for i in range(5):
        c.chat([{"role": "user", "content": f"q{i}"}], max_tokens=MAXTOK)
    recs = ledger_lines(tmp_path / "ledger.jsonl", final_only=False)
    assert len(recs) == 10 and sum(r["status"] == "reserved" for r in recs) == 5
    one = lc.cost_usd(lc.DEFAULT_MODEL, 100, 20)
    assert sum(r["cost_usd"] for r in recs) == pytest.approx(5 * one)
    assert c.spent()["total_usd"] == pytest.approx(5 * one)
    with open(tmp_path / "ledger.jsonl", "a") as f:                    # an old-format pair is still read correctly
        f.write(json.dumps({"run": "old", "rid": "o1", "status": "reserved", "cost_usd": 0.5,
                            "prompt_tokens": 10, "completion_tokens": 400}) + "\n")
    assert c.spent()["total_usd"] == pytest.approx(5 * one + 0.5)      # open: its worst case counts
    with open(tmp_path / "ledger.jsonl", "a") as f:
        f.write(json.dumps({"run": "old", "rid": "o1", "status": "ok", "cost_usd": 0.01,
                            "prompt_tokens": 5, "completion_tokens": 5}) + "\n")
    assert c.spent()["total_usd"] == pytest.approx(5 * one + 0.01)


def test_without_a_file_lock_nothing_is_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "fcntl", None)
    fake = FakeNebius()
    c = client(tmp_path, fake)
    with pytest.raises(lc.LLMUnavailable, match="lock") as e:
        c.chat(MSGS)
    assert not isinstance(e.value, lc.BudgetExhausted) and e.value.__context__ is None
    assert fake.requests == [] and not (tmp_path / "ledger.jsonl").exists()


# ------------------------------------------------------------------------------------------ many processes
def _hammer(ledger, budget, run, bill, barrier, out):
    """One worker process: its own client on the shared ledger, requesting until a cap refuses it 3 times in a row.
    `bill` = the fraction of the worst case the fake server reports as used."""
    prompt, _ = worst_case()
    rng = random.Random(os.getpid())

    def handler(request):
        time.sleep(rng.uniform(0.002, 0.015))                          # requests overlap: reservations stay open
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}],
                                         "usage": {"prompt_tokens": int(prompt * bill),
                                                   "completion_tokens": int(MAXTOK * bill)}})

    c = lc.LLMClient(FAKE_KEY, transport=httpx.MockTransport(handler), ledger_path=ledger, cache_path=None,
                     budget_usd=budget, run_id=run, sleep=lambda s: None)
    sent, refusals = 0, 0
    barrier.wait(60)
    while refusals < 3:
        try:
            c.chat(MSGS, max_tokens=MAXTOK)
            sent, refusals = sent + 1, 0
        except lc.BudgetExhausted:
            refusals += 1
            time.sleep(0.005)
    out.put(sent)


def _run_hammer(tmp_path, n, budget, bill):
    ctx = mp.get_context("fork")
    barrier, out = ctx.Barrier(n), ctx.Queue()
    ledger = tmp_path / "shared.jsonl"
    procs = [ctx.Process(target=_hammer, args=(str(ledger), budget, f"proc{i}", bill, barrier, out))
             for i in range(n)]
    for p in procs:
        p.start()
    sent = [out.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(30)
    assert [p.exitcode for p in procs] == [0] * n
    return sum(sent), ledger


@can_fork
def test_concurrent_processes_cannot_race_past_a_tiny_cap(tmp_path):
    """12 processes, each with its own client, hammer a cap with room for exactly 7 worst-case requests that are
    billed at their worst case: exactly 7 are sent, and replaying the ledger line by line the total the caps saw
    never went above the cap (zero overshoot), although several reservations were open at once."""
    _, worst = worst_case()
    budget = 7.5 * worst
    sent, ledger = _run_hammer(tmp_path, 12, budget, bill=1.0)
    peak, most_open, led = replay(ledger)
    assert sent == 7 and len(ledger_lines(ledger)) == 7
    assert peak <= budget + 1e-9 and led.totals["usd"] == pytest.approx(7 * worst, abs=1e-8)
    assert led.pending == {} and most_open >= 2


@can_fork
def test_concurrent_processes_reuse_freed_room_without_ever_passing_the_cap(tmp_path):
    """Billed at a tenth of the worst case, settled requests free room that the 12 processes race for: many more
    requests go through, and still no moment of the ledger is above the cap."""
    _, worst = worst_case()
    budget = 4.5 * worst
    sent, ledger = _run_hammer(tmp_path, 12, budget, bill=0.1)
    peak, most_open, led = replay(ledger)
    assert sent > 4 and sent == len(ledger_lines(ledger)) and led.pending == {}
    assert peak <= budget + 1e-9 and led.totals["usd"] <= budget and most_open >= 2


def _crash_after_reserving(ledger):
    """A worker that dies while its request is in flight: reserved, never settled."""
    def handler(request):
        os._exit(17)

    c = lc.LLMClient(FAKE_KEY, transport=httpx.MockTransport(handler), ledger_path=ledger, cache_path=None,
                     run_id="crashed")
    c.chat(MSGS, max_tokens=MAXTOK)


@can_fork
def test_a_crash_between_reserve_and_settle_leaves_the_reservation_counted(tmp_path):
    ledger = tmp_path / "shared.jsonl"
    p = mp.get_context("fork").Process(target=_crash_after_reserving, args=(str(ledger),))
    p.start()
    p.join(60)
    assert p.exitcode == 17
    _, worst = worst_case()
    (res,) = ledger_lines(ledger, final_only=False)
    assert res["status"] == "reserved" and res["run"] == "crashed"
    fake = FakeNebius()
    c = client(tmp_path, fake, ledger_path=ledger, budget_usd=1.5 * worst)
    assert c.spent()["total_usd"] == pytest.approx(worst, abs=1e-8) and len(c.ledger.pending) == 1
    with pytest.raises(lc.BudgetExhausted):                            # the dead request still holds its worst case
        c.chat(MSGS, max_tokens=MAXTOK)
    assert fake.requests == []
    roomy = client(tmp_path, fake, ledger_path=ledger, budget_usd=2.5 * worst)
    roomy.chat(MSGS, max_tokens=MAXTOK)
    assert len(fake.requests) == 1
