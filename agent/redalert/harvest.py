"""v4.6 "harvesters first" (harv_first, harv_spread; docs/redalert_design.md section 17).

Real matches (10-08..10-11, sidestep, 5000 cash): our cash sat at 0 from about 2:00 while only 2 harvesters worked
until 5-6 minutes; a harvester pays for itself in under two minutes (real recordings: about 1500-1700 credits a minute
each next to a fresh field). So once the war factory stands, on maps where the enemy army walks far (spawn-to-spawn
ground path >= harv_min_path: shadowfiend-ii; on the short pool maps v4.5 plays order for order):
- a harvester target over time (4 by harv_t4, 6 by harv_t6, 8 by harv_t8), capped by the ore near our spawn;
- a refinery more when the target exceeds harv_per_proc harvesters per refinery (the third one before the depot, a
  fourth after it), placed at the ore field near our base with the most ore per refinery;
- spending serialised behind it: while harvesters in production lag the target and money is short, and nothing is
  attacking, no new infantry beyond serial_infantry, no tower beyond what a threat at home asks for, no second war
  factory; the Vehicle queue builds harvesters first. A threat at home worth serial_threat, a harvester or building
  hit, or an enemy army worth serial_half on our half ends the serialisation at once (the v4.5 rules take over);
  rush / early-army signs (v4.5's) before 7:00 switch harvesters first off for the match.
- harv_weap2: a second war factory once 5 harvesters work and money floats (one factory cannot spend the income).
- harv_spread (own switch): idle harvesters go to the field with the most ore per harvester next to one of our
  refineries, and one harvester moves on when more than harv_share work the field of one refinery.
Pure functions of the view plus a little memory; never raises into the policy (the callers catch)."""
from __future__ import annotations

import math

from .world import dist, is_combat

AIR = frozenset({"heli", "mh60", "yak", "mig", "hind", "badr", "u2", "tran"})
FIELD_LINK = 2              # ore cells this close (Chebyshev) belong to one field
CAP_RADIUS = 15             # fields reaching within this of our spawn set the harvester cap
LEAD_S = 30.0               # the target is met when the harvesters are alive: queue them this much earlier
SERIAL_SPARE = 300          # money beyond what the harvesters / refinery in production still cost: no need to wait
SUPPRESS_UNTIL_S = 420      # rush / early-army signs before this switch harvesters first off for the match
BUILD_RADIUS = 15.0         # refinery cells kept within this of the yard (engine: 16 from the construction yard)
FIELD_NEAR_PROC = 9         # a field "belongs" to a refinery when one of its cells is this close to the dock
HIT_RECENT_TICKS = 10 * 25  # a harvester / building hit this recently ends the serialisation
HALF_RECENT_TICKS = 30 * 25
REBALANCE_GAP_TICKS = 15 * 25
WEAP_TO_FIRST_HARV_S = 150  # before the war factory only refineries bring harvesters: 2 from the opening


def fields(st) -> list[dict]:
    """Ore fields of the starting map picture (cached on the Static): cells, gem cells, centre."""
    got = getattr(st, "_ra_fields", None)
    if got is not None:
        return got
    out: list[dict] = []
    cells = set(st.ore_cells or [])
    gems = set(getattr(st, "gem_cells", None) or [])
    seen: set = set()
    for c in sorted(cells):
        if c in seen:
            continue
        comp, stack = [], [c]
        seen.add(c)
        while stack:
            x, y = stack.pop()
            comp.append((x, y))
            for dy in range(-FIELD_LINK, FIELD_LINK + 1):
                for dx in range(-FIELD_LINK, FIELD_LINK + 1):
                    n = (x + dx, y + dy)
                    if n in cells and n not in seen:
                        seen.add(n)
                        stack.append(n)
        cx = sum(p[0] for p in comp) / len(comp)
        cy = sum(p[1] for p in comp) / len(comp)
        g = sum(1 for p in comp if p in gems)
        out.append({"i": len(out), "cells": comp, "n": len(comp), "gems": g, "value": len(comp) + g,
                    "centre": (cx, cy)})
    st._ra_fields = out
    return out


class Harvest:
    def __init__(self, pol):
        self.pol = pol
        self._tick = None
        self._serial = False
        self.serial_ticks = 0
        self._prev_tick = None
        self.at: dict[int, int] = {}          # game second mark -> harvesters then (180 / 270 / 390)
        self.procs_built = 0
        self.rebalances = 0
        self.spreads = 0
        self._rebal_tick = -10 ** 9
        self._cap = None
        self.ended_by: str | None = None      # what ended the serialisation last (for the event log)
        self.suppressed = False               # early army / rush signs seen: off for the match
        self._path_len = None

    # ---- numbers ------------------------------------------------------------------------------------------------
    def on(self) -> bool:
        return bool(self.pol.p.harv_first)

    def cap(self, v) -> int:
        if self._cap is not None:
            return self._cap
        p = self.pol.p
        hi = int(p.harv_cap)
        st = v.st
        spawn = st.my_spawn or v.base
        if not st.grid_ok or spawn is None:
            self._cap = min(hi, 8)
            return self._cap
        # every field with a cell within CAP_RADIUS of our spawn counts whole (gems twice)
        n = sum(f["value"] for f in fields(st) if any(dist(c, spawn) <= CAP_RADIUS for c in f["cells"]))
        self._cap = max(4, min(hi, int(round(n / max(1.0, float(p.harv_cells_per))))))
        return self._cap

    def want(self, v, lead_s: float = 0.0) -> int:
        """Harvesters we want alive or in production now (lead_s later): the target curve LEAD_S ahead, since one
        takes 26 s to build."""
        return min(self.cap(v), int(math.floor(self.goal(v, lead_s) + 1e-9)))

    def goal(self, v, lead_s: float = 0.0) -> float:
        """The target curve (not rounded, not capped) at game second t + lead_s + LEAD_S."""
        p = self.pol.p
        t = v.game_s + lead_s + LEAD_S
        pts = [(WEAP_TO_FIRST_HARV_S, 2.0), (float(p.harv_t4), 4.0), (float(p.harv_t6), 6.0), (float(p.harv_t8), 8.0)]
        if t <= pts[0][0]:
            w = 2.0
        elif t >= pts[-1][0]:
            w = 8.0 + max(0.0, (t - pts[-1][0]) / max(1.0, pts[-1][0] - pts[-2][0])) * 2.0
        else:
            w = 2.0
            for (t0, w0), (t1, w1) in zip(pts, pts[1:]):
                if t0 <= t <= t1:
                    w = w0 + (w1 - w0) * (t - t0) / max(1.0, t1 - t0)
                    break
        return w

    def count(self, v) -> int:
        led = self.pol.ledger
        return v.unit_count.get("harv", 0) + v.in_prod.get("harv", 0) + (1 if led.is_pending(("train", "harv")) else 0)

    def procs(self, v) -> int:
        return self.pol.econ.role_count(v, "proc")

    def path_len(self, v) -> float:
        """Length of the ground path between the spawns (cells, diagonals 1.41): how far an enemy army walks."""
        if self._path_len is None:
            path = v.st.path or []
            self._path_len = sum(dist(a, b) for a, b in zip(path, path[1:])) if v.st.ready else 0.0
        return self._path_len

    def map_ok(self, v) -> bool:
        """Greed only where the enemy army walks far (harv_min_path; pool maps: shadowfiend-ii 92 yes, sidestep 55 and
        trapped 57 no). Dev seeds 601-608: on sidestep v4.5 wins many nash games with its 6-8 minute attack, which
        harvesters first delays (h5: 1 better / 5 worse); on shadowfiend-ii harvesters first was 9 better / 4 worse."""
        return self.path_len(v) >= float(self.pol.p.harv_min_path)

    def engaged(self, v) -> bool:
        """harv_first applies to this match: on, a long map, and no rush / early-army sign before SUPPRESS_UNTIL_S
        (sticky: then v4.5 plays the rest of the match, its third-refinery rule included). (Dev seeds 601006 /
        604015: with an early enemy army seen, harvesters and a third refinery before the depot left 20 rifles and no
        tank when the 4-minute attack came; 606038: the rush flag cleared at 220 s, harvesters first came back and the
        tank push at 290 s met rifles only.)"""
        if not self.on() or self.suppressed or not self.map_ok(v):
            return False
        army = self.pol.army
        if (army.rush or army.early_army or army.two_barracks) and v.game_s < SUPPRESS_UNTIL_S:
            self.suppressed = True
            self.pol.log_event(f"harvesters first off at {v.game_s:.0f}s: early army / rush signs")
            return False
        return True

    def active(self, v) -> bool:
        """The harvester target and its refinery apply now: engaged and no rush sign / real attack at the moment."""
        if not self.engaged(v):
            return False
        army = self.pol.army
        return not (army.rush or army.early_army or army.two_barracks or army.real_attack(v))

    # ---- serialisation -------------------------------------------------------------------------------------------
    def serialized(self, v) -> bool:
        """Below the harvester target with the war factory up, and no real threat: units and towers wait."""
        if self._tick == v.tick:
            return self._serial
        self._tick = v.tick
        self._serial = self._compute(v)
        if self._prev_tick is not None and self._serial:
            self.serial_ticks += max(0, v.tick - self._prev_tick)
        self._prev_tick = v.tick
        return self._serial

    def _compute(self, v) -> bool:
        if not self.active(v) or v.fact is None or not v.have.get("weap"):
            return False
        if v.remaining <= 240 or self.pol.econ.emergency:
            return False
        if v.unit_count.get("harv", 0) >= self.want(v):
            return False                                  # (the ones in production still need the money: alive count)
        rules = v.st.rules
        need = sum(rules.cost(it["item"]) * max(0.0, 1.0 - it["progress"]) for q in ("Vehicle", "Building")
                   for it in v.queue_items(q) if it["item"] in ("harv", "proc") and not it["ready"])
        if v.money >= need + SERIAL_SPARE:
            return False                                  # money for everything: nothing to wait for
        why = self.threat(v)
        if why:
            if why != self.ended_by:
                self.ended_by = why
                self.pol.log_event(f"harvesters first paused at {v.game_s:.0f}s: {why}")
            return False
        self.ended_by = None
        return True

    def threat(self, v) -> str | None:
        """A reason to drop the serialisation now, or None."""
        p = self.pol.p
        army = self.pol.army
        if army.rush or army.early_army or army.two_barracks:
            return "early rush signs"
        if army.real_attack(v):
            return "real attack"
        if army.threatened and army.threat_value >= p.serial_threat:
            return f"threat at home {army.threat_value:.0f}"
        hits = [t for t in army.harv_hit.values() if v.tick - t <= HIT_RECENT_TICKS]
        if hits:
            return "harvester hit"
        if army.blind_point is not None and v.tick - army.blind_tick <= HIT_RECENT_TICKS:
            return "building or unit hit near the base"
        if v.base is not None and v.st.enemy_spawn is not None:
            rules = v.st.rules
            es = v.st.enemy_spawn
            half = 0.0
            for e in army.enemy_units.values():
                if v.tick - e["tick"] > HALF_RECENT_TICKS or not is_combat(e["type"], rules) or e["type"] in AIR:
                    continue
                ep = (e["x"], e["y"])
                if dist(ep, v.base) < dist(ep, es):
                    half += army.cost(e["type"])
            if half >= p.serial_half:
                return f"enemy army on our half {half:.0f}"
        return None

    # ---- refineries ----------------------------------------------------------------------------------------------
    def need_proc(self, v) -> bool:
        """A refinery more: the target in a minute and a half exceeds harv_per_proc per refinery (counting the ones
        in production), war factory up, not under a real attack."""
        if not self.on() or not v.have.get("weap") or "proc" not in v.avail:
            return False
        p = self.pol.p
        procs = self.procs(v)
        if procs < 1 or procs >= int(p.max_procs) or v.remaining <= 300:
            return False
        if not self.active(v) or self.pol.econ.emergency:
            return False
        return min(float(self.cap(v)), self.goal(v, 90.0)) > p.harv_per_proc * procs + 1e-9

    def proc_spot(self, v):
        """Top-left for a new refinery: next to the field (within reach of the yard) with the most ore per refinery
        already serving it, the nearest such spot. None: let the old rule / the engine decide."""
        econ = self.pol.econ
        st = v.st
        if v.fact is None or not st.grid_ok:
            return None
        yard = (v.fact["x"] + 1.5, v.fact["y"] + 2.0)
        docks = [(b["x"] + 1, b["y"] + 1) for b in v.buildings if b["type"] == "proc"]
        cand = []
        for f in fields(st):
            near = [c for c in f["cells"] if dist(c, yard) <= BUILD_RADIUS + 2]
            if len(near) < 4:
                continue
            serving = sum(1 for d in docks if any(dist(d, c) <= FIELD_NEAR_PROC - 3 for c in f["cells"]))
            score = f["value"] / (1.0 + serving) - 0.5 * min(dist(c, yard) for c in near)
            cand.append((-score, f["i"], near))
        cand.sort()
        rules = st.rules
        w, h = rules.footprint("proc")
        for _s, _i, near in cand[:3]:
            cx = sum(c[0] for c in near) / len(near)
            cy = sum(c[1] for c in near) / len(near)
            spot = econ._best_spot(v, "proc", (cx, cy), 7, prefer_dist=3.5)
            if spot is None:
                continue
            corners = [(spot[0], spot[1]), (spot[0] + w - 1, spot[1]), (spot[0], spot[1] + h - 1),
                       (spot[0] + w - 1, spot[1] + h - 1)]
            if all(dist(c, yard) <= BUILD_RADIUS for c in corners):
                return spot
        return None

    # ---- harvester distribution (harv_spread) ----------------------------------------------------------------------
    def _field_of(self, v):
        """Per field near one of our refineries: the harvesters working it (nearest field within 6 cells)."""
        st = v.st
        docks = [(b["x"] + 1, b["y"] + 1) for b in v.buildings if b["type"] == "proc"]
        fs = [f for f in fields(st) if any(any(dist(d, c) <= FIELD_NEAR_PROC for c in f["cells"]) for d in docks)]
        work: dict[int, list] = {f["i"]: [] for f in fs}
        for u in v.units:
            if u["type"] != "harv":
                continue
            hp = (u["x"], u["y"])
            best = None
            for f in fs:
                d = min(dist(hp, c) for c in f["cells"])
                if d <= 6 and (best is None or d < best[0]):
                    best = (d, f["i"])
            if best is not None:
                work[best[1]].append(u)
        return fs, work

    def spread(self, v, out, idle_ids) -> list:
        """Harvest orders: idle harvesters to the best field near our refineries (ore per harvester, gems count twice),
        and one rebalance every REBALANCE_GAP_TICKS. Returns the orders' unit ids handled (the caller sends a plain
        harvest for the rest)."""
        from .orders import Cand
        led = self.pol.ledger
        handled = []
        if not idle_ids and v.tick - self._rebal_tick < REBALANCE_GAP_TICKS:
            return handled
        fs, work = self._field_of(v)
        if not fs:
            return handled
        load = {f["i"]: len(work[f["i"]]) for f in fs}

        def best_field(exclude=None):
            cands = [f for f in fs if f["i"] != exclude]
            if not cands:
                return None
            return max(cands, key=lambda f: (f["value"] / (1.0 + load[f["i"]]), -f["i"]))
        for uid in idle_ids:
            u = v.unit_by_id.get(uid)
            if u is None:
                continue
            f = best_field()
            if f is None:
                break
            cell = min(f["cells"], key=lambda c: (dist(c, (u["x"], u["y"])), c))
            if not st_ok(v, cell):
                continue
            out.append(Cand({"cmd": "harvest", "units": [uid], "to": [int(cell[0]), int(cell[1])]},
                            sig=("harvest", f["i"])))
            load[f["i"]] += 1
            handled.append(uid)
            self.spreads += 1
        if v.tick - self._rebal_tick >= REBALANCE_GAP_TICKS and len(fs) >= 2:
            crowded = max(fs, key=lambda f: (load[f["i"]], -f["i"]))
            if load[crowded["i"]] > int(self.pol.p.harv_share):
                f = best_field(exclude=crowded["i"])
                if f is not None and load[f["i"]] + 1 < load[crowded["i"]] and \
                        f["value"] / (1.0 + load[f["i"]]) > crowded["value"] / (1.0 + load[crowded["i"]] - 1):
                    movers = [u for u in work[crowded["i"]] if u["id"] not in handled
                              and led.unit_ok(u["id"], ("harvest", f["i"]), u["idle"], min_gap=125, same_gap=250)]
                    if movers:
                        tgt = min(f["cells"], key=lambda c: (dist(c, crowded["centre"]), c))
                        u = min(movers, key=lambda u: (dist((u["x"], u["y"]), tgt), u["id"]))
                        if st_ok(v, tgt):
                            out.append(Cand({"cmd": "harvest", "units": [u["id"]], "to": [int(tgt[0]), int(tgt[1])]},
                                            sig=("harvest", f["i"])))
                            handled.append(u["id"])
                            self.rebalances += 1
                            self._rebal_tick = v.tick
        return handled

    # ---- metrics -------------------------------------------------------------------------------------------------
    def track(self, v) -> None:
        for mark in (180, 270, 390):
            if mark not in self.at and v.game_s >= mark:
                self.at[mark] = v.unit_count.get("harv", 0)

    def summary(self) -> dict:
        return {"harv_180": self.at.get(180), "harv_270": self.at.get(270), "harv_390": self.at.get(390),
                "serial_s": round(self.serial_ticks / 25.0, 1), "harv_spreads": self.spreads,
                "harv_rebalances": self.rebalances}


def st_ok(v, cell) -> bool:
    return v.st.in_bounds(cell)
