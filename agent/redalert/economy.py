"""Base building: the MCV deploy, the Building and Defense queues (opening A or B, then mid-game rules), power,
placement hints (refinery by the ore, towers toward the threat), failed placements, repair, rally points, idle
harvesters (docs/redalert_design.md sections 5 and 6)."""
from __future__ import annotations

from .orders import Cand
from .world import POWER_DOWNABLE, PRODUCTION, dist

# Plan steps: (role, how many of that role we want in total). Roles resolve through available_production.
OPENING_A = [("power", 1), ("proc", 1), ("barracks", 1), ("proc", 2), ("power", 2), ("weap", 1), ("fix", 1),
             ("power", 3)]
OPENING_B_ALLIES = [("power", 1), ("proc", 1), ("barracks", 1), ("weap", 1), ("power", 2), ("proc", 2), ("fix", 1),
                    ("power", 3), ("weap", 2), ("proc", 3), ("power", 4)]
OPENING_B_SOVIET = [("power", 1), ("proc", 1), ("barracks", 1), ("weap", 1), ("power", 2), ("fix", 1), ("proc", 2),
                    ("power", 3), ("weap", 2), ("proc", 3), ("power", 4)]
ROLES = {"power": ("powr", "apwr"), "proc": ("proc",), "barracks": ("tent", "barr"), "weap": ("weap",),
         "fix": ("fix",), "dome": ("dome",)}
ANTI_INFANTRY_DEF = ("pbox", "ftur", "hbox")
ANTI_VEHICLE_DEF = ("tsla", "gun", "ftur", "pbox")
ECO_KEY = frozenset({"proc", "weap", "fix"})


class Economy:
    def __init__(self, pol):
        self.pol = pol
        self.plan: list | None = None
        self.reserve = 0                      # credits the unit queues leave for the current key building
        self.blocked: dict[str, int] = {}     # item -> tick until which it is not planned (placement failures)
        self.tried_at: dict[str, list] = {}   # item -> placement hints already refused
        self.deploy_moves = 0
        self.deploy_spot = None
        self.powered_down: set[int] = set()
        self.late_defenses = 0
        self.lost_fact_at = None

    # ---- helpers ----
    def role_count(self, v, role: str) -> int:
        return sum(v.have.get(t, 0) + v.in_prod.get(t, 0) + (1 if self.pol.ledger.is_pending(("build", t)) else 0)
                   for t in ROLES.get(role, (role,)))

    def role_item(self, v, role: str) -> str | None:
        for t in ROLES.get(role, (role,)):
            if t in v.avail and self.blocked.get(t, -1) < v.tick:
                return t
        return None

    def margin(self, v) -> int:
        n = len(v.buildings)
        return max(10, min(60, 10 + 10 * (n // 4)))

    def busy(self, v, q: str) -> bool:
        if v.queue_items(q):
            return True
        led, rules = self.pol.ledger, v.st.rules
        return any(k[0] == "build" and rules.queue(k[1]) == q and led.is_pending(k) for k in list(led.pending))

    # ---- every view ----
    def step(self, v, out: list[Cand], allow: bool) -> None:
        pol = self.pol
        if self.plan is None:
            rich = v.starting_cash >= pol.p.rich_cash_opening
            self.plan = OPENING_A if not rich else (OPENING_B_SOVIET if v.side == "soviet" else OPENING_B_ALLIES)
            pol.opening = "B" if rich else "A"
        self._deploy(v, out)
        self._place(v, out)
        self._reserve(v)
        if v.fact is None:
            return
        self._building_queue(v, out)
        self._defense_queue(v, out)
        self._power_down(v, out)
        if allow:
            self._upkeep(v, out)

    # ---- the MCV ----
    def _deploy(self, v, out) -> None:
        mcvs = [u for u in v.units if u["type"] == "mcv"]
        if not mcvs or v.fact is not None:
            return
        led = self.pol.ledger
        mcv = mcvs[0]
        if led.is_pending(("deploy",)):
            return
        if led.deploy_invalid > self.deploy_moves:
            # no room to unfold here: move a few cells toward the map centre (then sideways) and try again
            self.deploy_moves += 1
            st = v.st
            centre = ((st.bx0 + st.bx1) // 2, (st.by0 + st.by1) // 2)
            dirs = [centre, (mcv["x"] + 4, mcv["y"]), (mcv["x"], mcv["y"] + 4), (mcv["x"] - 4, mcv["y"]),
                    (mcv["x"], mcv["y"] - 4)]
            target = st.step_toward((mcv["x"], mcv["y"]), dirs[(self.deploy_moves - 1) % len(dirs)], 3)
            self.deploy_spot = target
            out.append(Cand({"cmd": "move", "units": [mcv["id"]], "to": list(target)}, key=("deploy",), urgent=True,
                            sig=("move", target)))
            return
        if self.deploy_spot is not None and dist((mcv["x"], mcv["y"]), self.deploy_spot) > 1.5 and not mcv["idle"]:
            return                                       # still driving to the new spot
        out.append(Cand({"cmd": "deploy", "units": [mcv["id"]]}, key=("deploy",), urgent=True, sig=("deploy",)))

    # ---- placing finished buildings ----
    def _place(self, v, out) -> None:
        led = self.pol.ledger
        for q, item in v.ready:
            if led.is_pending(("place", item)):
                continue
            fails = led.place_invalid.get(item, 0)
            if fails >= 3:
                # no room for it: cancel (refund) and do not plan it for 60 s
                led.place_invalid[item] = 0
                self.tried_at.pop(item, None)
                self.blocked[item] = v.tick + 60 * 25
                if not led.is_pending(("cancel", item)):
                    out.append(Cand({"cmd": "cancel", "item": item}, key=("cancel", item), urgent=True))
                continue
            at = self._place_hint(v, item, fails)
            order = {"cmd": "place", "item": item}
            if at is not None:
                order["at"] = [int(at[0]), int(at[1])]
                self.tried_at.setdefault(item, []).append(tuple(order["at"]))
            out.append(Cand(order, key=("place", item), urgent=True))

    def _occupied(self, v) -> set:
        rules = v.st.rules
        occ = set()
        for b in v.buildings:
            w, h = rules.footprint(b["type"])
            for yy in range(b["y"], b["y"] + h):
                for xx in range(b["x"], b["x"] + w):
                    occ.add((xx, yy))
        return occ

    def _fits(self, v, occ, x, y, w, h, gap: int) -> bool:
        st = v.st
        for yy in range(y - gap, y + h + gap):
            for xx in range(x - gap, x + w + gap):
                inside = x <= xx < x + w and y <= yy < y + h
                if (xx, yy) in occ:
                    return False
                if inside and (not st.can_build(xx, yy) or (xx, yy) in self._ore_set(v)):
                    return False
        return True

    def _ore_set(self, v):
        s = getattr(v.st, "_ore_set", None)
        if s is None:
            s = v.st._ore_set = set(v.st.ore_cells)
        return s

    def _adjacent(self, v, x, y, w, h) -> bool:
        rules = v.st.rules
        for b in v.buildings:
            if b["type"] in ("pbox", "hbox", "gun", "ftur", "tsla", "agun", "sam", "silo", "gap"):
                continue
            bw, bh = rules.footprint(b["type"])
            dx = max(b["x"] - (x + w), 0, x - (b["x"] + bw))
            dy = max(b["y"] - (y + h), 0, y - (b["y"] + bh))
            if max(dx, dy) <= 2:
                return True
        return False

    def _best_spot(self, v, item, target, radius: int, prefer_dist: float = 0.0):
        """The legal-looking top-left nearest to `target` (footprint inside buildable ground, one free cell around,
        next to one of our buildings), skipping hints already refused. None if nothing looks legal."""
        rules = v.st.rules
        w, h = rules.footprint(item)
        occ = self._occupied(v)
        tried = set(self.tried_at.get(item, []))
        best = None
        tx, ty = target
        for y in range(int(ty) - radius, int(ty) + radius + 1):
            for x in range(int(tx) - radius, int(tx) + radius + 1):
                if (x, y) in tried:
                    continue
                cx, cy = x + w / 2.0, y + h / 2.0
                d = abs(dist((cx, cy), target) - prefer_dist)
                if best is not None and d >= best[0]:
                    continue
                if not self._fits(v, occ, x, y, w, h, gap=1 if w > 1 else 0) or not self._adjacent(v, x, y, w, h):
                    continue
                best = (d, (x, y))
        return best[1] if best else None

    def _place_hint(self, v, item, fails: int):
        st = v.st
        if v.base is None or not st.grid_ok:
            return None
        try:
            if item == "proc":
                ore = st.ore_near(v.base, 16)
                if ore is None:
                    return None
                return self._best_spot(v, item, ore, 7, prefer_dist=3.5)
            if item in ANTI_INFANTRY_DEF or item in ANTI_VEHICLE_DEF or item in ("agun", "sam", "hbox"):
                toward = self.pol.army.threat_point or st.enemy_spawn or v.base
                target = st.step_toward(v.base, toward, 6)
                return self._best_spot(v, item, target, 4)
            if fails:
                return self._best_spot(v, item, v.base, 9, prefer_dist=5.0)
        except Exception:                                  # a hint is optional: the engine auto-places
            self.pol.note_exception("place_hint")
        return None

    # ---- money kept for the current key building in the opening ----
    def _reserve(self, v) -> None:
        self.reserve = 0
        if v.game_s > 330 or self.pol.army.threatened:
            return
        rules = v.st.rules
        for it in v.queue_items("Building"):
            if it["item"] in ECO_KEY and not it["ready"]:
                self.reserve = int(rules.cost(it["item"]) * (1.0 - it["progress"]))
                return

    # ---- the Building queue ----
    def _building_queue(self, v, out) -> None:
        if self.busy(v, "Building"):
            return
        item = self._next_building(v)
        if item is None:
            return
        out.append(Cand({"cmd": "build", "item": item}, key=("build", item), urgent=True, base=v.in_prod.get(item, 0)))

    def _power_item(self, v) -> str | None:
        if "apwr" in v.avail and v.money >= 1500 and v.have.get("powr", 0) >= 3:
            return "apwr" if self.blocked.get("apwr", -1) < v.tick else self.role_item(v, "power")
        return self.role_item(v, "power")

    def _next_building(self, v) -> str | None:
        rules = v.st.rules
        pol = self.pol
        p = pol.p
        need_power = v.surplus < 0
        # the plan: the first step whose role we have fewer of
        choice = None
        for role, n in self.plan or []:
            if self.role_count(v, role) < n:
                item = self.role_item(v, role)
                if item is not None:
                    choice = item
                    break
                if role in ("barracks", "power", "proc"):
                    continue                              # not buildable yet: look further down the plan
        if choice is None:
            choice = self._midgame(v)
        if v.remaining < 60:                              # only what finishes before the end
            if choice is not None and rules.build_time(choice) > v.remaining - 5:
                choice = None
        if need_power and choice not in ("powr", "apwr"):
            return self._power_item(v)
        if choice is None:
            # nothing else to do: keep the power margin for the units' production buildings
            if v.surplus < self.margin(v) and v.money > 600 and self.role_count(v, "power") < 12:
                return self._power_item(v)
            return None
        d = -rules.power(choice)
        if d > 0 and v.surplus - d < self.margin(v):
            pw = self._power_item(v)
            if pw is not None:
                return pw
        return choice

    def _midgame(self, v) -> str | None:
        p = self.pol.p
        procs = self.role_count(v, "proc")
        harv = v.unit_count.get("harv", 0)
        if procs < 1 or (procs < 2 and v.game_s > 60):
            return self.role_item(v, "proc")
        if self.role_count(v, "barracks") < 1:
            item = self.role_item(v, "barracks")
            if item:
                return item
        if self.role_count(v, "weap") < 1 and "weap" in v.avail:
            return self.role_item(v, "weap")
        if self.role_count(v, "fix") < 1 and "fix" in v.avail and v.money > 800:
            return self.role_item(v, "fix")
        if procs < 3 and v.game_s >= 200 and (harv >= procs + 1 or v.money > 2500):
            return self.role_item(v, "proc")
        if self.role_count(v, "weap") < min(2, p.max_weaps) and v.game_s >= 240 and v.money > 1500 and harv >= 3:
            return self.role_item(v, "weap")
        if procs < p.max_procs and v.game_s >= 420 and v.money > 3000:
            return self.role_item(v, "proc")
        return None

    # ---- the Defense queue: early towers, silos, late towers ----
    def _defense_queue(self, v, out) -> None:
        if self.busy(v, "Defense"):
            return
        army = self.pol.army
        rules = v.st.rules
        item = None
        if v.capacity > 0 and v.ore > 0.8 * v.capacity and "silo" in v.avail and v.have.get("silo", 0) < 4:
            item = "silo"
        else:
            towers = sum(v.have.get(t, 0) for t in ANTI_VEHICLE_DEF + ("hbox",))
            want = army.towers_wanted(v)
            if v.game_s > 360 and v.money > 2500 and v.have.get("weap", 0):
                want = max(want, min(int(self.pol.p.late_defenses), 1 + int(v.game_s // 300)))
            if towers < want:
                prefs = ANTI_VEHICLE_DEF if army.enemy_vehicle_share() > 0.5 else ANTI_INFANTRY_DEF + ANTI_VEHICLE_DEF
                for t in prefs:
                    if t in v.avail and self.blocked.get(t, -1) < v.tick:
                        if t == "tsla" and v.surplus - 80 < 0 and v.money < 1500:
                            continue
                        item = t
                        break
        if item is None:
            return
        if v.remaining < 60 and rules.build_time(item) > v.remaining - 5:
            return
        d = -rules.power(item)
        if d > 0 and v.surplus - d < 0 and item != "silo" and not army.threatened:
            return                                      # the Building queue adds power first
        out.append(Cand({"cmd": "build", "item": item}, key=("build", item), urgent=army.threatened,
                        base=v.in_prod.get(item, 0)))

    # ---- low power without money: switch off the hungry buildings ----
    def _power_down(self, v, out) -> None:
        led = self.pol.ledger
        if v.surplus < 0 and v.money < 300:
            for b in v.buildings:
                if b["type"] in POWER_DOWNABLE and not b["powered_down"] and b["id"] not in self.powered_down:
                    if b["type"] == "tsla" and self.pol.army.threatened:
                        continue
                    if not led.is_pending(("power", b["id"])):
                        self.powered_down.add(b["id"])
                        out.append(Cand({"cmd": "power_toggle", "building": b["id"], "on": False},
                                        key=("power", b["id"])))
                        return
        elif self.powered_down and v.surplus > 60:
            for bid in list(self.powered_down):
                b = v.bld_by_id.get(bid)
                if b is None:
                    self.powered_down.discard(bid)
                    continue
                if not led.is_pending(("power", bid)):
                    self.powered_down.discard(bid)
                    out.append(Cand({"cmd": "power_toggle", "building": bid, "on": True}, key=("power", bid)))
                    return

    # ---- repair, rally points, idle harvesters ----
    def _upkeep(self, v, out) -> None:
        p = self.pol.p
        led = self.pol.ledger
        st = v.st
        n = 0
        for b in sorted(v.buildings, key=lambda b: b["hp"]):
            if n >= 2:
                break
            key = ("repair", b["id"])
            if led.is_pending(key):
                continue
            if b["repairing"]:
                if v.cash < p.repair_stop_cash and b["type"] != "fact":
                    out.append(Cand({"cmd": "repair", "building": b["id"], "on": False}, key=key))
                    n += 1
                continue
            if (b["hp"] < p.repair_below and v.money > p.repair_min_cash) or (b["type"] == "fact" and b["hp"] < 50):
                out.append(Cand({"cmd": "repair", "building": b["id"], "on": True}, key=key))
                n += 1
        for b in v.buildings:
            if b["type"] not in PRODUCTION:
                continue
            target = st.guard if b["type"] in ("tent", "barr") else st.rally
            if target is None:
                continue
            target = tuple(st.clamp(target))
            if b.get("rally") is not None and tuple(b["rally"]) == target:
                continue
            key = ("rally", b["id"])
            if led.is_pending(key):
                continue
            out.append(Cand({"cmd": "rally", "building": b["id"], "to": list(target)}, key=key))
        idle_harv = [u["id"] for u in v.units if u["type"] == "harv" and u["idle"]
                     and led.unit_ok(u["id"], ("harvest",), True, min_gap=50, same_gap=125)]
        if idle_harv and (v.have.get("proc", 0) > 0):
            out.append(Cand({"cmd": "harvest", "units": idle_harv[:50]}, sig=("harvest",)))
