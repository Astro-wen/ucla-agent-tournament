"""The Infantry and Vehicle queues (docs/redalert_design.md section 7): harvesters first, a new MCV when the yard is
lost, then the main tank (first buildable of 3tnk / 2tnk / 1tnk), and infantry by need: a few early, many during an
early rush, a mix of rifle and rocket infantry later. Preference lists, never faction-hard-coded availability."""
from __future__ import annotations

from .orders import Cand
from .world import is_infantry

MAIN_TANKS = ("3tnk", "2tnk", "1tnk")
LIGHT_TANK = "1tnk"
AIR_TYPES = frozenset({"heli", "mh60", "yak", "mig", "hind"})


class Production:
    def __init__(self, pol):
        self.pol = pol

    def _pending(self, v, queue: str) -> bool:
        led, rules = self.pol.ledger, v.st.rules
        return any(k[0] == "train" and rules.queue(k[1]) == queue and led.is_pending(k) for k in list(led.pending))

    def step(self, v, out: list[Cand]) -> None:
        self._vehicles(v, out)
        self._infantry(v, out)

    # ---- infantry ----
    def _infantry(self, v, out) -> None:
        if not (v.have.get("tent") or v.have.get("barr")):
            return
        items = v.queue_items("Infantry")
        if len(items) >= 2 or self._pending(v, "Infantry"):
            return
        rules = v.st.rules
        army = self.pol.army
        p = self.pol.p
        alive = [u for u in v.units if is_infantry(u["type"], rules) and u["type"] not in ("medi", "mech", "e6", "spy")]
        queued = sum(1 for it in items)
        n = len(alive) + queued
        e1 = sum(1 for u in alive if u["type"] == "e1") + sum(1 for it in items if it["item"] == "e1")
        e3 = sum(1 for u in alive if u["type"] == "e3") + sum(1 for it in items if it["item"] == "e3")
        soviet = v.side == "soviet"
        threatened = army.threatened or army.rush
        late = v.remaining < 75
        inf_heavy = army.infantry_heavy(v)
        e_inf = army.enemy_inf_count(v, 90)
        idle_money = False                             # rifles only soak up money nothing else is using
        if late:
            target = n + 10
        elif army.rush:
            target = max(int(p.rush_infantry), min(40, int(1.1 * e_inf) + 4))
        elif inf_heavy:
            target = max(int(p.max_infantry), min(30, int(0.9 * e_inf) + 6))
        elif army.threatened:
            target = max(int(p.home_infantry) + 4, 10)
        else:
            target = int(p.max_infantry)
            if army.scout_lost_early and v.game_s < 240:
                target = max(target, int(p.early_infantry))
            idle_money = n >= (5 if soviet else 4) and not army.defense_first(v)
        if n >= target:
            return
        if not threatened and not late and v.have.get("weap") and v.game_s >= 240:
            # once tanks can be built, infantry stays a share of the army (more of it against an infantry army)
            inf_val = sum(rules.cost(u["type"]) for u in alive)
            veh_val = sum(rules.cost(u["type"]) for u in v.units if u["type"] in ("1tnk", "2tnk", "3tnk", "4tnk",
                                                                                     "jeep", "apc", "ftrk"))
            cap = 0.65 if inf_heavy else 0.4
            if inf_val > cap * (inf_val + veh_val) + 400:
                return
        veh_share = army.enemy_vehicle_share()
        if late:
            item = "e1"
        elif n == 0 or (not threatened and e1 < 3) or (veh_share < 0.2 and v.game_s < 240):
            item = "e1"
        else:
            want_e3 = 0.6 if veh_share > 0.5 else (0.35 if veh_share > 0.15 else 0.2)
            if inf_heavy or (army.rush and veh_share < 0.2):
                want_e3 = 0.1 if veh_share < 0.2 else 0.2
            item = "e3" if e3 < want_e3 * (e1 + e3 + 1) else "e1"
            if item == "e1" and soviet and "e2" in v.avail and army.enemy_infantry_share() > 0.6 and n % 3 == 2:
                item = "e2"
        if item not in v.avail:
            item = next((t for t in ("e1", "e3", "e2") if t in v.avail), None)
            if item is None:
                return
        cost = rules.cost(item)
        spend = v.money - (0 if threatened else self.pol.econ.reserve)
        if idle_money:
            spend -= 300 if not v.have.get("weap") else 600
        if late:
            if rules.build_time(item) > v.remaining - 5 or spend < cost:
                return
        elif spend < cost:
            return
        count = 1
        if threatened or late or inf_heavy:
            count = max(1, min(3, target - n, spend // max(1, cost)))
        out.append(Cand({"cmd": "train", "item": item, "count": int(count)}, key=("train", item), urgent=threatened,
                        base=v.in_prod.get(item, 0)))

    # ---- vehicles ----
    def _vehicles(self, v, out) -> None:
        if not v.have.get("weap"):
            return
        items = v.queue_items("Vehicle")
        if len(items) >= 2 or self._pending(v, "Vehicle"):
            return
        item = self._vehicle_choice(v)
        if item is None:
            return
        rules = v.st.rules
        cost = rules.cost(item)
        army = self.pol.army
        threatened = army.threatened
        reserve = 0 if (threatened or item in ("harv", "mcv")) else self.pol.econ.reserve
        if item not in ("harv", "mcv") and (army.rush or (army.infantry_heavy(v) and
                                                          army.enemy_army(v) > 0.8 * v.army_value)):
            reserve += 400                             # rifles (and towers) first against an infantry army
        spend = v.money - reserve
        if items and spend < 2 * cost:
            return                                     # a second item only when money is plentiful
        if spend < cost and not (item in ("harv", "mcv") and spend >= 0.5 * cost):
            return
        if v.remaining < 75 and rules.build_time(item) > v.remaining - 5:
            return
        out.append(Cand({"cmd": "train", "item": item, "count": 1}, key=("train", item), urgent=item in ("mcv",),
                        base=v.in_prod.get(item, 0)))

    def _vehicle_choice(self, v) -> str | None:
        army = self.pol.army
        p = self.pol.p
        if v.fact is None and "mcv" in v.avail and v.count("mcv") == 0:
            return "mcv"
        procs = v.have.get("proc", 0)
        harv = v.count("harv")
        target_harv = min(int(p.max_harvesters), 2 * procs, procs + int(v.game_s // 150))
        if procs and harv < procs and "harv" in v.avail:
            return "harv"
        if v.remaining > 240 and harv < target_harv and "harv" in v.avail and not army.threatened \
                and v.money > 900:
            return "harv"
        enemy_air = army.enemy_air_seen()
        if enemy_air and "ftrk" in v.avail and v.count("ftrk") < 2 and v.side == "soviet":
            return "ftrk"
        tank = next((t for t in MAIN_TANKS if t in v.avail and t != LIGHT_TANK), None)
        if tank is not None:
            n_main = v.count(tank)
            if LIGHT_TANK in v.avail and v.count(LIGHT_TANK) * 5 < n_main and n_main >= 5:
                return LIGHT_TANK
            return tank
        if LIGHT_TANK in v.avail:
            cap = 4 if (army.threatened or army.rush) else 2
            if v.have.get("fix", 0) == 0 and "fix" not in v.avail and v.count("fix") == 0:
                cap = 6                                   # no depot coming: light tanks are all we have
            if v.count(LIGHT_TANK) < cap:
                return LIGHT_TANK
        return None
