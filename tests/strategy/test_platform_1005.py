"""The Pokemon payload and runtime changes of the platform docs of 10-04/05 (skill/pokemon, diffed against
docs/platform/pokemon.md), each checked against our code with real recorded payloads:

  (a) the battle's decision counter (`state_version`) starts again at 0 when the battle begins (12 -> 0 after the
      draft; "never assume it only goes up"): the restart memory (agent/matchstate.py) orders states by (phase,
      state_version), so a worker restarted right after the drop resumes with the drafted sets;
  (b) `legal_actions` can be missing while `is_current_actor` is true (`next_actions` says get_legal_actions): the
      runner fetches them, the policy reads them through agent._raw_with_legal and answers exactly the same;
  (c) a finished session is deleted ~5 min after the end (SESSION_NOT_FOUND afterwards) and a broken battle server ends
      a match with termination_reason "failed": a worker that finds its session gone is restarted only while the seat
      is still assigned, once per cooldown, exactly like a worker whose match finished;
  (d) -1 / -2 are fixed board positions (slot 0: -1 itself, -2 its ally; slot 1 the reverse), see `board_position`;
  (e) both actives fainted with one reserve: one slot switches it in, the other passes (the template carries a
      `notice`); never the reserve twice, never pass + pass, whatever path answers;
  (f) wait_for_update answers `updated: false` after its timeout: the runner just waits again;
  (g) teambuild: optional `ivs`; a team that is there but malformed now uses up one of the 8 attempts;
  and the one change that needed code: every draft pick now has a 15 s clock (`decision_timeout_seconds`; then the
  server picks a random card for us), so a draft decision's budget fits inside it (agent/timecontrol.draft_seconds),
  and a last pick the server made for us still reaches the battle (policy._preview recovers our own sets)."""
import copy
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import matchstate
from agent import timecontrol as tc
from agent.agent import Contestant, doubles_ok
from agent.fallback import concrete, doubles_problems
from agent.pokemon import calc as calc_mod
from agent.pokemon import drafter as drafter_mod
from agent.pokemon import engine
from agent.pokemon import preview as preview_matrix
from agent.pokemon.calc import DamageCalc
from agent.pokemon.policy import PokemonPolicy
from agent.reasoning import unwrap
from altruagent import DecisionContext, GameState, LegalAction, WithReasoning
from altruagent.mcp_transport import MCPToolError
from altruagent.runner import _validate_decision, run_game

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "logs"
GAME = "pokemon_vgc_doubles_draft"
# A full real match (Yoriko_Red, 09-29): our six picks are rows 0-5 (draft state_version 1..10, the opponent makes the
# 12th pick: 12 picks in all), team preview row 6 (state_version 0 again), battle rows 7-17 (1..11).
MATCH = "yoriko/0495293f-a47a-4bf6-bdc3-00acaba14709.jsonl"
LAST_PICK, PREVIEW, FIRST_TURN = 5, 6, 7
HAVE_ENGINE = shutil.which("node") is not None and engine._paths() is not None


@pytest.fixture(autouse=True)
def _no_recording(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")             # replaying real payloads must not record them again


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_STATE", "1")
    monkeypatch.setenv("ARENA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setattr(matchstate, "_dir", None)
    return tmp_path / "state"


@pytest.fixture
def steady(monkeypatch):
    """No wall-clock cut in the draft search or the preview game (answers depend only on the state)."""
    monkeypatch.setenv("ARENA_TC_PREVIEW_S", "120")
    monkeypatch.setenv("ARENA_TC_DRAFT_S", "120")
    monkeypatch.setitem(preview_matrix.KNOBS, "wall_cap_s", 60.0)
    monkeypatch.setattr(drafter_mod, "SAFETY_WALL_S", 60.0)


def recs(rel=MATCH) -> list[dict]:
    return [json.loads(line) for line in open(LOGS / rel)]


def with_1005_fields(raw: dict) -> dict:
    """A recorded state as the platform sends it since 10-05: a draft state carries its pick clock and the auto-pick
    fields (pokemon.md section 4)."""
    raw = copy.deepcopy(raw)
    obs = raw.get("observation")
    if raw.get("phase") == "draft" and isinstance(obs, dict):
        obs.update({"decision_timeout_seconds": 15, "decision_deadline_at": "2026-10-16T17:00:15+00:00",
                    "battle_starting": False})
        for p in obs.get("picks") or []:
            p.setdefault("auto", False)
            p.setdefault("auto_reason", None)
    return raw


def set_version(raw: dict, v: int) -> dict:
    """The same state at another decision counter (state, legal actions and every action's input)."""
    raw = copy.deepcopy(raw)
    raw["state_version"] = v
    la = raw.get("legal_actions")
    if isinstance(la, dict):
        la["state_version"] = v
        for a in la.get("actions") or []:
            if isinstance(a.get("input"), dict) and "state_version" in a["input"]:
                a["input"]["state_version"] = v
    return raw


def ctx_of(raw: dict, seat=0) -> DecisionContext:
    return DecisionContext(session_id=raw["session_id"], tournament_id=None, game_type=GAME,
                           agent_id=raw["current_actor"]["agent_id"], seat_position=seat)


def norm(out) -> str:
    out = unwrap(out)
    if isinstance(out, LegalAction):
        return out.action_id
    return json.dumps(out, sort_keys=True, default=str)


def replay(raws: list[dict], c: Contestant | None = None) -> tuple[list[str], Contestant]:
    """Every state answered by one Contestant, directly (what an uninterrupted worker answers)."""
    c = c or Contestant()
    return [norm(c.choose_action(GameState.from_mcp_state(r), ctx_of(r))) for r in raws], c


# ================================================================ a platform session, played by the official runner
class Session:
    """One seat's MCP session replayed from a real recording, the way the tools deliver it since 10-05: after each of
    our answers a waiting state (the opponent's pick; the battle placeholder), `timeouts` wait_for_update answers with
    `updated: false`, then our next decision; after our last draft pick the opponent's pick and the battle-starting
    state (draft counter 12), then team preview at counter 0. `drop`: decision indices delivered without
    `legal_actions` (`next_actions` says get_legal_actions). `die_at`: the worker is killed while waiting for that
    decision (the official runtime then starts a new worker on the seat). Records every call."""

    def __init__(self, raws, *, start=0, drop=(), timeouts=1, die_at=None, termination="completed"):
        self.raws, self.k, self.drop, self.timeouts, self.die_at = raws, start, set(drop), timeouts, die_at
        self.termination = termination
        self.sent, self.waits, self.legal_calls = [], [], 0
        self.queue: list[dict] = []
        self.current = self._deliver(start)

    # -- what the server sends
    def _deliver(self, k: int) -> dict:
        raw = copy.deepcopy(self.raws[k])
        raw["updated"] = True
        if k in self.drop:
            raw["legal_actions"] = None
            raw["next_actions"] = [{"tool": "get_legal_actions", "hint": "The state changed while it was being read: "
                                                                        "call get_legal_actions."}]
        return raw

    def _waiting(self, prev: dict, version: int, phase: str, **extra) -> dict:
        me = prev["current_actor"]["agent_id"]
        if phase == "draft":
            obs = copy.deepcopy(prev["observation"])
            opp = next((s for s in (obs.get("rosters") or {}) if s != me), "opponent")
            obs.update({"current_seat": opp, **extra})
            la = {"session_id": prev["session_id"], "state_version": version, "actions": []}
            actor = {"agent_id": opp, "position": 1 - prev["current_actor"].get("position", 0)}
        else:
            obs = {"message": "No pending decision is currently available.", "waiting_for_action": False,
                   "finished": False}
            la, actor = None, None
        return {"session_id": prev["session_id"], "game_type": prev["game_type"], "runtime_adapter": "pokemon",
                "status": "in_progress", "state_version": version, "observation": obs, "phase": phase,
                "is_terminal": False, "is_current_actor": False, "current_actor": actor, "legal_actions": la,
                "next_actions": [{"tool": "wait_for_update", "hint": "Not your turn."}]}

    def _after_answer(self) -> None:
        prev, nxt = self.raws[self.k - 1], self.raws[self.k]
        sv = prev["state_version"] + 1
        if prev["phase"] == "draft" and nxt["phase"] == "draft":
            waits = [self._waiting(prev, sv, "draft")]
        elif prev["phase"] == "draft":
            waits = [self._waiting(prev, sv, "draft"),                    # the opponent's last pick, then
                     self._waiting(prev, sv + 1, "draft", draft_complete=True, battle_starting=True)]   # 12 -> 0
        else:
            waits = [self._waiting(prev, sv, nxt["phase"])]
        self.queue = [*waits, *([None] * self.timeouts), "next"]
        self.current = self.queue.pop(0)

    # -- the MCP tools (MCPGameSession's signatures)
    def get_state(self) -> GameState:
        return GameState.from_mcp_state(self.current)

    def get_legal_actions(self) -> dict:
        self.legal_calls += 1
        return copy.deepcopy(self.raws[self.k]["legal_actions"])

    def play_action(self, *, action_id=None, action=None, state_version, reasoning_summary=None) -> dict:
        want = self.raws[self.k]["legal_actions"]["state_version"]
        assert state_version == want, f"decision {self.k}: sent state_version {state_version}, the server has {want}"
        self.sent.append((action_id if action is None else json.dumps(action, sort_keys=True), reasoning_summary))
        self.k += 1
        if self.k == len(self.raws):
            last = self.raws[-1]
            self.current = {"session_id": last["session_id"], "game_type": last["game_type"], "phase": "moving",
                            "status": "completed", "state_version": last["state_version"] + 1, "is_terminal": True,
                            "is_current_actor": False, "observation": {"message": "Battle finished.", "finished": True},
                            "legal_actions": None, "next_actions": [{"tool": "get_result"}]}
            return {"accepted": True, "session_id": last["session_id"]}
        self._after_answer()
        return {"accepted": True, "state_version": self.current["state_version"], "state": self.current}

    def wait_for_update(self, *, since_version, since_message_seq=None, since_is_current_actor=None, since_phase=None,
                        timeout_seconds=None) -> GameState:
        self.waits.append((since_version, since_is_current_actor, since_phase))
        nxt = self.queue.pop(0)
        if nxt is None:                                  # the timeout: nothing changed
            return GameState.from_mcp_state({**self.current, "updated": False})
        if nxt == "next":
            if self.die_at == self.k:
                raise RuntimeError("worker killed")
            nxt = self._deliver(self.k)
        self.current = nxt
        return GameState.from_mcp_state(nxt)

    def get_result(self) -> dict:
        me = self.raws[0]["current_actor"]["agent_id"]
        return {"session_id": self.raws[0]["session_id"], "is_terminal": True, "status": "completed",
                "returns": {} if self.termination == "failed" else {me: 1.0},
                "winner_agent_id": None if self.termination == "failed" else me,
                "termination_reason": self.termination}


def platform_rows(battle_from_zero=False) -> list[dict]:
    raws = [with_1005_fields(r["raw"]) for r in recs()]
    if battle_from_zero:                 # (if the counter restarted at the battle proper too, not at team preview)
        raws = raws[:FIRST_TURN] + [set_version(r, r["state_version"] - 1) for r in raws[FIRST_TURN:]]
    return raws


def test_real_matches_restart_the_counter_and_progress_never_goes_back():
    """What the recordings show (140 VGC matches): the draft counts 0..11 (both seats' picks), team preview is 0 again,
    the battle counts on from there; matchstate.progress, which decides whether a saved state applies, never goes back
    along a real match (a later state never looks older than an earlier one)."""
    n = 0
    for f in sorted(LOGS.glob("*/*.jsonl")):
        raws = [json.loads(line)["raw"] for line in open(f)]
        if raws[0].get("game_type") != GAME:
            continue
        n += 1
        phases = [r["phase"] for r in raws]
        assert phases[:6] == ["draft"] * 6 and phases[6] == "team_preview" and set(phases[7:]) == {"moving"}, f
        assert raws[5]["state_version"] in (10, 11) and raws[6]["state_version"] == 0, f
        prog = [matchstate.progress(r, "pokemon") for r in raws]
        assert all(a <= b for a, b in zip(prog, prog[1:])), (f, prog)
    assert n == 140


@pytest.mark.parametrize("restart", ["preview", "battle"])
def test_the_official_runner_plays_a_real_match_through_the_10_05_session(restart, store, steady, monkeypatch):
    """The whole real match through the SDK's own run_game, as the platform now runs it: wait_for_update timing out
    with `updated: false` between decisions (f), every other decision delivered without `legal_actions` (b), the
    draft counter reaching 12 and the battle starting again at 0 (a; "battle": the counter restarting at the first
    battle turn instead of at team preview), and the worker killed right after the drop with the restart memory on: a
    new worker (fresh Contestant, as the official runtime starts one) takes over the seat. The answers sent are exactly
    those of one uninterrupted Contestant reading complete states, each sent with the state's own counter, and the new
    worker resumes the drafted sets."""
    raws = platform_rows(battle_from_zero=restart == "battle")
    at = PREVIEW if restart == "preview" else FIRST_TURN
    monkeypatch.setenv("ARENA_STATE", "0")
    want, whole = replay(raws)
    monkeypatch.setenv("ARENA_STATE", "1")
    ctx = ctx_of(raws[0])

    first = Session(raws, drop=range(1, len(raws), 2), timeouts=2, die_at=at)
    a = Contestant()
    with pytest.raises(RuntimeError, match="worker killed"):
        run_game(first, ctx, a)
    assert len(first.sent) == at and first.legal_calls == len(range(1, at, 2))
    assert 12 in [v for v, _, _ in first.waits]                       # waited at the draft's end: counter 12

    second = Session(raws, start=at, drop=range(1, len(raws), 2), timeouts=1)
    b = Contestant()
    final = run_game(second, ctx, b)
    assert final.is_terminal and final.termination_reason == "completed"
    assert [s for s, _ in first.sent + second.sent] == want
    assert b.resumed == matchstate.progress(raws[at - 1], "pokemon") and b.resumed < matchstate.progress(raws[at],
                                                                                                         "pokemon")
    assert b.pk.opp_cards == whole.pk.opp_cards and b.pk.cards == whole.pk.cards and b.pk.opp_cards
    assert a.fallbacks == b.fallbacks == 0 and a.joint_repairs == b.joint_repairs == 0
    assert all(w[1] is False for w in first.waits + second.waits)      # since_is_current_actor=false, as the doc asks


def test_the_10_05_draft_fields_change_no_answer(steady):
    """`decision_timeout_seconds`, `decision_deadline_at`, `battle_starting` and the picks' `auto` fields are read
    for the time budget only: every answer of the real match is the same with and without them."""
    raws = [r["raw"] for r in recs()]
    assert replay(raws)[0] == replay([with_1005_fields(r) for r in raws])[0]


# ================================================================ the 15 s draft clock (the change that needed code)
def draft_raw(clock=None) -> dict:
    raw = with_1005_fields(recs()[0]["raw"])
    if clock is None:
        raw["observation"].pop("decision_timeout_seconds")
    else:
        raw["observation"]["decision_timeout_seconds"] = clock
    return raw


def test_a_draft_pick_budget_fits_inside_its_clock():
    """The draft budget used to be 20 s (+2 s watchdog grace): past the 15 s after which the server picks a random card
    for us. Now it is the clock minus DRAFT_RESERVE_S, at most DRAFT_S."""
    d = tc.budget_for(draft_raw(15), GAME)
    assert d.kind == "draft" and d.source == "clock" and d.seconds == 10.0
    assert d.hard - d.start <= 15 - 2.5                     # >= 2.5 s left for noticing the turn and the round trips
    d = tc.budget_for(draft_raw(None), GAME)                # a state without the field: the documented 15 s
    assert d.seconds == 10.0 and d.source == "default"
    assert tc.budget_for(draft_raw(30), GAME).seconds == tc.DRAFT_S
    assert tc.budget_for(draft_raw(8), GAME).seconds == 3.0
    assert tc.budget_for(draft_raw(2), GAME).seconds == tc.MIN_S
    for junk in ("15", True, -3, float("nan"), None, {}):
        raw = draft_raw(15)
        raw["observation"]["decision_timeout_seconds"] = junk
        assert tc.budget_for(raw, GAME).seconds == 10.0
    raw = draft_raw(None)
    raw["decision_timeout_seconds"] = 12                    # (at the top level instead)
    assert tc.budget_for(raw, GAME).seconds == 7.0


SLOW_NODE = r'''
import json, sys, time
time.sleep(float(sys.argv[1]))
for line in sys.stdin:
    msg = json.loads(line)
    print(json.dumps({"id": msg["id"], "results": [
        {"rolls": [1], "defender_hp": 100, "move": c["move"]} for c in msg["calcs"]]}), flush=True)
'''


def test_a_cold_calculator_cannot_make_the_first_pick_miss_its_clock(tmp_path, monkeypatch):
    """A match process's first decision waits for node + @smogon/calc to load (DamageCalc.warm; seconds on a cold or
    loaded machine). That wait was bounded by the 20 s draft budget only: with a calculator that takes 8 s to start
    and a 6 s pick clock (scaled down from 15 s), the first pick went out after the clock. It is now bounded by the
    pick's budget; the pick is legal and in time, and the calculator keeps starting for the next decisions."""
    script = tmp_path / "slow_node.py"
    script.write_text(SLOW_NODE)
    monkeypatch.setattr(calc_mod, "_js_paths", lambda: (tmp_path, tmp_path))
    dc = DamageCalc()
    dc.enabled = True
    dc._command = lambda _script: [sys.executable, str(script), "8"]
    monkeypatch.setattr(DamageCalc, "_inst", dc)
    raw = draft_raw(6)
    try:
        c = Contestant()
        t = time.monotonic()
        out = unwrap(c.choose_action(GameState.from_mcp_state(raw), ctx_of(raw)))
        took = time.monotonic() - t
        assert took < 6 - 2, took
        ids = {a["action_id"] for a in raw["legal_actions"]["actions"]}
        assert isinstance(out, LegalAction) and out.action_id in ids
        assert dc.starts == 1 and dc.errors == 0 and dc.enabled         # still starting, not killed
    finally:
        dc._kill()


def test_a_hung_draft_decision_is_answered_before_the_pick_clock(monkeypatch):
    raw = draft_raw(3)                                       # budget: the 0.5 s floor
    c = Contestant()
    orig = c.pk.act
    monkeypatch.setattr(c.pk, "act", lambda st: time.sleep(4) or orig(st))
    t = time.monotonic()
    out = unwrap(c.choose_action(GameState.from_mcp_state(raw), ctx_of(raw)))
    assert time.monotonic() - t < 3 - 1.5
    assert isinstance(out, LegalAction) and out.action_id.startswith("draft_pick:")
    assert c.last_decision["path"] == "overrun"


def test_a_last_pick_the_server_made_for_us_still_reaches_the_battle(steady):
    """Our 11th pick (row 5, our last) runs out of its 15 s and the server picks for us (`"auto": true`): our worker
    never answers that pick, so the drafted set never entered the draft memory, and the battle played that Pokemon
    with a generic set. Team preview now recovers our own sets from every card seen in the draft, as it already did
    for the opponent's: the same memory and the same battle as when we made the pick ourselves (the server's random
    card is simulated by the recorded one)."""
    raws = [with_1005_fields(r["raw"]) for r in recs()]
    whole, a = replay(raws)
    skipped, b = replay(raws[:LAST_PICK] + raws[LAST_PICK + 1:])
    assert b.pk.cards == a.pk.cards and b.pk.opp_cards == a.pk.opp_cards
    assert skipped[LAST_PICK:] == whole[LAST_PICK + 1:]


# ================================================================ (c) a session gone after the end, "failed"
class FakeOfficial:
    control_url = "https://control.example.test"

    def __init__(self, grant):
        self._grant = grant

    def grant(self, seat_id, execution_id):
        return self._grant

    def renew_lease(self, seat_id, execution_id):
        return {"seat_id": seat_id, "execution_lease_expires_at": "x"}

    def close(self):
        pass


def official_worker(game) -> int:
    from altruagent.models import SeatGrant
    from altruagent.worker import TournamentWorkerInput, run_tournament_worker

    raw = recs()[0]["raw"]
    grant = SeatGrant.from_dict({"access_token": "seat-jwt-FAKE", "agent_id": raw["current_actor"]["agent_id"],
                                 "seat_id": "seat-1", "gameapi_server_url": "https://gameapi.example.test",
                                 "game_session_id": raw["session_id"], "match_id": "match-1", "seat_position": 0,
                                 "seat_count": 2, "game_type": GAME, "match_status": "in_progress"})
    return run_tournament_worker(TournamentWorkerInput("seat-1", "match-1", GAME, "agent.agent:create_agent",
                                                       "exec-" + "q" * 40),
                                 official_factory=lambda: FakeOfficial(grant),
                                 run_game_fn=lambda _game, context, contestant: run_game(game, context, contestant))


class Gone:
    """A session cleared ~5 min after its end (or abandoned by a game-server restart): every tool answers
    SESSION_NOT_FOUND."""

    def __getattr__(self, tool):
        def call(*a, **k):
            raise MCPToolError(f"MCP tool {tool!r} failed: Session not found", status_code=None,
                               error_code="SESSION_NOT_FOUND")
        return call


def test_a_session_gone_after_the_end_is_retried_like_a_finished_one(capsys, steady):
    """A worker that sees its match end with termination_reason "failed" (the battle server broke: no winner) exits as
    finished. A worker started on the seat after the session was cleared gets SESSION_NOT_FOUND and exits as failed;
    the official runtime treats the two alike: one new worker per cooldown while the platform still lists the seat
    (the wait the 10-06 review measured, ~20 s apart at our 10 s cooldown), none once it is gone. No crash loop."""
    from altruagent.supervisor import TournamentState, run_tournament_once
    from altruagent.worker import EXIT_MATCH_FAILURE, EXIT_SUCCESS

    raws = platform_rows()
    assert official_worker(Session(raws[:FIRST_TURN + 2], termination="failed")) == EXIT_SUCCESS
    assert "termination_reason=failed" in capsys.readouterr().out
    assert official_worker(Gone()) == EXIT_MATCH_FAILURE

    def schedule(exitcode) -> list[float]:
        procs, starts, clock, listed = [], [], [0.0], [True]

        class Proc:
            def __init__(self, target=None, args=(), daemon=None):
                self.exitcode, self.pid = None, len(procs) + 1
                procs.append(self)

            def start(self):
                pass

            def is_alive(self):
                return self.exitcode is None

            def join(self, timeout=None):
                pass

            def terminate(self):
                self.exitcode = -15

        official = SimpleNamespace(assignments=lambda: [SimpleNamespace(seat_id="seat-1", match_id="match-1",
                                                                        game_type=GAME)] if listed[0] else [])
        state = TournamentState()
        for t in range(0, 180, 5):                    # a poll every 5 s; the seat stays listed for 60 s after the end
            clock[0], listed[0] = float(t), t < 60
            for p in procs:
                if p.exitcode is None:
                    p.exitcode = exitcode             # every worker ends at once: the match is over / gone
            n = len(procs)
            run_tournament_once(official, state, agent_spec="agent.agent:create_agent", now=lambda: clock[0],
                                cooldown_seconds=10.0, process_factory=Proc, log=lambda m: None)
            if len(procs) > n:
                starts.append(t)
        return starts

    gone, finished = schedule(EXIT_MATCH_FAILURE), schedule(EXIT_SUCCESS)
    assert gone == finished
    assert all(b - a >= 10 for a, b in zip(gone, gone[1:])) and gone and max(gone) < 60


# ================================================================ (d) fixed board positions
def doubles_rows():
    for f in sorted(LOGS.glob("*/*.jsonl")):
        for k, line in enumerate(open(f)):
            rec = json.loads(line)
            acts = (rec["raw"].get("legal_actions") or {}).get("actions") or []
            tmpl = ((acts[0].get("input") or {}).get("action") or {}) if acts else {}
            if isinstance(tmpl.get("slots"), list):
                yield f, k, rec, tmpl


def test_minus_one_and_minus_two_are_fixed_positions_in_every_real_template():
    """Slot 0 is board position -1 and slot 1 is -2 in every recorded doubles template; no move ever offers a slot its
    own position, so a negative target is the partner's: -2 from slot 0, -1 from slot 1, which is how
    agent/pokemon/lookahead.py (`("me", -t - 1)`) and agent/js/search.js (`-(allyPos + 1)`) read them. Every
    negative target we ever sent was the partner (Helping Hand, Pollen Puff from slot 1: -1)."""
    n = sent = 0
    for f, k, rec, tmpl in doubles_rows():
        for s in tmpl["slots"]:
            n += 1
            assert s["board_position"] == -(s["slot"] + 1), (f, k)
            for o in s.get("options") or []:
                assert -(s["slot"] + 1) not in (o.get("targets") or []), (f, k, o)
        out = rec.get("out") if isinstance(rec.get("out"), dict) else {}
        for i in (0, 1):
            c = out.get(f"slot_{i}")
            if isinstance(c, dict) and isinstance(c.get("target"), int) and c["target"] < 0:
                assert c["target"] == -((1 - i) + 1), (f, k, c)
                sent += 1
    assert n > 2000 and sent == 10


def test_our_answers_aim_ally_moves_at_the_partner_position(steady):
    """The states where we sent an ally-targeted move, answered by today's agent (a fresh process each): a negative
    target is always the partner's position, and every answer is a legal pair."""
    seen = 0
    for f, k, rec, tmpl in doubles_rows():
        out = rec.get("out") if isinstance(rec.get("out"), dict) else {}
        if not any(isinstance(out.get(f"slot_{i}"), dict) and (out[f"slot_{i}"].get("target") or 0) < 0
                   for i in (0, 1)):
            continue
        raw = rec["raw"]
        ans = unwrap(Contestant().choose_action(GameState.from_mcp_state(raw), ctx_of(raw)))
        assert isinstance(ans, dict) and doubles_ok(raw, ans), (f, k, ans)
        for i in (0, 1):
            c = ans[f"slot_{i}"]
            if c.get("type") == "move" and (c.get("target") or 0) < 0:
                assert c["target"] == -((1 - i) + 1), (f, k, c)
        seen += 1
    assert seen == 10


# ================================================================ (e) both actives fainted, one reserve
def notice_rows() -> list[dict]:
    return [rec["raw"] for _, _, rec, tmpl in doubles_rows() if tmpl.get("notice")]


@pytest.mark.parametrize("path", ["classic", "search", "crash", "overrun", "unfilled", "safe_action"])
def test_both_fainted_with_one_reserve_switches_once_and_passes_once(path, monkeypatch):
    """Every real state with the 10-05 notice (9, in 7 matches; f525d6ac's three are the 09-29 incident the runner
    died on): whatever path answers, exactly one slot switches the reserve in and the other passes."""
    rows = notice_rows()
    assert len(rows) == 9 and all("ONE reserve" in (r["legal_actions"]["actions"][0]["input"]["action"]["notice"])
                                  for r in rows)
    if path == "search":
        if not HAVE_ENGINE:
            pytest.skip("node / pokemon-showdown missing")
        monkeypatch.setenv("ARENA_BATTLE", "search")
    if path == "overrun":
        monkeypatch.setenv("ARENA_TC_BATTLE_S", "0.3")
        monkeypatch.setenv("ARENA_TC_MIN_S", "0.3")
    for raw in rows:
        tmpl = raw["legal_actions"]["actions"][0]["input"]["action"]
        reserve = {o["species"] for s in tmpl["slots"] for o in s["options"] if o["type"] == "switch"}
        assert len(reserve) == 1
        state = GameState.from_mcp_state(raw)
        if path == "safe_action":
            out = concrete(state.legal_actions[0], raw)
        else:
            c = Contestant()
            if path == "crash":
                monkeypatch.setattr(c.pk, "act", lambda st: 1 / 0)
            elif path == "overrun":
                orig = c.pk.act
                monkeypatch.setattr(c.pk, "act", lambda st, orig=orig: time.sleep(0.8) or orig(st))
            elif path == "unfilled":
                monkeypatch.setattr(c.pk, "act", lambda st, t=tmpl: {"action": copy.deepcopy(t)})
            out = c.choose_action(state, ctx_of(raw))
            if isinstance(out, WithReasoning):
                out = out.action
        _validate_decision(out, state.legal_actions)                  # what the runner checks before sending
        assert isinstance(out, dict) and not doubles_problems(tmpl, out) and doubles_ok(raw, out), (path, out)
        picks = [out["slot_0"], out["slot_1"]]
        assert sorted(p["type"] for p in picks) == ["pass", "switch"], (path, picks)
        assert {p.get("species") for p in picks if p["type"] == "switch"} == reserve


# ================================================================ (g) teambuild
SET_KEYS = {"species", "item", "ability", "nature", "evs", "ivs", "moves", "tera_type", "level"}
STATS = {"hp", "atk", "def", "spa", "spd", "spe"}


def well_formed(team) -> bool:
    """pokemon.md section 5: a list of six sets; each with species / item / ability / nature, four moves, EVs 0-252 per
    stat and at most 510 in all; optional tera_type, level, ivs (the shape of evs, 0-31)."""
    assert isinstance(team, list) and len(team) == 6
    assert len({s["species"] for s in team}) == 6
    for s in team:
        assert isinstance(s, dict) and set(s) <= SET_KEYS, s
        assert all(isinstance(s.get(k), str) and s[k] for k in ("species", "item", "ability", "nature")), s
        assert isinstance(s["moves"], list) and len(s["moves"]) == 4 and len(set(s["moves"])) == 4, s
        assert all(isinstance(m, str) and m for m in s["moves"]), s
        evs = s["evs"]
        assert isinstance(evs, dict) and set(evs) <= STATS and sum(evs.values()) <= 510, s
        assert all(isinstance(v, int) and 0 <= v <= 252 for v in evs.values()), s
        ivs = s.get("ivs", {})
        assert isinstance(ivs, dict) and set(ivs) <= STATS and all(isinstance(v, int) and 0 <= v <= 31
                                                                   for v in ivs.values()), s
    return True


@pytest.mark.parametrize("failed", range(8))
def test_every_team_we_submit_is_well_formed(failed):
    """A team that is there but malformed now uses up one of the 8 attempts (pokemon.md section 5, 10-05): every team
    we submit (the policy's, after any number of rejected ones, and the safe fallback's) is well formed."""
    obs = {"phase": "teambuild", "battle_format": "gen9ou", "you_submitted": False, "opponent_submitted": False,
           "fallback_used": False, "submit_attempts_failed": failed, "submit_attempts_max": 8,
           "submit_attempts_remaining": 8 - failed}
    raw = {"session_id": "s-tb", "game_type": "pokemon_gen9ou_teambuild", "phase": "teambuild", "state_version": failed,
           "is_current_actor": True, "current_actor": {"agent_id": "me", "position": 0}, "observation": obs,
           "legal_actions": {"state_version": failed, "actions": [{"action_id": "submit_team", "input": {"action": {
               "type": "submit_team", "instructions": "Submit a structured 6-Pokemon team."}}}]}}
    out = unwrap(Contestant().choose_action(GameState.from_mcp_state(raw), DecisionContext(
        session_id="s-tb", tournament_id=None, game_type="pokemon_gen9ou_teambuild", agent_id="me")))
    assert isinstance(out, dict) and out.get("type") == "submit_team" and well_formed(out["team"])
    assert well_formed(PokemonPolicy().act(raw)["action"]["team"])
    fb = concrete(GameState.from_mcp_state(raw).legal_actions[0], raw)
    assert fb["type"] == "submit_team" and well_formed(fb["team"])
