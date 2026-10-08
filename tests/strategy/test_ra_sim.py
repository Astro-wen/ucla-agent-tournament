"""The local Red Alert simulator (local/ra_sim.py world, local/ra_server.py GameAPI, local/ra_bots.py opponents,
local/ra_platform.py matches through the official runner, local/fake_redalert.py over HTTP / MCP), checked against
docs/platform/redalert.md: the world's mechanics, every runner-side order check and engine verdict, the protocol's
errors and pacing, the end conditions, determinism, and whole matches through altruagent.runner.run_game."""
from __future__ import annotations

import json
import threading
import time

import pytest

from altruagent import WAIT
from local import ra_server
from local.ra_bots import make_bot
from local.ra_platform import main as platform_main
from local.ra_platform import make_factory, play_match
from local.ra_rules import Rules
from local.ra_server import MatchServer, SeatSession
from local.ra_sim import DEPLOY_TICKS, STEP, World

RULES = Rules()


@pytest.fixture(autouse=True)
def _no_recording(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")


def mk(seed=1, countries=("england", "russia"), **kw) -> World:
    return World(RULES, kw.pop("map_id", "sidestep"), seed, countries=countries, swap_spawns=False, **kw)


_BATCH = [0]


def send(w: World, seat: int, *orders) -> list[dict]:
    """The runner's checks, then the accepted orders reach the engine at the next step."""
    _BATCH[0] += 1
    checks = []
    for i, o in enumerate(orders):
        c = w.check_order(seat, o)
        if c["status"] == "pending":
            w.enqueue(seat, _BATCH[0], i, o, c, w.tick + STEP)
        checks.append(c)
    return checks


def run(w: World, seconds: float) -> None:
    for _ in range(int(round(seconds * 5))):
        w.step()


def verdicts(w: World, seconds: float = 0.4) -> list[tuple]:
    w.verdicts.clear()
    run(w, seconds)
    out = list(w.verdicts)
    w.verdicts.clear()
    return out


def mcv(w, seat):
    return next(a for a in w.actors.values() if a.owner == seat and a.type == "mcv")


def deploy(w, seat=0):
    send(w, seat, {"cmd": "deploy", "units": [mcv(w, seat).id]})
    run(w, 1.6)
    return w.fact(seat)


def until_ready(w, seat, item, limit_s=200):
    for _ in range(int(limit_s * 5)):
        w.step()
        if any(p["item"] == item and p["ready"] for p in w.observation(seat)["production"]):
            return True
    return False


def build_place(w, seat, item, at=None):
    assert send(w, seat, {"cmd": "build", "item": item})[0]["status"] == "pending", item
    assert until_ready(w, seat, item), item
    o = {"cmd": "place", "item": item}
    if at is not None:
        o["at"] = at
    send(w, seat, o)
    run(w, 0.4)
    return max((a for a in w.actors.values() if a.owner == seat and a.type == item), key=lambda a: a.id)


# ---- the map and the start ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("map_id", ["sidestep", "trapped", "shadowfiend-ii"])
def test_real_maps_bounds_spawns_and_a_deployable_mcv_per_side(map_id):
    info = RULES.map_info(map_id)
    w = World(RULES, map_id, 3, swap_spawns=False)
    assert (w.width, w.height, list(w.bounds)) == (info["width"], info["height"], info["bounds"])
    assert [list(s) for s in w.spawns] == info["spawns"]
    assert len(w.ore) > 100                                       # the layout's ore and gems
    for s in (0, 1):
        m = mcv(w, s)
        assert m.cell() == tuple(info["spawns"][s])
        assert w.observation(s)["economy"]["cash"] == 5000
        assert w.legal_actions(s)["deploy"] == [m.id]
        assert w.legal_actions(s)["enemy_base"] == info["spawns"][1 - s]
    assert {p.country for p in World(RULES, map_id, 5).players} <= {"england", "france", "germany", "russia",
                                                                    "ukraine"}


def test_deploy_makes_a_construction_yard_with_a_new_id_and_only_a_building_created_event():
    w = mk()
    old = mcv(w, 0).id
    send(w, 0, {"cmd": "deploy", "units": [old]})
    run(w, 0.4)
    assert w.fact(0) is None and w.observation(0)["units"][0]["act"] == "Deploy"
    send(w, 0, {"cmd": "deploy", "units": [old]})                # a repeat does not restart the unfolding
    run(w, (DEPLOY_TICKS + 5) / 25)
    f = w.fact(0)
    assert f is not None and f.id != old
    obs = w.observation(0)
    assert obs["units"] == [] and obs["base_center"] == [f.x, f.y]
    kinds = [e["kind"] for e in obs["events"]]
    assert "building_created" in kinds and "unit_lost" not in kinds
    assert "powr" in obs["available_production"] and "tent" not in obs["available_production"]
    assert w.legal_actions(0)["production_buildings"] == [{"id": f.id, "type": "fact"}]


# ---- production, money, power -----------------------------------------------------------------------------------------
def test_production_is_paid_as_it_goes_waits_for_placement_and_blocks_its_queue():
    w = mk()
    deploy(w)
    send(w, 0, {"cmd": "build", "item": "powr"}, {"cmd": "build", "item": "powr"})
    run(w, 3.6)                                                   # half of powr's 7.2 s
    obs = w.observation(0)
    first, second = [p for p in obs["production"] if p["item"] == "powr"]
    assert 0.4 < first["progress"] < 0.6 and second["progress"] == 0
    assert 4820 <= obs["economy"]["cash"] <= 4880                 # about half of 300 paid
    assert obs["buildings"][0]["producing"] == "powr"
    run(w, 4.0)
    obs = w.observation(0)
    assert obs["production"][0]["ready"] and obs["economy"]["cash"] == 4700
    assert any(e["kind"] == "production_ready" and e["type"] == "powr" for e in obs["events"])
    run(w, 10)                                                    # the queue waits for the placement
    assert w.observation(0)["production"][1]["progress"] == 0
    assert w.legal_actions(0)["place"] == ["powr"]
    send(w, 0, {"cmd": "place", "item": "powr"})
    run(w, 0.4)
    obs = w.observation(0)
    assert obs["economy"]["power_provided"] == 100 and [b["type"] for b in obs["buildings"]] == ["fact", "powr"]
    assert obs["production"][0]["progress"] > 0                   # the second one started


def test_production_stalls_without_money_and_cancel_refunds():
    w = mk(starting_cash=200)
    deploy(w)
    send(w, 0, {"cmd": "build", "item": "powr"})
    run(w, 20)
    obs = w.observation(0)
    assert obs["economy"]["cash"] == 0 and 0.6 < obs["production"][0]["progress"] < 0.7
    assert not obs["production"][0]["ready"]
    send(w, 0, {"cmd": "cancel", "item": "powr"})
    run(w, 0.4)
    obs = w.observation(0)
    assert obs["production"] == [] and obs["economy"]["cash"] == 200


def test_prerequisites_factions_and_producers_decide_available_production():
    w = mk(countries=("russia", "england"))
    deploy(w, 0)
    deploy(w, 1)
    a0, a1 = w.available(0), w.available(1)
    assert "barr" not in a0 and "e1" not in a0                    # needs a power plant / barracks first
    build_place(w, 0, "powr")
    build_place(w, 1, "powr")
    a0, a1 = w.available(0), w.available(1)
    assert "barr" in a0 and "tent" not in a0 and "kenn" in a0
    assert "tent" in a1 and "barr" not in a1
    build_place(w, 0, "barr")
    a0 = w.available(0)
    assert {"e1", "e2", "e3"} <= set(a0) and "medi" not in a0 and "ant" not in a0 and "3tnk" not in a0
    assert w.check_order(0, {"cmd": "train", "item": "3tnk"})["reason"] == "not_available"
    assert w.check_order(0, {"cmd": "build", "item": "e1"})["reason"] == "invalid"     # a unit: train it


def test_low_power_slows_production_and_switches_power_hungry_defenses_off():
    w = mk(countries=("russia", "england"), starting_cash=20000)
    deploy(w)
    build_place(w, 0, "powr")
    for item in ("barr", "proc", "weap"):
        build_place(w, 0, item)
    tsla = build_place(w, 0, "tsla")
    obs = w.observation(0)
    assert obs["economy"]["power_drained"] > obs["economy"]["power_provided"]
    assert any(e["kind"] == "low_power" for e in w.players[0].events)
    assert next(b for b in obs["buildings"] if b["id"] == tsla.id)["powered"] is False
    send(w, 0, {"cmd": "train", "item": "e1"})
    run(w, 4.0)                                                   # e1 takes 2.4 s at full power, 7.2 s on low
    assert not any(u["type"] == "e1" for u in w.observation(0)["units"])
    run(w, 4.0)
    assert any(u["type"] == "e1" for u in w.observation(0)["units"])


def test_refinery_brings_a_free_harvester_that_turns_ore_into_money():
    w = mk()
    deploy(w)
    build_place(w, 0, "powr")
    proc = build_place(w, 0, "proc")
    obs = w.observation(0)
    harv = [u for u in obs["units"] if u["type"] == "harv"]
    assert len(harv) == 1 and obs["economy"]["harvesters"] == 1 and obs["economy"]["resource_capacity"] == 2000
    assert any(e["kind"] == "unit_created" and e["type"] == "harv" for e in obs["events"])
    assert w.legal_actions(0)["harvesters"] == [harv[0]["id"]] and harv[0]["act"] == "Harvest"
    before = obs["economy"]["cash"] + obs["economy"]["ore"]
    run(w, 60)
    obs = w.observation(0)
    assert obs["economy"]["cash"] + obs["economy"]["ore"] >= before + 500       # at least one full load
    assert proc.id in {b["id"] for b in obs["buildings"]}


def test_placement_honours_a_legal_at_falls_back_near_the_yard_and_fails_without_room():
    w = mk()
    f = deploy(w)
    powr = build_place(w, 0, "powr", at=[f.x + 4, f.y])
    assert (powr.x, powr.y) == (f.x + 4, f.y)
    far = build_place(w, 0, "powr", at=[w.bx0 + 1, w.by0 + 1])        # far from the base: placed near the yard
    assert abs(far.x - f.x) <= 10 and abs(far.y - f.y) <= 10
    for y in range(w.height):                                     # no free cell left anywhere: invalid
        for x in range(w.width):
            if w.occ[y][x] == 0:
                w.terrain[y][x] = "^"
    send(w, 0, {"cmd": "build", "item": "powr"})
    assert until_ready(w, 0, "powr")
    send(w, 0, {"cmd": "place", "item": "powr"})
    v = verdicts(w)
    assert v[0][3:5] == ("rejected", "invalid")
    assert w.legal_actions(0)["place"] == ["powr"]                # still waiting in its queue


# ---- the runner's order checks and the engine's verdicts ----------------------------------------------------------------
def test_order_checks_match_section_5():
    w = mk()
    m = mcv(w, 0).id
    enemy = mcv(w, 1).id
    chk = lambda o: w.check_order(0, o)  # noqa: E731
    assert chk({"cmd": "fly", "units": [m]})["reason"] == "invalid"
    assert chk({"cmd": "move", "units": [m], "to": [3, 3], "speed": 2})["detail"] == "unknown field 'speed'"
    assert chk({"cmd": "move", "units": [m]})["reason"] == "invalid"                    # missing to
    assert chk({"cmd": "move", "units": [], "to": [3, 3]})["reason"] == "invalid"
    assert chk({"cmd": "move", "units": [m, m], "to": [3, 3]})["reason"] == "invalid"
    assert chk({"cmd": "move", "units": list(range(51)), "to": [3, 3]})["reason"] == "invalid"
    assert chk({"cmd": "move", "units": [m], "to": [w.width, 3]})["reason"] == "invalid"  # outside width x height
    part = chk({"cmd": "move", "units": [m, 99999, enemy], "to": [3, 3]})
    assert part["status"] == "pending" and part["commands"] == 1
    assert part["failed"] == [{"unit": 99999, "reason": "not_owned"}, {"unit": enemy, "reason": "not_owned"}]
    gone = chk({"cmd": "stop", "units": [99999]})
    assert gone["status"] == "rejected" and gone["reason"] == "not_owned"
    assert chk({"cmd": "attack", "units": [m], "target": enemy})["reason"] == "target_not_visible"
    assert chk({"cmd": "attack", "units": [m], "target": enemy, "to": [3, 3]})["reason"] == "invalid"
    assert chk({"cmd": "set_stance", "units": [m], "stance": "berserk"})["reason"] == "invalid"
    assert chk({"cmd": "build", "item": "weap"})["reason"] == "not_available"
    assert chk({"cmd": "place", "item": "powr"})["reason"] == "not_ready"
    assert chk({"cmd": "cancel", "item": "powr"})["reason"] == "not_available"
    assert chk({"cmd": "sell", "building": m})["reason"] == "invalid"                   # a unit
    assert chk({"cmd": "sell", "building": 424242})["reason"] == "not_owned"
    f = deploy(w)
    assert chk({"cmd": "move", "units": [f.id], "to": [3, 3]})["failed"] == [{"unit": f.id, "reason": "is_building"}]
    assert chk({"cmd": "stop", "units": [f.id]})["status"] == "pending"                 # stop takes buildings
    assert chk({"cmd": "train", "item": "e1", "count": 3})["reason"] == "not_available"
    assert chk({"cmd": "rally", "building": f.id, "to": [3, 3]})["status"] == "pending"
    assert chk({"cmd": "repair", "building": f.id, "on": False})["detail"] == "not_repairing"
    assert chk({"cmd": "power_toggle", "building": f.id, "on": True})["detail"] == "already_on"
    assert chk({"cmd": "build", "item": "POWR"})["status"] == "pending"                 # case does not matter


def test_engine_verdicts_cells_outside_bounds_dead_units_and_wrecks():
    w = mk()
    m = mcv(w, 0)
    e1 = w._spawn_unit(0, "e1", m.x + 2, m.y, event=False)
    send(w, 0, {"cmd": "move", "units": [e1.id], "to": [0, 0]})                 # inside the map, outside bounds
    v = verdicts(w)
    assert v[0][3:5] == ("rejected", "invalid") and v[0][7] == 0
    checks = send(w, 0, {"cmd": "move", "units": [e1.id, m.id], "to": [m.cell()[0], m.cell()[1] + 3]})
    assert checks[0]["commands"] == 2
    w._kill(e1, None)                                                        # dies before the engine applies it
    v = verdicts(w)
    assert v[0][3:5] == ("partial", "unknown_actor") and v[0][7] == 1
    tank = w._spawn_unit(0, "3tnk", m.x, m.y + 3, event=False)
    w._kill(tank, None)
    lost = next(e for e in w.players[0].events if e["kind"] == "unit_lost" and e["id"] == tank.id)
    husk = w.actors[lost["wreck_id"]]
    assert husk.wreck and husk.type == "3tnk.husk"
    row = next(u for u in w.observation(0)["units"] if u["id"] == husk.id)
    assert row["wreck"] is True
    assert w.check_order(0, {"cmd": "stop", "units": [husk.id]})["failed"] == [{"unit": husk.id, "reason": "wreck"}]
    run(w, 46)
    assert husk.id not in w.actors                                           # burnt out


# ---- units, fog, combat --------------------------------------------------------------------------------------------------
def test_move_at_unit_speed_and_rally_points():
    w = mk()
    m = mcv(w, 0)
    e1 = w._spawn_unit(0, "e1", m.x, m.y + 2, event=False)
    x0, y0 = e1.cell()
    send(w, 0, {"cmd": "move", "units": [e1.id], "to": [x0 + 10, y0]})
    run(w, 4.0)
    assert 4 <= e1.cell()[0] - x0 <= 6                                       # 1.32 cells/s
    assert w.observation(0)["units"][1]["act"] == "Move" and not w.observation(0)["units"][1]["idle"]
    run(w, 5.0)
    assert e1.cell() == (x0 + 10, y0) and w.idle(e1)


def test_fog_of_war_and_enemy_spotted_once():
    w = mk()
    m = mcv(w, 0)
    scout = w._spawn_unit(0, "jeep", m.x, m.y, event=False)
    assert w.observation(0)["visible_enemies"] == []
    target = mcv(w, 1)
    send(w, 0, {"cmd": "move", "units": [scout.id], "to": [target.cell()[0] - 3, target.cell()[1]]})
    run(w, 30)
    obs = w.observation(0)
    assert [e["id"] for e in obs["visible_enemies"]] == [target.id]
    assert w.legal_actions(0)["attack"] == [target.id]
    spotted = [e for e in w.players[0].events if e["kind"] == "enemy_spotted"]
    assert [e["id"] for e in spotted] == [target.id]
    run(w, 2)
    assert len([e for e in w.players[0].events if e["kind"] == "enemy_spotted"]) == 1
    assert obs["explored_percent"] > w.observation(1)["explored_percent"]


def test_infantry_fight_kills_costs_and_events():
    w = mk()
    a = [w._spawn_unit(0, "e1", 40.5 + i * 0.3, 40.5, event=False) for i in range(5)]
    b = [w._spawn_unit(1, "e1", 43.5 + i * 0.3, 40.5, event=False) for i in range(3)]
    run(w, 15)
    assert all(u.id not in w.actors for u in b) and any(u.id in w.actors for u in a)
    m0, m1 = w.players[0].mil, w.players[1].mil
    assert m0["kills_cost"] == 300 and m0["units_killed"] == 3 and m1["deaths_cost"] == 300 and m1["units_lost"] == 3
    kinds0 = {e["kind"] for e in w.players[0].events}
    kinds1 = [e["kind"] for e in w.players[1].events]
    assert "enemy_killed" in kinds0 and kinds1.count("unit_lost") == 3 and "under_attack" in kinds1


def test_default_stance_ignores_plain_buildings_attack_anything_and_explicit_attacks_do_not():
    w = mk()
    deploy(w, 1)
    yard = w.fact(1)
    hp = yard.hp
    e1 = w._spawn_unit(0, "e1", yard.x - 3.5, yard.y + 1.5, event=False)
    run(w, 4)
    assert yard.hp == hp and w.idle(e1)                                     # defend: units and defenses only
    send(w, 0, {"cmd": "set_stance", "units": [e1.id], "stance": "attack_anything"})
    run(w, 4)
    assert yard.hp < hp
    hp = yard.hp
    send(w, 0, {"cmd": "set_stance", "units": [e1.id], "stance": "defend"}, {"cmd": "attack", "units": [e1.id],
                                                                            "target": yard.id})
    run(w, 4)
    assert yard.hp < hp and w.observation(0)["units"][-1]["act"] == "Attack"


def test_defenses_shoot_units_in_range():
    w = mk(countries=("russia", "england"), starting_cash=20000)
    deploy(w)
    build_place(w, 0, "powr")
    build_place(w, 0, "barr")
    ftur = build_place(w, 0, "ftur")
    e1 = w._spawn_unit(1, "e1", ftur.x + 3.5, ftur.y + 0.5, event=False)
    run(w, 3)
    assert e1.id not in w.actors and w.players[0].mil["units_killed"] == 1


def test_victory_needs_every_building_and_the_mcv():
    w = mk()
    f = deploy(w, 1)
    assert not w.over
    w._kill(mcv(w, 0), None)
    w.step()
    assert w.over and w.winner == 1
    w2 = mk()
    w2._kill(mcv(w2, 0), None)
    w2._kill(mcv(w2, 1), None)
    w2.step()
    assert w2.over and w2.winner == -1
    assert f is not None


def test_sell_refunds_half_and_repair_costs_money():
    w = mk()
    deploy(w)
    powr = build_place(w, 0, "powr")
    powr.hp = powr.max_hp // 2
    assert w.legal_actions(0)["repair"] == [powr.id]
    cash = w.players[0].resources()
    send(w, 0, {"cmd": "repair", "building": powr.id})
    assert w.check_order(0, {"cmd": "repair", "building": powr.id})["detail"] == "already_repairing"
    run(w, 10)
    assert powr.hp > powr.max_hp // 2 and w.players[0].resources() < cash
    cash = w.players[0].resources()
    send(w, 0, {"cmd": "sell", "building": powr.id})
    run(w, 1.4)
    assert powr.id not in w.actors and w.players[0].resources() > cash
    assert any(e["kind"] == "building_sold" for e in w.players[0].events)


def test_events_carry_increasing_seq_and_the_window_is_20():
    w = mk()
    for i in range(30):
        w._event(0, "under_attack", id=i)
    ev = w.observation(0)["events"]
    assert len(ev) == 20 and [e["seq"] for e in ev] == list(range(11, 31))


def test_the_same_seed_and_orders_give_the_same_world():
    def play():
        w = World(RULES, "trapped", 9)
        for s in (0, 1):
            send(w, s, {"cmd": "deploy", "units": [mcv(w, s).id]})
        run(w, 2)
        for s in (0, 1):
            send(w, s, {"cmd": "build", "item": "powr"})
        run(w, 30)
        return json.dumps([w.observation(0), w.observation(1)], sort_keys=True)
    assert play() == play()


# ---- the protocol ---------------------------------------------------------------------------------------------------------
def started(world=None, **kw) -> MatchServer:
    srv = MatchServer(world or mk(), **kw)
    for s in (0, 1):
        srv.get_game_state(s)
    with srv.cv:
        srv._start()
    return srv


def test_waiting_for_agents_until_both_check_in():
    srv = MatchServer(mk())
    st = srv.get_game_state(0)
    assert st["phase"] == "waiting_for_agents" and st["state_version"] == 0 and st["observation"] is None
    assert st["waiting_for_players"] == [1] and st["is_current_actor"] is False
    assert srv.play_action(0, 0, {"type": "orders", "orders": [{"cmd": "stop", "units": [1]}]})["error"] == \
        "NOT_YOUR_TURN"
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    srv.get_game_state(1)
    st = srv.wait_for_update(0, 0, False, "waiting_for_agents", 5)
    assert st["phase"] == "playing" and st["state_version"] > 0 and st["is_current_actor"] is True
    assert st["players"][1]["checked_in"] and st["pacing"]["mode"] == "realtime"
    assert set(st["legal_actions"]) == {"build", "train", "place", "deploy", "repair", "attack", "attack_now",
                                        "enemy_base", "combat_units", "idle_units", "harvesters",
                                        "production_buildings", "how_to_send"}
    assert st["legal_actions"]["attack_now"] is None
    srv.stop()


def test_invalid_action_cases_do_not_count_as_batches():
    w = mk()
    srv = started(w)
    v = srv.state(0)["state_version"]
    m = mcv(w, 0).id
    assert srv.play_action(0, v, None, action_id="0")["error"] == "INVALID_ACTION"
    assert srv.play_action(0, v, {"type": "pass"})["error"] == "INVALID_ACTION"
    assert srv.play_action(0, v, {"type": "orders", "orders": []})["error"] == "INVALID_ACTION"
    assert srv.play_action(0, v, {"type": "orders", "orders": [{"cmd": "stop", "units": [m]}] * 21})["error"] == \
        "INVALID_ACTION"
    bad = srv.play_action(0, v, {"type": "orders", "orders": [{"cmd": "train", "item": "3tnk"},
                                                             {"cmd": "deploy", "units": [7]}]})
    assert bad["error"] == "INVALID_ACTION"
    from examples.llm.redalert import refusal_rows
    assert refusal_rows(bad["detail"]) == [(0, "train", "not_available", "3tnk is not in available_production"),
                                           (1, "deploy", "not_owned", "no unit of this order can take it")]
    units = [w._spawn_unit(0, "e1", 30.5, 30.5, event=False).id for _ in range(60)]
    big = srv.play_action(0, v, {"type": "orders", "orders": [{"cmd": "stop", "units": units[:50]}] * 5})
    assert big["error"] == "INVALID_ACTION" and "engine commands" in big["detail"]
    assert srv.seats[0].st["batches"] == 0 and srv.seats[0].st["invalid_actions"] == 6
    assert srv.state(0)["last_orders"] is None


def test_stale_and_future_state_versions():
    w = mk()
    srv = started(w)
    orders = {"type": "orders", "orders": [{"cmd": "stop", "units": [mcv(w, 0).id]}]}
    assert srv.play_action(0, w.tick + 5, orders)["error"] == "STALE_STATE"
    for _ in range(610):
        w.step()
    assert srv.play_action(0, 25, orders)["error"] == "STALE_STATE"
    assert srv.play_action(0, "25", orders)["error"] == "STALE_STATE"


def test_pending_verdicts_land_in_last_orders_and_problems():
    w = mk()
    srv = started(w)
    m = mcv(w, 0).id
    v = srv.state(0)["state_version"]
    ans = srv.play_action(0, v, {"type": "orders", "orders": [
        {"cmd": "deploy", "units": [m, 99999]}, {"cmd": "train", "item": "e1"}, {"cmd": "move", "units": [m],
                                                                                 "to": [0, 0]}]})
    assert ans["accepted"] and ans["pending"] and ans["accepted_orders"] == 2 and ans["rejected_orders"] == 1
    assert [r["status"] for r in ans["results"]] == ["pending", "rejected", "pending"]
    assert ans["results"][0]["failed"] == [{"unit": 99999, "reason": "not_owned"}]
    lo = srv.state(0)["last_orders"]
    assert lo["pending"] is True and lo["batch"] == 1 and lo["state_version"] == v
    for _ in range(3):
        with srv.cv:
            srv._advance()
    lo = srv.state(0)["last_orders"]
    assert lo["pending"] is False and lo["applied_at_tick"] is not None
    assert [(r["status"], r["reason"]) for r in lo["results"]] == [("partial", "not_owned"),
                                                                  ("rejected", "not_available"),
                                                                  ("rejected", "invalid")]
    probs = srv.state(0)["recent_order_problems"]
    assert [(p["batch"], p["index"], p["status"]) for p in probs] == [(1, 0, "partial"), (1, 1, "rejected"),
                                                                     (1, 2, "rejected")]


def test_lost_orders_and_noop_batches():
    w = mk()
    srv = started(w, lost_rate=1.0)
    v = srv.state(0)["state_version"]
    ans = srv.play_action(0, v, {"type": "orders", "orders": [{"cmd": "stop", "units": [mcv(w, 0).id]}]})
    assert ans["results"][0]["status"] == "pending"
    lo = srv.state(0)["last_orders"]
    assert lo["results"][0]["status"] == "lost" and lo["results"][0]["reason"] == "not_applied"
    f = deploy(w)
    srv2 = started(w)
    v = srv2.state(0)["state_version"]
    ok = srv2.play_action(0, v, {"type": "orders", "orders": [{"cmd": "power_toggle", "building": f.id, "on": True}]})
    assert ok["accepted"] and ok["results"][0]["detail"] == "already_on" and srv2.seats[0].st["batches"] == 1


def test_the_batch_gap_holds_a_quick_second_batch_until_the_next_view():
    w = mk()
    srv = started(w)
    t = threading.Thread(target=srv._run_lockstep, daemon=True)
    m = mcv(w, 0).id
    srv.seats[1].done = True                                        # the other seat is not playing
    t.start()
    order = {"type": "orders", "orders": [{"cmd": "stop", "units": [m]}]}
    first = srv.play_action(0, w.tick, order)
    sent1 = srv.seats[0].last_orders["sent_at_tick"]
    second = srv.play_action(0, w.tick, order)
    sent2 = srv.seats[0].last_orders["sent_at_tick"]
    srv.stop()
    assert first["accepted"] and second["accepted"] and sent2 >= sent1 + STEP


def test_a_second_batch_while_the_first_is_being_answered_is_not_your_turn():
    w = mk()
    srv = started(w)                                              # no clock thread: the world only moves below
    order = {"type": "orders", "orders": [{"cmd": "stop", "units": [mcv(w, 0).id]}]}
    assert srv.play_action(0, w.tick, order)["accepted"]
    out = {}
    sv = w.tick
    t = threading.Thread(target=lambda: out.setdefault("held", srv.play_action(0, sv, order)), daemon=True)
    t.start()                                                     # held for the 0.2 s gap
    deadline = time.monotonic() + 5
    while not srv.seats[0].in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert srv.play_action(0, sv, order)["error"] == "NOT_YOUR_TURN"
    assert srv.state(0)["is_current_actor"] is False
    with srv.cv:
        srv._advance()
    t.join(5)
    assert out["held"]["accepted"] and srv.seats[0].last_orders["sent_at_tick"] == sv + STEP
    la = srv.get_legal_actions(0)
    assert la["state_version"] == w.tick and la["deploy"] == [mcv(w, 0).id]


def test_cli_runs_games_in_parallel_processes(capsys):
    platform_main(["--a", "rusher", "--b", "idle", "--games", "2", "--max-minutes", "0.5", "--jobs", "2"])
    text = capsys.readouterr().out
    # 30 s: no fight yet, the tiebreak's assets_value goes to the side that built something
    assert "game 1 seed 7001 trapped A=Multi1" in text and "2 games rusher vs idle: A 2W 0L 0D, score 100%" in text


def test_time_limit_tiebreak_idle_rule_and_resignation():
    srv = started(mk(), max_minutes=0.1)
    with srv.cv:
        while not srv.terminal:
            srv._advance()
    assert srv.reason == "time_limit" and srv.winner is None and set(srv.returns.values()) == {0.5}
    w = mk()
    w.players[0].mil["kills_cost"] = 100                            # the kill balance decides first
    srv = started(w, max_minutes=0.1)
    with srv.cv:
        while not srv.terminal:
            srv._advance()
    assert srv.winner == 0 and srv.result(0)["your_return"] == 1.0
    srv = started(mk(), idle_s=5, max_minutes=1)
    with srv.cv:
        while not srv.terminal:
            srv._advance()
    assert srv.reason == "idle_timeout" and srv.world.tick < 40 * 25
    srv = started(mk())
    res = srv.resign(1)
    assert res["termination_reason"] == "resignation" and res["returns"] == {"agent-a": 1.0, "agent-b": 0.0}
    assert srv.state(1)["result"]["outcome"] == "loss" and srv.state(0)["result"]["outcome"] == "win"
    assert srv.play_action(0, 30, {"type": "orders", "orders": [{"cmd": "stop", "units": [1]}]})["error"] == \
        "GAME_ALREADY_COMPLETE"
    with pytest.raises(Exception) as e:
        SeatSession(srv, 0).play_action(state_version=30, action={"type": "orders", "orders": []})
    assert getattr(e.value, "error_code", None) == "GAME_ALREADY_COMPLETE"


def test_seat_session_raises_platform_error_codes():
    srv = started(mk())
    sess = SeatSession(srv, 0)
    st = sess.get_state()
    assert st.state_version > 0 and st.raw["pacing"]["mode"] == "realtime" and st.legal_actions == []
    with pytest.raises(Exception) as e:
        sess.play_action(state_version=st.state_version, action={"type": "orders", "orders": []})
    assert e.value.error_code == "INVALID_ACTION"
    with pytest.raises(Exception) as e:
        sess.send_message(message_type="chat", content="hi")
    assert e.value.error_code == "RUNTIME_UNAVAILABLE"
    assert sess.get_game_config("red_alert")["rules"]["maps"]["sidestep"]["spawns"]


# ---- whole matches through the official runner ----------------------------------------------------------------------------
def test_rusher_beats_idle_by_victory_through_the_runner():
    row = play_match(make_bot("rusher", 1), make_bot("idle", 2), seed=21, map_id="sidestep", max_minutes=8)
    assert row["result_a"] == "win" and row["reason"] == "victory"
    a, b = row["a"], row["b"]
    assert a["error"] is None and b["error"] is None and not a["hung"] and not b["hung"]
    assert a["first_proc_s"] is not None and a["peak_army"] >= 600 and a["kills_cost"] >= 2000
    assert a["batches"] > 10 and a["decisions_per_s"] > 1 and b["batches"] == 0
    assert row["forced_advances"] == 0


def test_matches_are_deterministic_in_lockstep():
    def strip(r):
        out = {k: v for k, v in r.items() if k != "wall_s"}
        for s in ("a", "b"):
            out[s] = {k: v for k, v in r[s].items() if k not in ("slowest_ms", "mean_ms")}
        return json.dumps(out, sort_keys=True)
    rows = [play_match(make_bot("llm", 3), make_bot("rusher", 4), seed=5, map_id="trapped", max_minutes=4)
            for _ in range(2)]
    assert strip(rows[0]) == strip(rows[1])


def test_the_llm_like_bot_makes_refused_orders_and_the_runner_keeps_going():
    row = play_match(make_bot("llm", 5), make_bot("turtle", 6), seed=8, map_id="shadowfiend-ii", max_minutes=8)
    a = row["a"]
    assert a["error"] is None and row["b"]["error"] is None
    assert a["invalid_actions"] + a["rejected"] + a["partial"] >= 1
    assert a["batches_per_s"] < 0.25                                  # one batch every 5-10 s


def test_our_deployed_agent_plays_a_match_on_the_sim():
    row = play_match(make_factory("ours")(1), make_bot("idle", 2), seed=3, map_id="sidestep", max_minutes=1)
    assert row["result_a"] == "draw" and row["reason"] == "time_limit"
    assert row["a"]["error"] is None and row["a"]["batches"] == 0 and row["a"]["decisions"] > 100


def test_realtime_clock_runs_at_the_speed_factor():
    t0 = time.monotonic()
    row = play_match(make_bot("turtle", 1), make_bot("idle", 2), seed=4, map_id="sidestep", clock="realtime",
                     speed=25, max_minutes=0.5)
    wall = time.monotonic() - t0
    assert row["reason"] == "time_limit" and row["game_s"] == 30.0
    assert 0.8 < wall < 8
    assert row["a"]["batches"] >= 3 and row["a"]["error"] is None


def test_cli_prints_per_match_metrics(capsys, tmp_path):
    out = tmp_path / "rows.json"
    platform_main(["--a", "rusher", "--b", "idle", "--games", "1", "--max-minutes", "1", "--json", str(out)])
    text = capsys.readouterr().out
    assert "game 0 seed 7000 sidestep" in text and "1 games rusher vs idle" in text
    rows = json.loads(out.read_text())["rows"]
    assert rows[0]["reason"] == "time_limit" and rows[0]["a"]["agent"] == "agent-rusher"


def test_wait_only_agents_never_send_and_bots_wait_before_the_start():
    bot = make_bot("rusher", 1)
    srv = MatchServer(mk())
    srv.get_game_state(0)
    st = SeatSession(srv, 0).get_state()
    assert bot.choose_action(st, None) is WAIT


def test_fake_platform_red_alert_match_against_a_rusher_over_mcp(tmp_path, monkeypatch):
    """local/fake_platform.py --official --red-alert 1 --ra-opponent rusher: our deployed agent (WAIT) through the
    SDK's official worker over HTTP / MCP loses its seat by victory to the scripted rusher on the simulator."""
    import os
    import signal
    import socket
    import subprocess
    import sys
    from pathlib import Path

    from altruagent.official import OfficialAgentClient
    from altruagent.worker import TournamentWorkerInput, run_tournament_worker
    from local.fake_platform import DEFAULT_OFFICIAL_KEY

    root = Path(__file__).resolve().parents[2]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stats = tmp_path / "platform.json"
    proc = subprocess.Popen([sys.executable, "-m", "local.fake_platform", "--port", str(port), "--official",
                             "--concurrent", "0", "--red-alert", "1", "--ra-seconds", "400", "--ra-opponent", "rusher",
                             "--ra-speed", "25", "--stats", str(stats), "--stats-every", "0.5"], cwd=root,
                            env=dict(os.environ, PYTHONPATH=str(root)), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        official = OfficialAgentClient(url, DEFAULT_OFFICIAL_KEY, load_env_file=False)
        rows, deadline = [], time.monotonic() + 30
        while not rows and time.monotonic() < deadline:
            try:
                official.authenticate()
                rows = official.assignments()
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(0.3)
        assert [(r.game_type, r.seat_count) for r in rows] == [("red_alert", 2)]
        monkeypatch.setenv("ALTRUAGENT_CONTROL_URL", url)
        monkeypatch.setenv("ALTRUAGENT_OFFICIAL_AGENT_KEY", DEFAULT_OFFICIAL_KEY)
        code = run_tournament_worker(TournamentWorkerInput(seat_id=rows[0].seat_id, match_id=rows[0].match_id,
                                                           game_type="red_alert", agent_spec="agent.agent:create_agent",
                                                           execution_id="exec-R-" + "q" * 40))
        assert code == 0
        deadline = time.monotonic() + 10
        st = {}
        while time.monotonic() < deadline:
            st = json.loads(stats.read_text()) if stats.exists() else {}
            if (st.get("totals") or {}).get("matches_done"):
                break
            time.sleep(0.3)
        ra = next(m for m in st["matches"] if m["game_type"] == "red_alert")
        assert ra["terminal"] and ra["reason"] == "victory" and ra["win"] is False and not ra["opponent_error"]
        assert ra["batches"] == 0 and ra["opponent_military"]["kills_cost"] >= 2000
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_limits_constants_match_the_doc():
    assert ra_server.MAX_ORDERS == 20 and ra_server.MAX_COMMANDS == 200 and ra_server.STALE_TICKS == 3000
    assert ra_server.MIN_BATCH_INTERVAL_S == 0.2 and ra_server.IDLE_S == 600
