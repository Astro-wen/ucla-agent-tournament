"""The army (docs/redalert_design.md sections 8 and 9): squads kept as our own id sets (HOME guards the base, MAIN is
the attack group, REINF follows MAIN while it is away, one early scout), the enemy we have seen (decaying memory of
units, remembered buildings), threat and rush detection at home, MAIN's state machine (GATHER -> ADVANCE along the
ground path's waypoints -> ENGAGE / SIEGE with focus fire -> HUNT; RETREAT when outmatched; DEFEND when home is hit),
and the tiebreak endgame (hold, careful, all-in)."""
from __future__ import annotations

from . import counters
from .orders import Cand
from .world import AIR, GROUND_DEFENSES, NON_COMBAT, WALLS, dist, is_combat, is_infantry

PRODUCTION_TARGETS = frozenset({"weap", "tent", "barr", "hpad", "afld", "afld.ukraine", "spen", "syrd", "kenn"})
TOWER_TYPES = frozenset({"pbox", "hbox", "gun", "ftur", "tsla"})
# HUNT (no enemy building known): the sweep points, then a grid over the whole map (world.Static.coverage).
SEEN_FROM_S = 120          # a search point counts as looked at once a unit of ours came within HUNT_SEE cells of it
HUNT_SEE = 4               # (from 2:00, when even a base built away from the spawns stands)
HUNT_GOAL_TICKS = 45 * 25  # a group heading this long to one point gives it up (unreachable, stuck)
LEAD_TICKS = 90 * 25       # an enemy unit seen this recently (away from our base) is followed first
HARV_DANGER = 6            # harv_guard: an enemy combat unit this close to a harvester attacks it
HARV_HIT_TICKS = 3 * 25    # an under_attack event on a harvester counts this long
HARV_HELP_RANGE = 40       # free combat units this close go help it
HARV_FLEE = 7              # cells a harvester drives away from its attackers when its refinery is on their side
EARLY_ARMY_S = 120         # econ_first: signs of an early enemy army before this (scout killed in their base, 2+
EARLY_ARMY_VALUE = 700     # barracks, or this much of their combat units seen) -> a few more rifles and one tower
ARTILLERY = frozenset({"arty", "v2rl"})     # attack_gate: long-range units that outrange an attack walking in


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
        self._kills_seen = 0
        self._bk = 0
        self.waves = 0
        self.stuck_pos = None
        self.stuck_tick = 0
        self.stuck_count = 0
        self.engage_value = 0.0
        self.engage_kills = 0
        self.hunt_i = 0
        self.hunt_tick = 0
        self.hunt_seen: set[tuple[int, int]] = set()
        self.hunt_goal: dict[int, tuple] = {}
        self._seen_tick = -10 ** 9
        self._search_cache: list | None = None
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
        self.harv_hit: dict[int, int] = {}      # harvester id -> tick of the last under_attack event on it
        self.harv_pulls = 0
        self.harv_helps = 0
        self.intel_tick = -10 ** 9      # last time we saw enemy combat units or looked at the enemy base
        self.rescout_tick = -10 ** 9
        self.rescouts = 0
        self.early_army = False         # econ_first: an early enemy army (EARLY_ARMY_S)
        self.two_barracks = False       # econ_first: two enemy barracks seen before 2:30 (a rush tell)
        self.mode = "normal"            # endgame: normal / hold / careful / allin
        self.tag = "opening"
        self.v = None
        # v4.7 (docs/redalert_design.md section 18)
        self.base_seen = False          # attack_gate: an enemy building was seen at least once
        self._main_types: dict[int, str] = {}   # trade_retreat: the attack group as of the last view (id -> type)
        self._trade: list[tuple[int, float, float]] = []    # (tick, our losses, our kills) in the current wave
        self._kills_prev: float | None = None
        self._trade_last = (0.0, 0.0)
        self._trade_cum = [0.0, 0.0]    # trade_retreat: our losses / kills since the wave started
        self.trade_retreats = 0
        self.recover_tick = -10 ** 9    # recover: the last failed wave
        self.comp_kept = 0              # attack_comp: rifles kept home at wave launches

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
            for ev in v.events:                       # where it died (the unit_lost event), when the event says
                if ev.get("kind") == "unit_lost" and ev.get("id") == self.scout and \
                        isinstance(ev.get("x"), int) and isinstance(ev.get("y"), int) and \
                        not isinstance(ev.get("x"), bool) and not isinstance(ev.get("y"), bool):
                    last = (ev["x"], ev["y"])
            if v.game_s < 150:
                self.scout_lost_early = True          # an army already stands in their base: more rifles early
                self.pol.log_event(f"scout killed early at {v.game_s:.0f}s near {last}")
            if v.game_s < EARLY_ARMY_S and last is not None and v.st.enemy_spawn is not None and \
                    dist(last, v.st.enemy_spawn) <= 12 and not self.early_army:
                # killed inside their base this early: an army stands at home (a rush may be coming); econ_first
                # answers with a few more rifles and one tower, not a stalled opening
                self.early_army = True
                self.pol.log_event(f"early enemy army: scout killed in their base at {v.game_s:.0f}s")
            if self.scout_phase in ("out", "look", "back") and v.game_s < 240 and last is not None and \
                    dist(last, v.st.enemy_spawn) > 12:
                # econ_first: only a scout killed on OUR half of the map means an army is on its way (the real loss
                # cb4bfc36: a picket 18 cells in front of the enemy base killed it and we built 24 rifles, 2 towers
                # and the war factory at 282 s)
                ours_half = v.base is not None and dist(last, v.base) < dist(last, v.st.enemy_spawn)
                if not self.pol.p.econ_first or ours_half:
                    self.pol.log_event(f"scout lost on the way at {v.game_s:.0f}s near {last}: expecting a rush")
                    self.rush, self.rush_tick = True, v.tick
                else:
                    self.pol.log_event(f"scout lost at {v.game_s:.0f}s near {last}, on their half: an enemy picket, "
                                       f"the opening goes on")
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
            elif self.pol.p.attack_comp and t == "e1":
                self.home.add(uid)                    # attack_comp: rifles made during a wave guard the base
            else:
                self.reinf.add(uid)
        # infantry over the home quota (after a rush) joins the attack group
        if not self.rush and len(self.home) > quota + 2:
            extra = sorted(self.home)[quota:]
            if self.pol.p.attack_comp and self.state not in ("GATHER", "RETREAT", "DEFEND"):
                # attack_comp: during a wave the rifles stay home (rockets / grenadiers still reinforce it)
                extra = [i for i in extra if v.unit_by_id[i]["type"] != "e1"]
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
        if v.tick - self.intel_tick > 25:
            # a look at their home (spawn, or a production building we know of): the army standing there, we see
            # (meeting part of their army on the way is no such look: v4.5 tuning seed 501032)
            homes = [v.st.enemy_spawn] if v.st.enemy_spawn is not None else []
            homes += [(b["x"], b["y"]) for b in self.enemy_blds.values() if b["type"] in PRODUCTION_TARGETS]
            if any(abs(u["x"] - h[0]) <= 7 and abs(u["y"] - h[1]) <= 7 for u in v.units for h in homes):
                self.intel_tick = v.tick
        # the largest enemy army seen at once, fading slowly (half-life 6 minutes): what an attack must beat even
        # after that army went back into the fog
        dt = max(0, v.tick - self._peak_tick)
        self._peak_tick = v.tick
        peak = self.enemy_peak * 0.5 ** (dt / (360 * 25.0))
        if self.pol.p.econ_first and v.kills > self._kills_seen:
            peak = max(0.0, peak - (v.kills - self._kills_seen))   # what we killed of it is gone
        self._kills_seen = max(self._kills_seen, v.kills)
        self.enemy_peak = max(vis_val, peak)
        for b in v.enemy_blds:
            self.enemy_blds[b["id"]] = {"type": b["type"], "tick": v.tick, "x": b["x"], "y": b["y"], "hp": b["hp"]}
            if b["type"] not in WALLS:
                self.base_seen = True
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
        if v.game_s < 150 and not self.two_barracks:
            barracks = sum(1 for b in self.enemy_blds.values() if b["type"] in ("tent", "barr"))
            if barracks >= 2:
                # two barracks this early: an infantry rush is likely (v4.5 tuning seed 506026: 35 rifles at 178 s)
                self.two_barracks = self.early_army = True
                self.pol.log_event(f"two enemy barracks at {v.game_s:.0f}s: infantry rush likely")
        if v.game_s < EARLY_ARMY_S and not self.early_army and v.st.enemy_spawn is not None:
            # counted inside their base only: a picket out on the road is not an army massing for a rush (v4.5
            # measurement seed block 99: the nash script's 4-unit picket set this off and the towers it brought cost
            # the games a v4.5 without them won)
            es = v.st.enemy_spawn
            seen = sum(self.cost(e["type"]) for e in self.enemy_units.values()
                       if is_combat(e["type"], rules) and e["type"] not in AIR and dist((e["x"], e["y"]), es) <= 12)
            if seen >= EARLY_ARMY_VALUE:
                self.early_army = True
                self.pol.log_event(f"early enemy army at {v.game_s:.0f}s (seen {seen} in their base)")

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
        return max(self.enemy_army(v, 180.0), self.enemy_peak, self.prior(v)) + 1.5 * self.enemy_towers()

    def blind(self, v) -> bool:
        p = self.pol.p
        return bool(p.econ_first) and v.game_s < p.prior_until_s and (v.tick - self.intel_tick) / 25.0 > p.blind_s

    def prior(self, v) -> float:
        """econ_first, blind (no enemy army seen and no look at their base for blind_s): the army a competent
        opponent has by now (an unseen army is not a weak one: v4.4 attacked 'enemy strength 0' at 240 s and lost
        its base to the counterattack)."""
        if not self.blind(v):
            return 0.0
        p = self.pol.p
        # (tried: minus what we killed in the last 3 minutes; tuning seed 501 116 -> 107 / 120 and 507 47 -> 34 / 60:
        # a skirmish won early made the first attack go too soon)
        # attack_gate: the cap grows on (real loss f5ce0e92: blind at 8:44 the prior said 9000, the army we met mid-map
        # was ~12000: 24 rifles, 10 rockets, 5 heavy tanks)
        cap = max(p.prior_cap, p.prior_late_cap) if p.attack_gate else p.prior_cap
        return min(float(cap), max(0.0, p.prior_rate * (v.game_s - 90.0)))

    def enemy_towers(self) -> float:
        return sum(self.cost(b["type"]) for b in self.enemy_blds.values() if b["type"] in GROUND_DEFENSES)

    # ---- counter_value: forces for agent/redalert/counters.py ------------------------------------------------------
    def own_force(self, ids, v) -> list:
        out = []
        for i in ids:
            u = v.unit_by_id.get(i)
            if u is not None:
                out.append((u["type"], 1.0, max(0.05, u["hp"] / 100.0)))
        return out

    def enemy_force(self, v) -> list:
        """The enemy army an attack has to beat, by type: recent sightings (full weight 90 s, fading out by 180 s)
        scaled up to enemy_strength's army part (the biggest army seen, the blind prior), or a generic mix when
        nothing was seen; plus the towers we know of."""
        rules = v.st.rules
        vals: dict[str, float] = {}
        for e in self.enemy_units.values():
            t = e["type"]
            if not is_combat(t, rules) or t in AIR:
                continue
            age = (v.tick - e["tick"]) / 25.0
            w = 1.0 if age <= 90 else max(0.0, (180 - age) / 90.0)
            if w > 0:
                vals[t] = vals.get(t, 0.0) + self.cost(t) * w
        total = sum(vals.values())
        target = max(total, self.enemy_peak, self.prior(v))
        if target > total:
            # the part we have not seen is a typical army, not more of what we saw (v4.5 tuning seed 507056: a
            # rifle / rocket picket scaled up to the prior looked easy prey for rifles; six tanks stood behind it)
            tank = "3tnk" if v.enemy_faction in ("russia", "ukraine", "soviet") else "2tnk"
            rest = target - total
            for t, share in (("e1", 0.3), ("e3", 0.15), (tank, 0.55)):
                vals[t] = vals.get(t, 0.0) + share * rest
        # attack_gate: towers and artillery shoot first (range, a fixed position we walk into: real loss d527c9fa,
        # gun + pbox + 2 artillery of range 12 at home)
        w_def = float(self.pol.p.gate_def_w) if self.pol.p.attack_gate else 1.0
        force = [(t, x / max(1, self.cost(t)) * (w_def if t in ARTILLERY else 1.0), 1.0) for t, x in vals.items()]
        force += [(b["type"], w_def, max(0.1, b["hp"] / 100.0)) for b in self.enemy_blds.values()
                  if b["type"] in GROUND_DEFENSES]
        return force

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
        if self.pol.p.econ_first and not self.rush:
            return False                               # a dead scout alone is no reason to stall the war factory
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
        floor = 600
        p = self.pol.p
        if p.econ_first and self.early_army and v.game_s < 420:
            want = max(want, int(p.early_army_towers))   # an early enemy army: towers against a rush
        if p.econ_first and not self.pol.first.get("weap"):
            floor = max(floor, p.real_threat)            # a light harass before the war factory: no tower money
        elif p.econ_first and v.game_s >= p.safety_tower_s and not (p.harv_first and self.pol.harv.serialized(v)):
            # economy first leaves a thinner army at home around 4:00, when a balanced opponent's first wave comes
            # (v4.5 tuning seed 501062: 29 infantry at 253 s razed a base with 20 rifles and 3 tanks): towers at the
            # front once the war factory stands
            want = max(want, 1 if v.game_s < p.safety_tower_s + 60 else int(p.safety_towers))
        if self.threatened and v.game_s < 360 and self.threat_value > floor:
            want = max(want, 1 if self.threat_value < 1500 else 2)
        if p.recover_towers > 0 and self.recovering(v) and \
                self.enemy_strength(v) > self.value(self.main | self.home, v):
            # recover: a wave failed and their army is the bigger one: towers hold the counter-push (real losses
            # f5ce0e92 / d527c9fa: the counter-push came 40 s / 5 minutes after the failed wave)
            want = max(want, int(p.recover_towers))
        return min(4, want)

    def recovering(self, v) -> bool:
        """recover: within recover_s of a failed wave (retreat after being outmatched / a losing trade) and no new
        wave since."""
        p = self.pol.p
        return bool(p.recover) and v.tick - self.recover_tick < p.recover_s * 25 and self.state not in (
            "ADVANCE", "ENGAGE", "SIEGE", "HUNT")

    def real_attack(self, v) -> bool:
        """A real attack on the base (an early rush, or the threat at home worth real_threat or more), as opposed to
        a scout or a light harass that the home guard handles."""
        return self.rush or (self.threatened and self.threat_value >= self.pol.p.real_threat)

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
            hid = ev.get("id")
            if ev.get("kind") == "under_attack" and str(ev.get("type") or "") == "harv" and isinstance(hid, int) \
                    and not isinstance(hid, bool):
                self.harv_hit[hid] = v.tick
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
        self._mark_seen(v)
        self._defend(v, out, allow)
        self._harv_guard(v, out)
        if allow:
            self._rescout(v)
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

    def _harv_guard(self, v, out) -> None:
        """harv_guard: a harvester under attack (an enemy combat unit within HARV_DANGER cells, or an under_attack
        event on it in the last 3 s) drives to its nearest refinery, or away from the attackers when that refinery
        is on their side; and while the base itself is not under attack (_defend handles that), the nearest free
        combat units (home guard, the attack group waiting at home) go there with about 1.5x the attackers' value."""
        if not self.pol.p.harv_guard:
            return
        alive = v.unit_by_id
        for k in [k for k in self.harv_hit if k not in alive]:
            del self.harv_hit[k]
        harvs = [u for u in v.units if u["type"] == "harv"]
        if not harvs:
            return
        rules = v.st.rules
        enemies = [e for e in v.enemies if is_combat(e["type"], rules) and e["type"] not in AIR]
        docks = [(b["x"] + 1, b["y"] + 1) for b in v.buildings if b["type"] == "proc"] or \
            ([v.base] if v.base is not None else [])
        taken: set[int] = set()
        for h in harvs:
            hp = (h["x"], h["y"])
            near = [e for e in enemies if dist((e["x"], e["y"]), hp) <= HARV_DANGER]
            hit = v.tick - self.harv_hit.get(h["id"], -10 ** 9) <= HARV_HIT_TICKS
            if not near and not hit:
                continue
            ec = _centroid([(e["x"], e["y"]) for e in near]) if near else None
            if docks and (near or h["hp"] < 60):
                dock = min(docks, key=lambda p: (dist(p, hp), p))
                to = dock
                if ec is not None and dist(dock, ec) < dist(hp, ec) + 1:
                    # the refinery is on the attackers' side: drive away from them instead
                    d = dist(hp, ec) or 1.0
                    to = v.st.clamp((hp[0] + (hp[0] - ec[0]) * HARV_FLEE / d, hp[1] + (hp[1] - ec[1]) * HARV_FLEE / d))
                if dist(to, hp) > 3 and self._send(out, v, [h["id"]], "move", to=to, urgent=True, min_gap=25,
                                                   same_gap=100, tag="harv"):
                    self.harv_pulls += 1
            if not near or self.threatened:
                continue
            want = 1.5 * sum(self.cost(e["type"]) * max(0.1, e["hp"] / 100.0) for e in near)
            pool = [i for i in self.home if i not in taken]
            if self.state == "GATHER":
                pool += [i for i in self.main if i not in taken]
            cand = []
            for i in pool:
                u = alive.get(i)
                if u is not None and dist((u["x"], u["y"]), hp) <= HARV_HELP_RANGE:
                    cand.append((dist((u["x"], u["y"]), hp), i))
            cand.sort()
            pick, got = [], 0.0
            for _d, i in cand:
                if got >= want and len(pick) >= 2:
                    break
                pick.append(i)
                got += self.value([i], v)
            if pick:
                taken.update(pick)
                if self._send(out, v, pick, "attack_move", to=v.st.clamp(ec), urgent=True, min_gap=10, same_gap=50,
                              tag="harv"):
                    self.harv_helps += 1

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

    def _rescout(self, v) -> None:
        """econ_first, blind: one cheap unit goes to look at the enemy base again (the picket, else an e1 of the
        home guard or of the attack group waiting at home, else a fast vehicle), at most once every 90 s."""
        if not self.blind(v) or v.game_s < 150 or self.threatened or self.mode != "normal":
            return
        if v.tick - self.rescout_tick < 90 * 25:
            return
        if self.scout is not None:
            if self.scout_phase == "picket":
                self.scout_phase, self.scout_tick = "out", v.tick
                self.rescout_tick = v.tick
                self.rescouts += 1
            return
        if self.state not in ("GATHER", "DEFEND"):
            return                                    # the attack group is out there: it sees for itself
        pick = None
        for kinds in (("e1",), ("e2", "e3", "dog"), ("jeep", "ftrk", "1tnk", "apc")):
            for i in sorted(self.home) + sorted(self.main):
                u = v.unit_by_id.get(i)
                if u is not None and u["type"] in kinds:
                    pick = i
                    break
            if pick is not None:
                break
        if pick is None:
            return
        self.home.discard(pick)
        self.main.discard(pick)
        self.scout, self.scout_phase, self.scout_tick = pick, "out", v.tick
        self.rescout_tick = v.tick
        self.rescouts += 1
        self.pol.log_event(f"blind at {v.game_s:.0f}s: re-scouting with {v.unit_by_id[pick]['type']} {pick}")

    # ---- MAIN ------------------------------------------------------------------------------------------------------
    def _go(self, v, state: str) -> None:
        if state != self.state:
            self.state, self.state_tick = state, v.tick
            self.order_tick = -10 ** 9
            self.focus = None
            self.stuck_pos, self.stuck_count = None, 0
            self.nothing_since = None

    def _should_attack(self, v, main_val: float, n: int, n_veh: int, go=None) -> bool:
        p = self.pol.p
        if n == 0 or self.mode in ("hold", "careful"):
            return False
        if self.mode == "allin":
            return True
        strength = self.enemy_strength(v)
        eq = main_val                                  # our attack group's worth against what it has to beat
        if p.counter_value and strength > 0:
            theirs = self.enemy_force(v)
            if theirs:
                ids = go if go is not None else sorted(i for i in self.main if i in v.unit_by_id)
                r = counters.ratio(self.own_force(ids, v), theirs, v.st.rules)
                eq = min(r, 100.0) * strength
        ratio = p.attack_ratio
        if p.attack_gate and not self.base_seen:
            # attack_gate: we never saw one of their buildings, so neither their towers nor the army at home (real
            # loss f5ce0e92: blind, 0 buildings seen all match, 20k went and traded 3.6k for 9.4k)
            ratio = max(ratio, p.gate_unseen_ratio)
        if strength > 0 and eq < ratio * strength:
            return False
        mult = p.recover_mult if self.recovering(v) else 1.3
        if self.last_fail_value > 0 and main_val < mult * self.last_fail_value:
            return False                               # the last wave had to retreat: come back bigger
        if n >= p.attack_min_units and main_val >= p.attack_min_value * self.fail_mult:
            return True
        if v.game_s >= p.attack_late_s and n_veh >= p.attack_late_units and main_val >= 2000 * self.fail_mult:
            return True
        if self.pol.prod.stranded(v) and n >= 15 and main_val >= 1500 * self.fail_mult:
            return True                                # no base to come back to: the army is all we can still use
        quiet = (v.tick - self.last_enemy_combat) / 25.0 >= p.quiet_s
        if v.game_s >= p.opportunistic_s and n_veh >= 3 and quiet and self.fail_mult <= 1.0:
            return True
        if p.econ_first and main_val < p.attack_3x_min:
            # a look at their home that shows next to nothing at 4:00 means their army is elsewhere, maybe on its
            # way (v4.5 measurement seed 62043: 14 rifles went for "3x the 100 we saw" and met 34 rifles)
            return False
        return strength > 0 and n >= 3 and eq >= 3.0 * strength and v.game_s >= 180

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
        if self.pol.p.trade_retreat:
            self._trade_track(v, ids)
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
        if self.pol.p.trade_retreat and self._trade_losing(v, c):
            self._start_retreat(v, out, ids, "losing trade")
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

    def _launch_split(self, v, ids, n_veh):
        """attack_comp: the wave takes the vehicles, the rocket / grenade infantry and at most comp_rifles_per_veh
        rifles per vehicle (a screen); the other rifles stay home (10% damage against heavy armour, and crushed and
        splashed in the engine: real loss d527c9fa, 21 rifles went, 5 came back). No vehicle: no split."""
        p = self.pol.p
        if not p.attack_comp or n_veh <= 0 or self.mode == "allin":
            return ids, []
        if n_veh < p.comp_min_veh or v.game_s < p.comp_from_s:
            return ids, []                             # an early, small wave needs its rifles
        rifles = [i for i in ids if v.unit_by_id[i]["type"] == "e1"]
        keep = int(p.comp_rifles_per_veh * n_veh)
        if len(rifles) <= keep:
            return ids, []
        # the healthiest rifles go with the wave
        rifles.sort(key=lambda i: (-v.unit_by_id[i]["hp"], i))
        stay = set(rifles[keep:])
        return [i for i in ids if i not in stay], sorted(stay)

    def _gather(self, v, out, ids, main_val, n_veh) -> None:
        st = v.st
        rally = st.rally or v.base
        go, stay = self._launch_split(v, ids, n_veh)
        go_val = self.value(go, v) if stay else main_val
        if self._should_attack(v, go_val, len(go), n_veh, go=go if stay else None):
            if stay:
                for i in stay:
                    self.main.discard(i)
                    self.home.add(i)
                self.comp_kept += len(stay)
                ids, main_val = go, go_val
            self.waves += 1
            self.wave_value = main_val
            self.recover_tick = -10 ** 9
            self._trade = []
            self._trade_cum = [0.0, 0.0]
            self.pol.log_event(f"attack wave {self.waves} at {v.game_s:.0f}s: {len(ids)} units, value {main_val:.0f}, "
                               f"enemy strength {self.enemy_strength(v):.0f} (towers {self.enemy_towers():.0f})"
                               + (f", {len(stay)} rifles stay home" if stay else ""))
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
        near = [i for i in ids if dist((v.unit_by_id[i]["x"], v.unit_by_id[i]["y"]), c) <= 15]
        ours = self.value(near, v)
        theirs = 0.0
        for e in enemies:
            w = 1.5 if e["type"] in GROUND_DEFENSES else 1.0
            theirs += w * self.cost(e["type"]) * max(0.1, e["hp"] / 100.0)
        if theirs <= 0:
            return False
        ratio = p.retreat_ratio if self.mode != "careful" else 1.0
        if p.counter_value:
            # by unit-type counters (towers' hit points and damage are in the numbers: no 1.5 weight)
            r = counters.ratio(self.own_force(near, v), [(e["type"], 1.0, max(0.1, e["hp"] / 100.0)) for e in enemies],
                               v.st.rules)
            eq_ours, eq_theirs = min(r, 100.0), 1.0
        else:
            eq_ours, eq_theirs = ours, theirs
        if self.state == "SIEGE" and len(blds) <= 2 and eq_ours >= 0.5 * eq_theirs:
            return False
        if eq_ours < ratio * eq_theirs:
            return True
        # heavy losses for few kills since this fight began
        if self.engage_value > 0 and ours < 0.5 * self.engage_value and \
                (v.kills - self.engage_kills) < 0.3 * (self.engage_value - ours) and eq_ours < eq_theirs:
            return True
        return False

    def _trade_track(self, v, ids) -> None:
        """trade_retreat: per view, the value of the attack group's units that died since the last view (full cost,
        as the platform counts deaths) and the kills since then, kept for the current wave."""
        rules = v.st.rules
        lost = sum(self.cost(t) for i, t in self._main_types.items() if i not in v.unit_by_id)
        kills = max(0, v.kills - self._kills_prev) if self._kills_prev is not None else 0
        self._kills_prev = v.kills
        self._main_types = {i: v.unit_by_id[i]["type"] for i in ids if is_combat(v.unit_by_id[i]["type"], rules)}
        if self.state in ("ADVANCE", "ENGAGE", "SIEGE", "HUNT"):
            if lost or kills:
                self._trade.append((v.tick, float(lost), float(kills)))
                self._trade_cum[0] += lost
                self._trade_cum[1] += kills
            win = int(self.pol.p.trade_window_s * 25)
            while self._trade and v.tick - self._trade[0][0] > win:
                self._trade.pop(0)
        else:
            self._trade = []
            self._trade_cum = [0.0, 0.0]

    def _trade_losing(self, v, c) -> bool:
        """trade_retreat: over the last trade_window_s our attack group lost trade_loss_ratio x what we killed, and at
        least trade_min_loss and trade_min_frac of the wave (real loss d527c9fa: 525-536 s we lost 5150 and killed
        3100, kept fighting, and lost 12k for 13.5k into towers and artillery; f5ce0e92: 5550 for 1800 in 10 s).
        Not in the all-in minute, not when nearly won, not when there is no base to fall back to."""
        p = self.pol.p
        if not self._trade or (self.mode == "allin" and v.remaining <= p.allin_s) or self._nearly_won(v, c):
            return False
        if self.pol.prod.stranded(v):
            return False
        lost = sum(t[1] for t in self._trade)
        kills = sum(t[2] for t in self._trade)
        self._trade_last = (lost, kills)
        if p.trade_cum_ratio > 0 and self._trade_cum[0] < p.trade_cum_ratio * self._trade_cum[1]:
            # the wave as a whole is still trading well: a bad dozen seconds after a good exchange is no reason to
            # turn back (real wins fffc7ced: 340-371 s killed 5400 for ~3000, then 369-381 s lost 2600 for 400;
            # 971ec5dc at 920 s)
            return False
        return lost >= p.trade_min_loss and lost >= p.trade_min_frac * max(1.0, self.wave_value) and \
            lost >= p.trade_loss_ratio * kills

    def _start_retreat(self, v, out, ids, why: str) -> None:
        if self.state != "RETREAT":
            extra = ""
            if why == "losing trade":
                extra = f": lost {self._trade_last[0]:.0f}, killed {self._trade_last[1]:.0f}, wave {self.wave_value:.0f}"
            self.pol.log_event(f"retreat at {v.game_s:.0f}s ({why}{extra})")
            if why in ("outmatched", "losing trade"):
                self.fail_mult = min(2.0, self.fail_mult * 1.25)
                self.last_fail_value = max(self.last_fail_value, self.wave_value)
                self.recover_tick = v.tick
            if why == "losing trade":
                self.trade_retreats += 1
                self._trade = []
                self._trade_cum = [0.0, 0.0]
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
        """No enemy building known: follow the freshest enemy sighting (its units lead back to its base), else look
        at the search points nobody of ours has been near yet, nearest first: the sweep points (around its spawn,
        distant ore, quadrants), then the map-wide grid, edges and corners included. 8+ units split into 2 groups,
        18+ into 3, each with its own point."""
        if v.enemy_blds or self.enemy_blds:
            self._go(v, "SIEGE")
            self._siege(v, out, ids, c)
            return
        k = 1 if len(ids) < 8 else (2 if len(ids) < 18 else 3)
        size = (len(ids) + k - 1) // k
        claimed: list = []
        lead = self._lead(v)
        for gi in range(k):
            g = ids[gi * size:(gi + 1) * size]
            if not g:
                continue
            gc = _centroid(self._pts(g, v)) or c
            target = lead if (gi == 0 and lead is not None) else self._search_target(v, gi, gc, claimed)
            claimed.append(target)
            self._send(out, v, g, "attack_move", to=target, min_gap=25, same_gap=200)

    def _search_points(self, v) -> list[tuple[int, int]]:
        if self._search_cache is not None:
            return self._search_cache
        st = v.st
        sweep = list(st.sweep) or [st.enemy_spawn]
        try:
            grid = st.coverage()
        except Exception as e:                            # noqa: BLE001 - the sweep points alone still work
            self.pol.note_exception("coverage", e)
            return sweep
        self._search_cache = sweep + [p for p in grid if all(dist(p, s) > 4 for s in sweep)]
        return self._search_cache

    def _mark_seen(self, v) -> None:
        """Once a second (from SEEN_FROM_S): search points within HUNT_SEE cells of one of our units (or a little
        more of one of our buildings) have been looked at."""
        if v.tick - self._seen_tick < 25 or v.game_s < SEEN_FROM_S or not v.st.ready:
            return
        self._seen_tick = v.tick
        todo = [p for p in self._search_points(v) if p not in self.hunt_seen]
        if not todo:
            return
        units = [(u["x"], u["y"]) for u in v.units]
        blds = [(b["x"], b["y"]) for b in v.buildings]
        r_u, r_b = HUNT_SEE * HUNT_SEE, (HUNT_SEE + 3) * (HUNT_SEE + 3)
        for p in todo:
            if any((p[0] - x) ** 2 + (p[1] - y) ** 2 <= r_u for x, y in units) or \
                    any((p[0] - x) ** 2 + (p[1] - y) ** 2 <= r_b for x, y in blds):
                self.hunt_seen.add(p)

    def _search_target(self, v, gi: int, gc, claimed: list) -> tuple[int, int]:
        goal = self.hunt_goal.get(gi)
        if goal is not None:
            t, since = goal
            if t not in self.hunt_seen and t not in claimed and v.tick - since <= HUNT_GOAL_TICKS:
                return t
            if t not in self.hunt_seen and v.tick - since > HUNT_GOAL_TICKS:
                self.hunt_seen.add(t)                     # could not get there: count it as looked at
        pts = self._search_points(v)
        todo = [p for p in pts if p not in self.hunt_seen and p not in claimed]
        if not todo:
            self.hunt_seen.clear()                        # looked everywhere and found nothing: start over
            todo = [p for p in pts if p not in claimed] or pts
        sweep = set(v.st.sweep)
        first = [p for p in todo if p in sweep] or todo
        t = min(first, key=lambda p: (dist(p, gc), p))
        self.hunt_goal[gi] = (t, v.tick)
        return t

    def _lead(self, v):
        """The freshest enemy unit seen in the last LEAD_TICKS away from our base and from our attack group (a
        harvester first: it drives back to its refinery), or None."""
        base = v.base
        mine = self._pts(sorted(self.main), v)
        best = None
        for e in self.enemy_units.values():
            if v.tick - e["tick"] > LEAD_TICKS:
                continue
            p = (e["x"], e["y"])
            if (base is not None and dist(p, base) < 20) or any(dist(p, m) <= 3 for m in mine):
                continue
            key = (e["type"] == "harv", e["tick"], -e["x"], -e["y"])
            if best is None or key > best[0]:
                best = (key, p)
        return v.st.clamp(best[1]) if best is not None else None

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
                "reinf": len(self.reinf), "rush": self.rush, "mode": self.mode, "hunt_seen": len(self.hunt_seen),
                "harv_pulls": self.harv_pulls, "harv_helps": self.harv_helps, "rescouts": self.rescouts,
                "early_army": self.early_army, "base_seen": self.base_seen, "trade_retreats": self.trade_retreats,
                "comp_kept": self.comp_kept}
