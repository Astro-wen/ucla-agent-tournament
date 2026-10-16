"""Stronger scripted Red Alert opponents for the local simulator (from the 10-08 review of the bot). Same runner
contract as local/ra_bots.py; local/ra_platform.py takes them by name (--b tanks,swarm,pro,raider,hider).

    tanks   fast war factory (barracks later), harvesters, continuous main tanks, attack waves of 6+ from ~4-5 min,
            explicit focus on defenses / production / refinery / power, home defense
    swarm   2-3 barracks, mass rifles (later rockets), stance attack_anything, first wave of 10 at ~100 s, then
            constant streaming; targets power plants first
    pro     balanced, like OpenRA's normal AI: double refinery, 2 harvesters per refinery, war factory + depot,
            2 towers, mixed army, attacks when its army is worth >= 5000 with 8+ units, defends home with everything
    raider  economy + early jeeps / flak trucks that hunt harvesters (avoid towers), towers at home, big push at 11 min
    hider   drives its MCV to a corner far from both spawns, builds power, barracks, 4 towers, rifles at home
    nash    the real loss of 10-11 as a script: double refinery, rifle / rocket picket a third of the way to us from
            the start (kills scouts), infantry probe at ~5:30, main push with 3+ heavy tanks from ~6:30
    riffo   the real loss d527c9fa (10-13) as a script: 4 towers + main tanks + artillery + rockets that stay home,
            first push at ~13:30 or at 14000 army value
    nashball the real loss f5ce0e92 (10-13) as a script: the whole army (rifles, rockets, heavy tanks) at a forward
            post 40% of the way to us, counter-push at 1.4x our largest army seen or from 11:00
    tom     the real losses of 10-14 / 10-15 (Tom_2026) as a script: fast war factory, continuous tanks with a
            rifle-heavy infantry mix, harvester-hunting jeeps / flak trucks, artillery later; probe at ~5:00, attacks
            from ~6:30, the big push at 9000+ or from 9:30
    tom2    the upgraded Tom_2026 of 10-15 (real losses 335d2a62 / 21bf0e2a): war factory by ~2:00, 4 harvesters,
            2 barracks, no towers, tanks without pause, attacks from ~4:45 and keeps feeding them; the army waits at
            home (our scout dies in their base: the early-army rule fires, as in every real game of 10-13..10-15)
    tom2p   tom2 with the army waiting a quarter of the way to us (scouts die on the road)
"""
from __future__ import annotations

import math

from .ra_bots import Base

DEF_PRIO = ("tsla", "gun", "pbox", "hbox", "ftur")
PROD = ("weap", "tent", "barr", "kenn")
TOWERS = {"allies": ("pbox", "gun"), "soviet": ("ftur", "tsla")}


def d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Strong(Base):
    style = "strong"
    STANCE = "attack_anything"

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.att: set[int] = set()
        self.stanced: set[int] = set()
        self.waves = 0

    def game_s(self, raw, obs):
        return (raw.get("time") or {}).get("game_seconds") or obs["tick"] / 25

    def stance(self, army):
        fresh = [u["id"] for u in army if u["id"] not in self.stanced]
        if fresh:
            self.stanced.update(fresh[:50])
            return {"cmd": "set_stance", "units": fresh[:50], "stance": self.STANCE}
        return None

    def pick_target(self, obs, c, prio):
        blds = [b for b in obs.get("visible_enemy_buildings") or [] if not b.get("wreck")]
        if not blds:
            return None

        def key(b):
            t = b["type"]
            for i, group in enumerate(prio):
                if t in group:
                    return (i, d((b["x"], b["y"]), c), b["id"])
            return (len(prio), d((b["x"], b["y"]), c), b["id"])
        return min(blds, key=key)

    def home_defense(self, obs, units, radius=14):
        bc = obs.get("base_center") or [0, 0]
        near = [e for e in obs.get("visible_enemies") or [] if not e.get("wreck")
                and d((e["x"], e["y"]), bc) <= radius and e.get("type") not in ("harv", "mcv")]
        if near and units:
            t = min(near, key=lambda e: (d((e["x"], e["y"]), bc), e["id"]))
            return {"cmd": "attack_move", "units": [u["id"] for u in units][:50], "to": [t["x"], t["y"]]}
        return None

    def push(self, obs, la, attackers, prio):
        """Attackers: idle ones near visible buildings attack by priority; idle ones elsewhere attack_move on."""
        out = []
        idle = [u for u in attackers if u.get("idle")]
        if not idle:
            return out
        cx = sum(u["x"] for u in idle) / len(idle)
        cy = sum(u["y"] for u in idle) / len(idle)
        enemies = [e for e in obs.get("visible_enemies") or [] if not e.get("wreck")
                   and e.get("type") not in ("harv",) and d((e["x"], e["y"]), (cx, cy)) <= 10]
        if enemies:
            t = min(enemies, key=lambda e: (d((e["x"], e["y"]), (cx, cy)), e["id"]))
            out.append({"cmd": "attack_move", "units": [u["id"] for u in idle][:50], "to": [t["x"], t["y"]]})
            return out
        t = self.pick_target(obs, (cx, cy), prio)
        if t is not None:
            out.append({"cmd": "attack", "units": [u["id"] for u in idle][:50], "target": t["id"]})
        else:
            out.append({"cmd": "attack_move", "units": [u["id"] for u in idle][:50],
                        "to": la.get("enemy_base") or [0, 0]})
        return out


class TankRush(Strong):
    style = "tanks"

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        soviet = self.side(obs) == "soviet"
        gs = self.game_s(raw, obs)
        money = self.money(obs)
        bar = "barr" if soviet else "tent"
        plan = [("powr", 1), ("proc", 1), (bar, 1), ("powr", 2), ("weap", 1), ("proc", 2), ("powr", 3), ("fix", 1),
                ("powr", 4)]
        if money > 2500:
            plan += [("weap", 2), ("powr", 5), ("proc", 3), ("powr", 6)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        avail = obs.get("available_production") or []
        inf = sum(1 for u in self.fighters(obs) if u.get("type") in ("e1", "e3"))
        if self.count(obs, bar) and not self.queue_busy(obs, "Infantry") and inf < 6 and money >= 300:
            out.append(self.train(obs, "e1", 3))
        if self.count(obs, "weap") and not self.queue_busy(obs, "Vehicle"):
            procs = self.count(obs, "proc")
            if self.count(obs, "harv") < min(4, 2 * procs) and money >= 1100:
                out.append(self.train(obs, "harv"))
            else:
                main = "3tnk" if soviet else "2tnk"
                light = "apc" if soviet else "1tnk"
                item = main if main in avail else light
                if money >= 600:
                    out.append(self.train(obs, item))
        army = [u for u in self.fighters(obs)]
        alive = {u["id"] for u in army}
        self.att &= alive
        st = self.stance(army)
        if st:
            out.append(st)
        home = [u for u in army if u["id"] not in self.att]
        hd = self.home_defense(obs, home)
        if hd:
            out.append(hd)
        elif (len(home) >= 6 or (gs >= 420 and len(home) >= 4)) or (self.att and len(home) >= 3):
            self.waves += 1
            out.append({"cmd": "attack_move", "units": [u["id"] for u in home][:50],
                        "to": la.get("enemy_base") or [0, 0]})
            self.att.update(u["id"] for u in home[:50])
        out += self.push(obs, la, [u for u in army if u["id"] in self.att],
                         (DEF_PRIO, PROD, ("proc",), ("powr", "apwr"), ("fact",)))
        return out


class Swarm(Strong):
    style = "swarm"

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        soviet = self.side(obs) == "soviet"
        bar = "barr" if soviet else "tent"
        gs = self.game_s(raw, obs)
        plan = [("powr", 1), (bar, 1), (bar, 2), ("proc", 1), ("powr", 2), (bar, 3), ("proc", 2), ("powr", 3)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        if self.count(obs, bar) and self.queued(obs, "e1") + self.queued(obs, "e3") < 5 and self.money(obs) >= 100:
            item = "e3" if (gs > 240 and self.rng.random() < 0.3) else "e1"
            out.append(self.train(obs, item, 5))
        army = self.fighters(obs)
        alive = {u["id"] for u in army}
        self.att &= alive
        st = self.stance(army)
        if st:
            out.append(st)
        home = [u for u in army if u["id"] not in self.att]
        if (gs >= 100 and len(home) >= 10) or (gs >= 150 and len(home) >= 8) or (self.att and len(home) >= 4):
            self.waves += 1
            out.append({"cmd": "attack_move", "units": [u["id"] for u in home][:50],
                        "to": la.get("enemy_base") or [0, 0]})
            self.att.update(u["id"] for u in home[:50])
        else:
            hd = self.home_defense(obs, home)
            if hd:
                out.append(hd)
        out += self.push(obs, la, [u for u in army if u["id"] in self.att],
                         (("powr", "apwr"), PROD, ("proc",), ("fact",), DEF_PRIO))
        return out


class Pro(Strong):
    style = "pro"
    COST = {"e1": 100, "e3": 300, "1tnk": 700, "2tnk": 850, "3tnk": 1150, "apc": 850, "ftrk": 600, "jeep": 500}

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        side = self.side(obs)
        soviet = side == "soviet"
        bar = "barr" if soviet else "tent"
        gs = self.game_s(raw, obs)
        money = self.money(obs)
        plan = [("powr", 1), (bar, 1), ("proc", 1), ("powr", 2), ("proc", 2), ("weap", 1), ("powr", 3), ("fix", 1),
                ("powr", 4)]
        if money > 2000 and gs > 300:
            plan += [("weap", 2), ("proc", 3), ("powr", 5)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        avail = obs.get("available_production") or []
        towers = TOWERS[side]
        if self.count(obs, "weap") and not self.queue_busy(obs, "Defense") and \
                sum(self.count(obs, t) for t in towers) < 2 and money > 900:
            out.append(self.build(obs, towers[0]))
        if self.count(obs, bar) and not self.queue_busy(obs, "Infantry") and money > (300 if gs < 240 else 900):
            out.append(self.train(obs, "e3" if self.rng.random() < 0.3 else "e1", 3))
        if self.count(obs, "weap") and not self.queue_busy(obs, "Vehicle"):
            procs = self.count(obs, "proc")
            if self.count(obs, "harv") < min(6, 2 * procs) and money >= 1100:
                out.append(self.train(obs, "harv"))
            elif money >= 700:
                main = "3tnk" if soviet else "2tnk"
                light = "apc" if soviet else "1tnk"
                out.append(self.train(obs, main if main in avail else light))
        army = self.fighters(obs)
        alive = {u["id"] for u in army}
        self.att &= alive
        st = self.stance(army)
        if st:
            out.append(st)
        home = [u for u in army if u["id"] not in self.att]
        hd = self.home_defense(obs, army if not self.att else home, radius=16)
        if hd:
            out.append(hd)
        else:
            val = sum(self.COST.get(u["type"], 500) for u in home)
            if (val >= 5000 and len(home) >= 8) or (self.att and len(home) >= 5):
                self.waves += 1
                out.append({"cmd": "attack_move", "units": [u["id"] for u in home][:50],
                            "to": la.get("enemy_base") or [0, 0]})
                self.att.update(u["id"] for u in home[:50])
        out += self.push(obs, la, [u for u in army if u["id"] in self.att],
                         (DEF_PRIO, PROD, ("proc",), ("powr", "apwr"), ("fact",)))
        return out


class Raider(Strong):
    style = "raider"

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.raiders: set[int] = set()

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        side = self.side(obs)
        soviet = side == "soviet"
        bar = "barr" if soviet else "tent"
        gs = self.game_s(raw, obs)
        money = self.money(obs)
        plan = [("powr", 1), ("proc", 1), (bar, 1), ("weap", 1), ("powr", 2), ("proc", 2), ("powr", 3), ("fix", 1),
                ("powr", 4)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        towers = TOWERS[side]
        if self.count(obs, bar) and not self.queue_busy(obs, "Defense") and \
                sum(self.count(obs, t) for t in towers) < 3 and money > 700:
            out.append(self.build(obs, towers[0]))
        if self.count(obs, bar) and not self.queue_busy(obs, "Infantry") and money > 500:
            out.append(self.train(obs, "e1", 2))
        fast = "ftrk" if soviet else "jeep"
        avail = obs.get("available_production") or []
        units = obs.get("units") or []
        alive_ids = {u["id"] for u in units if not u.get("wreck")}
        self.raiders &= alive_ids
        if self.count(obs, "weap") and not self.queue_busy(obs, "Vehicle"):
            if self.count(obs, "harv") < 2 * self.count(obs, "proc") and money >= 1100:
                out.append(self.train(obs, "harv"))
            elif self.count(obs, fast) < 5 and money >= 500:
                out.append(self.train(obs, fast))
            elif money >= 900:
                main = "3tnk" if soviet else "2tnk"
                out.append(self.train(obs, main if main in avail else ("apc" if soviet else "1tnk")))
        for u in units:
            if u.get("type") == fast and not u.get("wreck"):
                self.raiders.add(u["id"])
        raiders = [u for u in units if u["id"] in self.raiders]
        if raiders:
            cx = sum(u["x"] for u in raiders) / len(raiders)
            cy = sum(u["y"] for u in raiders) / len(raiders)
            ens = [e for e in obs.get("visible_enemies") or [] if not e.get("wreck")]
            harvs = [e for e in ens if e.get("type") == "harv"]
            towers_seen = [b for b in obs.get("visible_enemy_buildings") or [] if b.get("type") in DEF_PRIO]
            danger = [b for b in towers_seen if d((b["x"], b["y"]), (cx, cy)) <= 8]
            ids = [u["id"] for u in raiders][:50]
            if danger:
                if self.once(obs, "raid-back", 50):
                    out.append({"cmd": "move", "units": ids, "to": obs.get("base_center") or [5, 5]})
            elif harvs:
                t = min(harvs, key=lambda e: (d((e["x"], e["y"]), (cx, cy)), e["id"]))
                if self.once(obs, "raid-hit", 15):
                    out.append({"cmd": "attack", "units": ids, "target": t["id"]})
            elif len(raiders) >= 3 and all(u.get("idle") for u in raiders) and self.once(obs, "raid-go", 75):
                # the enemy's ore: a little off its spawn
                eb = la.get("enemy_base") or [0, 0]
                tx = int(eb[0] + self.rng.choice((-8, 8)))
                ty = int(eb[1] + self.rng.choice((-8, 8)))
                m = obs.get("map") or {}
                tx = max(2, min(int(m.get("width") or 90) - 3, tx))
                ty = max(2, min(int(m.get("height") or 90) - 3, ty))
                out.append({"cmd": "attack_move", "units": ids, "to": [tx, ty]})
        army = [u for u in self.fighters(obs) if u["id"] not in self.raiders]
        alive = {u["id"] for u in army}
        self.att &= alive
        home = [u for u in army if u["id"] not in self.att]
        hd = self.home_defense(obs, home)
        if hd:
            out.append(hd)
        elif gs >= 660 and len(home) >= 8:
            out.append({"cmd": "attack_move", "units": [u["id"] for u in home][:50],
                        "to": la.get("enemy_base") or [0, 0]})
            self.att.update(u["id"] for u in home[:50])
        out += self.push(obs, la, [u for u in army if u["id"] in self.att],
                         (DEF_PRIO, PROD, ("proc",), ("powr", "apwr"), ("fact",)))
        return out


class Hider(Strong):
    style = "hider"

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.spot = None
        self.bad: set = set()
        self.tries = 0

    def decide(self, raw, obs, la):
        out = []
        blds = obs.get("buildings") or []
        units = [u for u in obs.get("units") or [] if not u.get("wreck")]
        mcv = next((u for u in units if u.get("type") == "mcv"), None)
        side = self.side(obs)
        bar = "barr" if side == "soviet" else "tent"
        if mcv is not None and not any(b.get("type") == "fact" for b in blds):
            eb = la.get("enemy_base") or [0, 0]
            me = (mcv["x"], mcv["y"])
            if self.spot is None:
                self.spot = self._hide_spot(raw, eb, me)
            if d(me, self.spot) > 1.0:
                if mcv.get("idle") and self.once(obs, "drive", 25):
                    out.append({"cmd": "move", "units": [mcv["id"]], "to": list(self.spot)})
            elif self.once(obs, "deploy", 50):
                self.tries += 1
                if self.tries > 2:
                    self.bad.add(self.spot)
                    self.spot = self._hide_spot(raw, eb, me, near=self.spot)
                    self.tries = 0
                out.append({"cmd": "deploy", "units": [mcv["id"]]})
            return out
        out += self.basics(obs, la)
        plan = [("powr", 1), (bar, 1), ("proc", 1), ("powr", 2), ("powr", 3)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        towers = TOWERS[side]
        if self.count(obs, bar) and not self.queue_busy(obs, "Defense") and \
                sum(self.count(obs, t) for t in towers) < 4 and self.money(obs) > 700:
            out.append(self.build(obs, towers[0]))
        army = self.fighters(obs)
        if self.count(obs, bar) and not self.queue_busy(obs, "Infantry") and len(army) < 12 and self.money(obs) > 400:
            out.append(self.train(obs, "e1", 2))
        hd = self.home_defense(obs, army)
        if hd:
            out.append(hd)
        return out

    def _hide_spot(self, raw, eb, me, near=None):
        """A buildable 3x4 (+1) spot far from both spawns (from the map picture), or near `near` when retrying."""
        import json
        from pathlib import Path

        from agent.redalert.world import Static
        cfg = json.loads((Path(__file__).resolve().parents[1] / "data" / "red_alert_config.json").read_text())
        st = Static(raw, cfg)
        ore = set(st.ore_cells)
        best = None
        for y in range(st.by0 + 2, st.by1 - 4):
            for x in range(st.bx0 + 2, st.bx1 - 3):
                if (x, y) in self.bad:
                    continue
                ok = all(st.can_build(xx, yy) and (xx, yy) not in ore
                         for yy in range(y - 2, y + 4) for xx in range(x - 2, x + 3)) if st.grid_ok else True
                if not ok:
                    continue
                score = -d((x, y), near) if near is not None else min(d((x, y), eb), d((x, y), me))
                if best is None or score > best[0]:
                    best = (score, (x, y))
        return best[1] if best else (me[0], me[1])


class Nash(Strong):
    """The real loss of 10-11 (cb4bfc36, "Steve Nash Equilibrium", Soviet vs Soviet on sidestep), as a script: a
    double-refinery economy with a war factory and depot by ~4 min; its rifle / rocket infantry stands at a picket a
    third of the way to us from the start (it kills an early scout there); an infantry probe at ~5:30 when 8+ stand
    there; the main push from ~6:30 with 3+ heavy tanks and the infantry; reinforcing waves after; home defense."""
    style = "nash"

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.probed = False
        self.pushed = False

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        soviet = self.side(obs) == "soviet"
        bar = "barr" if soviet else "tent"
        gs = self.game_s(raw, obs)
        money = self.money(obs)
        plan = [("powr", 1), (bar, 1), ("proc", 1), ("powr", 2), ("proc", 2), ("weap", 1), ("powr", 3), ("fix", 1),
                ("powr", 4)]
        if gs > 360 and money > 1500:
            plan += [("proc", 3), ("powr", 5), ("weap", 2), ("powr", 6)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        avail = obs.get("available_production") or []
        army = self.fighters(obs)
        inf = [u for u in army if u.get("type") in ("e1", "e2", "e3")]
        cap = 10 if gs < 240 else (14 if gs < 420 else 18)
        if self.count(obs, bar) and not self.queue_busy(obs, "Infantry") and len(inf) < cap and \
                money > (200 if gs < 180 else 600):
            n_e3 = sum(1 for u in inf if u.get("type") == "e3")
            out.append(self.train(obs, "e3" if n_e3 * 2 < len(inf) else "e1", 2))
        if self.count(obs, "weap") and not self.queue_busy(obs, "Vehicle"):
            procs = self.count(obs, "proc")
            main = "3tnk" if soviet else "2tnk"
            if self.count(obs, "harv") < min(5, 2 * procs) and money >= 1100:
                out.append(self.train(obs, "harv"))
            elif main in avail and money >= 1000:
                out.append(self.train(obs, main))
        alive = {u["id"] for u in army}
        self.att &= alive
        st = self.stance(army)
        if st:
            out.append(st)
        home = [u for u in army if u["id"] not in self.att]
        hd = self.home_defense(obs, home, radius=16)
        if hd:
            out.append(hd)
            return out
        bc = obs.get("base_center") or [0, 0]
        eb = la.get("enemy_base") or [0, 0]
        picket = [int(bc[0] + 0.33 * (eb[0] - bc[0])), int(bc[1] + 0.33 * (eb[1] - bc[1]))]
        tanks = [u for u in home if u.get("type") in ("3tnk", "2tnk", "1tnk", "4tnk")]
        hinf = [u for u in home if u not in tanks]
        go = None
        if not self.pushed and len(tanks) >= 3 and len(hinf) >= 6 and gs >= 390:
            self.pushed = True
            go = home
        elif self.pushed and len(home) >= 5:
            go = home
        elif not self.probed and gs >= 330 and len(hinf) >= 8:
            self.probed = True
            go = hinf
        if go:
            self.waves += 1
            out.append({"cmd": "attack_move", "units": [u["id"] for u in go][:50], "to": eb})
            self.att.update(u["id"] for u in go[:50])
        else:
            idle_inf = [u["id"] for u in hinf if u.get("idle") and d((u["x"], u["y"]), picket) > 4]
            if idle_inf and self.once(obs, "picket", 125):
                out.append({"cmd": "attack_move", "units": idle_inf[:50], "to": picket})
        out += self.push(obs, la, [u for u in army if u["id"] in self.att],
                         (DEF_PRIO, PROD, ("proc",), ("powr", "apwr"), ("fact",)))
        return out


class Riffo(Strong):
    """The real loss of 10-13 (d527c9fa, "Riffo", France vs our Germany on sidestep) as a script: double refinery,
    war factory, radar dome; four towers at home (anti-infantry and anti-vehicle in turn); main tanks, artillery
    (one per four tanks, range 12) and rocket infantry that stay home and fight anything within 20 cells of the base;
    the first push from ~13:30 or once the army at home is worth 14000; reinforcing waves after."""
    style = "riffo"

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.pushed = False

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        side = self.side(obs)
        soviet = side == "soviet"
        bar = "barr" if soviet else "tent"
        gs = self.game_s(raw, obs)
        money = self.money(obs)
        plan = [("powr", 1), (bar, 1), ("proc", 1), ("powr", 2), ("proc", 2), ("weap", 1), ("powr", 3), ("dome", 1),
                ("fix", 1), ("powr", 4)]
        if gs > 420 and money > 1500:
            plan += [("proc", 3), ("powr", 5), ("weap", 2), ("powr", 6)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        avail = obs.get("available_production") or []
        towers = ("ftur", "tsla") if soviet else ("pbox", "gun")
        have = sum(self.count(obs, t) for t in towers)
        if self.count(obs, "weap") and not self.queue_busy(obs, "Defense") and have < 4 and money > 700:
            pick = towers[have % 2] if towers[have % 2] in avail else towers[0]
            out.append(self.build(obs, pick))
        army = self.fighters(obs)
        inf = [u for u in army if u.get("type") in ("e1", "e3")]
        if self.count(obs, bar) and not self.queue_busy(obs, "Infantry") and \
                len(inf) < (8 if gs < 300 else 12) and money > 300:
            out.append(self.train(obs, "e3" if (gs > 240 and self.rng.random() < 0.6) else "e1", 2))
        if self.count(obs, "weap") and not self.queue_busy(obs, "Vehicle"):
            procs = self.count(obs, "proc")
            main = "3tnk" if soviet else "2tnk"
            art = "v2rl" if soviet else "arty"
            if self.count(obs, "harv") < min(6, 2 * procs) and money >= 1100:
                out.append(self.train(obs, "harv"))
            elif art in avail and self.count(obs, art) < 1 + self.count(obs, main) // 4 and money >= 900:
                out.append(self.train(obs, art))
            elif main in avail and money >= 900:
                out.append(self.train(obs, main))
            elif "1tnk" in avail and money >= 700:
                out.append(self.train(obs, "1tnk"))
        alive = {u["id"] for u in army}
        self.att &= alive
        st = self.stance(army)
        if st:
            out.append(st)
        home = [u for u in army if u["id"] not in self.att]
        hd = self.home_defense(obs, home, radius=20)
        if hd:
            out.append(hd)
        else:
            val = sum(Pro.COST.get(u["type"], 850) for u in home)
            if (not self.pushed and (gs >= 810 or val >= 14000) and len(home) >= 6) or (self.pushed and len(home) >= 6):
                self.pushed = True
                self.waves += 1
                out.append({"cmd": "attack_move", "units": [u["id"] for u in home][:50],
                            "to": la.get("enemy_base") or [0, 0]})
                self.att.update(u["id"] for u in home[:50])
        out += self.push(obs, la, [u for u in army if u["id"] in self.att],
                         (DEF_PRIO, PROD, ("proc",), ("powr", "apwr"), ("fact",)))
        return out


class NashBall(Strong):
    """The real loss of 10-13 (f5ce0e92, "Steve Nash Equilibrium", Russia vs our Ukraine on sidestep) as a script:
    double refinery; the whole army (many rifles and rockets, heavy tanks) stands together at a forward post 40% of the
    way to us and fights whatever comes there; a probe of 2 tanks at ~6:30; the counter-push once its army is worth
    1.4x the largest army of ours it saw in the last 90 s (and 6000+), or from 11:00; reinforcing waves after."""
    style = "nashball"

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.pushed = False
        self.probed = False
        self.seen: list[tuple[int, int]] = []          # (tick, value of our army in sight)

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        soviet = self.side(obs) == "soviet"
        bar = "barr" if soviet else "tent"
        gs = self.game_s(raw, obs)
        money = self.money(obs)
        plan = [("powr", 1), (bar, 1), ("proc", 1), ("powr", 2), ("proc", 2), ("weap", 1), ("powr", 3), ("fix", 1),
                ("powr", 4)]
        if gs > 360 and money > 1500:
            plan += [("proc", 3), ("powr", 5), (bar, 2), ("powr", 6)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        avail = obs.get("available_production") or []
        army = self.fighters(obs)
        inf = [u for u in army if u.get("type") in ("e1", "e2", "e3")]
        cap = 10 if gs < 240 else (20 if gs < 420 else 34)
        if self.count(obs, bar) and not self.queue_busy(obs, "Infantry") and len(inf) < cap and \
                money > (200 if gs < 180 else 500):
            n_e3 = sum(1 for u in inf if u.get("type") == "e3")
            out.append(self.train(obs, "e3" if n_e3 * 3 < len(inf) else "e1", 3))
        if self.count(obs, "weap") and not self.queue_busy(obs, "Vehicle"):
            procs = self.count(obs, "proc")
            main = "3tnk" if soviet else "2tnk"
            if self.count(obs, "harv") < min(6, 2 * procs) and money >= 1100:
                out.append(self.train(obs, "harv"))
            elif main in avail and money >= 1000:
                out.append(self.train(obs, main))
        alive = {u["id"] for u in army}
        self.att &= alive
        st = self.stance(army)
        if st:
            out.append(st)
        # the largest army of ours in sight over the last 90 s
        ours = sum(Pro.COST.get(e.get("type"), 0) for e in obs.get("visible_enemies") or [] if not e.get("wreck"))
        tick = obs["tick"]
        self.seen = [(t, x) for t, x in self.seen if tick - t <= 90 * 25] + [(tick, ours)]
        top = max(x for _t, x in self.seen)
        home = [u for u in army if u["id"] not in self.att]
        hd = self.home_defense(obs, home, radius=16)
        if hd:
            out.append(hd)
            return out
        bc = obs.get("base_center") or [0, 0]
        eb = la.get("enemy_base") or [0, 0]
        post = [int(bc[0] + 0.4 * (eb[0] - bc[0])), int(bc[1] + 0.4 * (eb[1] - bc[1]))]
        tanks = [u for u in home if u.get("type") in ("3tnk", "2tnk", "1tnk", "4tnk")]
        val = sum(Pro.COST.get(u["type"], 500) for u in home)
        go = None
        if not self.pushed and ((gs >= 420 and val >= 6000 and val >= 1.4 * top) or (gs >= 660 and len(home) >= 8)):
            self.pushed = True
            go = home
        elif self.pushed and len(home) >= 8:
            go = home
        elif not self.probed and gs >= 390 and len(tanks) >= 2:
            self.probed = True
            go = tanks[:2]
        if go:
            self.waves += 1
            out.append({"cmd": "attack_move", "units": [u["id"] for u in go][:50], "to": eb})
            self.att.update(u["id"] for u in go[:50])
        else:
            idle = [u["id"] for u in home if u.get("idle") and d((u["x"], u["y"]), post) > 5]
            if idle and self.once(obs, "post", 125):
                out.append({"cmd": "attack_move", "units": idle[:50], "to": post})
        out += self.push(obs, la, [u for u in army if u["id"] in self.att],
                         (DEF_PRIO, PROD, ("proc",), ("powr", "apwr"), ("fact",)))
        return out


class Tom(Strong):
    """The real losses of 10-14 / 10-15 to "Tom_2026" (6d0c7844 and f1cd3ba4 on shadowfiend-ii, edf99e95 on sidestep)
    as a script: a fast war factory before the second refinery, a third refinery and a second war factory once money
    floats, main tanks without pause and a rifle-heavy infantry mix (about three rifles per rocket); two fast light
    vehicles (jeep / flak truck) hunt harvesters from ~4:30; artillery from a radar dome after ~7:00 (one per four
    tanks). The army stands together at a post 30% of the way to us; a probe of 2 tanks and 4 infantry at ~4:50;
    attacks with everything once it is worth 1.2x the largest army of ours it saw in the last 90 s (and 5000+, from
    6:30), or from 9:30 regardless; reinforcing waves after; home defense with everything at home."""
    style = "tom"
    COST = {"e1": 100, "e3": 300, "1tnk": 700, "2tnk": 850, "3tnk": 1150, "apc": 850, "ftrk": 600, "jeep": 500,
            "arty": 850, "v2rl": 900}

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.probed = False
        self.pushed = False
        self.raiders: set[int] = set()
        self.seen: list[tuple[int, int]] = []          # (tick, value of our army in sight)

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        soviet = self.side(obs) == "soviet"
        bar = "barr" if soviet else "tent"
        gs = self.game_s(raw, obs)
        money = self.money(obs)
        # no money sunk into buildings it does not need yet: power only as consumers come, the radar dome (for
        # artillery) late
        plan = [("powr", 1), (bar, 1), ("proc", 1), ("weap", 1), ("powr", 2), ("proc", 2), ("fix", 1), ("powr", 3)]
        if gs > 210 and money > 800:
            plan += [("proc", 3), ("weap", 2), ("powr", 4)]
        if gs > 480 and money > 1500:
            plan += [("dome", 1), ("powr", 5)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        avail = obs.get("available_production") or []
        units = obs.get("units") or []
        army = self.fighters(obs)
        main = "3tnk" if soviet else "2tnk"
        fast = "ftrk" if soviet else "jeep"
        art = "v2rl" if soviet else "arty"
        tanks_all = [u for u in army if u.get("type") in ("3tnk", "2tnk", "1tnk", "4tnk")]
        inf = [u for u in army if u.get("type") in ("e1", "e2", "e3")]
        cap = 10 if gs < 200 else (20 if gs < 420 else 32)
        if self.count(obs, bar) and not self.queue_busy(obs, "Infantry") and len(inf) < cap and \
                money > (150 if gs < 200 else 350):
            n_e3 = sum(1 for u in inf if u.get("type") == "e3")
            out.append(self.train(obs, "e3" if gs > 200 and n_e3 * 4 < len(inf) else "e1", 2))
        if self.count(obs, "weap") and not self.queue_busy(obs, "Vehicle"):
            procs = self.count(obs, "proc")
            if self.count(obs, "harv") < min(6, 2 * procs) and money >= 1100:
                out.append(self.train(obs, "harv"))
            elif fast in avail and gs >= 240 and len(tanks_all) >= 2 and self.count(obs, fast) < 2 and money >= 600:
                out.append(self.train(obs, fast))
            elif art in avail and gs >= 420 and self.count(obs, art) < len(tanks_all) // 4 and money >= 900:
                out.append(self.train(obs, art))
            elif main in avail and money >= 800:
                out.append(self.train(obs, main))
            elif "1tnk" in avail and money >= 700:
                out.append(self.train(obs, "1tnk"))
        # raiders: the fast vehicles hunt harvesters, back off from towers
        alive_ids = {u["id"] for u in units if not u.get("wreck")}
        self.raiders &= alive_ids
        for u in units:
            if u.get("type") == fast and not u.get("wreck"):
                self.raiders.add(u["id"])
        raiders = [u for u in units if u["id"] in self.raiders]
        if raiders:
            cx = sum(u["x"] for u in raiders) / len(raiders)
            cy = sum(u["y"] for u in raiders) / len(raiders)
            ens = [e for e in obs.get("visible_enemies") or [] if not e.get("wreck")]
            harvs = [e for e in ens if e.get("type") == "harv"]
            danger = [b for b in obs.get("visible_enemy_buildings") or [] if b.get("type") in DEF_PRIO
                      and d((b["x"], b["y"]), (cx, cy)) <= 8]
            ids = [u["id"] for u in raiders][:50]
            if danger:
                if self.once(obs, "raid-back", 50):
                    out.append({"cmd": "move", "units": ids, "to": obs.get("base_center") or [5, 5]})
            elif harvs:
                t = min(harvs, key=lambda e: (d((e["x"], e["y"]), (cx, cy)), e["id"]))
                if self.once(obs, "raid-hit", 15):
                    out.append({"cmd": "attack", "units": ids, "target": t["id"]})
            elif all(u.get("idle") for u in raiders) and gs >= 270 and self.once(obs, "raid-go", 100):
                eb = la.get("enemy_base") or [0, 0]
                tx = int(eb[0] + self.rng.choice((-7, 7)))
                ty = int(eb[1] + self.rng.choice((-7, 7)))
                m = obs.get("map") or {}
                tx = max(2, min(int(m.get("width") or 90) - 3, tx))
                ty = max(2, min(int(m.get("height") or 90) - 3, ty))
                out.append({"cmd": "attack_move", "units": ids, "to": [tx, ty]})
        army = [u for u in army if u["id"] not in self.raiders]
        alive = {u["id"] for u in army}
        self.att &= alive
        st = self.stance(army)
        if st:
            out.append(st)
        # the largest army of ours in sight over the last 90 s
        ours = sum(self.COST.get(e.get("type"), 0) for e in obs.get("visible_enemies") or [] if not e.get("wreck"))
        tick = obs["tick"]
        self.seen = [(t, x) for t, x in self.seen if tick - t <= 90 * 25] + [(tick, ours)]
        top = max(x for _t, x in self.seen)
        home = [u for u in army if u["id"] not in self.att]
        hd = self.home_defense(obs, home, radius=16)
        if hd:
            out.append(hd)
        else:
            bc = obs.get("base_center") or [0, 0]
            eb = la.get("enemy_base") or [0, 0]
            post = [int(bc[0] + 0.3 * (eb[0] - bc[0])), int(bc[1] + 0.3 * (eb[1] - bc[1]))]
            tanks = [u for u in home if u.get("type") in ("3tnk", "2tnk", "1tnk", "4tnk")]
            hinf = [u for u in home if u not in tanks]
            val = sum(self.COST.get(u["type"], 850) for u in home)
            go = None
            if not self.pushed and ((gs >= 390 and val >= 5000 and val >= 1.1 * top) or (gs >= 570 and len(home) >= 8)):
                self.pushed = True
                go = home
            elif self.pushed and len(home) >= 8:
                go = home
            elif not self.probed and gs >= 290 and len(tanks) >= 2 and len(hinf) >= 4:
                self.probed = True
                go = tanks[:2] + hinf[:4]
            if go:
                self.waves += 1
                out.append({"cmd": "attack_move", "units": [u["id"] for u in go][:50], "to": eb})
                self.att.update(u["id"] for u in go[:50])
            else:
                idle = [u["id"] for u in home if u.get("idle") and d((u["x"], u["y"]), post) > 5]
                if idle and self.once(obs, "post", 125):
                    out.append({"cmd": "attack_move", "units": idle[:50], "to": post})
        out += self.push(obs, la, [u for u in army if u["id"] in self.att],
                         (DEF_PRIO, PROD, ("proc",), ("powr", "apwr"), ("fact",)))
        return out


class Tom2(Strong):
    """The upgraded Tom_2026 of 10-15 (real losses 335d2a62 on trapped, 21bf0e2a on sidestep; measured from the
    recordings, docs/progress.md v4.9): no towers; double refinery and the war factory by ~2:30, the service depot
    right after it (the main tanks need it), the refineries' harvesters doubled, two barracks, then tanks without
    pause and a rifle-heavy infantry stream (about four rifles per rocket), a jeep / flak truck from ~4:20. It does not
    wait: the first attack reaches us at ~4:45-5:10 (2-3 tanks, 12-20 rifles, 2-5 rockets), then every new group of 6
    follows it in; a second war factory once money floats after 5:00, a radar dome (artillery) after 7:00. Real Tom by
    7:00 (distinct units seen): 6-7 tanks, ~30 rifles, ~5 rockets, 2-3 jeeps."""
    style = "tom2"
    TANKS = ("3tnk", "2tnk", "1tnk", "4tnk")
    POST = 0.08                 # where the army waits before its first attack: this share of the way to us

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.first = False

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        soviet = self.side(obs) == "soviet"
        bar = "barr" if soviet else "tent"
        gs = self.game_s(raw, obs)
        money = self.money(obs)
        # (the depot is the main tanks' prerequisite: right after the war factory)
        # (tried: the war factory before the second refinery, like `tom`: a weaker army at 5:00, v4.7 won 55 / 60)
        plan = [("powr", 1), (bar, 1), ("proc", 1), ("proc", 2), ("weap", 1), ("powr", 2), ("fix", 1), (bar, 2),
                ("powr", 3)]
        if gs > 420:
            plan += [("dome", 1), ("powr", 4)]
        if gs > 300 and money > 2000:
            plan += [("weap", 2), ("powr", 5), ("proc", 3), ("powr", 6)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        avail = obs.get("available_production") or []
        army = self.fighters(obs)
        main = "3tnk" if soviet else "2tnk"
        fast = "ftrk" if soviet else "jeep"
        art = "v2rl" if soviet else "arty"
        tanks_all = [u for u in army if u.get("type") in self.TANKS]
        inf = [u for u in army if u.get("type") in ("e1", "e2", "e3")]
        procs = self.count(obs, "proc")
        need_harv = self.count(obs, "weap") and self.count(obs, "harv") < min(6, 2 * procs)
        if self.count(obs, "weap") and not self.queue_busy(obs, "Vehicle"):
            if need_harv and money >= 1100:
                out.append(self.train(obs, "harv"))
            elif need_harv:
                pass                                         # saving for the harvester
            elif fast in avail and gs >= 260 and len(tanks_all) >= 2 and \
                    self.count(obs, fast) < (1 if gs < 400 else 2) and money >= 600:
                out.append(self.train(obs, fast))
            elif art in avail and gs >= 480 and self.count(obs, art) < len(tanks_all) // 4 and money >= 900:
                out.append(self.train(obs, art))
            elif main in avail and money >= 700:
                out.append(self.train(obs, main))
            elif "1tnk" in avail and money >= 700:
                out.append(self.train(obs, "1tnk"))
        cap = 16 if gs < 240 else (32 if gs < 420 else 40)
        floor = 1250 if need_harv else 100
        if self.count(obs, bar) and not self.queue_busy(obs, "Infantry") and len(inf) < cap and money >= floor:
            n_e3 = sum(1 for u in inf if u.get("type") == "e3")
            out.append(self.train(obs, "e3" if gs > 200 and n_e3 * 5 < len(inf) else "e1", 2))
        alive = {u["id"] for u in army}
        self.att &= alive
        st = self.stance(army)
        if st:
            out.append(st)
        home = [u for u in army if u["id"] not in self.att]
        hd = self.home_defense(obs, home, radius=16)
        eb = la.get("enemy_base") or [0, 0]
        if hd:
            out.append(hd)
        else:
            tanks = [u for u in home if u.get("type") in self.TANKS]
            go = None
            if not self.first and gs >= 225 and ((len(tanks) >= 2 and len(home) >= 12) or gs >= 300):
                self.first = True
                go = home
            elif self.first and len(home) >= 6:
                go = home
            if go:
                self.waves += 1
                out.append({"cmd": "attack_move", "units": [u["id"] for u in go][:50], "to": eb})
                self.att.update(u["id"] for u in go[:50])
            else:
                bc = obs.get("base_center") or [0, 0]
                # tom2: at home, a few cells toward us (our scouts die inside the base, as in the real games, where
                # the early-army rule fired in every one); tom2p: a post a quarter of the way (scouts die on the road)
                f = self.POST
                post = [int(bc[0] + f * (eb[0] - bc[0])), int(bc[1] + f * (eb[1] - bc[1]))]
                idle = [u["id"] for u in home if u.get("idle") and d((u["x"], u["y"]), post) > 5]
                if idle and self.once(obs, "post", 125):
                    out.append({"cmd": "attack_move", "units": idle[:50], "to": post})
        out += self.push(obs, la, [u for u in army if u["id"] in self.att],
                         (DEF_PRIO, PROD, ("proc",), ("powr", "apwr"), ("fact",)))
        return out


class Tom2Post(Tom2):
    """tom2 with its army waiting at a post a quarter of the way to us (our scout dies on the road, so the early-army
    rule does not fire)."""
    style = "tom2p"
    POST = 0.25


BOTS = {"tanks": TankRush, "swarm": Swarm, "pro": Pro, "raider": Raider, "hider": Hider, "nash": Nash, "riffo": Riffo,
        "nashball": NashBall, "tom": Tom, "tom2": Tom2, "tom2p": Tom2Post}
STYLES = tuple(BOTS)


def make_bot(style: str, seed: int = 0):
    if style not in BOTS:
        raise ValueError(f"unknown bot style {style!r}; one of {', '.join(STYLES)}")
    return BOTS[style](seed)
