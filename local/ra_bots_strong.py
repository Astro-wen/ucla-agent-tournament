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


BOTS = {"tanks": TankRush, "swarm": Swarm, "pro": Pro, "raider": Raider, "hider": Hider}
STYLES = tuple(BOTS)


def make_bot(style: str, seed: int = 0):
    if style not in BOTS:
        raise ValueError(f"unknown bot style {style!r}; one of {', '.join(STYLES)}")
    return BOTS[style](seed)
