"""Base building: the MCV deploy, the Building and Defense queues (opening A or B, then mid-game rules), power,
placement hints (refinery by the ore, towers toward the threat), failed placements, repair, rally points, idle
harvesters (docs/redalert_design.md sections 5 and 6)."""
from __future__ import annotations

from .orders import Cand
from .world import POWER_DOWNABLE, PRODUCTION, dist

# Plan steps: (role, how many of that role we want in total). Roles resolve through available_production.
# The barracks comes right after the first power plant (12 s before the refinery): the first rifleman scouts the enemy
# base early enough to see an infantry rush coming (local simulator: proc-first saw it too late on shadowfiend-ii).
OPENING_A = [("power", 1), ("barracks", 1), ("proc", 1), ("proc", 2), ("power", 2), ("weap", 1), ("fix", 1),
             ("power", 3)]
OPENING_B_ALLIES = [("power", 1), ("barracks", 1), ("proc", 1), ("weap", 1), ("power", 2), ("proc", 2), ("fix", 1),
                    ("power", 3), ("weap", 2), ("proc", 3), ("power", 4)]
OPENING_B_SOVIET = [("power", 1), ("barracks", 1), ("proc", 1), ("weap", 1), ("power", 2), ("fix", 1), ("proc", 2),
                    ("power", 3), ("weap", 2), ("proc", 3), ("power", 4)]
# v4.9 open_fast: opening A with the war factory before the second refinery (Tom_2026's tanks reached us at 4:45)
OPENING_F = [("power", 1), ("barracks", 1), ("proc", 1), ("weap", 1), ("power", 2), ("proc", 2), ("fix", 1),
             ("power", 3)]
ROLES = {"power":("powr", "apwr"), "proc": ("proc",), "barracks": ("tent", "barr"), "weap": ("weap",),
         "fix": ("fix",), "dome": ("dome",)}
ANTI_INFANTRY_DEF = ("pbox", "ftur", "hbox")
ANTI_VEHICLE_DEF = ("tsla", "gun", "ftur", "pbox")
AT_DEF = ("gun", "tsla")                  # v4.8 at_towers: the anti-tank towers (Allies / Soviet)
POWER_FOR_TICKS = 10 * 25                 # v4.8: a tower waiting for power asks the Building queue this long
ECO_KEY = frozenset({"proc", "weap", "fix"})
POWER = ("powr", "apwr")
TOWERS = frozenset({"pbox", "hbox", "gun", "ftur", "tsla", "agun", "sam", "gap"})
# Harvester emergency (every harvester lost while a refinery stands): after this long at zero harvesters (a new
# refinery's free harvester takes a moment to appear), with at least this much of the match left.
EMERGENCY_AFTER_TICKS = 5 * 25
EMERGENCY_MIN_REMAINING_S = 90
SELL_MIN_REMAINING_S = 300      # selling loses half the building's value (assets): only with time to earn it back
SELL_GAP_TICKS = 5 * 25         # one sale at a time: its refund shows before the next is considered
MAX_SELLS = 3                   # per emergency
LOW_POWER_SLOWDOWN = 3.0        # production runs at a third of the speed on low power (OpenRA LowPowerModifier)
POWER_CANCEL_S = 20.0           # low power: cancel the building in progress if it would take longer than this


class Economy:
    def __init__(self, pol):
        self.pol = pol
        self.plan: list | None = None
        self.reserve = 0                      # credits the unit queues leave for the current key building
        self.blocked: dict[str, int] = {}     # item -> tick until which it is not planned (placement failures)
        self.tried_at: dict[str, list] = {}   # item -> placement hints already refused
        self.deploy_moves = 0
        self.deploy_fails_seen = 0
        self.deploy_spot = None
        self.old_base = None                  # where our construction yard stood (a new MCV unfolds near it)
        self.cancels: list = []               # (tick, item) cancelled after failed placements
        self.full_until = -10 ** 9            # no new buildings before this tick (the base is full)
        self.powered_down: set[int] = set()
        self.late_defenses = 0
        self.lost_fact_at = None
        self.emergency = False                # every harvester lost (harv_emergency)
        self.emergencies = 0
        self.harv_seen = False
        self.harv_zero_since = None
        self.sold: dict[int, int] = {}        # building id -> tick we sold it
        self.sells = 0                        # sales in the current emergency
        self.sales = 0                        # whole match
        self.power_cancels = 0
        self.power_for = -10 ** 9             # v4.8 at_towers: tick an anti-tank tower last waited for power
        self.at_towers = 0                    # v4.8: anti-tank tower orders (summary)
        self.at_item = None                   # v4.8: the anti-tank tower type ordered (placed at_place cells out)

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
            if not rich and pol.p.open_fast:
                self.plan, pol.opening = OPENING_F, "F"
        if v.fact is not None:
            self.old_base = (v.fact["x"] + 1, v.fact["y"] + 1)
            self.deploy_spot = None
        self.harv_emergency(v)
        self._deploy(v, out)
        self._place(v, out)
        self._reserve(v)
        if v.fact is None:
            if allow and self.pol.p.idle_harv_fix:
                # (v4.6 fix: without a yard the upkeep below never ran, so a harvester left idle by a harvester-guard
                # pull never went back to work: dev seed 602053 sat on 390 credits with 8 harvesters for 7 minutes)
                self._idle_harvesters(v, out)
            return
        self._building_queue(v, out)
        self._defense_queue(v, out)
        self._power_down(v, out)
        if allow:
            self._upkeep(v, out)

    # ---- every harvester lost ----
    def harv_emergency(self, v) -> bool:
        """True while a refinery stands but no harvester is left (after EMERGENCY_AFTER_TICKS): no income at all, so
        the next harvester comes before anything else (production._harvester_first) and nothing new is started."""
        harv = v.unit_count.get("harv", 0)
        if harv > 0:
            self.harv_seen = True
        on = False
        if harv == 0 and self.harv_seen and v.have.get("proc", 0) > 0 and v.remaining > EMERGENCY_MIN_REMAINING_S:
            if self.harv_zero_since is None:
                self.harv_zero_since = v.tick
            on = v.tick - self.harv_zero_since >= EMERGENCY_AFTER_TICKS
        else:
            self.harv_zero_since = None
        if on and not self.emergency:
            self.emergencies += 1
            self.sells = 0
            self.pol.log_event(f"every harvester lost at {v.game_s:.0f}s (money {v.money}): a harvester first")
        elif self.emergency and not on:
            self.pol.log_event(f"harvester emergency over at {v.game_s:.0f}s (sold {self.sells})")
        self.emergency = on
        return on

    def sell_spare(self, v, out, short: float) -> None:
        """Sell one building we can spare (never the yard, the last refinery, power plant, barracks or war factory,
        the depot), the one whose refund (half its value) best covers `short`. Towers only when the base is quiet."""
        if v.remaining < SELL_MIN_REMAINING_S or self.sells >= MAX_SELLS:
            return
        if any(v.tick - t < SELL_GAP_TICKS for t in self.sold.values()):
            return
        b = self._spare(v, short)
        if b is None:
            return
        self.sold[b["id"]] = v.tick
        self.sells += 1
        self.sales += 1
        self.pol.log_event(f"selling {b['type']} {b['id']} at {v.game_s:.0f}s for the harvester (short {short:.0f})")
        out.append(Cand({"cmd": "sell", "building": b["id"]}, key=("sell", b["id"]), urgent=True))

    def _spare(self, v, short: float):
        rules = v.st.rules
        blds = [b for b in v.buildings if b["id"] not in self.sold]
        n = {}
        for b in blds:
            n[b["type"]] = n.get(b["type"], 0) + 1
        quiet = not self.pol.army.threatened
        groups = [
            [b for b in blds if b["type"] == "silo" and v.ore <= 0],
            [b for b in blds if b["type"] == "dome"],
            [b for b in blds if b["type"] in ("tent", "barr") and n.get("tent", 0) + n.get("barr", 0) > 1],
            [b for b in blds if b["type"] in TOWERS and quiet],
            [b for b in blds if b["type"] == "weap" and n.get("weap", 0) > 1],
            # a refinery without harvesters earns nothing: one of several can go (the others take the new harvester)
            [b for b in blds if b["type"] == "proc" and n.get("proc", 0) > 1],
            [b for b in blds if b["type"] in POWER and n.get("powr", 0) + n.get("apwr", 0) > 1
             and v.surplus - rules.power(b["type"]) >= 0],
        ]

        def refund(b):
            return 0.5 * rules.cost(b["type"]) * max(0.0, b["hp"]) / 100.0
        for g in groups:
            if g:
                # the smallest refund that covers the shortfall, else the biggest one
                cover = [b for b in g if refund(b) >= short]
                pick = min(cover, key=lambda b: (refund(b), b["id"])) if cover else \
                    max(g, key=lambda b: (refund(b), -b["id"]))
                if refund(pick) >= 50:
                    return pick
        return None

    # ---- the MCV ----
    def _deploy(self, v, out) -> None:
        mcvs = [u for u in v.units if u["type"] == "mcv"]
        if not mcvs or v.fact is not None:
            return
        led = self.pol.ledger
        mcv = mcvs[0]
        if led.is_pending(("deploy",)):
            return
        pos = (mcv["x"], mcv["y"])
        rebuilt = len(v.buildings) > 0 or v.game_s > 60   # a new MCV after losing the yard: the base is crowded
        failed = led.deploy_invalid > self.deploy_fails_seen
        if self.deploy_spot is None and (failed or (rebuilt and not self.deploy_moves)):
            self.deploy_fails_seen = led.deploy_invalid
            self.deploy_moves += 1
            target = self._deploy_spot(v, pos)
            if target is not None and dist(target, pos) > 0.5:
                self.deploy_spot = target
                out.append(Cand({"cmd": "move", "units": [mcv["id"]], "to": list(target)}, key=("deploy",),
                                urgent=True, sig=("move", target)))
                return
        if self.deploy_spot is not None:
            if dist(pos, self.deploy_spot) > 0.5 and not mcv["idle"]:
                return                                   # still driving to the clear spot
            if dist(pos, self.deploy_spot) > 1.5 and mcv["idle"]:
                out.append(Cand({"cmd": "move", "units": [mcv["id"]], "to": list(self.deploy_spot)},
                                key=("deploy",), urgent=True, sig=("move", self.deploy_spot)))
                return
            self.deploy_spot = None
        out.append(Cand({"cmd": "deploy", "units": [mcv["id"]]}, key=("deploy",), urgent=True, sig=("deploy",)))

    def _deploy_spot(self, v, pos):
        """A cell where the MCV can unfold (the yard covers x-1..x+1, y-1..y+2): clear buildable ground with a free
        cell around it, no building or ore, away from the spots already refused; the nearest one to the old base
        (else to the MCV). Without the map picture: 4 cells toward the map centre."""
        st = v.st
        tried = set(self.tried_at.get("__deploy__", []))
        if not st.grid_ok:
            centre = ((st.bx0 + st.bx1) // 2, (st.by0 + st.by1) // 2)
            return st.step_toward(pos, centre, 4)
        occ = self._occupied(v)
        anchor = self.old_base or pos
        best = None
        for r in range(0, 14):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    x, y = int(pos[0]) + dx, int(pos[1]) + dy
                    if (x, y) in tried:
                        continue
                    if self._fits(v, occ, x - 1, y - 1, 3, 4, gap=1):
                        d = dist((x, y), anchor) + 0.5 * dist((x, y), pos)
                        if best is None or d < best[0]:
                            best = (d, (x, y))
            if best is not None and r >= 3:
                break
        if best is None:
            return None
        self.tried_at.setdefault("__deploy__", []).append(best[1])
        return best[1]

    # ---- placing finished buildings ----
    def _place(self, v, out) -> None:
        led = self.pol.ledger
        for q, item in v.ready:
            if led.is_pending(("place", item)) or led.is_pending(("cancel", item)):
                continue                                 # (a cancelled item still shows ready until it is applied)
            fails = led.place_invalid.get(item, 0)
            if fails >= 3:
                # no room for it: cancel (refund) and do not plan it for 60 s; two different items in 3 minutes = the
                # base is full: no new building for 2 minutes
                led.place_invalid[item] = 0
                self.tried_at.pop(item, None)
                self.blocked[item] = v.tick + 60 * 25
                self.cancels = [(t, i) for t, i in self.cancels if v.tick - t < 180 * 25] + [(v.tick, item)]
                if len({i for _t, i in self.cancels}) >= 2 and v.tick >= self.full_until:
                    self.full_until = v.tick + 120 * 25
                    self.pol.log_event(f"no room to place buildings at {v.game_s:.0f}s: none planned for 2 minutes")
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
                if self.pol.p.harv_first and v.have.get("proc", 0) >= 2 and self.pol.harv.engaged(v):
                    spot = self.pol.harv.proc_spot(v)      # refineries 3+: the field with the most ore per refinery
                    if spot is not None and tuple(spot) not in set(self.tried_at.get(item, [])):
                        return spot
                ore = st.ore_near(v.base, 16)
                if ore is None:
                    return None
                return self._best_spot(v, item, ore, 7, prefer_dist=3.5)
            if item in ANTI_INFANTRY_DEF or item in ANTI_VEHICLE_DEF or item in ("agun", "sam", "hbox"):
                toward = self.pol.army.threat_point or st.enemy_spawn or v.base
                # v4.8 at_towers: anti-tank towers stand in the base (at_place cells out), where the decisive push
                # comes, not 6 cells out where shelling and probes draw the army into fights (dev seeds 1501)
                step = float(self.pol.p.at_place) if self.pol.p.at_towers > 0 and item == self.at_item else 6
                target = st.step_toward(v.base, toward, step)
                return self._best_spot(v, item, target, 4)
            if fails:
                return self._best_spot(v, item, v.base, 9, prefer_dist=5.0)
        except Exception:                                  # a hint is optional: the engine auto-places
            self.pol.note_exception("place_hint")
        return None

    # ---- money kept for the current key building in the opening ----
    def _reserve(self, v) -> None:
        """A small buffer so the key building of the opening never stalls. Production is paid as it progresses, so
        holding its whole remaining cost would leave money idle (seen in the simulator: ~2000 unspent for 100 s)."""
        self.reserve = 0
        if self.pol.army.threatened:
            return
        rules = v.st.rules
        if v.surplus < 0:
            # low power: the power plant being built comes first (up to 300 kept for it)
            for it in v.queue_items("Building"):
                if it["item"] in POWER and not it["ready"]:
                    self.reserve = min(300, int(rules.cost(it["item"]) * (1.0 - it["progress"])))
                    return
        if v.game_s > 330:
            return
        for it in v.queue_items("Building"):
            if it["item"] in ECO_KEY and not it["ready"]:
                self.reserve = min(400, int(rules.cost(it["item"]) * (1.0 - it["progress"])))
                return

    # ---- the Building queue ----
    def _power_first(self, v, out) -> bool:
        """Low power (a power plant lost) while the Building queue works on something else: production runs at a
        third of the speed until that item is done, so cancel it (the refund is full) when it would take longer than
        POWER_CANCEL_S; the next view builds the power plant. True when a cancel went out."""
        if v.surplus >= 0 or not v.buildings or any(it in POWER for _q, it in v.ready):
            return False
        if self._power_item(v) is None or v.tick < self.full_until:
            return False                                 # no power plant can come now (none to build, no room)
        head = next((it for it in v.queue_items("Building") if not it["ready"]), None)
        if head is None or head["item"] in POWER:
            return False
        rules = v.st.rules
        left_s = (1.0 - head["progress"]) * rules.build_time(head["item"]) * LOW_POWER_SLOWDOWN
        if left_s <= POWER_CANCEL_S or self.pol.ledger.is_pending(("cancel", head["item"])):
            return False
        self.power_cancels += 1
        self.pol.log_event(f"low power at {v.game_s:.0f}s: cancelling {head['item']} "
                           f"({head['progress']:.0%} done) for a power plant")
        out.append(Cand({"cmd": "cancel", "item": head["item"]}, key=("cancel", head["item"]), urgent=True))
        return True

    def _building_queue(self, v, out) -> None:
        if self.emergency and v.surplus >= 0:
            return                                       # every credit goes to the new harvester
        if self._power_first(v, out):
            return
        if self.busy(v, "Building") or v.tick < self.full_until:
            return
        item = self._next_building(v)
        if item is None:
            return
        army = self.pol.army
        if self.pol.p.econ_first and army.early_army and not self.pol.first.get("weap") and item not in POWER and \
                any(not it["ready"] and it["item"] in TOWERS for it in v.queue_items("Defense")):
            # an early enemy army: the tower in production gets the money before the next building starts (v4.5
            # measurement seed 62005: the war factory started next to it, the tower sat at 8 % until the 36-rifle
            # rush came at 178 s)
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
        if p.harv_first and choice in (None, "fix", "weap", "dome") and pol.harv.need_proc(v) and \
                not (choice == "fix" and (self.role_count(v, "proc") >= 3 or not p.harv_proc_first)):
            # harvesters first: the third refinery before the depot (the Vehicle queue builds harvesters until the
            # target, so the depot is not needed earlier), a fourth one only after it
            choice = self.role_item(v, "proc") or choice
        if (pol.army.rush or pol.army.defense_first(v)) and choice not in (None, "powr", "apwr", "tent", "barr", "proc") \
                and v.money < 1500:
            choice = None                                 # an early rush: the money goes to rifles and towers
        if p.at_towers > 0 and v.tick - self.power_for <= POWER_FOR_TICKS and choice not in POWER and \
                v.surplus < 100 + self.margin(v) and v.remaining >= 120:
            pw = self._power_item(v)                      # v4.8: an anti-tank tower waits for this power plant
            if pw is not None:
                return pw
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
        # econ_first: the third refinery waits for the army until 6:00 (two refineries already pay for one war
        # factory; v4.5 tuning seed 502010: a third refinery at 210 s and no tank before 300 s lost to the first wave)
        greedy_ok = not self.pol.p.econ_first or v.game_s >= 360 or v.money > 2500
        if procs < 3 and v.game_s >= 200 and (harv >= procs + 1 or v.money > 2500) and greedy_ok and \
                not (p.harv_first and self.pol.harv.engaged(v)):
            return self.role_item(v, "proc")          # (harv_first: harvest.need_proc decides the refineries)
        if self.role_count(v, "weap") < min(2, p.max_weaps) and v.game_s >= 240 and v.money > 2000 and harv >= 3 \
                and v.army_value >= 3000 and not (p.harv_first and self.pol.harv.serialized(v)):
            return self.role_item(v, "weap")
        if p.weap2_s > 0 and self.role_count(v, "weap") < min(2, p.max_weaps) and v.game_s >= p.weap2_s and \
                v.money > p.weap2_cash and harv >= 3 and v.have.get("fix", 0) and \
                not (p.harv_first and self.pol.harv.serialized(v)):
            # v4.9: one war factory cannot spend two refineries' income (simulator vs tom2: 1300-2600 floating at
            # 2:00-4:45 while the tank army came); the second one earlier, once the depot stands
            return self.role_item(v, "weap")
        if p.harv_first and p.harv_weap2 and self.pol.harv.engaged(v) and \
                self.role_count(v, "weap") < min(2, p.max_weaps) and \
                v.game_s >= 180 and v.money > p.harv_weap2 and harv >= 5 and not self.pol.harv.serialized(v):
            # harvesters first: the income outgrows one war factory (simulator: 2000-4000 floating at 4-6 minutes)
            return self.role_item(v, "weap")
        if procs < p.max_procs and v.game_s >= 420 and v.money > 3000:
            return self.role_item(v, "proc")
        return None

    # ---- the Defense queue: early towers, silos, late towers ----
    def _defense_queue(self, v, out) -> None:
        if self.busy(v, "Defense") or v.tick < self.full_until:
            return
        army = self.pol.army
        rules = v.st.rules
        if self.emergency and (not army.threatened or (self.pol.p.econ_first and v.money < rules.cost("harv"))):
            return                                      # (econ_first: no tower eats the harvester's last credits)
        item = at = None
        if v.capacity > 0 and v.ore > 0.8 * v.capacity and "silo" in v.avail and v.have.get("silo", 0) < 4:
            item = "silo"
        else:
            towers = sum(v.have.get(t, 0) for t in ANTI_VEHICLE_DEF + ("hbox",))
            want = army.towers_wanted(v)
            if v.game_s > 360 and v.money > 2500 and v.have.get("weap", 0) and \
                    not (self.pol.p.harv_first and self.pol.harv.serialized(v)):
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
                item = at = self._at_tower(v)
        if item is None:
            return
        if v.remaining < 60 and rules.build_time(item) > v.remaining - 5:
            return
        d = -rules.power(item)
        if d > 0 and v.surplus - d < 0 and item != "silo" and not army.threatened:
            return                                      # the Building queue adds power first
        if at is not None:
            self.at_towers += 1
            self.at_item = at
        out.append(Cand({"cmd": "build", "item": item}, key=("build", item), urgent=army.threatened,
                        base=v.in_prod.get(item, 0)))

    def _at_tower(self, v) -> str | None:
        """v4.8 at_towers: an enemy tank army seen and their army not much smaller than ours: anti-tank towers (gun:
        range 6.5 over the tanks' 4.75; tesla coil) toward the threat, up to at_towers of them, besides the towers
        above (the real losses 6d0c7844 / edf99e95 / f1cd3ba4 had 2 anti-infantry towers from the early-army rule and
        none against the tanks that razed the base). A tesla coil waits for its power (the Building queue adds a power
        plant first: power_for)."""
        p = self.pol.p
        if p.at_towers <= 0 or not v.have.get("weap") or v.remaining < 120:
            return None
        army = self.pol.army
        if not army.at_threat(v):
            return None
        have = sum(v.have.get(t, 0) + v.in_prod.get(t, 0) for t in AT_DEF)
        if have >= int(p.at_towers):
            return None
        for t in AT_DEF:
            if t in v.avail and self.blocked.get(t, -1) < v.tick:
                d = -v.st.rules.power(t)
                if d > 0 and v.surplus - d < self.margin(v):
                    # the Building queue: a power plant for it first, keeping the margin (dev seeds 1501: without it the
                    # towers cost 1501064 58 s of low power, a third of the production speed)
                    self.power_for = v.tick
                    return None
                return t
        return None

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
                # (the same money as the start test below: cash alone flipped it on and off every 2 s while the ore
                # in the refineries kept cash + ore above the start threshold)
                if (v.money < p.repair_stop_cash or self.emergency) and b["type"] != "fact":
                    out.append(Cand({"cmd": "repair", "building": b["id"], "on": False}, key=key))
                    n += 1
                continue
            if b["id"] in self.sold:
                continue
            if (b["hp"] < p.repair_below and v.money > p.repair_min_cash and not self.emergency) or \
                    (b["type"] == "fact" and b["hp"] < 50):
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
        self._idle_harvesters(v, out)

    def _idle_harvesters(self, v, out) -> None:
        p = self.pol.p
        led = self.pol.ledger
        idle_harv = [u["id"] for u in v.units if u["type"] == "harv" and u["idle"]
                     and led.unit_ok(u["id"], ("harvest",), True, min_gap=50, same_gap=125)]
        if p.harv_spread and v.have.get("proc", 0) > 0:
            try:
                done = set(self.pol.harv.spread(v, out, idle_harv[:10]))
            except Exception as e:                         # noqa: BLE001 - plain harvest orders still go out
                self.pol.note_exception("harv_spread", e)
                done = set()
            idle_harv = [i for i in idle_harv if i not in done]
        if idle_harv and (v.have.get("proc", 0) > 0):
            out.append(Cand({"cmd": "harvest", "units": idle_harv[:50]}, sig=("harvest",)))
