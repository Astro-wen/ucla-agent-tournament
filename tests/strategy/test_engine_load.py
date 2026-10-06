"""The engine search on a slow, contended or memory-short machine (agent/pokemon/engine.py, policy._search_pick): a
search that overruns finishes in the background instead of its process being killed, a request that had to wait
shrinks its safety net, a hung process is still replaced, a machine-wide cap on search processes (memory) turns a match
to the classic pick loudly, repeated slow searches switch to one sample, and the health line reads what really
happened (agent/health.py)."""
import json
import logging
import queue
import time

import pytest
from altruagent import DecisionContext, GameState

from agent import health, matchstate
from agent.agent import Contestant
from agent.pokemon import dex, engine
from agent.pokemon import policy as policy_mod
from agent.pokemon.policy import PokemonPolicy

needs_engine = pytest.mark.skipif(not engine.available()[0], reason="needs node and pokemon-showdown")


@pytest.fixture
def eng(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_LOCK_DIR", str(tmp_path / "locks"))
    made = []

    def new():
        e = engine.SearchEngine()
        made.append(e)
        return e
    yield new
    for e in made:
        e.close()


def probe_request(hard_ms: int = 3900):
    req = json.loads((dex.DATA / engine.PROBE_FILE).read_text())
    return req["state"], {**req["params"], "hard_ms": hard_ms}


def wait_for(pred, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ---- a slow search is not killed ------------------------------------------------------------------------------------
@needs_engine
def test_an_overrun_search_finishes_in_the_background(eng):
    e = eng()
    assert e.warm(30)
    pid = e.proc.pid
    state, params = probe_request(hard_ms=20000)
    assert e.search(state, params, 0.05) is None and e.timeouts == 1 and e._owed is not None
    assert e.search(state, probe_request()[1], 0.2) is None          # still finishing it: busy, not killed
    assert e.busy == 1 and e.proc is not None and e.proc.pid == pid and e.proc.poll() is None
    assert wait_for(lambda: e._owed[0].qsize() > 0)                  # its late answer arrives
    ans = e.search(state, probe_request()[1], 30)
    assert ans is not None and isinstance(ans.get("action"), dict)
    assert e.late == 1 and e.starts == 1 and e.proc.pid == pid and e.errors == 0


@needs_engine
def test_a_hung_process_is_still_replaced(eng, monkeypatch):
    e = eng()
    assert e.warm(30)
    pid = e.proc.pid
    e._owed = (queue.Queue(), 999, time.monotonic() - 60, 0.1)       # owes an answer for a minute
    assert e.search(*probe_request(), 5) is None
    assert e.hung == 1 and e.errors == 1 and "hung" in e.last_error
    assert e.proc is not None and e.proc.pid != pid                  # restarted at once (first failure: no backoff)
    assert e.warm(30) and e.search(*probe_request(), 30) is not None


@needs_engine
def test_a_request_that_waited_for_the_warm_up_shrinks_its_net(eng):
    e = eng()
    sent = []
    orig = e._send

    def spy(msg):
        sent.append(msg)
        return orig(msg)
    e._send = spy
    state, params = probe_request(hard_ms=3900)
    t0 = time.monotonic()
    e.search(state, params, 3.0)                                     # cold: spawn + load + search in 3 s
    nets = [m["params"]["hard_ms"] for m in sent if m.get("cmd") == "search"]
    if nets:                                                         # (not sent when the load took the 3 s)
        assert nets[0] <= 2400 and nets[0] < 3900
    assert time.monotonic() - t0 < 3.5
    f = eng()
    assert f.search(state, params, 0.2) is None and "not warm" in f.last_error
    assert f.proc is not None and f.proc.poll() is None and f.errors == 0   # left loading for the next decision


@needs_engine
def test_a_search_cut_by_its_net_still_answers():
    e = engine.SearchEngine(admit=False)
    try:
        assert e.warm(30)
        state, params = probe_request(hard_ms=1)
        ans = e.search(state, params, 30)
        assert ans is not None and ans["info"].get("cut") and isinstance(ans.get("action"), dict)
    finally:
        e.close()


@needs_engine
def test_probe_answers_a_real_search():
    good, why = engine.probe(60, search=True)
    assert good and "a search answered in" in why, why


# ---- the machine-wide cap -------------------------------------------------------------------------------------------
@needs_engine
def test_a_full_machine_plays_classic_until_a_slot_frees(eng, monkeypatch, caplog):
    monkeypatch.setenv("ARENA_SEARCH_MAX_PROCS", "1")
    monkeypatch.setattr(engine, "ADMIT_RETRY_S", 0.0)
    a, b = eng(), eng()
    assert a.warm(30)
    with caplog.at_level(logging.ERROR):
        assert not b.warm(5) and b.proc is None and b.denied == 1
    assert b.last_error.startswith("no search slot free") and "ENGINE SEARCH NOT STARTED" in caplog.text
    assert b.search(*probe_request(), 5) is None
    a.close()                                                        # a match ends: its slot is free
    assert b.warm(30) and b.search(*probe_request(), 30) is not None
    assert b.stats()["slot"] == "1/1"


def test_low_memory_refuses_a_new_search_process(eng, monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "1")                          # the deployed agent (see the next test)
    monkeypatch.setattr(engine, "mem_available_mb", lambda: 120)
    e = eng()
    if not e.enabled:
        pytest.skip("no engine")
    assert not e.warm(2) and e.proc is None and e.last_error.startswith("low memory: 120 MB")


@needs_engine
def test_local_runs_are_never_refused_a_search_process(eng, monkeypatch):
    """Local simulators, ladders, panels, replays and tests (ARENA_RECORD=0, no knob): a local game never plays the
    classic pick because other jobs hold the machine's slots or its memory is low (a panel with --jobs 6 and a champion
    of this code runs 12 search processes: more than the 11 slots of a 7.5 GB machine)."""
    monkeypatch.setenv("ARENA_RECORD", "0")
    for k in ("ARENA_SEARCH_MAX_PROCS", "ARENA_SEARCH_MIN_FREE_MB"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(engine, "_meminfo", lambda: {"MemTotal": 1024, "MemAvailable": 300})   # 1 slot, low memory
    monkeypatch.setattr(engine, "_cgroup_mem", lambda: (None, None))
    assert not engine.admission_on() and engine.search_cap() == 1
    a, b = eng(), eng()
    assert a.warm(30) and b.warm(30) and a.denied == b.denied == 0
    monkeypatch.setenv("ARENA_RECORD", "1")                          # the deployed agent: the cap holds
    c = eng()
    assert engine.admission_on() and not c.warm(2) and c.denied == 1 and c.proc is None
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setenv("ARENA_SEARCH_MAX_PROCS", "5")                # an explicit knob applies anywhere
    assert engine.admission_on()


def test_the_cap_follows_the_memory(monkeypatch):
    monkeypatch.delenv("ARENA_SEARCH_MAX_PROCS", raising=False)
    for total, cg, want in ((1024, None, 1), (2048, None, 2), (4096, None, 5), (8192, None, 12), (8192, 2048, 2)):
        monkeypatch.setattr(engine, "_meminfo", lambda t=total: {"MemTotal": t, "MemAvailable": t // 2})
        monkeypatch.setattr(engine, "_cgroup_mem", lambda c=cg: (c, c))
        assert engine.search_cap() == want, (total, cg)
    monkeypatch.setenv("ARENA_SEARCH_MAX_PROCS", "3")
    assert engine.search_cap() == 3
    monkeypatch.setenv("ARENA_SEARCH_MAX_PROCS", "0")
    assert engine.search_cap() == 0                                  # no cap


def test_cgroup_headroom_counts_reclaimable_page_cache(monkeypatch):
    mb = 2 ** 20
    files = {"/proc/self/cgroup": "0::/user.slice/arena.service\n",
             "/sys/fs/cgroup/user.slice/arena.service/memory.max": f"{1024 * mb}\n",
             "/sys/fs/cgroup/user.slice/arena.service/memory.current": f"{900 * mb}\n",
             "/sys/fs/cgroup/user.slice/arena.service/memory.stat": f"anon {500 * mb}\ninactive_file {300 * mb}\n",
             "/sys/fs/cgroup/user.slice/memory.max": "max\n"}
    monkeypatch.setattr(engine, "_read", lambda p: files.get(p))
    assert engine._cgroup_mem() == (1024, 1024 - 900 + 300)
    files["/proc/self/cgroup"] = "0::/\n"                           # no limit anywhere
    assert engine._cgroup_mem() == (None, None)


def test_mem_available_takes_the_tighter_of_host_and_cgroup(monkeypatch):
    monkeypatch.setattr(engine, "_meminfo", lambda: {"MemAvailable": 3000})
    monkeypatch.setattr(engine, "_cgroup_mem", lambda: (1024, 400))
    assert engine.mem_available_mb() == 400
    monkeypatch.setattr(engine, "_cgroup_mem", lambda: (None, None))
    assert engine.mem_available_mb() == 3000


# ---- the policy under load ------------------------------------------------------------------------------------------
class FakeEngine:
    enabled = True
    timeouts = 0
    last_error = None

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def start_background(self):
        pass

    def warm(self, timeout=0.0):
        return True

    def search(self, state, params, timeout, battle_json=None, grace=0.0):
        self.calls.append({"params": dict(params), "timeout": timeout, "grace": grace})
        a = self.answers.pop(0) if self.answers else None
        if a == "timeout":
            self.timeouts += 1
            self.last_error = "no answer in 5.10s"
            return None
        return a


def _battle_policy(monkeypatch, answers):
    """A search-mode Contestant through the draft and preview of a real match, its engine replaced by a fake."""
    fake = FakeEngine(answers)
    monkeypatch.setattr(engine.SearchEngine, "get", classmethod(lambda cls: fake))
    rs = [json.loads(line) for line in open(dex.repo_root() / "logs" / "yoriko" /
                                             "0495293f-a47a-4bf6-bdc3-00acaba14709.jsonl")]
    c = Contestant()
    c.pk.battle_mode = "search"
    return c, fake, rs


def _decide(c, rec):
    raw = rec["raw"]
    return c.choose_action(GameState.from_mcp_state(raw), DecisionContext(
        session_id=raw["session_id"], tournament_id=None, game_type=raw["game_type"], agent_id="A"))


def test_slow_searches_switch_to_one_sample_and_back(monkeypatch, caplog):
    monkeypatch.delenv("ARENA_SEARCH_SAMPLES", raising=False)
    c, fake, rs = _battle_policy(monkeypatch, ["timeout", "timeout"])
    battle = [r for r in rs if r["raw"]["phase"] == "moving"]
    for r in rs[:7]:                                     # draft + team preview
        _decide(c, r)
    with caplog.at_level(logging.WARNING):
        for r in battle[:2]:                             # two timeouts: one sample from now on (loud)
            _decide(c, r)
    assert c.pk.search_lite and "one sample per search" in caplog.text
    for r in battle[2:4]:
        _decide(c, r)
    samples = [call["params"].get("samples") for call in fake.calls]
    assert samples[:2] == [None, None] and samples[2:] == [1, 1], samples
    assert all(0 < call["grace"] <= policy_mod.SEARCH_GRACE_S for call in fake.calls)
    assert all(abs(call["params"]["hard_ms"] - 800 * call["timeout"]) <= 1 for call in fake.calls)  # net unchanged
    assert c.last_decision["battle"] == "classic"        # (the fake answers nothing)
    pol = c.pk
    for _ in range(policy_mod.LITE_RECOVER - 1):         # fast searches in a row: back to the full search
        pol._search_slow({"ms": 300}, 3900)
    assert pol.search_lite
    pol._search_slow({"ms": 300}, 3900)
    assert not pol.search_lite and pol.search_slow == 0
    pol2 = PokemonPolicy()
    monkeypatch.setenv("ARENA_SEARCH_SAMPLES", "2")      # a pinned sample count is never changed
    pol2._search_slow(None, 3900, timed_out=True)
    pol2._search_slow(None, 3900, timed_out=True)
    assert not pol2.search_lite
    monkeypatch.delenv("ARENA_SEARCH_SAMPLES")
    pol3 = PokemonPolicy()                               # answers cut by their net (a big matrix, at any load):
    for _ in range(4):                                   # counted, never the one-sample mode
        pol3._search_slow({"cut": True, "ms": 3200}, 3900)
    assert not pol3.search_lite and pol3.search_slow == 0 and pol3.search_stats["cut"] == 4


def test_degradation_is_loud_counted_and_recorded(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("ARENA_RECORD", "1")
    from agent import agent as agent_mod
    monkeypatch.setattr(agent_mod, "RECORD_DIR", str(tmp_path / "rec"))
    c, fake, rs = _battle_policy(monkeypatch, ["timeout"] * 20)
    with caplog.at_level(logging.WARNING):
        for r in rs:
            _decide(c, r)
    assert "ENGINE SEARCH DEGRADED" in caplog.text
    ss = c.pk.search_stats
    assert ss["degraded"] == ss["decisions"] > 0
    rows = [json.loads(line) for line in open(tmp_path / "rec" / f"{rs[0]['raw']['session_id']}.jsonl")]
    battle = [r for r in rows if r["decision"]["kind"] == "battle"]
    assert battle and all(r["decision"]["battle"] == "classic" and not r["search"]["used"]
                          and r["search"]["why"].startswith("engine: no answer") for r in battle)
    cnt = c.counters()
    assert cnt["battle"] == len(battle) and cnt["used"] == 0 and sum(cnt["machine"].values()) == len(battle)
    assert "used=0" in c.summary_line() and "degraded=" in c.summary_line()


# ---- the health line ----------------------------------------------------------------------------------------------
def test_health_reads_the_matches_live_counters(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(matchstate, "_dir", None)
    monkeypatch.setenv("ARENA_BATTLE", "search")
    assert health.live() is None
    matchstate.write_health("a", "s1", {"mode": "search", "battle": 20, "used": 19, "machine": {}})
    matchstate.write_health("a", "s2", {"mode": "search", "battle": 20, "used": 19, "machine": {}})
    assert health.search_status(allow_probe=False) == "ok(used=38/40)"
    matchstate.write_health("a", "s3", {"mode": "search", "battle": 20, "used": 1, "lite": True,
                                        "machine": {"engine: no answer in 4.83s": 10, "engine: not warm in time": 9}})
    v = health.search_status(allow_probe=False)
    assert v == "DEGRADED(used=39/60,no_answer=10,not_warm=9,lite=1)", v
    monkeypatch.setenv("ARENA_BATTLE", "classic")
    assert health.search_status() == "classic(ARENA_BATTLE)"


def test_health_probes_when_idle_and_caches_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(matchstate, "_dir", None)
    monkeypatch.setenv("ARENA_BATTLE", "search")
    calls = []

    def fake_probe(timeout=60.0, search=False):
        calls.append(search)
        return True, "simulator loads in 1.0s, a search answered in 1.7s; search.js in x"
    monkeypatch.setattr(engine, "probe", fake_probe)
    assert health.search_status() == "idle(probe=1.7s)"
    assert health.search_status() == "idle(probe=1.7s)" and calls == [True]      # cached
    monkeypatch.setattr(engine, "probe", lambda timeout=60.0, search=False: (False, "no node"))
    assert health.search_status(every_s=0) == "FAILED(no_node)"
