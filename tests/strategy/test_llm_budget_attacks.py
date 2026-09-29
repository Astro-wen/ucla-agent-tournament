"""Adversarial checks of local/llm_client.py's spend caps: ways a caller, a server, the environment or the ledger file
could make the client spend past NEBIUS_BUDGET_USD / the per-run caps, and the guards that stop each one.

No network and no real key: every client talks to an httpx.MockTransport with FAKE_KEY, every ledger is a file under
tmp_path (or /dev/null), and the autouse fixture clears NEBIUS_* and points the .env at nothing."""
import gzip
import json
import multiprocessing as mp
import os
import threading
import time

import httpx
import pytest

from local import field_opps as fo
from local import llm_client as lc
from local import llm_seats as ls
from local import werewolf_env as we

from .test_field_opps import DRAFT, MOVING, PREVIEW
from .test_llm_budget import MAXTOK, MSGS, can_fork, replay, worst_case
from .test_llm_opponents import FAKE_KEY, FakeNebius, client, ledger_lines


def _no_network(self, request):
    raise AssertionError("a test tried to reach the network")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for k in lc.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEBIUS_DOTENV", str(tmp_path / "no.env"))
    monkeypatch.setenv("NEBIUS_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("NEBIUS_CACHE", "off")
    monkeypatch.setattr(lc, "_SHARED", {})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _no_network)    # only MockTransport answers here
    yield


def billed(prompt, completion=MAXTOK, content="{}"):
    """A handler that answers every request with this usage."""
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": prompt, "completion_tokens": completion}})
    return handler


def spend_until_refused(c, n=20, messages=MSGS, max_tokens=MAXTOK):
    """Calls until the client refuses (or n calls): the number of calls that did not end in a refusal."""
    done = 0
    for _ in range(n):
        try:
            c.chat(messages, max_tokens=max_tokens)
        except lc.LLMUnavailable:
            break
        except lc.LLMError:
            pass
        done += 1
    return done


# ------------------------------------------------------------------------------------------ the server side
@pytest.mark.parametrize("failure", ["decoding", "gzip", "runtime"])
def test_a_failure_after_the_request_left_stays_charged_at_its_worst_case(tmp_path, failure):
    """A reply that breaks while it is being read (a corrupt gzip body -> httpx.DecodingError, which is not an httpx
    TransportError) or any other exception once the request was handed to the transport: the model may have run and
    billed, so the attempt keeps its worst case -- otherwise every such failure is free and a caller that simply
    retries spends without limit."""
    def handler(request):
        if failure == "decoding":
            raise httpx.DecodingError("bad gzip", request=request)
        if failure == "gzip":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"not gzip at all")
        raise RuntimeError("the transport broke after sending")

    _, worst = worst_case()
    sent = []
    c = client(tmp_path, lambda r: (sent.append(1), handler(r))[1], budget_usd=3.5 * worst, retries=0)
    assert spend_until_refused(c) == 3 and len(sent) == 3                # not 20 "free" requests
    assert c.spent()["total_usd"] == pytest.approx(3 * worst, abs=1e-7) and c.spent()["total_usd"] <= 3.5 * worst
    assert all(r.get("estimated") for r in ledger_lines(tmp_path / "ledger.jsonl"))


def test_a_request_that_cannot_be_built_is_never_reserved_or_sent(tmp_path):
    fake = FakeNebius()
    c = client(tmp_path, fake)
    with pytest.raises(lc.LLMError) as e:
        c.chat([{"role": "user", "content": object()}])                # not JSON: httpx cannot encode it
    assert not isinstance(e.value, lc.LLMUnavailable) and fake.requests == []
    assert not (tmp_path / "ledger.jsonl").exists() or ledger_lines(tmp_path / "ledger.jsonl", final_only=False) == []


def test_a_digit_heavy_prompt_is_reserved_at_a_true_worst_case(tmp_path):
    """Qwen / Llama-3 / GPT tokenizers split numbers into single digits: a prompt of game numbers can be ~1 token a
    byte, 3x the old 3-bytes-a-token estimate, so the 'worst case' reservation was not one and the ledger could pass
    the cap. A byte-level tokenizer never makes more tokens than bytes: that is the bound now."""
    digits = [{"role": "system", "content": "hp " + " ".join(str(i % 97) for i in range(900))},
              {"role": "user", "content": "0123456789" * 300}]
    one_token_per_byte = sum(len(m["content"].encode()) for m in digits) + 5 * len(digits) + 3
    fake = billed(one_token_per_byte)
    probe = lc.LLMClient(None, ledger_path=None, cache_path=None)
    est_prompt, _, worst = probe.estimate(digits, MAXTOK)
    assert est_prompt >= one_token_per_byte
    c = client(tmp_path, fake, budget_usd=1.2 * worst)
    comp = c.chat(digits, max_tokens=MAXTOK)
    assert comp.cost_usd <= worst + 1e-12 and c.spent()["total_usd"] <= 1.2 * worst
    with pytest.raises(lc.BudgetExhausted):
        c.chat(digits, max_tokens=MAXTOK)


def test_a_server_that_bills_past_max_tokens_is_recorded_and_stops_the_next_request(tmp_path):
    """No client can stop a server that ignores max_tokens: the overshoot is bounded by the requests already in
    flight, it is recorded at what the server reported (never hidden), and the next request sees it."""
    prompt, worst = worst_case()
    fake = billed(prompt, 20 * MAXTOK)
    sent = []
    c = client(tmp_path, lambda r: (sent.append(1), fake(r))[1], budget_usd=3.5 * worst)
    assert spend_until_refused(c) == 1 and len(sent) == 1
    assert c.spent()["total_usd"] == pytest.approx(lc.cost_usd(lc.DEFAULT_MODEL, prompt, 20 * MAXTOK))


def test_huge_or_junk_max_tokens_never_sends(tmp_path):
    fake = FakeNebius()
    c = client(tmp_path, fake, budget_usd=20.0)
    with pytest.raises(lc.BudgetExhausted):
        c.chat(MSGS, max_tokens=10 ** 12)
    for junk in (float("inf"), float("nan"), "lots", None):
        with pytest.raises(lc.LLMError) as e:
            c.chat(MSGS, max_tokens=junk)
        assert not isinstance(e.value, lc.LLMUnavailable)
    assert fake.requests == []
    assert not (tmp_path / "ledger.jsonl").exists() or ledger_lines(tmp_path / "ledger.jsonl", final_only=False) == []


class Abort(BaseException):
    """Like KeyboardInterrupt / SystemExit / a gevent Timeout: not an Exception, so nothing in the client catches it."""


def test_an_exception_that_escapes_between_reserve_and_settle_leaves_the_reservation(tmp_path):
    _, worst = worst_case()

    def handler(request):
        raise Abort()

    c = client(tmp_path, handler, budget_usd=1.5 * worst)
    with pytest.raises(Abort):
        c.chat(MSGS, max_tokens=MAXTOK)
    assert len(c.ledger.pending) == 1 and c.spent()["total_usd"] == pytest.approx(worst, abs=1e-8)
    fake = FakeNebius()
    with pytest.raises(lc.BudgetExhausted):
        client(tmp_path, fake, budget_usd=1.5 * worst).chat(MSGS, max_tokens=MAXTOK)
    assert fake.requests == []


# ------------------------------------------------------------------------------------------ the ledger file
def test_a_torn_last_line_does_not_swallow_the_next_reservation(tmp_path):
    """A writer killed mid-line (or a full disk) leaves a last line without its newline: the next reservation must
    not be glued onto it (one unparseable line = a reservation nobody counts), or a second worker passes the cap."""
    p = tmp_path / "ledger.jsonl"
    p.write_bytes(json.dumps({"run": "old", "rid": "o", "status": "ok", "cost_usd": 0.0}).encode() + b"\n"
                  + b'{"run": "dead", "rid": "d", "status": "ok", "cost_us')
    prompt, worst = worst_case()
    second = FakeNebius(usage=(prompt, MAXTOK))
    seen = []

    def first(request):
        probe = lc.Ledger(p)
        probe.refresh()
        seen.append(len(probe.pending))
        try:                                                           # a second worker while this one is in flight
            client(tmp_path, second, budget_usd=1.5 * worst, run_id="w2").chat(MSGS, max_tokens=MAXTOK)
        except lc.BudgetExhausted:
            pass
        return billed(prompt)(request)

    client(tmp_path, first, budget_usd=1.5 * worst, run_id="w1").chat(MSGS, max_tokens=MAXTOK)
    assert seen == [1] and second.requests == []
    peak, _, led = replay_lenient(p)
    assert peak <= 1.5 * worst + 1e-9 and led.pending == {}


def replay_lenient(path):
    """replay() for a ledger with unparseable lines (skipped, as Ledger.refresh does)."""
    led, peak, most_open = lc.Ledger(None), 0.0, 0
    for line in path.read_bytes().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        led._add(rec)
        peak, most_open = max(peak, led.totals["usd"]), max(most_open, len(led.pending))
    return peak, most_open, led


@pytest.mark.parametrize("how", ["deleted", "truncated", "replaced"])
def test_a_ledger_that_loses_its_history_mid_run_refuses_instead_of_starting_over(tmp_path, how):
    """Deleting, truncating or swapping the ledger under a running client used to reset its totals to zero: a fresh
    budget. A client that has read spend it can no longer see refuses (fail closed) from then on. A swapped-in file
    is not read from the old offset either (that skipped its head: here $1 of another run's spend)."""
    prompt, worst = worst_case()
    p = tmp_path / "ledger.jsonl"
    fake = FakeNebius(usage=(prompt, MAXTOK))
    c = client(tmp_path, fake, budget_usd=(3.5 if how == "replaced" else 2.5) * worst)
    c.chat(MSGS, max_tokens=MAXTOK)
    c.chat(MSGS, max_tokens=MAXTOK)
    if how == "deleted":
        p.unlink()
    elif how == "truncated":
        p.write_text("")
    else:                                                              # a different file, longer than what was read
        other = tmp_path / "other.jsonl"
        other.write_text(json.dumps({"run": "x", "rid": "x0", "status": "ok", "cost_usd": 1.0, "pad": "p" * 4000})
                         + "\n")
        os.replace(other, p)
    with pytest.raises(lc.LLMUnavailable, match="lost"):
        c.chat(MSGS, max_tokens=MAXTOK)
    assert len(fake.requests) == 2 and not c.available


def test_a_ledger_that_is_deleted_while_a_request_is_in_flight_refuses_the_next_one(tmp_path):
    prompt, worst = worst_case()
    p = tmp_path / "ledger.jsonl"
    sent = []

    def handler(request):
        sent.append(1)
        if len(sent) == 2:
            p.unlink()
        return billed(prompt)(request)

    c = client(tmp_path, handler, budget_usd=3.5 * worst)
    assert spend_until_refused(c) == 2 and len(sent) == 2


def test_a_ledger_that_drops_what_is_written_refuses(tmp_path):
    """NEBIUS_LEDGER=/dev/null (or anything that is not a regular file) accepts every write and always reads back
    empty: every check would see $0 spent."""
    prompt, worst = worst_case()
    fake = FakeNebius(usage=(prompt, MAXTOK))
    c = client(tmp_path, fake, ledger_path=os.devnull, budget_usd=1.5 * worst)
    with pytest.raises(lc.LLMUnavailable, match="ledger"):
        c.chat(MSGS, max_tokens=MAXTOK)
    assert fake.requests == [] and not c.available


def test_a_ledger_that_turns_read_only_mid_flight_keeps_the_worst_case_and_refuses(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    prompt, worst = worst_case()
    p = tmp_path / "ledger.jsonl"

    def handler(request):
        p.chmod(0o444)                                                 # the settle line cannot be written
        return billed(prompt // 10, MAXTOK // 10)(request)

    c = client(tmp_path, handler, budget_usd=20.0)
    try:
        c.chat(MSGS, max_tokens=MAXTOK)
        led = lc.Ledger(p)
        led.refresh()
        assert len(led.pending) == 1 and led.totals["usd"] == pytest.approx(worst, abs=1e-8)
        fake = FakeNebius()
        with pytest.raises(lc.LLMUnavailable, match="ledger"):
            client(tmp_path, fake).chat(MSGS, max_tokens=MAXTOK)
        assert fake.requests == []
    finally:
        p.chmod(0o644)


def test_garbage_lines_and_clock_skew_never_hide_spend(tmp_path):
    p = tmp_path / "ledger.jsonl"
    lines = [b"[1, 2]", b'"reserved"', b"42", b"null", b"\xff\xfe binary", b"{}",
             json.dumps({"run": "r", "rid": ["not", "a", "str"], "status": "reserved", "reserve_usd": 0.25}).encode(),
             json.dumps({"run": "r", "rid": "a", "status": "ok", "cost_usd": 0.5, "t": 4102444800}).encode(),  # 2100
             json.dumps({"run": "r", "rid": "b", "status": "ok", "cost_usd": 0.25, "t": -1}).encode(),
             json.dumps({"run": "r", "rid": "c", "status": "ok", "cost_usd": 0.125}).encode()]           # no t
    p.write_bytes(b"\n".join(lines) + b"\n")
    c = client(tmp_path, FakeNebius(), budget_usd=1.2)
    assert c.spent()["total_usd"] == pytest.approx(1.125)
    with pytest.raises(lc.BudgetExhausted):
        c.chat(MSGS, max_tokens=300_000)


# ------------------------------------------------------------------------------------------ configuration
@pytest.mark.parametrize("raw", ["$1.84", "1,84", "1.84 USD", "abc", "nan", "inf", "-inf"])
def test_a_budget_setting_that_cannot_be_read_refuses_instead_of_becoming_the_default(monkeypatch, tmp_path, raw):
    """A typo in NEBIUS_BUDGET_USD used to fall back to the $5 default: an eval meant to stop at $1.84 would have
    run to $5. Now an unreadable budget is $0 (everything refused, and the reason says so)."""
    monkeypatch.setenv("NEBIUS_BUDGET_USD", raw)
    monkeypatch.setenv("NEBIUS_API_KEY", FAKE_KEY)
    fake = FakeNebius()
    c = lc.LLMClient.from_env(transport=httpx.MockTransport(fake))
    assert c.budget_usd == 0.0
    with pytest.raises(lc.BudgetExhausted, match="NEBIUS_BUDGET_USD"):
        c.chat(MSGS, max_tokens=10)
    assert fake.requests == [] and "NEBIUS_BUDGET_USD" in c.unavailable_reason


@pytest.mark.parametrize("name, raw, attr, want", [
    ("NEBIUS_MAX_CALLS", "100.0", "max_calls", 100), ("NEBIUS_MAX_CALLS", "1e2", "max_calls", 100),
    ("NEBIUS_MAX_CALLS", "abc", "max_calls", 0), ("NEBIUS_MAX_CALLS", "-3", "max_calls", 0),
    ("NEBIUS_MAX_CALLS", "inf", "max_calls", 0),
    ("NEBIUS_MAX_TOKENS", "1e6", "max_tokens", 1_000_000), ("NEBIUS_MAX_TOKENS", "1_000_000", "max_tokens", 1_000_000),
    ("NEBIUS_MAX_TOKENS", "lots", "max_tokens", 0), ("NEBIUS_MAX_TOKENS", "nan", "max_tokens", 0)])
def test_run_caps_that_cannot_be_read_refuse_instead_of_becoming_the_default(monkeypatch, name, raw, attr, want):
    monkeypatch.setenv(name, raw)
    assert getattr(lc.LLMClient.from_env(), attr) == want
    assert getattr(lc.LLMClient(FAKE_KEY, ledger_path=None, cache_path=None, **{attr: raw}), attr) == want


def test_a_self_built_client_cannot_raise_the_operators_caps(monkeypatch, tmp_path):
    """The eval scripts export NEBIUS_BUDGET_USD / NEBIUS_RUN_BUDGET_USD / NEBIUS_MAX_CALLS / NEBIUS_MAX_TOKENS; code
    that builds its own LLMClient (or passes overrides to from_env / shared_client) may lower them, never raise
    them."""
    prompt, worst = worst_case()
    monkeypatch.setenv("NEBIUS_BUDGET_USD", repr(1.5 * worst))
    monkeypatch.setenv("NEBIUS_RUN_BUDGET_USD", repr(1.2 * worst))
    monkeypatch.setenv("NEBIUS_MAX_CALLS", "7")
    monkeypatch.setenv("NEBIUS_MAX_TOKENS", "9000")
    monkeypatch.setenv("NEBIUS_API_KEY", FAKE_KEY)
    fake = FakeNebius(usage=(prompt, MAXTOK))
    own = client(tmp_path, fake, budget_usd=5.0, run_budget_usd=5.0, max_calls=2000, max_tokens=3_000_000)
    assert (own.budget_usd, own.run_budget_usd, own.max_calls, own.max_tokens) == \
        (pytest.approx(1.5 * worst), pytest.approx(1.2 * worst), 7, 9000)
    assert client(tmp_path, fake, max_calls=3).max_calls == 3                   # lowering is fine
    over = lc.LLMClient.from_env(transport=httpx.MockTransport(fake), budget_usd=10.0, max_calls=10 ** 6)
    assert over.budget_usd == pytest.approx(1.5 * worst) and over.max_calls == 7
    assert lc.shared_client(budget_usd=10.0).budget_usd == pytest.approx(1.5 * worst)
    own.chat(MSGS, max_tokens=MAXTOK)
    with pytest.raises(lc.BudgetExhausted):
        over.chat(MSGS, max_tokens=MAXTOK)
    assert len(fake.requests) == 1


def test_dotenv_caps_are_ceilings_for_from_env_overrides_too(monkeypatch, tmp_path):
    env = tmp_path / "x.env"
    env.write_text(f"NEBIUS_API_KEY={FAKE_KEY}\nNEBIUS_BUDGET_USD=0.5\nNEBIUS_MAX_CALLS=9\n")
    monkeypatch.setenv("NEBIUS_DOTENV", str(env))
    c = lc.LLMClient.from_env(transport=httpx.MockTransport(FakeNebius()), budget_usd=10.0, max_calls=500)
    assert c.budget_usd == 0.5 and c.max_calls == 9
    assert lc.LLMClient.from_env(budget_usd=0.25).budget_usd == 0.25


def test_a_client_without_a_shared_ledger_file_never_sends(tmp_path):
    """ledger_path=None keeps the ledger in this process only: its 'global' budget would be per client, so N such
    clients could each spend it. It refuses real requests (tests and tools that only estimate still work)."""
    fake = FakeNebius()
    c = client(tmp_path, fake, ledger_path=None)
    with pytest.raises(lc.LLMUnavailable, match="ledger"):
        c.chat(MSGS)
    assert fake.requests == [] and not c.available
    assert c.estimate(MSGS, MAXTOK)[2] > 0


# ------------------------------------------------------------------------------------------ concurrency
def test_threads_sharing_one_client_cannot_overshoot(tmp_path):
    gate, inflight, refused = threading.Event(), [], []
    prompt, worst = worst_case()

    def held(request):
        inflight.append(1)
        gate.wait(10)
        return billed(prompt)(request)

    c = client(tmp_path, held, budget_usd=3.5 * worst)

    def go():
        try:
            c.chat(MSGS, max_tokens=MAXTOK)
        except lc.BudgetExhausted:
            refused.append(1)

    threads = [threading.Thread(target=go) for _ in range(10)]
    for t in threads:
        t.start()
    deadline = time.time() + 10
    while len(inflight) + len(refused) < 10 and time.time() < deadline:
        time.sleep(0.01)
    gate.set()
    for t in threads:
        t.join(10)
    assert len(inflight) == 3 and len(refused) == 7
    peak, _, led = replay(tmp_path / "ledger.jsonl")
    assert peak <= 3.5 * worst + 1e-9 and led.pending == {}


def _torn_worker(ledger, budget, run, barrier, out):
    prompt, _ = worst_case()
    c = lc.LLMClient(FAKE_KEY, transport=httpx.MockTransport(lambda r: (time.sleep(0.01), billed(prompt)(r))[1]),
                     ledger_path=ledger, cache_path=None, budget_usd=budget, run_id=run, sleep=lambda s: None)
    barrier.wait(60)
    sent = refusals = 0
    while refusals < 3:
        try:
            c.chat(MSGS, max_tokens=MAXTOK)
            sent, refusals = sent + 1, 0
        except lc.BudgetExhausted:
            refusals += 1
            time.sleep(0.003)
    out.put(sent)


@can_fork
def test_processes_racing_on_a_ledger_with_a_torn_last_line_stay_under_the_cap(tmp_path):
    _, worst = worst_case()
    budget = 5.5 * worst
    ledger = tmp_path / "shared.jsonl"
    ledger.write_bytes(b'{"run": "dead", "rid": "d", "status": "reser')
    ctx = mp.get_context("fork")
    barrier, out = ctx.Barrier(8), ctx.Queue()
    procs = [ctx.Process(target=_torn_worker, args=(str(ledger), budget, f"p{i}", barrier, out)) for i in range(8)]
    for p in procs:
        p.start()
    sent = sum(out.get(timeout=120) for _ in procs)
    for p in procs:
        p.join(30)
    peak, _, led = replay_lenient(ledger)
    assert sent == 5 and peak <= budget + 1e-9 and led.pending == {}


# ------------------------------------------------------------------------------------------ the seats' call paths
def test_the_vgc_llm_real_seat_sends_nothing_once_the_budget_is_spent(monkeypatch, tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text(json.dumps({"run": "earlier", "rid": "e", "status": "ok", "cost_usd": 1.0}) + "\n")
    monkeypatch.setenv("NEBIUS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("NEBIUS_BUDGET_USD", "1.0")
    fake = FakeNebius()
    monkeypatch.setattr(lc, "_SHARED", {(): lc.LLMClient.from_env(transport=httpx.MockTransport(fake))})
    s, smoke = fo.make("llm_real"), fo.make("smoke")
    s.reset()
    smoke.reset()
    for st in (DRAFT, PREVIEW, MOVING):
        assert s.choose(st) == smoke.choose(st)
    assert fake.requests == [] and s.stats["crashes"] == 0 and lc.shared_client().stats.refused == 6


class HonestUsage(FakeNebius):
    """FakeNebius whose usage a real tokenizer could report for the request (never more tokens than bytes)."""

    def __call__(self, request):
        self.usage = (len(request.content) // 2, 200)
        return super().__call__(request)


def test_the_werewolf_llm_real_seats_send_nothing_past_the_budget(monkeypatch, tmp_path):
    import random
    fake = HonestUsage(answer=lambda schema, body: {"action_id": schema["properties"]["action_id"]["enum"][0],
                                                    "reasoning_summary": "x"}
                       if "action_id" in (schema or {}).get("properties", {}) else
                       {"decision": "end", "message": "", "recipient": None})
    monkeypatch.setenv("NEBIUS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("NEBIUS_BUDGET_USD", "0.001")                    # room for ~3 of the ~6 requests of a game
    monkeypatch.setattr(lc, "_SHARED", {(): lc.LLMClient.from_env(transport=httpx.MockTransport(fake))})
    seats = [ls.LLMWerewolfSeat(), ls.LLMWerewolfSeat()]                 # the shared client, from the environment
    g = we.Game([seats[0], we.OfficialBaseline(), seats[1]] + [we.OfficialBaseline() for _ in range(4)],
                random.Random(4))
    g.play()
    c = lc.shared_client()
    assert c.stats.refused > 0 and c.stats.calls == len(fake.requests) > 0
    peak, _, led = replay(tmp_path / "ledger.jsonl")
    assert peak <= 0.001 + 1e-12 and led.totals["usd"] <= 0.001
    assert sum(g.stats[k][i] for k in ("crash", "illegal") for i in (0, 2)) == 0


def test_gzip_bodies_are_still_read_normally(tmp_path):
    """The DecodingError guard above must not charge a correct compressed reply as unknown."""
    body = json.dumps({"choices": [{"message": {"content": "{}"}}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
    c = client(tmp_path, lambda r: httpx.Response(200, headers={"content-encoding": "gzip"},
                                                  content=gzip.compress(body)))
    comp = c.chat(MSGS)
    assert comp.cost_usd == pytest.approx(lc.cost_usd(lc.DEFAULT_MODEL, 10, 5))
