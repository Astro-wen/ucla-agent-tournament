"""Scripted Red Alert opponents for the local simulator, written as ordinary runner contestants (choose_action returns
an orders batch or altruagent.WAIT, on_action_result counts refusals), so they play through the same protocol and
runner as any agent. Deterministic for a given seed and the same views.

    idle     never sends anything (it still checks in): the floor any bot must beat
    turtle   builds a full base with defenses and an army that stays home; attacks only what comes near
    rusher   power + barracks, masses rifle infantry and attacks the enemy base at about 2 minutes (stance
             attack_anything, so they shoot buildings too), then keeps reinforcing
    llm      an "LLM-like" slow player: acts every 5-10 s of game time, follows the primer build order, makes the
             mistakes LLM agents make (placing too early, untrainable items, dead ids, cells outside the bounds,
             unknown fields, the same build twice), and attacks with legal_actions.attack_now

Use `make_bot(style, seed)` (local/ra_platform.py's factories use it).
"""
from __future__ import annotations

import random

from altruagent import WAIT, WithReasoning

STYLES = ("idle", "turtle", "rusher", "llm")


def _obs(state) -> dict | None:
    raw = state.raw if hasattr(state, "raw") else state
    obs = raw.get("observation")
    return obs if isinstance(obs, dict) else None


class Base:
    style = "base"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(f"{self.style}:{seed}")
        self.sent: dict[str, int] = {}             # item -> tick a build / train was last sent
        self.refusals: dict[str, int] = {}
        self.invalid_batches = 0
        self.batches = 0
        self.choices = 0

    # -- runner contract --
    def choose_action(self, state, context):
        self.choices += 1
        raw = state.raw
        obs = _obs(raw)
        if obs is None or raw.get("phase") != "playing":
            return WAIT
        la = raw.get("legal_actions") or {}
        orders = [o for o in self.decide(raw, obs, la) if o][:20]
        if not orders:
            return WAIT
        self.batches += 1
        return {"type": "orders", "orders": orders}

    def on_action_result(self, result, context):
        if result.get("error") == "INVALID_ACTION":
            self.invalid_batches += 1
            return
        for r in result.get("results") or []:
            if r.get("status") == "rejected":
                self.refusals[r.get("reason") or "?"] = self.refusals.get(r.get("reason") or "?", 0) + 1

    def decide(self, raw, obs, la) -> list:
        return []

    # -- helpers --
    @staticmethod
    def side(obs) -> str:
        return "soviet" if obs.get("faction") in ("russia", "ukraine") else "allies"

    @staticmethod
    def count(obs, typ) -> int:
        return sum(1 for b in obs.get("buildings") or [] if b.get("type") == typ) + \
            sum(1 for u in obs.get("units") or [] if u.get("type") == typ and not u.get("wreck"))

    @staticmethod
    def queued(obs, item) -> int:
        return sum(1 for p in obs.get("production") or [] if p.get("item") == item)

    @staticmethod
    def queue_busy(obs, queue) -> bool:
        return any(p.get("queue") == queue for p in obs.get("production") or [])

    def recently_sent(self, obs, item, ticks=40) -> bool:
        t = self.sent.get(item)
        return t is not None and obs["tick"] - t < ticks and not self.queued(obs, item)

    def money(self, obs) -> int:
        e = obs.get("economy") or {}
        return int(e.get("cash") or 0) + int(e.get("ore") or 0)

    def build(self, obs, item) -> dict | None:
        if item not in (obs.get("available_production") or []) or self.recently_sent(obs, item):
            return None
        self.sent[item] = obs["tick"]
        return {"cmd": "build", "item": item}

    def train(self, obs, item, count=1) -> dict | None:
        if item not in (obs.get("available_production") or []) or self.recently_sent(obs, item):
            return None
        self.sent[item] = obs["tick"]
        return {"cmd": "train", "item": item, "count": count}

    def once(self, obs, key, ticks=25) -> bool:
        """True (and remembered) unless `key` was sent within the last `ticks` (verdicts take a few ticks)."""
        t = self.sent.get(key)
        if t is not None and obs["tick"] - t < ticks:
            return False
        self.sent[key] = obs["tick"]
        return True

    def basics(self, obs, la) -> list:
        """Deploy the MCV, place what is ready."""
        out = []
        if la.get("deploy") and not any(b.get("type") == "fact" for b in obs.get("buildings") or []) \
                and self.once(obs, "deploy", 50):
            out.append({"cmd": "deploy", "units": la["deploy"][:1]})
        for item in la.get("place") or []:
            if self.once(obs, "place:" + item, 15):
                out.append({"cmd": "place", "item": item})
        return out

    def next_building(self, obs, plan) -> dict | None:
        """The first building of `plan` (a list of (type, how many)) we have fewer of, when the Building queue is
        free; a power plant first when power runs short."""
        if self.queue_busy(obs, "Building"):
            return None
        e = obs.get("economy") or {}
        if any(b.get("type") == "fact" for b in obs.get("buildings") or []) and \
                int(e.get("power_drained") or 0) + 40 > int(e.get("power_provided") or 0) and \
                self.count(obs, "powr") >= 1:
            return self.build(obs, "powr")
        for typ, n in plan:
            if self.count(obs, typ) < n:
                return self.build(obs, typ)
        return None

    @staticmethod
    def fighters(obs) -> list[dict]:
        skip = {"mcv", "harv", "medi", "mech", "e6", "spy", "thf", "truk"}
        return [u for u in obs.get("units") or [] if not u.get("wreck") and u.get("type") not in skip]


class IdleBot(Base):
    style = "idle"

    def decide(self, raw, obs, la):
        return []


class TurtleBot(Base):
    style = "turtle"

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        soviet = self.side(obs) == "soviet"
        bar = "barr" if soviet else "tent"
        defense = ("ftur", "tsla") if soviet else ("pbox", "gun")
        plan = [("powr", 1), (bar, 1), ("proc", 1), ("powr", 2), ("weap", 1), ("powr", 3), ("proc", 2)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        if self.count(obs, bar) and not self.queue_busy(obs, "Defense"):
            have = sum(self.count(obs, d) for d in defense)
            if have < 6:
                pick = defense[1] if have % 2 and defense[1] in (obs.get("available_production") or []) else defense[0]
                out.append(self.build(obs, pick))
        army = self.fighters(obs)
        if self.count(obs, bar) and not self.queue_busy(obs, "Infantry") and len(army) < 20 and self.money(obs) > 400:
            out.append(self.train(obs, "e1" if self.rng.random() < 0.6 else "e3", 3))
        if self.count(obs, "weap") and not self.queue_busy(obs, "Vehicle") and self.money(obs) > 1200:
            if self.count(obs, "harv") < 2:
                out.append(self.train(obs, "harv"))
            else:
                tank = "3tnk" if soviet else "2tnk"
                light = "ftrk" if soviet else "1tnk"
                avail = obs.get("available_production") or []
                out.append(self.train(obs, tank if tank in avail else light))
        # defend: anything that comes within 18 cells of the base gets attacked; otherwise gather at home
        bc = obs.get("base_center") or [0, 0]
        near = [e for e in (obs.get("visible_enemies") or []) + (obs.get("visible_enemy_buildings") or [])
                if not e.get("wreck") and abs(e["x"] - bc[0]) + abs(e["y"] - bc[1]) <= 18]
        idle = [u["id"] for u in army if u.get("idle")]
        if near and army:
            t = min(near, key=lambda e: abs(e["x"] - bc[0]) + abs(e["y"] - bc[1]))
            out.append({"cmd": "attack_move", "units": [u["id"] for u in army][:50], "to": [t["x"], t["y"]]})
        elif idle and obs["tick"] % 250 < 5:
            out.append({"cmd": "move", "units": idle[:50], "to": [bc[0] + 2, bc[1] + 5]})
        return out


class RusherBot(Base):
    style = "rusher"
    ATTACK_AT_S = 120
    WAVE = 6

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.attacking: set[int] = set()
        self.stanced: set[int] = set()

    def decide(self, raw, obs, la):
        out = self.basics(obs, la)
        soviet = self.side(obs) == "soviet"
        bar = "barr" if soviet else "tent"
        plan = [("powr", 1), (bar, 1), ("proc", 1), (bar, 2), ("powr", 2)]
        nb = self.next_building(obs, plan)
        if nb:
            out.append(nb)
        if self.count(obs, bar) and self.queued(obs, "e1") < 5 and self.money(obs) >= 100:
            out.append(self.train(obs, "e1", 5))
        army = [u for u in self.fighters(obs)]
        alive = {u["id"] for u in army}
        self.attacking &= alive
        fresh = [u["id"] for u in army if u["id"] not in self.stanced]
        if fresh:
            out.append({"cmd": "set_stance", "units": fresh[:50], "stance": "attack_anything"})
            self.stanced.update(fresh[:50])
        game_s = (raw.get("time") or {}).get("game_seconds") or obs["tick"] / 25
        home = [u["id"] for u in army if u["id"] not in self.attacking]
        if (game_s >= self.ATTACK_AT_S and len(home) >= self.WAVE) or (self.attacking and len(home) >= self.WAVE):
            target = la.get("enemy_base") or [0, 0]
            out.append({"cmd": "attack_move", "units": home[:50], "to": target})
            self.attacking.update(home[:50])
        # attackers that went idle near the enemy: hit the nearest visible building explicitly
        blds = [b for b in obs.get("visible_enemy_buildings") or [] if not b.get("wreck")]
        idle_att = [u for u in army if u["id"] in self.attacking and u.get("idle")]
        if blds and idle_att:
            cx = sum(u["x"] for u in idle_att) / len(idle_att)
            cy = sum(u["y"] for u in idle_att) / len(idle_att)
            t = min(blds, key=lambda b: (abs(b["x"] - cx) + abs(b["y"] - cy), b["id"]))
            out.append({"cmd": "attack", "units": [u["id"] for u in idle_att][:50], "target": t["id"]})
        elif idle_att and not blds:
            out.append({"cmd": "attack_move", "units": [u["id"] for u in idle_att][:50],
                        "to": la.get("enemy_base") or [0, 0]})
        return out


class LLMLikeBot(Base):
    """Acts every 5-10 s of game time with a short batch; about one batch in six carries a typical mistake."""
    style = "llm"
    MISTAKE_RATE = 0.17

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.next_tick = 0
        self.mistakes = 0

    def decide(self, raw, obs, la):
        tick = obs["tick"]
        if tick < self.next_tick:
            return []
        self.next_tick = tick + int(self.rng.uniform(5, 10) * 25)
        out = self.basics(obs, la)
        soviet = self.side(obs) == "soviet"
        bar = "barr" if soviet else "tent"
        plan = [("powr", 1), (bar, 1), ("proc", 1), ("weap", 1), ("powr", 2), ("proc", 2), ("powr", 3)]
        if not self.queue_busy(obs, "Building"):
            for typ, n in plan:
                if self.count(obs, typ) < n:
                    if typ in (obs.get("available_production") or []):
                        out.append({"cmd": "build", "item": typ})
                    break
        if self.count(obs, bar) and self.queued(obs, "e1") < 3:
            out.append({"cmd": "train", "item": "e1", "count": 5})
        if self.count(obs, "weap") and not self.queue_busy(obs, "Vehicle"):
            light = "ftrk" if soviet else "1tnk"
            out.append({"cmd": "train", "item": light, "count": 2})
        game_s = (raw.get("time") or {}).get("game_seconds") or tick / 25
        if la.get("attack_now") and len(la.get("combat_units") or []) >= 6 and game_s > 240:
            out.append(dict(la["attack_now"]))
        if self.rng.random() < self.MISTAKE_RATE:
            out.append(self.mistake(obs, la))
            self.mistakes += 1
        if not out:
            return []
        return out

    def mistake(self, obs, la) -> dict:
        kind = self.rng.choice(("place_early", "unavailable", "dead_ids", "out_of_bounds", "unknown_field",
                                "double_build"))
        units = [u["id"] for u in obs.get("units") or [] if not u.get("wreck")]
        if kind == "place_early":
            return {"cmd": "place", "item": "weap"}
        if kind == "unavailable":
            return {"cmd": "train", "item": "4tnk", "count": 1}
        if kind == "dead_ids":
            return {"cmd": "move", "units": (units[:1] or []) + [99999], "to": obs.get("base_center") or [5, 5]}
        if kind == "out_of_bounds" and units:
            m = obs.get("map") or {}
            return {"cmd": "move", "units": units[:1], "to": [0, int(m.get("height") or 2) - 1]}
        if kind == "unknown_field":
            return {"cmd": "build", "item": "powr", "priority": "high"}
        return {"cmd": "build", "item": "powr"}

    def choose_action(self, state, context):
        out = super().choose_action(state, context)
        if isinstance(out, dict):
            return WithReasoning(out, "Following the build order; attacking when the army is ready.")
        return out


BOTS = {"idle": IdleBot, "turtle": TurtleBot, "rusher": RusherBot, "llm": LLMLikeBot}


def make_bot(style: str, seed: int = 0):
    if style not in BOTS:
        raise ValueError(f"unknown bot style {style!r}; one of {', '.join(STYLES)}")
    return BOTS[style](seed)
