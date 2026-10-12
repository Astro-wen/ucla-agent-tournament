"""The rule-based Red Alert bot (agent/redalert/): parsing, order validation and batch limits, the sent-not-seen
ledger and refusal blacklist, the opening, windows mode, exception containment, and whole simulated matches. Real
payload shapes come from the local simulator (local/ra_server.py: docs/platform/redalert.md sections 4-5)."""
from __future__ import annotations

import copy

import pytest

from agent.redalert import RedAlertPolicy
from agent.redalert.ledger import Ledger, order_key
from agent.redalert.orders import COMMAND_CAP, Cand, anchor, check, finalize, fragile, noop
from agent.redalert.params import load as load_params
from agent.redalert.world import Static, View
from local.ra_rules import Rules
from local.ra_server import MatchServer
from local.ra_sim import World

RULES = Rules()
CONFIG = RULES.config


@pytest.fixture(autouse=True)
def _no_recording(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")


def server(map_id="sidestep", seed=1, countries=("england", "russia"), **kw) -> MatchServer:
    w = World(RULES, map_id, seed, countries=countries, swap_spawns=False)
    srv = MatchServer(w, **kw)
    for s in (0, 1):
        srv.get_game_state(s)
    with srv.cv:
        srv._start()
    return srv


def advance(srv, steps=1):
    with srv.cv:
        for _ in range(steps):
            srv._advance()


def drive(srv, pol, seconds, seat=0, record=None):
    """Our policy plays `seat` view by view (the other seat idles), batches through the server's real checks."""
    for _ in range(int(seconds * 5)):
        st = srv.state(seat)
        orders, _tag = pol.decide(st, CONFIG)
        if orders:
            res = srv.play_action(seat, st["state_version"], {"type": "orders", "orders": copy.deepcopy(orders)})
            pol.on_result(res)
            if record is not None:
                record.append((st["state_version"], orders, res))
        advance(srv)


def view_of(raw) -> View:
    st = Static(raw, CONFIG)
    v = View(raw, st)
    st.setup(v.base, v.enemy_hint)
    return View(raw, st)


# ---- parsing --------------------------------------------------------------------------------------------------------
def test_view_drops_wrecks_and_husks_and_counts_production():
    srv = server()
    raw = srv.state(0)
    obs = raw["observation"]
    obs["units"].append({"id": 900, "type": "2tnk.husk", "x": 10, "y": 10, "hp": 50, "wreck": True})
    obs["units"].append({"id": 901, "type": "harv.husk", "x": 10, "y": 10, "hp": 50})
    obs["visible_enemies"] = [{"id": 950, "type": "3tnk.husk", "x": 30, "y": 30, "hp": 10, "wreck": True},
                              {"id": 951, "type": "e1", "x": 31, "y": 30, "hp": 80}]
    obs["production"] = [{"queue": "Building", "item": "powr", "progress": 1.0, "remaining_s": 0, "paused": False,
                          "ready": True}, {"queue": "Infantry", "item": "e1", "progress": 0.5, "ready": False}]
    v = view_of(raw)
    assert 900 not in v.unit_by_id and 901 not in v.unit_by_id
    assert [e["id"] for e in v.enemies] == [951] and v.enemy_ids == {951}
    assert v.ready == [("Building", "powr")] and v.in_prod == {"powr": 1, "e1": 1}
    assert v.unit_count.get("mcv") == 1 and v.base is not None


def test_view_survives_odd_payloads():
    st = Static({}, None)
    for raw in ({}, {"observation": {}}, {"observation": {"units": [None, 3, {"id": True}], "buildings": "x",
                                                          "economy": None, "production": [1]}}):
        v = View(raw, st)
        assert v.units == [] and v.buildings == [] and v.cash == 0


@pytest.mark.parametrize("map_id", ["sidestep", "trapped", "shadowfiend-ii"])
def test_ground_path_and_waypoints_on_every_pool_map(map_id):
    srv = server(map_id)
    raw = srv.state(0)
    st = Static(raw, CONFIG)
    v = View(raw, st)
    st.setup(v.base, v.enemy_hint)
    assert st.grid_ok and st.path[0] == st.my_spawn and st.path[-1] == tuple(srv.world.spawns[1])
    assert all(st.can_pass(*c) for c in st.path)
    assert all(st.in_bounds(c) for c in st.waypoints + [st.rally, st.guard, st.picket, st.staging])
    assert st.waypoints[-1] == st.enemy_spawn and len(st.sweep) >= 4
    assert st.ore_near(v.base, 16) is not None


def test_unknown_map_falls_back_to_a_straight_line():
    raw = {"map": {"id": "nowhere", "width": 60, "height": 40, "bounds": [1, 1, 58, 38]},
           "observation": {"base_center": [5, 5], "units": [], "buildings": []},
           "legal_actions": {"enemy_base": [50, 30]}}
    st = Static(raw, {"rules": {"buildings": {"powr": {"cost": 300}}, "maps": {}}})
    st.setup((5, 5), (50, 30))
    assert not st.grid_ok and st.path[0] == (5, 5) and st.path[-1] == (50, 30) and st.waypoints[-1] == (50, 30)


# ---- orders ---------------------------------------------------------------------------------------------------------
def test_check_refuses_what_the_platform_would_and_prunes_dead_ids():
    srv = server()
    v = view_of(srv.state(0))
    mcv = v.units[0]["id"]
    assert check({"cmd": "move", "units": [mcv], "to": [30, 30], "speed": 3}, v) == "unknown field"
    assert check({"cmd": "attack", "units": [mcv]}, v) == "attack needs one of target / to"
    assert check({"cmd": "attack", "units": [mcv], "target": 12345}, v) == "target not visible"
    assert check({"cmd": "move", "units": [mcv], "to": [0, 0]}, v) == "to outside bounds"
    assert check({"cmd": "move", "units": [mcv], "to": [30.5, 3]}, v) == "to not a cell"
    o = {"cmd": "move", "units": [99999, mcv, mcv], "to": [30, 30]}
    assert check(o, v) is None and o["units"] == [mcv]
    assert check({"cmd": "move", "units": [99999], "to": [30, 30]}, v) == "no unit left"
    assert check({"cmd": "train", "item": "e1", "count": 1}, v) == "not available"
    assert check({"cmd": "place", "item": "powr"}, v) == "not ready"
    assert check({"cmd": "deploy", "units": [mcv]}, v) is None
    assert check({"cmd": "fly", "units": [mcv]}, v) == "unknown cmd"


def test_finalize_keeps_the_batch_limits_and_one_order_per_unit():
    srv = server()
    v = view_of(srv.state(0))
    mcv = v.units[0]["id"]
    led = Ledger()
    cands = [Cand({"cmd": "deploy", "units": [mcv]}, urgent=True)]
    cands += [Cand({"cmd": "move", "units": [mcv], "to": [30, 30]})]          # same unit: dropped
    cands += [Cand({"cmd": "power_toggle", "building": 1, "on": True})]        # not our building: dropped
    orders, kept, dropped = finalize(cands, v, led)
    assert orders == [{"cmd": "deploy", "units": [mcv]}] and dropped == 2
    # 60 units x 4 orders would be 240 engine commands: trimmed to the cap; never more than 20 orders
    fake = [{"id": 1000 + i, "type": "e1", "x": 20, "y": 20, "hp": 100, "idle": True, "act": None, "stance": "defend"}
            for i in range(200)]
    v.units += fake
    v.unit_by_id.update({u["id"]: u for u in fake})
    cands = [Cand({"cmd": "move", "units": [1000 + 50 * k + i for i in range(50)], "to": [30, 30]}) for k in range(4)]
    cands += [Cand({"cmd": "stop", "units": [1000 + i]}) for i in range(30)]
    orders, kept, dropped = finalize(cands, v, led)
    assert sum(len(o["units"]) for o in orders) <= COMMAND_CAP and len(orders) <= 20
    assert all(len(o["units"]) <= 50 for o in orders)


def test_noop_is_always_a_valid_order():
    srv = server()
    v = view_of(srv.state(0))
    o = noop(v)
    assert o is not None and check(dict(o), v) is None
    assert srv.world.check_order(0, o)["status"] in ("pending", "ok")


def test_a_batch_of_fragile_orders_carries_an_anchor_that_is_always_valid():
    srv = server()
    pol = RedAlertPolicy()
    drive(srv, pol, 3)                                          # the yard stands
    v = view_of(srv.state(0))
    assert fragile({"cmd": "place", "item": "powr"}) and fragile({"cmd": "attack", "units": [1, 2, 3], "target": 9})
    assert not fragile({"cmd": "build", "item": "powr"}) and not fragile({"cmd": "move", "units": [1, 2, 3], "to": [1, 1]})
    a = anchor(v)
    assert a["cmd"] == "power_toggle" and v.bld_by_id[a["building"]]["type"] == "fact" and a["on"] is True
    assert srv.world.check_order(0, a) == {"status": "ok", "reason": "ok", "detail": "already_on", "commands": 0,
                                           "failed": [], "cmd": "power_toggle"}
    # a lone place whose building vanished before the check: the batch is still accepted thanks to the anchor
    res = srv.play_action(0, srv.state(0)["state_version"],
                          {"type": "orders", "orders": [{"cmd": "place", "item": "weap"}, a]})
    assert res.get("accepted") and res["results"][0]["status"] == "rejected"


# ---- the ledger -----------------------------------------------------------------------------------------------------
def test_a_build_is_never_sent_twice_before_the_state_shows_it():
    srv = server()
    pol = RedAlertPolicy()
    log = []
    for _ in range(40):                                # 8 s: deploy, then the first power plant (7.2 s to build)
        st = srv.state(0)
        orders, _ = pol.decide(st, CONFIG)
        if orders:
            log.append(orders)
            pol.on_result(srv.play_action(0, st["state_version"], {"type": "orders", "orders": copy.deepcopy(orders)}))
        assert sum(1 for p in srv.world.players[0].queues["Building"] if p.name == "powr") <= 1
        advance(srv)
    builds = [o["item"] for orders in log for o in orders if o["cmd"] == "build"]
    assert builds.count("powr") == 1


def test_refusals_clear_pending_and_three_identical_ones_block_the_order():
    led = Ledger()
    led.tick = 100
    order = {"cmd": "place", "item": "weap"}
    led.add_pending(("place", "weap"), 100)
    led.note_sent(100, [order], [("place", "weap")])
    led.on_result({"accepted": True, "results": [{"index": 0, "cmd": "place", "status": "rejected",
                                                  "reason": "not_ready"}]})
    assert not led.is_pending(("place", "weap"))
    for sv in (105, 110):
        led.note_sent(sv, [dict(order)], [None])
        led.on_result({"error": "INVALID_ACTION", "detail": "No order was valid: [0] place: not_ready (weap)"})
    assert led.refusals[order_key(order)]["count"] == 3 and led.invalid_actions == 2

    class V:
        ready, avail, unit_by_id = [], set(), {}
    assert led.blocked(order, V())                     # still not ready: blocked
    V.ready = [("Building", "weap")]
    assert not led.blocked(order, V())                 # the reason no longer holds
    led.tick += 31 * 25
    V.ready = []
    assert not led.blocked(order, V())                 # 30 s later it may go again


def test_batch_errors_drop_the_pending_entries_and_lost_verdicts_are_resent():
    led = Ledger()
    led.tick = 200
    led.add_pending(("build", "powr"), 200)
    led.note_sent(200, [{"cmd": "build", "item": "powr"}], [("build", "powr")])
    led.on_result({"error": "STALE_STATE", "detail": "too old"})
    assert not led.is_pending(("build", "powr")) and led.errors == {"STALE_STATE": 1}
    led.add_pending(("build", "proc"), 205)
    led.note_sent(205, [{"cmd": "build", "item": "proc"}], [("build", "proc")])

    class V:
        tick, last_orders, in_prod, ready = 215, {"state_version": 205, "batch": 7, "pending": False,
                                                  "results": [{"index": 0, "status": "lost", "reason": "not_applied"}]}, {}, []
        problems = [{"batch": 7, "index": 0, "cmd": "build", "status": "lost", "reason": "not_applied"}]
    led.update(V())
    assert not led.is_pending(("build", "proc")) and led.lost == 1     # counted once (problems row deduped)


def test_a_temporary_failure_keeps_the_pending_entries_since_the_batch_may_have_landed():
    # SDK PR #7: after a temporary failure of play_action (a 5xx, a tool failure) the runner reports the code, never
    # resends and asks again from a fresh state; the batch may have landed, so nothing of it is queued again at once.
    led = Ledger()
    led.tick = 300
    led.add_pending(("build", "powr"), 300)
    led.note_unit(7, ("move", 1, 2))
    led.note_sent(300, [{"cmd": "build", "item": "powr"}, {"cmd": "move", "units": [7], "to": [1, 2]}],
                  [("build", "powr"), None])
    led.on_result({"error": "internal_error", "detail": "MCP tool 'play_action' request failed with HTTP 502"})
    assert led.is_pending(("build", "powr")) and 7 in led.unit_cmd
    assert led.uncertain == 1 and led.errors == {"internal_error": 1} and led.invalid_actions == 0
    led.tick = 300 + 80                                # never shown in production: it expires and may go again
    assert not led.is_pending(("build", "powr"))


def test_pending_entries_expire():
    led = Ledger()
    led.tick = 0
    led.add_pending(("build", "powr"), 0)
    led.tick = 74
    assert led.is_pending(("build", "powr"))
    led.tick = 80
    assert not led.is_pending(("build", "powr"))


# ---- decisions ------------------------------------------------------------------------------------------------------
def test_the_first_view_deploys_the_mcv_and_the_opening_follows():
    srv = server()
    pol = RedAlertPolicy()
    st = srv.state(0)
    orders, tag = pol.decide(st, CONFIG)
    assert orders == [{"cmd": "deploy", "units": [st["legal_actions"]["deploy"][0]]}] and tag == "opening"
    log = []
    drive(srv, pol, 70, record=log)
    p = srv.world.players[0]
    owned = sorted(a.type for a in srv.world.own(0, units=False))
    assert {"fact", "powr", "tent", "proc"} <= set(owned)
    assert p.place_delays and max(p.place_delays) <= 25            # every ready building placed within 1 s
    assert p.low_power_ticks == 0
    assert all(r.get("status") != "rejected" for _sv, _o, res in log for r in res.get("results", []))
    assert pol.ledger.invalid_actions == 0 and pol.exceptions == 0


def test_soviet_opening_uses_the_soviet_barracks():
    srv = server(countries=("ukraine", "england"))
    pol = RedAlertPolicy()
    drive(srv, pol, 40)
    assert "barr" in {a.type for a in srv.world.own(0, units=False)}


def test_windows_mode_always_answers_a_valid_batch():
    srv = server(pacing="windows")
    pol = RedAlertPolicy()
    for _ in range(6):
        st = srv.state(0)
        orders, _ = pol.decide(st, CONFIG)
        assert orders, "windows mode: WAIT would end the seat"
        res = srv.play_action(0, st["state_version"], {"type": "orders", "orders": orders})
        assert res.get("accepted"), res
        advance(srv, 25)                                         # the next 5-second window


def test_decide_never_raises():
    pol = RedAlertPolicy()
    for raw in (None, {}, {"observation": None}, {"observation": []}, {"observation": {"units": "x"}},
                {"observation": {}, "pacing": {"mode": "windows"}}, {"observation": {"economy": {"cash": "lots"}}}):
        orders, tag = pol.decide(raw, None)
        assert orders is None and isinstance(tag, str)
    pol.on_result(None)
    pol.on_result({"results": "nope"})
    pol.on_result({"error": "INVALID_ACTION", "detail": 5})


def test_a_failing_module_is_contained_and_repeated_failures_switch_to_safe_mode(monkeypatch, caplog):
    srv = server()
    pol = RedAlertPolicy()

    def boom(*a, **k):
        raise RuntimeError("army broke")
    monkeypatch.setattr(pol.army, "step", boom)
    orders, _ = pol.decide(srv.state(0), CONFIG)
    assert orders and orders[0]["cmd"] == "deploy"              # the economy still answered
    assert pol.exceptions == 1 and pol.exc_kinds == {"army": 1}
    for _ in range(5):
        advance(srv)
        pol.decide(srv.state(0), CONFIG)
    assert pol.safe_entries == 1 and pol.view.tick < pol.safe_until
    assert sum(1 for r in caplog.records if "army failed" in r.getMessage()) == 1   # logged once per kind


def kill_own(srv, seat, types):
    w = srv.world
    with srv.cv:
        victims = [a for a in w.own(seat) if a.type in types]
        for a in victims:
            w._kill(a, None)
    return len(victims)


def test_every_harvester_lost_with_no_money_a_harvester_comes_first(monkeypatch):
    # review 10-08: with all harvesters dead and the money gone, the tank and the war factory in progress ate every
    # credit and the bot sat at zero income for the rest of the match. Now: cancel for the refund, sell a spare.
    srv = server()
    pol = RedAlertPolicy()
    monkeypatch.setattr(pol.army, "_main_step", lambda v, out: None)     # no attack: the idle seat must live on
    drive(srv, pol, 300)
    p = srv.world.players[0]
    assert kill_own(srv, 0, {"harv"}) >= 2
    p.cash, p.ore = 0, 0
    drive(srv, pol, 90)
    assert pol.econ.emergencies == 1 and not pol.econ.emergency
    assert any(a.type == "harv" for a in srv.world.own(0)), "no harvester rebuilt within 90 s"
    assert pol.ledger.invalid_actions == 0 and pol.exceptions == 0


def test_low_power_cancels_a_slow_building_in_progress_for_a_power_plant():
    srv = server()
    pol = RedAlertPolicy()
    w = srv.world
    for _ in range(400):                               # until something slow is in the Building queue
        drive(srv, pol, 1)
        q = w.players[0].queues["Building"]
        if q and q[0].name in ("proc", "weap", "fix") and not q[0].done and q[0].progress < 0.4:
            break
    item = w.players[0].queues["Building"][0].name
    assert kill_own(srv, 0, {"powr", "apwr"}) >= 1
    log = []
    drive(srv, pol, 3, record=log)
    assert {"cmd": "cancel", "item": item} in [o for _sv, orders, _r in log for o in orders]
    drive(srv, pol, 30)
    assert any(a.type in ("powr", "apwr") for a in w.own(0)) and pol.econ.power_cancels == 1


def test_repair_starts_and_stops_on_the_same_money():
    srv = server()
    pol = RedAlertPolicy()
    drive(srv, pol, 180)
    w = srv.world
    b = next(a for a in w.own(0, units=False) if a.type == "powr")
    b.hp = b.max_hp * 0.5
    repairs = 0
    for _ in range(150):                               # 30 s at cash 250 + ore 400 (start > 500, stop < 300)
        w.players[0].cash, w.players[0].ore = 250, 400
        st = srv.state(0)
        orders, _ = pol.decide(st, CONFIG)
        if orders:
            repairs += sum(1 for o in orders if o["cmd"] == "repair" and o["building"] == b.id)
            pol.on_result(srv.play_action(0, st["state_version"], {"type": "orders", "orders": copy.deepcopy(orders)}))
        advance(srv)
    assert repairs == 1


def test_search_points_cover_every_reachable_cell_of_each_map():
    from agent.redalert.world import COVER_STEP
    for map_id in ("sidestep", "trapped", "shadowfiend-ii"):
        srv = server(map_id)
        st = Static(srv.state(0), CONFIG)
        v = View(srv.state(0), st)
        st.setup(v.base, v.enemy_hint)
        pts = st.coverage()
        reach = st._reachable(st.my_spawn)
        assert pts and all(st.can_pass(*p) and st.in_bounds(p) for p in pts)
        far = [k for k in reach if min((k % st.width - p[0]) ** 2 + (k // st.width - p[1]) ** 2 for p in pts)
               > (COVER_STEP + 1) ** 2]
        assert not far, f"{map_id}: {len(far)} reachable cells farther than {COVER_STEP + 1} from every point"


def test_a_base_built_away_from_both_spawns_is_found():
    # review 10-08, league seed 81 game 9: the hider unfolds its MCV in a corner; the HUNT that only cycled the sweep
    # points never saw it in 15 minutes and lost the tiebreak 0 / 100 with 6x the assets.
    from local.ra_platform import make_factory, play_match
    seed = 81009
    row = play_match(make_factory("ours")(seed * 2 + 1), make_factory("hider")(seed * 2 + 2), seed=seed,
                     map_id="sidestep", a_seat=1, max_minutes=14)
    assert row["a"]["error"] is None and row["a"]["buildings_killed"] >= 5, row["a"]


def test_without_a_yard_or_an_mcv_to_come_the_money_goes_into_units(monkeypatch):
    srv = server()
    pol = RedAlertPolicy()
    monkeypatch.setattr(pol.army, "_main_step", lambda v, out: None)     # no attack: the idle seat must live on
    drive(srv, pol, 130)
    w = srv.world
    assert not any(a.type == "fix" for a in w.own(0))
    kill_own(srv, 0, {"fact", "mcv"})
    drive(srv, pol, 120)
    v = pol.view
    assert pol.prod.stranded(v)
    inf = sum(1 for a in w.own(0, buildings=False) if a.type in ("e1", "e3", "e2"))
    assert inf > int(pol.p.max_infantry), f"only {inf} infantry with {v.money} credits"


def test_slow_frames_never_skip_the_army_step_more_than_twice_in_a_row(monkeypatch):
    srv = server()
    t = [0.0]

    def clock():
        t[0] += 0.06                                   # every reading 60 ms later: each frame is "slow"
        return t[0]
    pol = RedAlertPolicy(clock=clock)
    calls = []
    real = pol.army.step
    monkeypatch.setattr(pol.army, "step", lambda *a, **k: (calls.append(1), real(*a, **k)))
    for _ in range(9):
        pol.decide(srv.state(0), CONFIG)
        advance(srv)
    assert len(calls) == 3 and pol.army_skipped == 6


def test_params_from_the_environment():
    p = load_params({"ARENA_RA_ATTACK_MIN_VALUE": "5000", "ARENA_RA_THROTTLE_TICKS": "x", "ARENA_RA_RETREAT_RATIO": "nan"})
    assert p.attack_min_value == 5000 and p.throttle_ticks == 9 and p.retreat_ratio == 0.7


def test_summary_has_the_match_metrics():
    srv = server()
    pol = RedAlertPolicy()
    drive(srv, pol, 20)
    s = pol.summary()
    assert s["first_powr_s"] is not None and s["batches"] >= 2 and s["ms_p99"] is not None
    text = pol.summary_text()
    assert "first_powr_s=" in text and "invalid_action=0" in text and "exceptions=0" in text


# ---- v4.5: economy first, harvester guard, blind prior, counters ----------------------------------------------------
INFANTRY_TYPES = ("e1", "e2", "e3")


def _kill_scout_at(srv, pol, where):
    """Our first scout is moved to `where` and killed there (as by an enemy picket)."""
    for _ in range(400):
        drive(srv, pol, 0.2)
        if pol.army.scout is not None:
            break
    sid = pol.army.scout
    assert sid is not None
    w = srv.world
    with srv.cv:
        a = w.actors[sid]
        a.x, a.y = float(where[0]), float(where[1])
        a.order, a.oq = None, []
    drive(srv, pol, 0.6)                                         # its new position is seen
    with srv.cv:
        w._kill(w.actors[sid], None)


def _opening_after_scout_loss(where, env=None):
    """sidestep, us at (23, 44), the enemy spawn at (74, 53): returns (policy, most infantry before the war factory,
    war factory time)."""
    srv = server()
    pol = RedAlertPolicy(params=load_params(env or {}))
    _kill_scout_at(srv, pol, where)
    most, weap_s = 0, None
    for _ in range(170):
        drive(srv, pol, 1)
        own = list(srv.world.own(0))
        if weap_s is None and any(a.type == "weap" for a in own):
            weap_s = pol.view.game_s
        if weap_s is None:
            most = max(most, sum(1 for a in own if a.type in INFANTRY_TYPES))
    assert pol.ledger.invalid_actions == 0 and pol.exceptions == 0
    return pol, most, weap_s


def test_econ_first_a_scout_killed_at_an_enemy_picket_does_not_stall_the_opening():
    # the real loss of 10-11 (cb4bfc36): the scout died at an enemy picket 18 cells in front of its base, the bot
    # called it a rush, built 24 rifles and 2 towers and had its war factory at 282 s
    pol, most, weap_s = _opening_after_scout_loss((58, 50))
    assert not pol.army.rush and not pol.army.early_army
    assert most <= int(pol.p.pre_weap_infantry), most
    assert weap_s is not None and weap_s <= 175, weap_s


def test_econ_first_off_keeps_the_v4_3_reaction():
    pol, most, weap_s = _opening_after_scout_loss((58, 50), {"ARENA_RA_ECON_FIRST": "0"})
    assert pol.army.scout_lost_early and most > 12
    assert weap_s is None or weap_s > 175


def test_econ_first_a_scout_killed_on_our_half_still_means_a_rush():
    pol, _most, _weap_s = _opening_after_scout_loss((40, 47))
    assert pol.army.rush_tick > 0


def test_a_scout_killed_inside_their_base_raises_the_guard_a_little():
    pol, most, weap_s = _opening_after_scout_loss((72, 52))
    assert pol.army.early_army and not pol.army.rush
    assert int(pol.p.pre_weap_infantry) < most <= int(pol.p.pre_weap_infantry + pol.p.early_army_infantry)
    assert weap_s is not None and weap_s <= 200, weap_s


def test_a_harvester_under_attack_drives_away_and_the_home_guard_goes_to_it():
    srv = server()
    pol = RedAlertPolicy()
    monkey_main = pol.army._main_step
    pol.army._main_step = lambda v, out: None                     # the attack group stays home
    drive(srv, pol, 150)
    pol.army._main_step = monkey_main
    w = srv.world
    harv = next(a for a in w.own(0) if a.type == "harv")
    proc = next(a for a in w.own(0) if a.type == "proc")
    with srv.cv:
        harv.x, harv.y = proc.x + 13.0, proc.y + 2.0            # out in the field, away from the base
        harv.order, harv.oq = None, []
        w._spawn_unit(1, "e1", harv.x + 2.0, harv.y)
    orders = []
    for _ in range(5):
        st = srv.state(0)
        out, _tag = pol.decide(st, CONFIG)
        if out:
            orders += out
            pol.on_result(srv.play_action(0, st["state_version"], {"type": "orders", "orders": copy.deepcopy(out)}))
        advance(srv)
    assert any(o["cmd"] == "move" and o["units"] == [harv.id] for o in orders), orders
    assert any(o["cmd"] == "attack_move" and set(o["units"]) & pol.army.home for o in orders), orders
    assert pol.army.harv_pulls >= 1 and pol.ledger.invalid_actions == 0 and pol.exceptions == 0


def test_blind_the_enemy_army_is_assumed_until_we_look_at_their_base():
    srv = server()
    pol = RedAlertPolicy()
    drive(srv, pol, 2)
    v = pol.view
    v.game_s, v.tick = 300.0, 300 * 25
    pol.army.intel_tick = -10 ** 9
    p = pol.p
    assert pol.army.prior(v) == min(p.prior_cap, p.prior_rate * (300 - 90))
    assert pol.army.enemy_strength(v) >= pol.army.prior(v)
    pol.army.intel_tick = v.tick - 25 * 30                         # looked 30 s ago
    assert pol.army.prior(v) == 0.0


def test_counter_ratio():
    from agent.redalert import counters
    from agent.redalert.world import Rules as BotRules
    r = BotRules(CONFIG)
    # identical compositions: the cost ratio
    assert abs(counters.ratio([("e1", 20, 1.0)], [("e1", 10, 1.0)], r) - 2.0) < 1e-9
    assert abs(counters.ratio([("3tnk", 3, 1.0), ("e3", 2, 1.0)], [("3tnk", 3, 1.0), ("e3", 2, 1.0)], r) - 1.0) < 1e-9
    # rockets against an equal cost of rifles lose, against tanks they do well
    assert counters.ratio([("e3", 10, 1.0)], [("e1", 30, 1.0)], r) < 0.5
    assert counters.ratio([("e3", 10, 1.0)], [("3tnk", 3, 0.87)], r) > 1.0
    # nothing to shoot back: inf; nothing on our side: 0
    assert counters.ratio([("e1", 1, 1.0)], [("harv", 2, 1.0)], r) == float("inf")
    assert counters.ratio([], [("e1", 1, 1.0)], r) == 0.0
