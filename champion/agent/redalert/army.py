"""The army (docs/redalert_design.md sections 8 and 9): squads kept as our own id sets (HOME guards the base, MAIN is
the attack group, REINF follows MAIN while it is away, one early scout), the enemy we have seen (decaying memory of
units, remembered buildings), threat and rush detection at home, MAIN's state machine (GATHER -> ADVANCE along the
ground path's waypoints -> ENGAGE / SIEGE with focus fire -> HUNT; RETREAT when outmatched; DEFEND when home is hit),
and the tiebreak endgame (hold, careful, all-in)."""
from __future__ import annotations

from .orders import Cand
from .world import AIR, GROUND_DEFENSES, NON_COMBAT, WALLS, dist, is_combat, is_infantry

PRODUCTION_TARGETS = frozenset({"weap", "tent", "barr", "hpad", "afld", "afld.ukraine", "spen", "syrd", "kenn"})
TOWER_TYPES = frozenset({"pbox", "hbox", "gun", "ftur", "tsla"})


def _centroid(pts):
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _cell(c):
    return [int(round(c[0])), int(round(c[1]))]


class Army:
    def __init__(self, pol):
        self.pol = pol
        self.home: set[int] = set()
        self.main: set[int] = set()
        self.reinf: set[int] = set()
        self.scout: int | None = None
        self.scout_phase = None
        self.scout_tick = 0
        self.scout_pos = None
        self.scouted = False
        self.state = "GATHER"
        self.state_tick = 0
        self.wp = 0
        self.wp_tick = 0
        self.order_tick = -10 ** 9
        self.focus: int | None = None
        self.focus_tick = -10 ** 9
        self.fail_mult = 1.0
        self.last_fail_value = 0.0      # value of the last wave that had to retreat: the next one must be bigger
        self.wave_value = 0.0
        self.enemy_peak = 0.0
        self._peak_tick = 0
        self._bk = 0
        self.waves = 0
        self.stuck_pos = None
        self.stuck_tick = 0
        self.stuck_count = 0
        self.engage_value = 0.0
        self.engage_kills = 0
        self.hunt_i = 0
        self.hunt_tick = 0
        self.nothing_since = None
        # what we know of the enemy
        self.enemy_units: dict[int, dict] = {}
        self.enemy_blds: dict[int, dict] = {}
        self.last_enemy_combat = -10 ** 9
        self.enemy_air_tick = -10 ** 9
        # home
        self.threatened = False
        self.threat_value = 0.0
        self.threat_point = None
        self.threat_tick = -10 ** 9
        self.blind_point = None
        self.blind_tick = -10 ** 9
        self._ev_seq = -1
        self.scout_lost_early = False
        self.defense_tick = -10 ** 9
        self.defense_point = None
        self.rush = False
        self.rush_tick = -10 ** 9
        self.mode = "normal"            # endgame: normal / hold / careful / allin
        self.tag = "opening"
        self.v = None

    # ---- bookkeeping ----------------------------------------------------------------------------------------------
    def cost(self, t: str) -> int:
        return self.pol.static.rules.cost(t)

    def value(self, ids, v) -> float:
        tot = 0.0
        for i in ids:
            u = v.unit_by_id.get(i)
            if u is not None:
                tot += self.cost(u["type"]) * max(0.05, u["hp"] / 100.0)
        return tot

    def refresh(self, v) -> None:
        self.v = v
        rules = v.st.rules
        alive = v.unit_by_id
        for s in (self.home, self.main, self.reinf):
            s.intersection_update(alive)
        if self.scout is not None and self.scout not in alive:
            # killed on its way out, far from the enemy base: something is coming our way
            last = self.scout_pos
            if v.game_s < 150:
                self.scout_lost_early = True          # an army already stands in their base: more rifles early
                self.pol.log_event(f"scout killed early at {v.game_s:.0f}s near {last}")
            if self.scout_phase in ("out", "look", "back") and v.game_s < 240 and last is not None and \
                    dist(last, v.st.enemy_spawn) > 12:
                self.pol.log_event(f"scout lost on the way at {v.game_s:.0f}s near {last}: expecting a rush")
                self.rush, self.rush_tick = True, v.tick
            self.scout = None
            self.scouted = True
        quota = 10 ** 6 if self.rush else int(self.pol.p.home_infantry)
        for u in v.units:
            uid, t = u["id"], u["type"]
            if uid in self.home or uid in self.main or uid in self.reinf or uid == self.scout:
                continue
            if not is_combat(t, rules) or t in AIR:
                continue
            if not self.scouted and self.scout is None and t == "e1" and v.game_s < 200:
                self.scout, self.scout_phase, self.scout_tick = uid, "out", v.tick
                continue
            if is_infantry(t, rules) and len(self.home) < quota:
                self.home.add(uid)
            elif self.state in ("GATHER", "RETREAT", "DEFEND"):
                self.main.add(uid)
            else:
                self.reinf.add(uid)
        # infantry over the home quota (after a rush) joins the attack group
        if not self.rush and len(self.home) > quota + 2:
            extra = sorted(self.home)[quota:]
            for uid in extra:
                self.home.discard(uid)
                (self.main if self.state in ("GATHER", "RETREAT", "DEFEND") else self.reinf).add(uid)
        self._remember(v)

    def _remember(self, v) -> None:
        rules = v.st.rules
        vis_val = 0.0
        for e in v.enemies:
            t = e["type"]
            self.enemy_units[e["id"]] = {"type": t, "tick": v.tick, "x": e["x"], "y": e["y"], "hp": e["hp"]}
            if t in AIR:
                self.enemy_air_tick = v.tick
            if is_combat(t, rules) and t not in AIR:
                self.last_enemy_combat = v.tick
                vis_val += self.cost(t) * max(0.1, e["hp"] / 100.0)
        # the largest enemy army seen at once, fading slowly (half-life 6 minutes): what an attack must beat even
        # after that army went back into the fog
        dt = max(0, v.tick - self._peak_tick)
        self._peak_tick = v.tick
        self.enemy_peak = max(vis_val, self.enemy_peak * 0.5 ** (dt / (360 * 25.0)))
        for b in v.enemy_blds:
            self.enemy_blds[b["id"]] = {"type": b["type"], "tick": v.tick, "x": b["x"], "y": b["y"], "hp": b["hp"]}
        for k in [k for k, e in self.enemy_units.items() if v.tick - e["tick"] > 240 * 25]:
            del self.enemy_units[k]
        mine = [(u["x"], u["y"]) for u in v.units]
        for store, seen in ((self.enemy_blds, {b["id"] for b in v.enemy_blds}), (self.enemy_units, v.enemy_ids)):
            if not store:
                continue
            for k in [k for k in store if k not in seen]:
                e = store[k]
                # one of our units stands where we saw it and does not see it: it is gone (dead or moved on)
                if any(abs(p[0] - e["x"]) <= 2 and abs(p[1] - e["y"]) <= 2 for p in mine):
                    del store[k]
        bk = v.buildings_killed
        if bk > self._bk:
            self.fail_mult, self.last_fail_value = 1.0, 0.0      # razing a building resets the caution
        self._bk = bk

    def enemy_army(self, v, secs: float = 60.0) -> float:
        """Cost of the enemy combat units seen recently: full weight up to half of `secs`, fading out by `secs`."""
        rules = v.st.rules
        tot = 0.0
        half = secs / 2.0
        for e in self.enemy_units.values():
            if not is_combat(e["type"], rules) or e["type"] in AIR:
                continue
            age = (v.tick - e["tick"]) / 25.0
            w = 1.0 if age <= half else max(0.0, (secs - age) / half)
            tot += self.cost(e["type"]) * w
        return tot

    def enemy_strength(self, v) -> float:
        """What an attack has to beat: the enemy army (recent sightings, or the biggest army seen at once, fading)
        plus 1.5x the towers we know of."""
        return max(self.enemy_army(v, 180.0), self.enemy_peak) + 1.5 * self.enemy_towers()

    def enemy_towers(self) -> float:
        return sum(self.cost(b["type"]) for b in self.enemy_blds.values() if b["type"] in GROUND_DEFENSES)

    def enemy_vehicle_share(self) -> float:
        v = self.v
        if v is None:
            return 0.0
        rules = v.st.rules
        veh = inf = 0.0
        for e in self.enemy_units.values():
            if v.tick - e["tick"] > 60 * 25 or not is_combat(e["type"], rules):
                continue
            c = self.cost(e["type"])
            if is_infantry(e["type"], rules):
                inf += c
            else:
                veh += c
        veh += 0.5 * sum(self.cost(b["type"]) for b in self.enemy_blds.values() if b["type"] in ("gun", "tsla"))
        if veh + inf <= 0:
            return 0.1 if v.game_s < 300 else 0.3       # nothing seen: early armies are infantry
        return veh / (veh + inf)

    def enemy_infantry_share(self) -> float:
        return 1.0 - self.enemy_vehicle_share()

    def enemy_inf_count(self, v, secs: float = 60.0) -> int:
        rules = v.st.rules
        return sum(1 for e in self.enemy_units.values() if v.tick - e["tick"] <= secs * 25
                   and is_combat(e["type"], rules) and is_infantry(e["type"], rules))

    def infantry_heavy(self, v) -> bool:
        """The enemy army we have seen is mostly infantry (by cost) and not trivial: answer with rifles and towers."""
        return self.enemy_vehicle_share() <= 0.45 and self.enemy_army(v) >= 800

    def defense_first(self, v) -> bool:
        """Before 4:00, after an early rush sign (rush seen, or our scout killed early by an army at home): rifles
        come before the war factory and the depot until we have a solid infantry screen."""
        if v.game_s >= 240 or not (self.rush or self.scout_lost_early):
            return False
        rules = v.st.rules
        inf = sum(1 for u in v.units if is_infantry(u["type"], rules) and is_combat(u["type"], rules))
        return inf < int(self.pol.p.early_infantry)

    def enemy_air_seen(self) -> bool:
        v = self.v
        return v is not None and v.tick - self.enemy_air_tick < 120 * 25

    def towers_wanted(self, v) -> int:
        if self.rush:
            return 2 if v.game_s < 300 else 3
        want = 0
        e_army = self.enemy_army(v)
        if self.infantry_heavy(v) and e_army > 0.8 * v.army_value:
            want = 2 if e_army < 2 * max(1, v.army_value) else 3
        if self.threatened and v.game_s < 360 and self.threat_value > 600:
            want = max(want, 1 if self.threat_value < 1500 else 2)
        return min(4, want)

    # ---- home: threat and rush -----------------------------------------------------------------------------------
    def assess(self, v) -> None:
        rules = v.st.rules
        p = self.pol.p
        base = v.base
        if base is None:
            self.threatened = False
            return
        r = p.threat_radius
        blds = [(b["x"], b["y"]) for b in v.buildings]
        harvs = [(u["x"], u["y"]) for u in v.units if u["type"] == "harv" and dist((u["x"], u["y"]), base) < 28]
        threat = []
        for e in v.enemies:
            if not is_combat(e["type"], rules) or e["type"] in AIR:
                continue
            ep = (e["x"], e["y"])
            if dist(ep, base) <= r or any(abs(ep[0] - b[0]) <= 8 and abs(ep[1] - b[1]) <= 8 for b in blds) \
                    or any(dist(ep, h) <= 6 for h in harvs):
                threat.append(e)
        for b in v.enemy_blds:                       # an enemy tower or yard creeping up next to our base
            if b["type"] in GROUND_DEFENSES and dist((b["x"], b["y"]), base) <= r:
                threat.append(b)
        self._blind(v, base)
        if threat:
            self.threat_value = sum(self.cost(e["type"]) * max(0.1, e["hp"] / 100.0) for e in threat)
            self.threat_point = _centroid([(e["x"], e["y"]) for e in threat])
            self.threat_tick = v.tick
            self.threatened = True
        elif self.blind_point is not None and v.tick - self.blind_tick <= 6 * 25:
            # our actors are being hit (or lost) near the base by attackers we cannot see (an attacker at weapon
            # range can stand outside a building's sight): go there, the defenders will see them on arrival
            self.threat_point = self.blind_point
            self.threat_value = max(self.threat_value, 600.0)
            self.threat_tick = v.tick
            self.threatened = True
        elif v.tick - self.threat_tick > 5 * 25:
            self.threatened = False
            self.threat_value = 0.0
        # an early rush: lots of enemy units on our half of the map before 6:00 while our army is small (an army
        # standing at home in its own base is not a rush)
        if v.game_s < 360:
            es = v.st.enemy_spawn
            coming = [e for e in self.enemy_units.values()
                      if v.tick - e["tick"] < 45 * 25 and is_combat(e["type"], rules) and e["type"] not in AIR
                      and (dist((e["x"], e["y"]), base) < dist((e["x"], e["y"]), es) or dist((e["x"], e["y"]), base) < 30)]
            e_army = sum(self.cost(e["type"]) for e in coming)
            inf_seen = sum(1 for e in coming if is_infantry(e["type"], rules))
            ours = v.army_value
            barracks = sum(1 for b in self.enemy_blds.values() if b["type"] in ("tent", "barr"))
            if ((inf_seen >= 6 or e_army >= 1200 or (self.threatened and self.threat_value >= 800))
                    and ours < 1.5 * max(e_army, self.threat_value)) or (barracks >= 2 and v.game_s < 200
                                                                         and ours < 1500):
                if not self.rush:
                    self.pol.log_event(f"early rush detected at {v.game_s:.0f}s (seen {e_army:.0f}, ours {ours})")
                self.rush, self.rush_tick = True, v.tick
        if self.rush and (v.game_s >= 420 or (v.tick - self.rush_tick > 60 * 25 and not self.threatened)):
            self.rush = False

    def _blind(self, v, base) -> None:
        """New under_attack / unit_lost / building_lost events at a position near our base (events are hints: only
        a new seq counts, positions only when the event carries x / y)."""
        for ev in v.events:
            seq = ev.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= self._ev_seq:
                continue
            self._ev_seq = max(self._ev_seq, seq)
            if ev.get("kind") not in ("under_attack", "unit_lost", "building_lost"):
                continue
            x, y = ev.get("x"), ev.get("y")
            if not isinstance(x, int) or not isinstance(y, int) or isinstance(x, bool) or isinstance(y, bool):
                aid = ev.get("id")
                own = v.unit_by_id.get(aid) or v.bld_by_id.get(aid) if isinstance(aid, int) else None
                if own is None:
                    continue
                x, y = own["x"], own["y"]
            if str(ev.get("type") or "") in ("harv",) and dist((x, y), base) > 28:
                continue
            if dist((x, y), base) <= 25:
                self.blind_point, self.blind_tick = (x, y), v.tick

    # ---- endgame ---------------------------------------------------------------------------------------------------
    def endgame(self, v) -> None:
        p = self.pol.p
        if v.remaining > p.endgame_s:
            self.mode = "normal"
            return
        margin = v.kills - v.deaths
        if margin > p.hold_margin:
            self.mode = "hold"
        elif margin > 0:
            self.mode = "careful"
        elif margin < 0:
            self.mode = "allin"
        else:
            self.mode = "hold" if (v.kills == 0 and v.deaths == 0) else "careful"

    # ---- orders helper --------------------------------------------------------------------------------------------
    def _send(self, out, v, ids, cmd: str, to=None, target=None, urgent=False, min_gap=20, same_gap=75,
              tag="") -> int:
        led = self.pol.ledger
        sig = (cmd, tuple(to) if to is not None else None, target)
        ok = []
        for i in ids:
            u = v.unit_by_id.get(i)
            if u is None:
                continue
            if led.unit_ok(i, sig, u["idle"], min_gap, same_gap):
                ok.append(i)
        if not ok:
            return 0
        ok.sort()
        for k in range(0, len(ok), 50):
            o = {"cmd": cmd, "units": ok[k:k + 50]}
            if to is not None:
                o["to"] = _cell(to)
            if target is not None:
                o["target"] = target
            out.append(Cand(o, urgent=urgent, sig=sig, tag=tag))
        return len(ok)

    def _pts(self, ids, v):
        return [(v.unit_by_id[i]["x"], v.unit_by_id[i]["y"]) for i in ids if i in v.unit_by_id]

    # ---- every view ----------------------------------------------------------------------------------------------
    def step(self, v, out: list[Cand], allow: bool) -> None:
        self.refresh(v)
        self.assess(v)
        self.endgame(v)
        self._defend(v, out, allow)
        if allow:
            self._scout_step(v, out)
            self._main_step(v, out)
            self._reinf_step(v, out)
            self._home_idle(v, out)
        self._set_tag(v)

    def _set_tag(self, v) -> None:
        if self.mode == "hold" or self.mode == "careful":
            self.tag = "endgame-hold"
        elif self.mode == "allin":
            self.tag = "endgame-push"
        elif self.threatened:
            self.tag = "defending"
        elif self.state in ("ADVANCE", "ENGAGE", "SIEGE", "HUNT"):
            self.tag = "attacking"
        elif v.game_s < 180:
            self.tag = "opening"
        elif self.main:
            self.tag = "gathering"
        else:
            self.tag = "economy"

    # ---- home defense ----------------------------------------------------------------------------------------------
    def _defend(self, v, out, allow: bool) -> None:
        if not self.threatened or self.threat_point is None:
            if self.state == "DEFEND" and v.tick - self.threat_tick > 5 * 25:
                self._go(v, "GATHER")
            return
        tp = self.threat_point
        if v.base is not None and dist(tp, v.base) > 30:
            return
        moved = self.defense_point is None or dist(tp, self.defense_point) > 4
        if v.tick - self.defense_tick < 3 * 25 and not moved:
            return
        towers = sum(self.cost(b["type"]) for b in v.buildings if b["type"] in TOWER_TYPES)
        home_val = self.value(self.home, v)
        sent = self._send(out, v, sorted(self.home), "attack_move", to=tp, urgent=True, min_gap=10, same_gap=50,
                          tag="defend")
        if self.state in ("GATHER", "DEFEND") or (self.state == "RETREAT"):
            # the attack group waits at home: it always fights with the home guard (no defeat in detail)
            if self.threat_value > 0.25 * (home_val + towers) or self.rush or self.state == "GATHER":
                if self.state != "DEFEND":
                    self._go(v, "DEFEND")
                sent += self._send(out, v, sorted(self.main | self.reinf), "attack_move", to=tp, urgent=True,
                                   min_gap=10, same_gap=50, tag="defend")
        elif self.state in ("ADVANCE", "ENGAGE", "SIEGE", "HUNT"):
            main_val = self.value(self.main, v)
            razing = self.state == "SIEGE" and len(v.enemy_blds) >= 1 and self.mode != "hold"
            if self.threat_value >= 0.5 * max(1.0, main_val) and not razing:
                self.pol.log_event(f"recalling the attack group to defend at {v.game_s:.0f}s")
                self._go(v, "DEFEND")
                sent += self._send(out, v, sorted(self.main), "attack_move", to=tp, urgent=True, min_gap=10,
                                   same_gap=50, tag="defend")
            if self.reinf:
                sent += self._send(out, v, sorted(self.reinf), "attack_move", to=tp, urgent=True, min_gap=10,
                                   same_gap=50, tag="defend")
        if sent:
            self.defense_tick, self.defense_point = v.tick, tp

    def _home_idle(self, v, out) -> None:
        if self.threatened:
            return
        g = v.st.guard or v.base
        if g is None:
            return
        far = [i for i in self.home if i in v.unit_by_id and v.unit_by_id[i]["idle"]
               and dist((v.unit_by_id[i]["x"], v.unit_by_id[i]["y"]), g) > 6]
        if far:
            self._send(out, v, far, "attack_move", to=g, min_gap=50, same_gap=250)

    # ---- the early scout -------------------------------------------------------------------------------------------
    def _scout_step(self, v, out) -> None:
        if self.scout is None:
            return
        u = v.unit_by_id.get(self.scout)
        if u is None:
            return
        st = v.st
        pos = (u["x"], u["y"])
        self.scout_pos = pos
        if self.scout_phase == "out":
            # all the way into the base (its barracks and army are what we want to see), not just its first building
            arrived = dist(pos, st.enemy_spawn) <= 4 or (u["hp"] < 60 and bool(v.enemy_blds))
            if arrived or v.tick - self.scout_tick > 90 * 25 or self.rush:
                self.scout_phase, self.scout_tick = "look", v.tick
            else:
                self._send(out, v, [self.scout], "move", to=st.enemy_spawn, min_gap=25, same_gap=125)
                return
        if self.scout_phase == "look":
            # walk through the far side of the base (barracks exits, the army standing there) for a few seconds
            beyond = st.step_toward(st.enemy_spawn, (2 * st.enemy_spawn[0] - st.my_spawn[0],
                                                    2 * st.enemy_spawn[1] - st.my_spawn[1]), 6)
            if v.tick - self.scout_tick > 8 * 25 or dist(pos, beyond) <= 2 or self.rush:
                self.scout_phase, self.scouted = "back", True
                self.pol.log_event(f"scout turns back at {v.game_s:.0f}s at {pos} (enemy spawn {st.enemy_spawn}, "
                                   f"buildings seen {sorted(b['type'] for b in self.enemy_blds.values())}, "
                                   f"enemy units seen {len(self.enemy_units)})")
            else:
                self._send(out, v, [self.scout], "move", to=beyond, min_gap=25, same_gap=125)
                return
        if self.scout_phase == "back":
            if dist(pos, st.picket) <= 3:
                self.scout_phase = "picket"
            else:
                self._send(out, v, [self.scout], "move", to=st.picket, min_gap=25, same_gap=125)
        if self.scout_phase == "picket" and (self.rush or v.game_s > 420):
            self.home.add(self.scout)                 # the picket comes home when the base needs it
            self.scout = None

    # ---- MAIN ------------------------------------------------------------------------------------------------------
    def _go(self, v, state: str) -> None:
        if state != self.state:
            self.state, self.state_tick = state, v.tick
            self.order_tick = -10 ** 9
            self.focus = None
            self.stuck_pos, self.stuck_count = None, 0
            self.nothing_since = None

    def _should_attack(self, v, main_val: float, n: int, n_veh: int) -> bool:
        p = self.pol.p
        if n == 0 or self.mode in ("hold", "careful"):
            return False
        if self.mode == "allin":
            return True
        strength = self.enemy_strength(v)
        if strength > 0 and main_val < p.attack_ratio * strength:
            return False
        if self.last_fail_value > 0 and main_val < 1.3 * self.last_fail_value:
            return False                               # the last wave had to retreat: come back bigger
        if n >= p.attack_min_units and main_val >= p.attack_min_value * self.fail_mult:
            return True
        if v.game_s >= p.attack_late_s and n_veh >= p.attack_late_units and main_val >= 2000 * self.fail_mult:
            return True
        quiet = (v.tick - self.last_enemy_combat) / 25.0 >= p.quiet_s
        if v.game_s >= p.opportunistic_s and n_veh >= 3 and quiet and self.fail_mult <= 1.0:
            return True
        return strength > 0 and n >= 3 and main_val >= 3.0 * strength and v.game_s >= 180

    def _main_step(self, v, out) -> None:
        st = v.st
        ids = sorted(i for i in self.main if i in v.unit_by_id)
        if not ids:
            if self.state not in ("GATHER", "DEFEND"):
                self._go(v, "GATHER")
            return
        pts = self._pts(ids, v)
        c = _centroid(pts)
        main_val = self.value(ids, v)
        rules = st.rules
        n_veh = sum(1 for i in ids if not is_infantry(v.unit_by_id[i]["type"], rules))
        if self.mode == "allin" and self.state in ("GATHER", "DEFEND") and not self.threatened:
            # everything goes: home units too in the last minute and a half
            if v.remaining <= self.pol.p.allin_s:
                for i in list(self.home) + list(self.reinf):
                    self.home.discard(i)
                    self.reinf.discard(i)
                    self.main.add(i)
                ids = sorted(i for i in self.main if i in v.unit_by_id)
            self.waves += 1
            self.wave_value = main_val
            self.pol.log_event(f"all-in wave {self.waves} at {v.game_s:.0f}s: {len(ids)} units (behind on kills)")
            self._go(v, "ADVANCE")
            self.wp = self._first_wp(v)
            self.wp_tick = v.tick
        state = self.state
        if state == "DEFEND":
            return                                    # _defend drives it; back to GATHER when the threat is gone
        if state == "GATHER":
            self._gather(v, out, ids, main_val, n_veh)
            return
        if state == "RETREAT":
            self._retreat_step(v, out, ids, c)
            return
        if self.mode == "hold" and not self._nearly_won(v, c):
            self._start_retreat(v, out, ids, "hold the lead")
            return
        near_enemies = self._enemies_near(v, c, self.pol.p.engage_radius + 2)
        near_blds = [b for b in v.enemy_blds if dist((b["x"], b["y"]), c) <= 14]
        if near_enemies:
            if self._outmatched(v, ids, c, near_enemies, near_blds):
                self._start_retreat(v, out, ids, "outmatched")
                return
            if state != "ENGAGE":
                self.engage_value, self.engage_kills = main_val, v.kills
                self._go(v, "ENGAGE")
            self._engage(v, out, ids, c, near_enemies)
            return
        if state == "ENGAGE":
            self._go(v, "SIEGE" if (near_blds or v.enemy_blds) else "ADVANCE")
        if near_blds or (self.state == "SIEGE"):
            if self.state != "SIEGE":
                self._go(v, "SIEGE")
            self._siege(v, out, ids, c)
            return
        if self.state == "HUNT":
            self._hunt(v, out, ids, c)
            return
        self._advance(v, out, ids, c)

    def _first_wp(self, v) -> int:
        st = v.st
        base = v.base or st.my_spawn
        r = dist(st.rally, base) if st.rally else 0
        for k, w in enumerate(st.waypoints):
            if dist(w, base) > r + 3:
                return k
        return max(0, len(st.waypoints) - 1)

    def _gather(self, v, out, ids, main_val, n_veh) -> None:
        st = v.st
        rally = st.rally or v.base
        if self._should_attack(v, main_val, len(ids), n_veh):
            self.waves += 1
            self.wave_value = main_val
            self.pol.log_event(f"attack wave {self.waves} at {v.game_s:.0f}s: {len(ids)} units, value {main_val:.0f}, "
                               f"enemy strength {self.enemy_strength(v):.0f} (towers {self.enemy_towers():.0f})")
            self._go(v, "ADVANCE")
            self.wp = self._first_wp(v)
            self.wp_tick = v.tick
            self._advance(v, out, ids, _centroid(self._pts(ids, v)))
            return
        far = [i for i in ids if v.unit_by_id[i]["idle"]
               and dist((v.unit_by_id[i]["x"], v.unit_by_id[i]["y"]), rally) > 5]
        if far:
            self._send(out, v, far, "attack_move", to=rally, min_gap=50, same_gap=250)

    def _advance(self, v, out, ids, c) -> None:
        st = v.st
        wps = st.waypoints
        if self.wp >= len(wps):
            self._go(v, "SIEGE")
            self._siege(v, out, ids, c)
            return
        target = wps[self.wp]
        pts = self._pts(ids, v)
        near = sum(1 for p in pts if dist(p, target) <= 5)
        core = [p for p in pts if dist(p, c) <= 20] or pts
        if near >= 0.8 * len(core) or v.tick - self.wp_tick > 20 * 25:
            self.wp += 1
            self.wp_tick = v.tick
            self.stuck_count = 0
            if self.wp >= len(wps):
                self._go(v, "SIEGE")
                self._siege(v, out, ids, c)
                return
            target = wps[self.wp]
        self._stuck_check(v, c)
        if self.stuck_count >= 3:
            self.wp = min(len(wps), self.wp + 1)
            self.stuck_count = 0
            self.wp_tick = v.tick
            return
        self._send(out, v, ids, "attack_move", to=target, min_gap=20, same_gap=125)

    def _stuck_check(self, v, c) -> None:
        if self.stuck_pos is None or dist(c, self.stuck_pos) > 1.5:
            self.stuck_pos, self.stuck_tick = c, v.tick
            return
        if v.tick - self.stuck_tick > 3 * 25:
            self.stuck_count += 1
            self.stuck_tick = v.tick
            for i in list(self.main):                 # force a re-send
                self.pol.ledger.unit_cmd.pop(i, None)

    def _enemies_near(self, v, c, r: float) -> list[dict]:
        rules = v.st.rules
        out = [e for e in v.enemies if is_combat(e["type"], rules) and e["type"] not in AIR
               and dist((e["x"], e["y"]), c) <= r]
        out += [b for b in v.enemy_blds if b["type"] in GROUND_DEFENSES and dist((b["x"], b["y"]), c) <= r]
        return out

    def _outmatched(self, v, ids, c, enemies, blds) -> bool:
        p = self.pol.p
        if self.mode == "allin" and v.remaining <= p.allin_s:
            return False
        ours = self.value([i for i in ids if dist((v.unit_by_id[i]["x"], v.unit_by_id[i]["y"]), c) <= 15], v)
        theirs = 0.0
        for e in enemies:
            w = 1.5 if e["type"] in GROUND_DEFENSES else 1.0
            theirs += w * self.cost(e["type"]) * max(0.1, e["hp"] / 100.0)
        if theirs <= 0:
            return False
        ratio = p.retreat_ratio if self.mode != "careful" else 1.0
        if self.state == "SIEGE" and len(blds) <= 2 and ours >= 0.5 * theirs:
            return False
        if ours < ratio * theirs:
            return True
        # heavy losses for few kills since this fight began
        if self.engage_value > 0 and ours < 0.5 * self.engage_value and \
                (v.kills - self.engage_kills) < 0.3 * (self.engage_value - ours) and ours < theirs:
            return True
        return False

    def _start_retreat(self, v, out, ids, why: str) -> None:
        if self.state != "RETREAT":
            self.pol.log_event(f"retreat at {v.game_s:.0f}s ({why})")
            if why == "outmatched":
                self.fail_mult = min(2.0, self.fail_mult * 1.25)
                self.last_fail_value = max(self.last_fail_value, self.wave_value)
        self._go(v, "RETREAT")
        rally = v.st.rally or v.base
        self._send(out, v, ids, "move", to=rally, urgent=True, min_gap=5, same_gap=125)

    def _retreat_step(self, v, out, ids, c) -> None:
        rally = v.st.rally or v.base
        if dist(c, rally) <= 7 or v.tick - self.state_tick > 30 * 25:
            self._go(v, "GATHER")
            return
        idle = [i for i in ids if v.unit_by_id[i]["idle"]]
        if idle:
            self._send(out, v, idle, "move", to=rally, min_gap=25, same_gap=125)

    def _nearly_won(self, v, c) -> bool:
        return self.state == "SIEGE" and 0 < len(v.enemy_blds) <= 2

    def _engage(self, v, out, ids, c, enemies) -> None:
        if v.tick - self.order_tick < 2 * 25:
            return
        towers = [e for e in enemies if e["type"] in GROUND_DEFENSES and dist((e["x"], e["y"]), c) <= 9]
        units = [e for e in enemies if e["type"] not in GROUND_DEFENSES]
        self.order_tick = v.tick
        if towers and len(units) <= 2:
            t = min(towers, key=lambda e: (e["hp"], dist((e["x"], e["y"]), c), e["id"]))
            self._send(out, v, ids, "attack", target=t["id"], min_gap=10, same_gap=100)
            return
        pc = _centroid([(e["x"], e["y"]) for e in (units or enemies)])
        self._send(out, v, ids, "attack_move", to=v.st.clamp(pc), min_gap=10, same_gap=60)

    def _target_priority(self, b: dict, has_tesla: bool) -> int:
        t = b["type"]
        if t in GROUND_DEFENSES:
            return 1
        if has_tesla and t in ("powr", "apwr"):
            return 2
        if t == "harv":
            return 3
        if t in PRODUCTION_TARGETS:
            return 4
        if t == "proc":
            return 5
        if t in ("powr", "apwr"):
            return 6
        if t == "fact":
            return 7
        if t in WALLS:
            return 9
        return 8

    def _siege(self, v, out, ids, c) -> None:
        blds = list(v.enemy_blds)
        harvs = [e for e in v.enemies if e["type"] == "harv" and dist((e["x"], e["y"]), c) <= 8]
        if not blds and not harvs:
            # nothing in sight: walk to the nearest building we remember, else hunt
            known = sorted(self.enemy_blds.values(), key=lambda b: dist((b["x"], b["y"]), c))
            if known:
                tgt = (known[0]["x"], known[0]["y"])
                self.nothing_since = None
                self._send(out, v, ids, "attack_move", to=v.st.clamp(tgt), min_gap=20, same_gap=150)
                return
            if self.nothing_since is None:
                self.nothing_since = v.tick
            if v.tick - self.nothing_since > 15 * 25:
                self._go(v, "HUNT")
                self.hunt_i, self.hunt_tick = 0, v.tick
                self._hunt(v, out, ids, c)
            else:
                self._send(out, v, ids, "attack_move", to=v.st.enemy_spawn, min_gap=25, same_gap=200)
            return
        self.nothing_since = None
        has_tesla = any(b["type"] == "tsla" for b in blds)
        cands = blds + harvs
        cands.sort(key=lambda b: (self._target_priority(b, has_tesla), dist((b["x"], b["y"]), c), b["hp"], b["id"]))
        focus_alive = self.focus is not None and any(b["id"] == self.focus for b in cands)
        if focus_alive and v.tick - self.focus_tick < 6 * 25:
            idle = [i for i in ids if v.unit_by_id[i]["idle"]]
            if idle:
                self._send(out, v, idle, "attack", target=self.focus, min_gap=10, same_gap=50)
            return
        first = cands[0]
        self.focus, self.focus_tick = first["id"], v.tick
        if len(ids) > 12 and len(cands) > 1:
            half = len(ids) // 2
            self._send(out, v, ids[:half], "attack", target=first["id"], min_gap=10, same_gap=100)
            self._send(out, v, ids[half:], "attack", target=cands[1]["id"], min_gap=10, same_gap=100)
        else:
            self._send(out, v, ids, "attack", target=first["id"], min_gap=10, same_gap=100)

    def _hunt(self, v, out, ids, c) -> None:
        st = v.st
        if v.enemy_blds or self.enemy_blds:
            self._go(v, "SIEGE")
            self._siege(v, out, ids, c)
            return
        pts = st.sweep or [st.enemy_spawn]
        target = pts[self.hunt_i % len(pts)]
        upts = self._pts(ids, v)
        near = sum(1 for p in upts if dist(p, target) <= 6)
        if near >= 0.7 * len(upts) or v.tick - self.hunt_tick > 35 * 25:
            self.hunt_i += 1
            self.hunt_tick = v.tick
            target = pts[self.hunt_i % len(pts)]
        if len(ids) >= 8:
            half = len(ids) // 2
            other = pts[(self.hunt_i + 1) % len(pts)]
            self._send(out, v, ids[:half], "attack_move", to=target, min_gap=25, same_gap=200)
            self._send(out, v, ids[half:], "attack_move", to=other, min_gap=25, same_gap=200)
        else:
            self._send(out, v, ids, "attack_move", to=target, min_gap=25, same_gap=200)

    # ---- reinforcements -------------------------------------------------------------------------------------------
    def _reinf_step(self, v, out) -> None:
        ids = sorted(i for i in self.reinf if i in v.unit_by_id)
        if not ids:
            return
        if self.state in ("GATHER", "RETREAT", "DEFEND"):
            self.main.update(ids)
            self.reinf.clear()
            return
        main_ids = [i for i in self.main if i in v.unit_by_id]
        mc = _centroid(self._pts(main_ids, v))
        if mc is None:
            self.main.update(ids)
            self.reinf.clear()
            return
        pts = self._pts(ids, v)
        rc = _centroid(pts)
        moving = [i for i in ids if not v.unit_by_id[i]["idle"]]
        val = self.value(ids, v)
        p = self.pol.p
        # merge those close to the attack group
        for i in ids:
            u = v.unit_by_id[i]
            if dist((u["x"], u["y"]), mc) <= 9:
                self.reinf.discard(i)
                self.main.add(i)
        ids = sorted(i for i in self.reinf if i in v.unit_by_id)
        if not ids:
            return
        if moving or len(ids) >= p.reinforce_units or val >= p.reinforce_value or self.mode == "allin":
            self._send(out, v, ids, "attack_move", to=v.st.clamp(mc), min_gap=50, same_gap=100)
        else:
            rally = v.st.rally or v.base
            far = [i for i in ids if v.unit_by_id[i]["idle"]
                   and dist((v.unit_by_id[i]["x"], v.unit_by_id[i]["y"]), rally) > 5]
            if far and rc is not None:
                self._send(out, v, far, "attack_move", to=rally, min_gap=50, same_gap=250)

    def summary(self) -> dict:
        return {"state": self.state, "waves": self.waves, "home": len(self.home), "main": len(self.main),
                "reinf": len(self.reinf), "rush": self.rush, "mode": self.mode}
