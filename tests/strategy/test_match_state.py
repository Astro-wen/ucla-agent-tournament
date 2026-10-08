"""A match worker restarted mid-match (the official runtime: a new process, a new create_agent()) plays on exactly as an
uninterrupted one (agent/matchstate.py, Contestant._bind / _after_decision), plus the store's own guarantees (private,
atomic, bounded, pruned, keyed by a sanitised id), the robustness fixes that ship with it (a doubles template listed
after another action, game type spellings, a card without moves, the same decision asked twice, a restarted chat
window) and what a decision records about itself (decision path, search outcome, per-match summary)."""
import json
import logging
import os
import random
import stat
import time
from pathlib import Path

import pytest
from altruagent import DecisionContext, GameState, LegalAction, SendMessage, WithReasoning

from agent import agent as agent_mod
from agent import matchstate
from agent.agent import Contestant, game_kind
from agent.pokemon import drafter as drafter_mod
from agent.pokemon import preview as preview_matrix

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "logs"
# real platform recordings: a full v4 match, Fake Out on turn 1, the double faint (f525d6ac), the blind doubles state
# (4b85cc32 row 16)
SESSIONS = ["yoriko/0495293f-a47a-4bf6-bdc3-00acaba14709.jsonl",
            "apocalypse/141b61de-f7fc-41e8-b065-80441aed0f6b.jsonl",
            "yoriko/f525d6ac-e0a9-4b66-ba7c-a57c7ceb323d.jsonl",
            "apocalypse/4b85cc32-cbb0-4440-aa8d-2cfe28f92987.jsonl"]


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("ARENA_STATE", "1")
    monkeypatch.setenv("ARENA_STATE_DIR", str(d))
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setattr(matchstate, "_dir", None)
    return d


@pytest.fixture
def steady(monkeypatch):
    """No wall-clock cut in the draft search or the preview game (answers depend only on the state, like det.sh)."""
    monkeypatch.setenv("ARENA_TC_PREVIEW_S", "120")
    monkeypatch.setenv("ARENA_TC_DRAFT_S", "120")
    monkeypatch.setitem(preview_matrix.KNOBS, "wall_cap_s", 60.0)
    monkeypatch.setattr(drafter_mod, "SAFETY_WALL_S", 60.0)


def recs(rel):
    return [json.loads(line) for line in open(LOGS / rel)]


def norm(out):
    if isinstance(out, WithReasoning):
        out = out.action
    if isinstance(out, LegalAction):
        return out.action_id
    if isinstance(out, SendMessage):
        return ("chat", out.content, tuple(out.recipients or ()))
    return json.dumps(out, sort_keys=True, default=str)


def decide(c, rec, agent_id):
    raw = rec["raw"]
    ctx = DecisionContext(session_id=raw.get("session_id"), tournament_id=None, game_type=raw.get("game_type"),
                          agent_id=agent_id)
    gs = GameState.from_mcp_state(raw)
    return norm(c.choose_action(gs, ctx) if rec["kind"] == "action" else c.choose_message(gs, ctx))


def memory(text: str) -> dict:
    """A saved state minus what legitimately differs between the two instances (time, resume count)."""
    m = json.loads(text)
    m.pop("t", None)
    m.get("counters", {}).pop("resumes", None)
    return m


# ---- the store ------------------------------------------------------------------------------------------------------
def test_on_where_decisions_are_recorded_and_overridable(monkeypatch):
    for rec, st, want in (("1", "", True), ("0", "", False), (None, "", True), ("0", "1", True), ("1", "0", False)):
        monkeypatch.delenv("ARENA_RECORD", raising=False) if rec is None else monkeypatch.setenv("ARENA_RECORD", rec)
        monkeypatch.setenv("ARENA_STATE", st)
        assert matchstate.enabled() is want, (rec, st)


def test_typed_json_round_trip():
    x = {"t": (1, "a", (True, None)), "s": {"b", "a"}, "d": {0: ("x", None), 1: [1.5, {"k": {2}}]},
         (("me", "sp")): 3, "tag": {"__t": [1]}, "l": [(1,), set()], "f": 0.1 + 0.2}
    y = matchstate.decode(json.loads(json.dumps(matchstate.encode(x))))
    assert y == x
    assert isinstance(y["t"], tuple) and isinstance(y["s"], set) and isinstance(y["d"][0], tuple)
    assert y["tag"] == {"__t": [1]}                           # a real key that looks like a tag stays a dict
    with pytest.raises(TypeError):
        matchstate.encode({"x": object()})


def test_private_atomic_bounded_and_sanitised(store, monkeypatch):
    assert matchstate.save("agent/../x", "../../etc/passwd", {"game": "pokemon", "a": 1})
    files = [p for p in store.iterdir() if p.is_file()]
    assert len(files) == 1 and files[0].parent == store
    assert files[0].name == "agent____x--______etc_passwd.json"
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert matchstate.load("agent/../x", "../../etc/passwd") == {"game": "pokemon", "a": 1}
    assert matchstate.key("a", "") is None and not matchstate.save("a", None, {"x": 1})
    monkeypatch.setattr(matchstate, "MAX_BYTES", 200)
    assert not matchstate.save("agent/../x", "../../etc/passwd", {"big": "x" * 500})
    assert matchstate.load("agent/../x", "../../etc/passwd") == {"game": "pokemon", "a": 1}   # the old state stays
    assert not list(store.glob("*.tmp")) and not list(store.glob(".*.tmp"))


def test_a_crashed_write_leaves_the_previous_state(store, monkeypatch):
    assert matchstate.save("a", "s1", {"n": 1})

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(matchstate.os, "replace", boom)
    assert not matchstate.save("a", "s1", {"n": 2})
    assert matchstate.load("a", "s1") == {"n": 1}
    assert not [p for p in store.iterdir() if p.name.endswith(".tmp")]


def test_pruned_by_age_and_count(store, monkeypatch):
    matchstate.state_dir()
    now = time.time()
    for i in range(6):
        p = store / f"a--s{i}.json"
        p.write_text("{}")
        os.utime(p, (now - i * 100, now - i * 100))
    old = store / "a--old.json"
    old.write_text("{}")
    os.utime(old, (now - matchstate.TTL_S - 5, now - matchstate.TTL_S - 5))
    monkeypatch.setattr(matchstate, "MAX_FILES", 4)
    assert matchstate.prune(store, now) == 3
    assert sorted(p.name for p in store.glob("*.json")) == [f"a--s{i}.json" for i in range(4)]


def test_falls_back_to_a_private_temp_dir(tmp_path, monkeypatch):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    monkeypatch.setenv("ARENA_STATE_DIR", str(ro / "state"))
    monkeypatch.setattr(matchstate, "_dir", None)
    monkeypatch.setattr(matchstate.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    try:
        d = matchstate.state_dir()
        if os.geteuid() == 0:                                  # root writes anywhere
            return
        assert d is not None and str(d).startswith(str(tmp_path / "tmp"))
    finally:
        ro.chmod(0o700)


def test_a_new_game_never_resumes_the_last_one():
    """The local simulators reuse one session id: a Pokemon draft before our first pick, or any state earlier than the
    saved one, is a new match."""
    first = {"phase": "draft", "state_version": 1, "current_actor": {"agent_id": "me"},
             "observation": {"rosters": {"me": [], "you": [{"card_id": "c1"}]}}}
    later = {"phase": "moving", "state_version": 7, "observation": {"turn": 5}}
    saved = {"game": "pokemon", "progress": matchstate.progress(later, "pokemon")}
    assert not matchstate.resumable(saved, first, "pokemon")
    assert not matchstate.resumable(saved, {"phase": "moving", "state_version": 3}, "pokemon")
    assert matchstate.resumable(saved, later, "pokemon")
    assert matchstate.resumable(saved, {"phase": "moving", "state_version": 8}, "pokemon")
    assert not matchstate.resumable(saved, later, "werewolf")
    mid_draft = {"phase": "draft", "state_version": 5, "current_actor": {"agent_id": "me"},
                 "observation": {"rosters": {"me": [{"card_id": "c2"}]}}}
    assert matchstate.resumable({"game": "pokemon", "progress": [1, 2]}, mid_draft, "pokemon")
    ww = {"game": "werewolf", "progress": [2]}
    assert matchstate.resumable(ww, {"game_state": {"day": 2}}, "werewolf")
    assert matchstate.resumable(ww, {"observation": "Day 3 | phase day_vote"}, "werewolf")
    assert not matchstate.resumable(ww, {"game_state": {"day": 1}}, "werewolf")


# ---- a restarted worker plays exactly like an uninterrupted one ------------------------------------------------------
@pytest.mark.parametrize("rel", SESSIONS)
def test_a_restart_before_any_decision_resumes_exactly(rel, store, steady):
    """Worker A plays the whole recorded match; for every decision k a fresh Contestant B (a new worker that took over
    after A's decision k-1, with A's saved file) answers decision k exactly as A did and leaves exactly A's memory. A
    B asked A's decision k again (the worker died after answering, before the answer was sent) answers the same."""
    rs = recs(rel)
    sid = rs[0]["raw"]["session_id"]
    a = Contestant()
    outs, snaps = [], []
    for r in rs:
        outs.append(decide(a, r, "A"))
        snaps.append((store / f"A--{sid}.json").read_text())
    assert a.resumed is None and a.decisions == len(rs)
    for k in range(1, len(rs)):
        (store / f"B--{sid}.json").write_text(snaps[k - 1])
        b = Contestant()
        assert decide(b, rs[k], "B") == outs[k], (rel, k)
        assert b.resumed is not None
        assert memory((store / f"B--{sid}.json").read_text()) == memory(snaps[k]), (rel, k)
    for k in range(len(rs)):                                  # died after answering k, before sending it
        (store / f"B--{sid}.json").write_text(snaps[k])
        assert decide(Contestant(), rs[k], "B") == outs[k], (rel, k)


def test_without_the_store_a_restart_loses_the_draft(store, monkeypatch, steady):
    """What the store is for: without it a restarted worker knows neither set of the draft (robust-1 / intel-4)."""
    rs = recs(SESSIONS[0])
    sid = rs[0]["raw"]["session_id"]
    a = Contestant()
    for r in rs[:9]:
        decide(a, r, "A")
    assert a.pk.opp_cards and a.pk.cards and a.pk.opp_roster
    b = Contestant()
    decide(b, rs[9], "B")                                     # no saved state for B
    assert not b.pk.opp_cards and b.resumed is None
    (store / f"B--{sid}.json").write_text((store / f"A--{sid}.json").read_text())
    c = Contestant()
    decide(c, rs[9], "B")
    assert c.pk.opp_cards == a.pk.opp_cards and c.pk.cards == a.pk.cards and c.pk.opp_roster == a.pk.opp_roster
    assert c.pk.lineup_prior == a.pk.lineup_prior and c.resumed is not None


def test_the_same_decision_twice_answers_the_same(steady):
    """robust-3: the end of a doubles decision updates the battle memory (Fake Out freshness, Protect chains); a second
    ask of the same state (the runner re-asks after a race error) starts from the memory of the first ask."""
    rs = recs("apocalypse/141b61de-f7fc-41e8-b065-80441aed0f6b.jsonl")
    once, twice = Contestant(), Contestant()
    for k, r in enumerate(rs):
        want = decide(once, r, "A")
        assert decide(twice, r, "A") == want, k
        assert decide(twice, r, "A") == want, k              # (and `twice` goes on exactly like `once`)


def test_werewolf_restarted_at_every_decision_plays_the_same_games(store):
    """Local Werewolf games with our seat restarted before every decision and chat (a fresh Contestant each time,
    resuming from the store) are the same games, message for message, as with one uninterrupted Contestant."""
    from local import werewolf_env as we
    from local.werewolf_bots import HeuristicSeat, LLMSeat

    class Restarting(we.ContestantSeat):
        def reset(self):
            matchstate.remove("ours", "local")

        def choose(self, raw):
            self.agent = Contestant()
            return super().choose(raw)

        def message(self, raw):
            self.agent = Contestant()
            return super().message(raw)

    def play(g, ours):
        rng = random.Random(1000 + g)
        others = [rng.choice([HeuristicSeat, LLMSeat, we.RandomSeat])() for _ in range(6)]
        seat = g % 7
        seats = others[:seat] + [ours] + others[seat:]
        game = we.Game(seats, random.Random(g))
        game.play()
        return game, seat

    chats = 0
    for g in range(8):
        a, seat = play(g, we.ContestantSeat(Contestant))
        b, _ = play(g, Restarting(Contestant))
        assert a.messages == b.messages and a.votes == b.votes and a.winner == b.winner and a.log == b.log, g
        assert sum(b.stats[k][seat] for k in ("crash", "illegal", "bad_chat", "chat_after_terminate")) == 0
        chats += sum(1 for m in a.messages if m["sender"] == seat)
    assert chats > 5


@pytest.mark.parametrize("k", [3, 12])
def test_the_official_runtime_restarting_a_worker_resumes_the_match(k, store, steady, caplog):
    """The official tournament runtime (`python -m agent --tournament`, altruagent/worker.py run_tournament_worker, a
    fake official client, no network): a seat worker dies after decision k-1 of a real match (k=3 mid-draft, k=12 in
    the battle); the runtime's next worker for the seat gets a fresh create_agent() and the same grant (agent id, game
    session id), and plays the rest exactly as one uninterrupted worker."""
    from types import SimpleNamespace

    from altruagent.models import SeatGrant
    from altruagent.worker import EXIT_SUCCESS, TournamentWorkerInput, run_tournament_worker

    rs = recs(SESSIONS[0])
    sid = rs[0]["raw"]["session_id"]
    grant = SeatGrant.from_dict({"access_token": "seat-jwt-FAKE", "agent_id": "synthetic-1", "seat_id": "seat-1",
                                 "gameapi_server_url": "https://gameapi.example.test", "game_session_id": sid,
                                 "match_id": "match-1", "seat_position": 0, "seat_count": 2,
                                 "game_type": "pokemon_vgc_doubles_draft", "match_status": "in_progress"})

    class FakeOfficial:
        control_url = "https://control.example.test"

        def grant(self, seat_id, execution_id):
            return grant

        def renew_lease(self, seat_id, execution_id):
            return {"seat_id": seat_id, "execution_lease_expires_at": "x"}

        def close(self):
            pass

    def worker(rows, answers, dies=False):
        def run_game(game, context, contestant):
            for r in rows:
                gs = GameState.from_mcp_state(r["raw"])
                answers.append(norm(contestant.choose_action(gs, context)))
            if dies:
                raise RuntimeError("worker died")
            return SimpleNamespace(termination_reason="game_over", returns={})
        return run_tournament_worker(TournamentWorkerInput("seat-1", "match-1", "pokemon_vgc_doubles_draft",
                                                           "agent.agent:create_agent", "exec-" + "q" * 40),
                                     official_factory=FakeOfficial, run_game_fn=run_game)

    whole, split = [], []
    assert worker(rs, whole) == EXIT_SUCCESS
    matchstate.remove("synthetic-1", sid)
    assert worker(rs[:k], split, dies=True) != EXIT_SUCCESS
    assert (store / f"synthetic-1--{sid}--p0.json").is_file()              # (the grant's seat is part of the key)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent"):
        assert worker(rs[k:], split) == EXIT_SUCCESS
    assert split == whole
    assert f"resumed match {sid[:8]} after a worker restart" in caplog.text


def test_two_seats_of_one_agent_in_a_match_keep_their_own_memory(store, monkeypatch):
    """Self-play over claimed Testing seats (one process per seat, the same agent id and session id): a seat's first
    decision must not take over the memory another seat of ours saved (its chat quota, queue and said set, its private
    messages). Each seat keeps its own file, the games are the ones played without the store, and a restarted seat
    still resumes its own memory."""
    from altruagent import DecisionContext as Ctx

    from local import werewolf_env as we
    from local.werewolf_bots import HeuristicSeat, LLMSeat

    class Ours(we.ContestantSeat):
        def __init__(self, pos, restart=False):
            super().__init__(Contestant)
            self.pos, self.restart = pos, restart

        def _ctx(self):
            return Ctx(session_id="sess-selfplay", tournament_id=None, game_type="werewolf", agent_id="ours",
                       seat_position=self.pos)

        def choose(self, raw):
            if self.restart:
                self.agent = Contestant()
            return super().choose(raw)

        def message(self, raw):
            if self.restart:
                self.agent = Contestant()
            return super().message(raw)

    def play(g, on, restart=False):
        matchstate.remove("ours", "sess-selfplay")
        monkeypatch.setenv("ARENA_STATE", "1" if on else "0")
        rng = random.Random(1000 + g)
        seats = [Ours(p, restart and p == 5) if p in (2, 5) else rng.choice([HeuristicSeat, LLMSeat, we.RandomSeat])()
                 for p in range(7)]
        game = we.Game(seats, random.Random(g))
        game.play()
        return game, seats

    for g in (1, 9, 11):                                      # (games where the shared file changed the play)
        off, _ = play(g, False)
        on, seats = play(g, True)
        assert on.messages == off.messages and on.votes == off.votes and on.winner == off.winner, g
        assert seats[2].agent.resumed is None and seats[5].agent.resumed is None, g
        assert sorted(p.name for p in store.glob("ours--*.json")) == ["ours--sess-selfplay--p2.json",
                                                                      "ours--sess-selfplay--p5.json"]
        again, seats = play(g, True, restart=True)            # seat 5 restarted before every decision
        assert again.messages == off.messages and again.votes == off.votes, g
        assert seats[5].agent.resumed is not None and seats[2].agent.resumed is None, g


def test_a_saved_state_of_another_seat_never_applies(store):
    saved = {"game": "werewolf", "seat": 2, "progress": [2]}
    assert matchstate.resumable(saved, {"game_state": {"day": 2}}, "werewolf", seat=2)
    assert not matchstate.resumable(saved, {"game_state": {"day": 2}}, "werewolf", seat=5)
    assert matchstate.key("a", "s", 5) == "a--s--p5" and matchstate.key("a", "s", True) == "a--s"
    assert matchstate.save("a", "s", {"game": "werewolf", "n": 1}, seat=5)
    assert matchstate.load("a", "s") is None and matchstate.load("a", "s", 5) == {"game": "werewolf", "n": 1}
    matchstate.remove("a", "s")                                # every seat of the match
    assert matchstate.load("a", "s", 5) is None


def test_prune_never_removes_files_the_store_did_not_write(store, monkeypatch):
    """ARENA_STATE_DIR pointed at a shared directory (data/, the repo root): only the store's own files ever go."""
    matchstate.state_dir()
    old = time.time() - matchstate.TTL_S - 60
    keep = ["vgc_cards.json", "search_probe.json", "notes.tmp", "a-b.json", "x--y.jsonl"]
    for name in keep + ["a--s1.json", "a--s2--p3.json", ".a--s3.json.abc123_x.tmp"]:
        (store / name).write_text("{}")
        os.utime(store / name, (old, old))
    monkeypatch.setattr(matchstate, "MAX_FILES", 0)
    assert matchstate.prune(store) == 3
    assert sorted(p.name for p in store.iterdir() if p.is_file()) == sorted(keep)


def test_a_restarted_chat_window_keeps_the_quota_and_never_repeats(monkeypatch):
    """robust-9, without a saved state (another machine took over): our own chats in new_messages count."""
    class Talky:
        reactive_chat = False

        def observe(self, st):
            pass

        def chat(self, st):
            return [{"content": f"line {i}", "recipients": []} for i in range(5)]

    st = {"phase": "messaging", "your_position": 3, "game_state": {"day": 2}, "terminated_messaging": [],
          "new_messages": [{"seq": 1, "sender": 3, "type": "chat", "content": "line 0", "recipients": []},
                           {"seq": 2, "sender": 5, "type": "chat", "content": "hello", "recipients": []},
                           {"seq": 3, "sender": 3, "type": "chat", "content": "line 1", "recipients": []},
                           {"seq": 0, "sender": 3, "type": "chat", "content": "line 4", "recipients": [], "day": 1}]}
    ctx = DecisionContext(session_id="s", tournament_id=None, game_type="werewolf", agent_id="a")
    c = Contestant()
    c.ww = Talky()
    sent = []
    for _ in range(8):
        out = c.choose_message(GameState.from_mcp_state(st), ctx)
        if not isinstance(out, SendMessage):
            break
        sent.append(out.content)
        # the next state shows it (the server's new_messages carries our own chats; one missing there did not land and
        # would be sent again, v4.2.4: test_resend_after_hiccup.py)
        st = {**st, "new_messages": st["new_messages"] + [
            {"seq": 10 + len(sent), "sender": 3, "type": "chat", "content": out.content, "recipients": []}]}
    assert sent == ["line 2", "line 3", "line 4"]              # 2 already sent this window: 3 left, none repeated


# ---- robustness fixes that ship with it ----------------------------------------------------------------------------
def _doubles_state():
    return recs("yoriko/f525d6ac-e0a9-4b66-ba7c-a57c7ceb323d.jsonl")


def test_doubles_template_listed_after_another_action(steady):
    """robust-4: a forfeit listed before the doubles template never yields an empty doubles_turn or a forfeit."""
    from agent.agent import doubles_ok
    c = Contestant()
    n = 0
    for k, r in enumerate(_doubles_state()):
        raw = json.loads(json.dumps(r["raw"]))
        if raw.get("phase") == "moving":
            raw["legal_actions"]["actions"].insert(0, {"action_id": "forfeit", "label": "Forfeit the battle",
                                                       "input": {"action": {"type": "forfeit"}}})
            out = c.choose_action(GameState.from_mcp_state(raw), DecisionContext(
                session_id=raw["session_id"], tournament_id=None, game_type=raw["game_type"], agent_id="A"))
            act = out.action if isinstance(out, WithReasoning) else out
            assert isinstance(act, dict) and act.get("type") == "doubles_turn" and doubles_ok(raw, act), (k, act)
            assert c.last_decision["path"].startswith("policy"), c.last_decision
            n += 1
        decide(c, r, "A")                    # the recorded state itself (the same decision: the same memory)
    assert n >= 5


def test_policy_fallback_never_forfeits():
    from agent.pokemon.policy import PokemonPolicy
    st = {"phase": "draft", "observation": {}, "legal_actions": {"actions": [
        {"action_id": "forfeit", "label": "Forfeit", "input": {"action": {"type": "forfeit"}}},
        {"action_id": "draft_pick:c1", "input": {"action": {"type": "draft_pick", "card_id": "c1"}}}]}}
    assert PokemonPolicy().fallback(st) == {"action": {"type": "draft_pick", "card_id": "c1"}}


@pytest.mark.parametrize("gt", ["Pokemon_VGC_Doubles_Draft", " POKEMON-VGC-DOUBLES-DRAFT ", "pokemon", None])
def test_game_type_spellings_play_the_policy(gt, steady):
    """robust-7: another spelling of the game type (or none) still plays the Pokemon policy, not safe actions."""
    rs = recs(SESSIONS[0])
    c = Contestant()
    for r in rs[:10]:
        raw = dict(r["raw"], game_type=gt)
        ctx = DecisionContext(session_id=raw["session_id"], tournament_id=None, game_type=gt, agent_id="A")
        c.choose_action(GameState.from_mcp_state(raw), ctx)
        assert c.last_decision["path"] == "policy", (gt, c.last_decision)
    assert c.pk.opp_cards and c._game == "pokemon_vgc_doubles_draft"


def test_game_kind():
    assert game_kind("pokemon_vgc_doubles_draft") == ("pokemon", "pokemon_vgc_doubles_draft")
    for g in ("pokemon_gen9ou_draft", "pokemon_gen9random", "pokemon_gen9same", "pokemon_gen9ou_teambuild"):
        assert game_kind(g) == ("pokemon", g)
    assert game_kind("Werewolf") == ("werewolf", "werewolf")
    assert game_kind("tic_tac_toe", {"legal_actions": {"actions": [{"action_id": "0"}]}})[0] == ""
    singles = {"phase": "moving", "observation": {"is_doubles": False},
               "legal_actions": {"actions": [{"action_id": "move:0"}, {"action_id": "switch:1"}]}}
    assert game_kind("pokemon", singles) == ("pokemon", "pokemon_singles")
    assert game_kind(None, {"legal_actions": {"actions": [{"action_id": "draft_pick:x"}]}}) == \
        ("pokemon", "pokemon_vgc_doubles_draft")
    assert game_kind("", {"your_position": 2, "game_state": {"day": 1}})[0] == "werewolf"


def test_a_card_without_moves_does_not_break_the_draft(steady):
    """robust-10: "moves": null in a pool card scores it as moveless instead of crashing the draft decision."""
    rs = recs(SESSIONS[0])
    c = Contestant()
    raw = json.loads(json.dumps(rs[0]["raw"]))
    raw["observation"]["available_cards"][0]["moves"] = None
    c.choose_action(GameState.from_mcp_state(raw), DecisionContext(
        session_id=raw["session_id"], tournament_id=None, game_type=raw["game_type"], agent_id="A"))
    assert c.last_decision["path"] == "policy" and c.fallbacks == 0


def test_another_match_starts_fresh():
    """One instance handed a second match (the SDK never does) starts it with fresh memory."""
    rs = recs(SESSIONS[0])
    c = Contestant()
    for r in rs[:6]:
        decide(c, r, "A")
    assert c.pk.draft_pool
    raw = json.loads(json.dumps(rs[0]["raw"]))
    raw["session_id"] = "another-session"
    c.choose_action(GameState.from_mcp_state(raw), DecisionContext(
        session_id="another-session", tournament_id=None, game_type=raw["game_type"], agent_id="A"))
    assert set(c.pk.draft_pool) == {x["card_id"] for x in raw["observation"]["available_cards"]}
    assert c.decisions == 1


# ---- what a decision records about itself ------------------------------------------------------------------------
def test_records_the_decision_path_and_a_summary(tmp_path, monkeypatch, steady):
    monkeypatch.setenv("ARENA_RECORD", "1")
    monkeypatch.setattr(agent_mod, "RECORD_DIR", str(tmp_path / "rec"))
    rs = recs(SESSIONS[0])
    c = Contestant()
    for r in rs:
        decide(c, r, "A")
    rows = [json.loads(line) for line in open(tmp_path / "rec" / f"{rs[0]['raw']['session_id']}.jsonl")]
    assert len(rows) == len(rs)
    assert all(r["decision"]["path"] == "policy" and "ms" in r["decision"] for r in rows)
    battle = [r for r in rows if r["decision"]["kind"] == "battle"]
    assert battle and all(r["decision"]["battle"] == "classic" for r in battle)   # (tests pin ARENA_BATTLE=classic)
    line = c.summary_line()
    assert line.startswith("[match-summary] session=0495293f game=pokemon_vgc_doubles_draft decisions=18")
    assert "battle=" in line and "engine[" in line and "calc[" in line
