"""Archived Tom opponent definition with its original helper dependencies made explicit."""
from local.ra_bots_strong import DEF_PRIO, PROD, Strong, d


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
        plan = [("powr", 1), (bar, 1), ("proc", 1), ("powr", 2), ("weap", 1), ("proc", 2), ("powr", 3), ("fix", 1),
                ("powr", 4)]
        if gs > 240 and money > 1000:
            plan += [("proc", 3), ("powr", 5), ("weap", 2), ("dome", 1), ("powr", 6), ("powr", 7)]
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
        cap = 8 if gs < 200 else (18 if gs < 420 else 30)
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
            if not self.pushed and ((gs >= 390 and val >= 5000 and val >= 1.2 * top) or (gs >= 570 and len(home) >= 8)):
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

