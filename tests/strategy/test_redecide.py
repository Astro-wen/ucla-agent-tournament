"""v4.2.5: a Pokemon move that did not land never counts as played.

PokemonPolicy writes its per-battle memory (who was on the field: Fake Out freshness; our last actions: Protect chains;
speed-control turns; the singles memory) as it answers a battle decision. The same decision point can be asked again:
the runner re-reads the state and asks again after a move it could not send (SDK PR #7) or after a race error, and a
restarted worker re-asks the one its predecessor died on (agent/matchstate.py). The second ask must start from the
memory of the first, or the move that never landed counts as played (turn 1: Fake Out "not fresh" any more; a Protect
"twice in a row", never). Since v4.2 a second ask restored that memory only under the same state_version and only in
doubles; now:

  * the decision point is the battle's position (`policy.decision_point`: turn, forced slots, both sides' actives, the
    battle events in the protocol log), whatever the state_version, in doubles and singles;
  * a move the server refused (Contestant.on_action_result: STALE_STATE / NOT_YOUR_TURN, the decision closed and the
    move timer played for us) is rolled back out of that memory, and out of the saved match state, unless an earlier
    send of the same point got no answer (it may have landed: then the refusal is our own move, kept as played) or
    the policy did not answer that decision (busy, overrun, fallback);
  * a battle or team-preview budget stays inside the platform's own countdown (`observation.clock`, live doc 10-08)
    when the state is read late (the Showdown timer line is as old as the request).

Driven through the real altruagent.runner.run_game with a scripted game session over real platform recordings
(logs/yoriko, logs/apocalypse), classic battle policy (tests/conftest.py)."""
import copy
import json
import threading
from pathlib import Path

import pytest
from altruagent import DecisionContext, GameState, LegalAction, WithReasoning
from altruagent.mcp_transport import MCPToolError
from altruagent.runner import run_game

from agent import matchstate
from agent import timecontrol as tc
from agent.agent import REFUSED_MOVE, Contestant
from agent.pokemon import drafter as drafter_mod
from agent.pokemon import preview as preview_matrix
from agent.pokemon import singles as singles_mod
from agent.pokemon.policy import PokemonPolicy, decision_point
from tests.strategy.test_resend_after_hiccup import Game
from tests.test_runner import result_dict

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "logs"
FAKE_OUT = "apocalypse/141b61de-f7fc-41e8-b065-80441aed0f6b.jsonl"     # Fake Out on turn 1 (both slots)
PROTECT = "apocalypse/dbff3ec4-8ba7-40de-8449-41db5ee98794.jsonl"      # Protect in 9 of 11 battle decisions
DOUBLE_FAINT = "yoriko/f525d6ac-e0a9-4b66-ba7c-a57c7ceb323d.jsonl"     # forced switches, both slots (rows 10-12)
SINGLES = ["yoriko/c8efbea9-7ef0-4b14-9843-9d3006c45101.jsonl",         # pokemon_gen9same (singles)
           "apocalypse/c8efbea9-7ef0-4b14-9843-9d3006c45101.jsonl"]
MEM = PokemonPolicy._TURN_MEMORY


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Answers depend only on the state (no wall-clock cut in the draft search or the preview game, as det.sh)."""
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setenv("ARENA_REASONING", "off")
    monkeypatch.setenv("ARENA_TC_PREVIEW_S", "120")
    monkeypatch.setenv("ARENA_TC_DRAFT_S", "120")
    monkeypatch.setitem(preview_matrix.KNOBS, "wall_cap_s", 60.0)
    monkeypatch.setattr(drafter_mod, "SAFETY_WALL_S", 60.0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_STATE", "1")
    monkeypatch.setenv("ARENA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(matchstate, "_dir", None)
    return tmp_path / "state"


def raws(rel) -> list[dict]:
    return [json.loads(line)["raw"] for line in open(LOGS / rel) if json.loads(line).get("kind") == "action"]


def bumped(raw: dict, by: int = 1) -> dict:
    """The same state read again, its state_version raised by `by` (a re-read is the same decision point whatever its
    version: the platform's counter moves with our decisions, pokemon.md section 8, not with the battle)."""
    out = copy.deepcopy(raw)
    out["state_version"] = (raw.get("state_version") or 0) + by
    return out


def ctx_of(raw: dict, agent_id: str = "a1") -> DecisionContext:
    return DecisionContext(session_id=raw.get("session_id") or "pk", tournament_id=None,
                           game_type=raw.get("game_type") or "pokemon_vgc_doubles_draft", agent_id=agent_id,
                           seat_position=0)


def sent(call) -> str:
    return json.dumps([call["action_id"], call["action"]], sort_keys=True)


def norm(out) -> str:
    if isinstance(out, WithReasoning):
        out = out.action
    if isinstance(out, LegalAction):
        return out.action_id
    return json.dumps(out, sort_keys=True, default=str)


def memory(pol: PokemonPolicy) -> str:
    """The policy's match memory (what a restarted worker would take over), minus the re-ask counter."""
    m = pol.export_memory()
    m.pop("redecided", None)
    return json.dumps(matchstate.encode(m), sort_keys=True)


def moves(answers, *ids) -> int:
    return sum(1 for a in answers if any(f'"move_id": "{i}"' in a for i in ids))


def through_runner(rows: list[dict], fail=None, bump: int = 0, agent=None):
    """Play one recorded match of ours (its action states, in order) through run_game. `fail`: every decision's first
    send fails with this error (a factory) and the runner re-reads the state (state_version + `bump`) and asks again."""
    game = Game(session_id=rows[0]["session_id"])
    for raw in rows:
        game.queue_state(GameState.from_mcp_state(raw))
        if fail is not None:
            game.queue_state(GameState.from_mcp_state(bumped(raw, bump)))
            game.queue_play_action(fail())
        game.queue_play_action({"accepted": True, "status": "in_progress"})
    game.queue_state(GameState.from_mcp_state({**rows[-1], "is_terminal": True, "is_current_actor": False,
                                               "legal_actions": None, "status": "completed"}))
    game.queue_result(result_dict())
    agent = agent or Contestant()
    run_game(game, ctx_of(rows[0]), agent, sleep=lambda s: None, log=lambda m: None)
    return [sent(c) for c in game.play_action_calls], agent


# ---- the decision point ---------------------------------------------------------------------------------------------
def test_a_re_read_is_the_same_decision_point_and_every_later_decision_another():
    rows = raws(DOUBLE_FAINT)
    battle = [r for r in rows if r.get("phase") == "moving"]
    points = [decision_point(r, r["observation"]) for r in battle]
    for r, p in zip(battle, points):
        assert decision_point(bumped(r, 1), r["observation"]) == p          # another version: the same point
        late = copy.deepcopy(r["observation"])                              # timer / chat lines while it waits
        late["protocol_log"] = list(late.get("protocol_log") or []) + [
            "|inactive|Time left: 30 sec this turn | 300 sec total", "|c|☆opp|gl hf", "|inactive|Opp has 20 seconds left."]
        assert decision_point(r, late) == p
        assert matchstate.decode(json.loads(json.dumps(matchstate.encode(p)))) == p    # survives the saved state
    # rows 10-12 of the recording: the double faint asked three times (same version), everything else distinct
    assert points[3] == points[4] == points[5]
    assert len(set(points[:4])) == 4
    # within one turn, two decisions forcing the same slot (our U-turn mid-turn, a faint later that turn) differ
    st = battle[1]
    later = copy.deepcopy(st["observation"])
    later["protocol_log"] = later["protocol_log"] + ["|switch|p1a: Whimsicott|Whimsicott, L50|100/100",
                                                     "|move|p2a: Gyarados|Waterfall|p1a: Whimsicott",
                                                     "|-damage|p1a: Whimsicott|0 fnt", "|faint|p1a: Whimsicott"]
    assert decision_point(st, later) != decision_point(st, st["observation"])
    # no battle turn to go by: only the same version is the same decision
    blind = {k: v for k, v in st["observation"].items() if k != "turn"}
    assert decision_point(st, blind) != decision_point(bumped(st, 1), blind)
    assert decision_point(st, blind) == decision_point(copy.deepcopy(st), blind)


# ---- PR #7: a move the runner could not send, decided again from the re-read state -----------------------------------
@pytest.mark.parametrize("bump", [0, 1])
@pytest.mark.parametrize("rel", [FAKE_OUT, PROTECT])
def test_a_move_that_could_not_be_sent_is_decided_again_from_the_same_memory(rel, bump):
    """Every decision's first send fails (HTTP 503: the runner re-reads, the version unchanged or raised); the moves
    that go out, each sent twice, are exactly the undisturbed match's, and so is the memory at the end."""
    rows = raws(rel)
    once, a = through_runner(rows)
    twice, b = through_runner(rows, fail=lambda: MCPToolError("MCP request failed with HTTP 503", status_code=503),
                              bump=bump)
    assert twice[0::2] == twice[1::2] == once, rel
    assert memory(b.pk) == memory(a.pk)
    n_battle = sum(1 for r in rows if r.get("phase") == "moving")
    assert b.pk.redecided == n_battle and a.pk.redecided == 0 and b.decisions == 2 * len(rows)
    assert moves(once, "fakeout") >= 1 if rel == FAKE_OUT else moves(once, "protect", "detect", "spikyshield") >= 3


@pytest.mark.parametrize("bump", [0, 1])
def test_turn_one_fake_out_survives_being_asked_again(bump):
    """The plainest case: turn 1, both leads fresh. The first ask plays Fake Out and remembers both leads as on the
    field; before v4.2.5 a second ask under another version read that memory, found them not fresh and dropped it."""
    rows = raws(FAKE_OUT)
    k = next(i for i, r in enumerate(rows) if r.get("phase") == "moving")
    c = Contestant()
    for r in rows[:k]:
        c.choose_action(GameState.from_mcp_state(r), ctx_of(r))
    first = norm(c.choose_action(GameState.from_mcp_state(rows[k]), ctx_of(rows[k])))
    after_first = {m: copy.deepcopy(getattr(c.pk, m)) for m in MEM}
    again = norm(c.choose_action(GameState.from_mcp_state(bumped(rows[k], bump)), ctx_of(rows[k])))
    assert '"fakeout"' in first and again == first
    assert {m: getattr(c.pk, m) for m in MEM} == after_first


# ---- a move the server refused ------------------------------------------------------------------------------------
STALL = ("protect", "detect", "spikyshield", "kingsshield", "banefulbunker", "silktrap", "burningbulwark", "obstruct")


def first_protect(rows) -> int:
    """The first battle decision of a recorded match where our (classic) policy Protects with a slot."""
    c = Contestant()
    for i, r in enumerate(rows):
        out = norm(c.choose_action(GameState.from_mcp_state(r), ctx_of(r)))
        if r.get("phase") == "moving" and moves([out], *STALL):
            return i
    raise AssertionError("no Protect in this match")


@pytest.mark.parametrize("code", ["STALE_STATE", "NOT_YOUR_TURN", "WRONG_PHASE"])
def test_a_refused_move_is_not_remembered_as_played(code, store):
    """Our Protect is refused (STALE_STATE / NOT_YOUR_TURN: the decision had closed and the move timer played a random
    choice for us); the runner reports it (on_action_result) and the next state is the next turn. That decision does
    not see the Protect (no "twice in a row"; our last actions unknown) but does see the turn that happened (who was on
    the field), and the match state a restarted worker would resume says the same."""
    rows = raws(PROTECT)
    k = first_protect(rows)
    assert rows[k + 1].get("phase") == "moving" and rows[k + 1]["observation"]["turn"] > rows[k]["observation"]["turn"]
    ref = Contestant()                                     # the move landed: what the next decision would start from
    for r in rows[:k + 1]:
        ref.choose_action(GameState.from_mcp_state(r), ctx_of(r))
    landed = {m: copy.deepcopy(getattr(ref.pk, m)) for m in MEM}
    c = Contestant()
    for r in rows[:k]:
        c.choose_action(GameState.from_mcp_state(r), ctx_of(r))
    before = {m: copy.deepcopy(getattr(c.pk, m)) for m in MEM}
    seen: list = []
    real = c.pk._track_conditions
    c.pk._track_conditions = lambda obs: (seen.append({m: copy.deepcopy(getattr(c.pk, m)) for m in MEM}), real(obs))
    game = Game(session_id=rows[k]["session_id"])
    game.queue_state(GameState.from_mcp_state(rows[k]), GameState.from_mcp_state(rows[k + 1]),
                     GameState.from_mcp_state({**rows[k + 1], "is_terminal": True, "is_current_actor": False,
                                               "legal_actions": None, "status": "completed"}))
    game.queue_play_action(MCPToolError(f"{code}: refused", status_code=None, error_code=code),
                           {"accepted": True, "status": "in_progress"})
    game.queue_result(result_dict())
    saved = []
    hook = c.on_action_result

    def spy(result, context):
        hook(result, context)
        saved.append(matchstate.load("a1", rows[k]["session_id"], 0))
    c.on_action_result = spy

    run_game(game, ctx_of(rows[k]), c, sleep=lambda s: None, log=lambda m: None)

    assert moves([sent(game.play_action_calls[0])], *STALL)
    assert seen[0] == before                                # the refused decision itself
    nxt = seen[1]                                           # the next turn's decision starts from:
    assert any(mv in STALL for _, mv in landed["last_move"].values())          # (had it landed: "Protected last")
    assert nxt["last_move"] == {} and nxt["last_protect"] == before["last_protect"] != landed["last_protect"]
    for m in ("prev_active", "prev_opp", "opp_prev_active", "cond_set"):       # ... and the turn did happen
        assert nxt[m] == landed[m], m
    assert c.refused_moves == 1 and c.pk.redecided == 0
    pk = saved[0]["pk"]
    assert {m: pk[m] for m in MEM} == nxt and saved[0]["counters"]["refused_moves"] == 1
    assert "refused_moves=1" in c.summary_line()


@pytest.mark.parametrize("code", ["WRONG_PHASE", "RUNTIME_TEMPORARILY_UNAVAILABLE"])
def test_a_refused_move_then_the_same_decision_again_restores_all_of_it(code, store):
    """The move is refused (a race, or a busy engine: a transient error, not rolled back) and the re-read is the same
    decision (still ours): it is decided from exactly the memory of its first ask (a partial rollback is undone by the
    full restore of `_same_decision`)."""
    rows = raws(FAKE_OUT)
    k = next(i for i, r in enumerate(rows) if r.get("phase") == "moving")
    once, _ = through_runner(rows[:k + 1])
    game = Game(session_id=rows[0]["session_id"])
    for r in rows[:k]:
        game.queue_state(GameState.from_mcp_state(r))
        game.queue_play_action({"accepted": True, "status": "in_progress"})
    game.queue_state(GameState.from_mcp_state(rows[k]), GameState.from_mcp_state(bumped(rows[k], 1)),
                     GameState.from_mcp_state({**rows[k], "is_terminal": True, "is_current_actor": False,
                                               "legal_actions": None, "status": "completed"}))
    game.queue_play_action(MCPToolError(f"{code}: refused", status_code=None, error_code=code),
                           {"accepted": True, "status": "in_progress"})
    game.queue_result(result_dict())
    c = Contestant()
    run_game(game, ctx_of(rows[0]), c, sleep=lambda s: None, log=lambda m: None)
    calls = [sent(x) for x in game.play_action_calls]
    assert calls[-1] == calls[-2] == once[-1] and moves([once[-1]], "fakeout")
    assert c.refused_moves == (1 if code in REFUSED_MOVE else 0) and c.pk.redecided == 1


def test_other_errors_and_landed_moves_keep_the_memory(store):
    """A 5xx with a code (the move may have landed), an accepted move, a Werewolf result or junk: nothing rolls back,
    and the hook never raises (the runner would end the match)."""
    rows = raws(FAKE_OUT)
    k = next(i for i, r in enumerate(rows) if r.get("phase") == "moving")
    c = Contestant()
    for r in rows[:k + 1]:
        c.choose_action(GameState.from_mcp_state(r), ctx_of(r))
    after = {m: copy.deepcopy(getattr(c.pk, m)) for m in MEM}
    for result in ({"error": "INTERNAL_ERROR", "detail": "500"}, {"accepted": True, "status": "in_progress"},
                   {"error": "RUNTIME_TEMPORARILY_UNAVAILABLE", "detail": "busy"},
                   {"error": None}, {"error": ["x"]}, None, "STALE_STATE", 3):
        c.on_action_result(result, ctx_of(rows[k]))
    assert {m: getattr(c.pk, m) for m in MEM} == after and c.refused_moves == 0
    for path in ("fallback", "overrun", "crash", "busy", None):     # the policy did not answer the refused decision
        c.last_decision = {**c.last_decision, "path": path}
        c.on_action_result({"error": "STALE_STATE"}, ctx_of(rows[k]))
        assert {m: getattr(c.pk, m) for m in MEM} == after and c.refused_moves == 0, path
    c.last_decision = {**c.last_decision, "path": "policy"}
    c.pk._asked = None                                       # the latest decision was no battle decision
    c.on_action_result({"error": "STALE_STATE"}, ctx_of(rows[k]))
    assert {m: getattr(c.pk, m) for m in MEM} == after and c.refused_moves == 0
    ww = Contestant()
    ww._family = "werewolf"
    ww.on_action_result({"error": "STALE_STATE"}, ctx_of(rows[k]))
    assert ww.refused_moves == 0


def _send_error(e: str) -> MCPToolError:
    if e.isdigit():                                          # a gateway error: no MCP code, the runner just re-reads
        return MCPToolError(f"MCP request failed with HTTP {e}", status_code=int(e))
    return MCPToolError(f"{e}: refused", status_code=None, error_code=e)


@pytest.mark.parametrize("errors,landed_kept,rollbacks", [
    (("504", "NOT_YOUR_TURN"), True, 0),             # the 504'd send landed: the resend is "already submitted"
    (("504", "STALE_STATE"), True, 0),               # ... or "wrong version" (the landed move moved it)
    (("WRONG_PHASE", "504", "NOT_YOUR_TURN"), True, 1),   # a refusal first, then an unanswered send that landed
    (("WRONG_PHASE", "NOT_YOUR_TURN"), False, 2),    # refused every time: the timer played for us
])
def test_a_resend_refused_after_an_unanswered_send_keeps_that_move(errors, landed_kept, rollbacks, store):
    """Our Protect is sent and the answer is lost (HTTP 504: no MCP code, so no on_action_result), but the server
    applies the move a moment later; the runner's re-read is served before that (same state, still our decision), so
    the same point is decided again and sent again, and that send is refused BECAUSE the first one landed. The next
    turn must remember the Protect (else it Protects twice in a row, which fails 2 times in 3), as before v4.2.5. A
    refusal of the previous ask of the point (the server answered) does not count as an unanswered send."""
    rows = raws(PROTECT)
    k = first_protect(rows)
    ref = Contestant()
    for r in rows[:k + 1]:
        ref.choose_action(GameState.from_mcp_state(r), ctx_of(r))
    landed = {m: copy.deepcopy(getattr(ref.pk, m)) for m in MEM}
    c = Contestant()
    for r in rows[:k]:
        c.choose_action(GameState.from_mcp_state(r), ctx_of(r))
    before = {m: copy.deepcopy(getattr(c.pk, m)) for m in MEM}
    seen: list = []
    real = c.pk._track_conditions
    c.pk._track_conditions = lambda obs: (seen.append({m: copy.deepcopy(getattr(c.pk, m)) for m in MEM}), real(obs))
    game = Game(session_id=rows[k]["session_id"])
    game.queue_state(*[GameState.from_mcp_state(rows[k]) for _ in errors], GameState.from_mcp_state(rows[k + 1]),
                     GameState.from_mcp_state({**rows[k + 1], "is_terminal": True, "is_current_actor": False,
                                               "legal_actions": None, "status": "completed"}))
    game.queue_play_action(*[_send_error(e) for e in errors], {"accepted": True, "status": "in_progress"})
    game.queue_result(result_dict())
    run_game(game, ctx_of(rows[k]), c, sleep=lambda s: None, log=lambda m: None)
    calls = [sent(x) for x in game.play_action_calls]
    assert len(calls) == len(errors) + 1 and len(set(calls[:-1])) == 1 and moves(calls[:1], *STALL)
    assert all(s == before for s in seen[:len(errors)])     # every ask of the point: the memory from before it
    nxt = seen[len(errors)]
    if landed_kept:
        assert nxt == landed
    else:
        assert nxt["last_move"] == {} and nxt["last_protect"] == before["last_protect"] != landed["last_protect"]
        for m in ("prev_active", "prev_opp", "opp_prev_active", "cond_set"):
            assert nxt[m] == landed[m], m
    assert c.refused_moves == rollbacks and c.pk.redecided == len(errors) - 1


def test_a_refusal_on_the_busy_path_leaves_the_previous_decision_alone(store):
    """Decision k (our Protect) lands. Decision k+1 is answered by the busy path (an earlier overrun policy call is
    still running, so the policy is not asked and its latest point is still k's); k+1's fallback is refused after that
    call finished. Nothing rolls back: k's Protect was played."""
    rows = raws(PROTECT)
    k = first_protect(rows)
    c = Contestant()
    c.watchdog = True
    for r in rows[:k + 1]:
        c.choose_action(GameState.from_mcp_state(r), ctx_of(r))
    after_k = {m: copy.deepcopy(getattr(c.pk, m)) for m in MEM}
    assert any(mv in STALL for _, mv in after_k["last_move"].values())
    hold = threading.Event()
    busy = threading.Thread(target=hold.wait, daemon=True)
    busy.start()
    c._busy = busy
    try:
        c.choose_action(GameState.from_mcp_state(rows[k + 1]), ctx_of(rows[k + 1]))
    finally:
        hold.set()
        busy.join()
    assert c.last_decision["path"] == "busy"
    c.on_action_result({"error": "STALE_STATE", "detail": "x"}, ctx_of(rows[k + 1]))
    assert {m: getattr(c.pk, m) for m in MEM} == after_k and c.refused_moves == 0


# ---- a restarted worker re-asked the decision its predecessor died on -------------------------------------------------
def test_a_restarted_worker_asked_the_same_decision_under_another_version_answers_the_same(store):
    """Worker A answers decision k and dies before the move is sent; worker B (a fresh Contestant with A's saved state)
    reads the decision again with a raised state_version: B answers as A did and leaves A's memory."""
    rows = raws(FAKE_OUT)
    sid = rows[0]["session_id"]
    a = Contestant()
    outs, snaps = [], []
    for r in rows:
        outs.append(norm(a.choose_action(GameState.from_mcp_state(r), ctx_of(r, "A"))))
        snaps.append(matchstate.load("A", sid, 0))
    for k, r in enumerate(rows):
        if r.get("phase") != "moving":
            continue
        matchstate.save("B", sid, snaps[k], seat=0)
        b = Contestant()
        assert norm(b.choose_action(GameState.from_mcp_state(bumped(r, 1)), ctx_of(r, "B"))) == outs[k], k
        assert b.resumed is not None and b.pk.redecided == 1
        mine, theirs = matchstate.load("B", sid, 0)["pk"], snaps[k]["pk"]
        mine.pop("redecided"), theirs.pop("redecided")
        assert mine == theirs, k


# ---- singles --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("rel", SINGLES)
def test_a_singles_decision_asked_again_starts_from_the_same_singles_memory(rel, monkeypatch):
    """singles.decide remembers who was out and whether it Protected (singles_prev) as it answers; a second ask (any
    version) sees the memory the first ask saw and answers the same."""
    seen: list = []
    real = singles_mod.decide

    def spy(pol, st, obs):
        seen.append(copy.deepcopy(pol.singles_prev))
        return real(pol, st, obs)
    monkeypatch.setattr(singles_mod, "decide", spy)
    once, twice = Contestant(), Contestant()
    n = 0
    for r in raws(rel):
        want = norm(once.choose_action(GameState.from_mcp_state(r), ctx_of(r)))
        if r.get("phase") != "moving":
            twice.choose_action(GameState.from_mcp_state(r), ctx_of(r))
            continue
        seen.clear()
        assert norm(twice.choose_action(GameState.from_mcp_state(r), ctx_of(r))) == want
        assert norm(twice.choose_action(GameState.from_mcp_state(bumped(r, 1)), ctx_of(r))) == want
        assert len(seen) == 2 and seen[0] == seen[1]
        assert twice.pk.singles_prev == once.pk.singles_prev
        n += 1
    assert n >= 30 and twice.pk.redecided == n


# ---- the platform's clock (pokemon.md section 8, live doc 10-08) ----------------------------------------------------
def clocked(clock, *lines, phase="moving"):
    obs = {"protocol_log": ["|turn|3", *lines]}
    if clock is not None:
        obs["clock"] = clock
    return {"phase": phase, "game_type": "pokemon_vgc_doubles_draft", "observation": obs}


STALE = "|inactive|Time left: 55 sec this turn | 420 sec total"


@pytest.mark.parametrize("clock,line,want,source", [
    ({"decision_seconds_left": 41, "bank_seconds_left": 312}, STALE, 10.0, "clock"),     # the doc's example: the cap
    ({"decision_seconds_left": 12, "bank_seconds_left": 380}, STALE, 4.5, "clock"),      # read late: (12 - 3) x 0.5
    ({"decision_seconds_left": 4}, STALE, 0.5, "clock"),                                 # floor
    ({"bank_seconds_left": 100}, STALE, 3.5, "clock"),                                   # (100 - 30) / 20
    ({"decision_seconds_left": 30, "bank_seconds_left": 30}, None, 0.5, "clock"),       # no timer line, little bank
    ({"decision_seconds_left": "12"}, STALE, 10.0, "timer"),                             # junk clocks: ignored
    ({"decision_seconds_left": -1, "bank_seconds_left": float("nan")}, STALE, 10.0, "timer"),
    ({"decision_seconds_left": True}, None, 10.0, "default"),
    ([12, 300], None, 10.0, "default"),
    (None, None, 10.0, "default"),
])
def test_a_battle_budget_stays_inside_the_platform_clock(clock, line, want, source):
    d = tc.budget_for(clocked(clock, *([line] if line else [])), "pokemon_vgc_doubles_draft")
    assert d.kind == "battle" and d.seconds == pytest.approx(want) and d.source == source
    if source == "clock" and isinstance(clock, dict) and clock.get("decision_seconds_left") is not None:
        assert d.hard - d.start < clock["decision_seconds_left"]          # even the watchdog answers in time


@pytest.mark.parametrize("clock,want,source", [
    ({"decision_seconds_left": 85}, 30.0, "clock"),
    ({"decision_seconds_left": 20}, 8.5, "clock"),
    ({"bank_seconds_left": 10}, 30.0, "default"),          # team preview does not use the bank
    (None, 30.0, "default"),
])
def test_a_team_preview_budget_stays_inside_the_platform_clock(clock, want, source, monkeypatch):
    monkeypatch.delenv("ARENA_TC_PREVIEW_S")                   # (the deployed budget, not this file's steady one)
    d = tc.budget_for(clocked(clock, phase="team_preview"), "pokemon_vgc_doubles_draft")
    assert d.kind == "preview" and d.seconds == pytest.approx(want) and d.source == source
