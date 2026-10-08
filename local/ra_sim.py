"""A deterministic, engine-free Red Alert world for testing a rule-based bot's LOGIC end to end (never its tactics at
OpenRA fidelity). local/ra_server.py wraps it in the platform's GameAPI protocol; local/ra_platform.py plays matches
through the official runner. Rules come from local/ra_rules.py (the platform's reference + OpenRA RA facts).

What it models (docs/platform/redalert.md is the contract):
- 25 ticks a second, stepped one view (5 ticks, 0.2 s) at a time; the observation's `tick` is the state_version.
- Starting cash (default 5000), one MCV per side at its spawn (the map's real bounds / spawns / layout), random
  countries per seed. MCV deploy -> a construction yard (`fact`) with a NEW id after a short unfold, refused when the
  3x4 footprint has no room.
- Production: one queue per type (Building, Defense, Infantry, Vehicle, Ship, Aircraft), one item at a time per queue,
  paid as it progresses (stalls without money), build_time_s from the reference, faster with several producers of
  the queue (OpenRA BuildTimeSpeedReduction), three times slower on low power. Prerequisites, faction-only items and
  producers decide available_production. A finished building waits in its queue (and blocks it) until placed.
- Placement on an occupancy grid: `at` is a hint (bounds, terrain, overlap, within 2 cells of one of your buildings),
  otherwise the nearest free spot around the construction yard; `invalid` when none is free.
- Power: plants provide (scaled by health), buildings drain; low power slows production and switches the
  tesla coil / radar / AA off (`powered: false`); power_toggle for the buildings that support it.
- Refinery with a free harvester; harvesters drive to the nearest ore ($ 25 / bale, gems 50) from the map layout,
  load 20 bales, drive back and unload into cash storage (`ore`, capped by resource_capacity); fields deplete and
  regrow slowly around ore mines.
- Units leave the producing building (to its rally point). move / attack_move / attack (unit, building or ground) /
  guard / stop / harvest / set_stance / enter_transport / unload: straight-line motion at the unit's speed.
- Fog of war: sight radius per actor; visible_enemies / visible_enemy_buildings; explored_percent.
- Combat: per-weapon damage x armor versus table / reload period (OpenRA numbers), defenses, damage to buildings.
  OpenRA target rules: the default stance `defend` fires at units and defenses in weapon range without moving and
  never at plain buildings; attack_move engages the same targets on the way; `attack_anything` also takes buildings
  and chases; an explicit `attack` hits anything visible.
- Deaths: kills/deaths cost, units/buildings killed/lost, husks (wreck: true, burning out in ~45 s), sell (50 %),
  repair (costs money).
- Events (unit_created, building_created, production_ready, under_attack, unit_lost, building_lost, building_sold,
  enemy_spotted, enemy_killed, low_power, power_restored) with per-seat seq numbers.
- Victory (every enemy building but walls, and the MCV, destroyed), draws.

Deterministic: the same seed and the same orders at the same ticks give the same world.
"""
from __future__ import annotations

import math
import random

from .ra_rules import (BALE_LOAD_TICKS, BALE_UNLOAD_TICKS, BLOCKED, BUILDABLE, COUNTRIES, DISABLED_ON_LOW_POWER,
                       GEM_DENSITY, GEM_VALUE, HARVESTER_CAPACITY, HUSK_TICKS, LOW_POWER_MODIFIER, MAX_ORE_DENSITY,
                       ORE_CHARS, ORE_DENSITY, ORE_VALUE, POWER_TOGGLE, REPAIR_COST_PERCENT, REPAIR_INTERVAL_TICKS,
                       REPAIR_STEP_HP, SELL_REFUND, SIDES, SPEEDUP, SPEEDUP_DEFAULT, STORAGE, TICKS_PER_SECOND, WALLS,
                       Rules, default_rules)

STEP = 5                                   # ticks per world step (one view, 0.2 s)
DT = STEP / TICKS_PER_SECOND
QUEUES = ("Building", "Defense", "Infantry", "Vehicle", "Ship", "Aircraft")
STANCES = ("hold_fire", "return_fire", "defend", "attack_anything")
DEFAULT_STANCE = "defend"                  # OpenRA AutoTarget.InitialStance for a human (non-bot) player
TRANSPORTS = {"apc": 5, "tran": 5, "lst": 5}
DEPLOY_TICKS = 25                          # the MCV unfolds for about a second
SELL_TICKS = 25
UNDER_ATTACK_EVERY = 10 * TICKS_PER_SECOND
EVENT_WINDOW = 20
ORE_GROW_EVERY = 3 * TICKS_PER_SECOND
EXPLORE_EVERY = TICKS_PER_SECOND
BUCKET = 8
ADJACENT = 2                               # OpenRA Building.Adjacent: a new building within 2 cells of one of yours
AUTO_PLACE_RADIUS = 24
HARV_FULL_SPEED = 0.85                     # Harvester.FullyLoadedSpeed
DOCK_TICKS = 75                            # GUESS: turning into the refinery and its docking animation
BALE_TICKS = BALE_LOAD_TICKS + 1           # HarvestResource: a 4-tick Wait per bale plus the activity's own tick
DEFENSE_TYPES = frozenset({"pbox", "hbox", "gun", "ftur", "tsla", "agun", "sam", "gap", "pdox", "iron", "mslo"})
PRODUCTION_TYPES = frozenset({"fact", "tent", "barr", "kenn", "weap", "syrd", "spen", "hpad", "afld", "afld.ukraine"})
UNIT_CMDS = frozenset({"move", "attack_move", "attack", "stop", "deploy", "unload", "guard", "enter_transport",
                       "harvest", "set_stance"})
BUILDING_OK_CMDS = frozenset({"attack", "stop", "set_stance", "guard"})   # may also name your own buildings
ORDER_FIELDS = {
    "move": ({"units", "to"}, {"queued"}), "attack_move": ({"units", "to"}, {"queued"}),
    "attack": ({"units"}, {"target", "to", "queued"}), "stop": ({"units"}, set()), "deploy": ({"units"}, set()),
    "unload": ({"units"}, set()), "guard": ({"units", "target"}, {"queued"}),
    "enter_transport": ({"units", "target"}, {"queued"}), "harvest": ({"units"}, {"to", "queued"}),
    "set_stance": ({"units", "stance"}, set()), "build": ({"item"}, set()), "train": ({"item"}, {"count"}),
    "place": ({"item"}, {"at"}), "cancel": ({"item"}, set()), "sell": ({"building"}, set()),
    "repair": ({"building"}, {"on"}), "power_toggle": ({"building"}, {"on"}), "set_primary": ({"building"}, set()),
    "rally": ({"building", "to"}, set()),
}
MAX_UNITS_PER_ORDER = 50
MAX_TRAIN_COUNT = 10


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class Actor:
    __slots__ = ("id", "owner", "type", "bld", "x", "y", "w", "h", "hp", "max_hp", "cost", "armor", "sight", "weapon",
                 "speed", "stance", "order", "oq", "engage", "cd", "hit_by", "hit_tick", "ua_tick", "wreck",
                 "expire", "powered_down", "repairing", "rally", "sell_tick", "deploy_tick", "cargo", "cargo_value",
                 "hstate", "hcell", "htick", "passengers", "mark", "repair_tick", "toggle_tick", "repair_cmd_tick",
                 "created")

    def __init__(self, aid: int, owner: int, typ: str, rules: Rules, x: float, y: float, tick: int):
        self.id, self.owner, self.type = aid, owner, typ
        self.bld = rules.is_building(typ)
        self.x, self.y = x, y
        self.w, self.h = rules.footprint(typ) if self.bld else (0, 0)
        self.max_hp = self.hp = rules.hp(typ)
        self.cost = rules.cost(typ)
        self.armor = rules.armor(typ)
        self.sight = rules.sight(typ)
        self.weapon = rules.weapon(typ)
        self.speed = 0.0 if self.bld else rules.speed(typ)
        self.stance = DEFAULT_STANCE
        self.order = None                  # {"kind", "to", "target"} or None
        self.oq: list = []                 # queued orders
        self.engage = None                 # auto-target id
        self.cd = 0
        self.hit_by = None
        self.hit_tick = -10 ** 9
        self.ua_tick = -10 ** 9
        self.wreck = False
        self.expire = None
        self.powered_down = False
        self.repairing = False
        self.rally = None
        self.sell_tick = None
        self.deploy_tick = None
        self.cargo = 0
        self.cargo_value = 0
        self.hstate = None
        self.hcell = None
        self.htick = 0
        self.passengers: list = []
        self.mark = None
        self.repair_tick = 0
        self.toggle_tick = -10 ** 9
        self.repair_cmd_tick = -10 ** 9
        self.created = tick

    # geometry
    def center(self) -> tuple[float, float]:
        if self.bld:
            return self.x + self.w / 2.0, self.y + self.h / 2.0
        return self.x, self.y

    def cell(self) -> tuple[int, int]:
        return int(math.floor(self.x)), int(math.floor(self.y))

    def dist_from(self, px: float, py: float) -> float:
        """Distance from a point to this actor (to the nearest point of a building's footprint)."""
        if self.bld:
            dx = max(self.x - px, 0.0, px - (self.x + self.w))
            dy = max(self.y - py, 0.0, py - (self.y + self.h))
            return math.hypot(dx, dy)
        return math.hypot(self.x - px, self.y - py)

    def nearest_point(self, px: float, py: float) -> tuple[float, float]:
        if self.bld:
            return min(max(px, self.x), self.x + self.w), min(max(py, self.y), self.y + self.h)
        return self.x, self.y


class Item:
    __slots__ = ("name", "queue", "cost", "ticks", "progress", "paid", "done", "paused")

    def __init__(self, name: str, queue: str, cost: int):
        self.name, self.queue, self.cost = name, queue, cost
        self.ticks = None                  # set when production starts
        self.progress = 0.0
        self.paid = 0                      # credits (integers, like the engine)
        self.done = False
        self.paused = False


class Player:
    def __init__(self, seat: int, country: str, cash: int, w: int, h: int):
        self.seat = seat
        self.country = country
        self.side = SIDES.get(country, "allies")
        self.cash = int(cash)
        self.ore = 0
        self.queues: dict[str, list[Item]] = {q: [] for q in QUEUES}
        self.primary: dict[str, int] = {}
        self.power_provided = 0
        self.power_drained = 0
        self.low_power = False
        self.events: list[dict] = []
        self.seq = 0
        self.mil = {"kills_cost": 0, "deaths_cost": 0, "assets_value": 0, "army_value": 0, "units_killed": 0,
                    "units_lost": 0, "buildings_killed": 0, "buildings_lost": 0}
        self.explored = bytearray(w * h)
        self.explored_count = 0            # explored cells inside map.bounds
        self.visible: set[int] = set()
        self.spotted: set[int] = set()
        self.defeated = False
        self.first_built: dict[str, int] = {}     # building type -> tick first created (metrics)
        self.peak_army = 0
        self.kills_this_step: list[str] = []
        self.storage_full = False

    def resources(self) -> int:
        return self.cash + self.ore

    def take(self, amount: int) -> bool:
        """OpenRA PlayerResources.TakeCash: stored ore first ("spend ore before cash"), then cash; False (nothing
        taken) if short."""
        amount = int(amount)
        if amount <= 0:
            return True
        if self.cash + self.ore < amount:
            return False
        self.ore -= amount
        if self.ore < 0:
            self.cash += self.ore
            self.ore = 0
        return True

    def refund(self, amount: int) -> None:
        self.cash += int(amount)


class World:
    def __init__(self, rules: Rules | None = None, map_id: str = "sidestep", seed: int = 0, *,
                 starting_cash: int = 5000, countries: tuple[str, str] | None = None, swap_spawns: bool | None = None,
                 starting_units: str = "none", explored_map: bool = False, apply_delay_ticks: tuple = (5, 10)):
        self.rules = rules or default_rules()
        self.seed = seed
        self.rng = random.Random(f"world:{seed}")
        self.order_rng = [random.Random(f"orders:{seed}:{s}") for s in (0, 1)]
        self.map_id = map_id
        info = self.rules.map_info(map_id)
        self.width, self.height = int(info["width"]), int(info["height"])
        self.bounds = tuple(int(v) for v in info["bounds"])
        bx, by, bw, bh = self.bounds
        self.bx0, self.by0, self.bx1, self.by1 = bx, by, bx + bw - 1, by + bh - 1
        self.spawns = [tuple(int(v) for v in s) for s in info.get("spawns") or []][:2]
        if len(self.spawns) < 2:
            self.spawns = [(bx + 5, by + 5), (self.bx1 - 5, self.by1 - 5)]
        if swap_spawns is None:
            swap_spawns = self.rng.random() < 0.5
        if swap_spawns:
            self.spawns = [self.spawns[1], self.spawns[0]]
        if countries is None:
            countries = (self.rng.choice(COUNTRIES), self.rng.choice(COUNTRIES))
        self.starting_cash = int(starting_cash)
        self.starting_units = starting_units
        self.explored_map = bool(explored_map)
        self.apply_delay_ticks = tuple(apply_delay_ticks)
        self.tick = 0
        self.next_id = 100
        self.actors: dict[int, Actor] = {}
        self.players = [Player(s, countries[s], starting_cash, self.width, self.height) for s in (0, 1)]
        self._load_terrain(info)
        self.occ = [[0] * self.width for _ in range(self.height)]
        self.pending: list = []            # (apply_tick, seat, batch, index, order, skipped)
        self.verdicts: list = []           # filled by step(): (seat, batch, index, status, reason, detail, failed, ok)
        self.winner = None                 # seat, or -1 for a draw, once over
        self.over = False
        self._avail_cache: dict = {}
        for s in (0, 1):
            sx, sy = self.spawns[s]
            self._spawn_unit(s, "mcv", sx + 0.5, sy + 0.5, event=False)
            if starting_units in ("light", "heavy"):
                p = self.players[s]
                kinds = ["e1"] * (4 if starting_units == "light" else 8)
                kinds += (["1tnk"] if p.side == "allies" else ["3tnk"]) * (1 if starting_units == "light" else 3)
                for i, k in enumerate(kinds):
                    self._spawn_unit(s, k, sx + 0.5 + (i % 4) - 1.5, sy + 2.5 + i // 4, event=False)
            if self.explored_map:
                self.players[s].explored = bytearray(b"\x01" * (self.width * self.height))
        self._update_power()
        self._update_visibility(force_explore=True)
        self._update_stats()

    # ---- terrain -------------------------------------------------------------------------------------------------
    def _load_terrain(self, info: dict) -> None:
        self.terrain = [["#"] * self.width for _ in range(self.height)]
        for y in range(self.by0, self.by1 + 1):
            for x in range(self.bx0, self.bx1 + 1):
                if 0 <= x < self.width and 0 <= y < self.height:
                    self.terrain[y][x] = "."
        self.ore: dict[tuple[int, int], list] = {}       # cell -> [value per bale, density]
        self.mines: list[tuple[int, int]] = []
        lay = info.get("layout") or {}
        rows, origin = lay.get("rows") or [], lay.get("origin") or [self.bx0, self.by0]
        for i, row in enumerate(rows):
            y = int(origin[1]) + i
            if not 0 <= y < self.height:
                continue
            for j, ch in enumerate(row):
                x = int(origin[0]) + j
                if not 0 <= x < self.width:
                    continue
                self.terrain[y][x] = ch
                if ch in ORE_CHARS:
                    self.ore[(x, y)] = [GEM_VALUE, GEM_DENSITY] if ORE_CHARS[ch] == "gems" else [ORE_VALUE, ORE_DENSITY]
                elif ch == "M":
                    self.mines.append((x, y))
        self.ore_cells = sorted(self.ore)

    def in_bounds(self, x: int, y: int) -> bool:
        return self.bx0 <= x <= self.bx1 and self.by0 <= y <= self.by1

    def in_map(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def buildable(self, x: int, y: int) -> bool:
        return self.in_bounds(x, y) and self.terrain[y][x] in BUILDABLE and self.occ[y][x] == 0 \
            and (x, y) not in self.ore

    # ---- actors --------------------------------------------------------------------------------------------------
    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _event(self, seat: int, kind: str, **kw) -> None:
        p = self.players[seat]
        p.seq += 1
        ev = {"seq": p.seq, "tick": self.tick, "kind": kind}
        ev.update(kw)
        p.events.append(ev)
        if len(p.events) > EVENT_WINDOW * 3:
            del p.events[:-EVENT_WINDOW]

    def _spawn_unit(self, seat: int, typ: str, x: float, y: float, event: bool = True) -> Actor:
        x = min(max(x, self.bx0 + 0.5), self.bx1 + 0.5)
        y = min(max(y, self.by0 + 0.5), self.by1 + 0.5)
        a = Actor(self._new_id(), seat, typ, self.rules, x, y, self.tick)
        self.actors[a.id] = a
        if typ == "harv":
            a.hstate = "seek"
        if event:
            cx, cy = a.cell()
            self._event(seat, "unit_created", id=a.id, type=typ, x=cx, y=cy)
        return a

    def _add_building(self, seat: int, typ: str, x: int, y: int, event: bool = True) -> Actor:
        a = Actor(self._new_id(), seat, typ, self.rules, x, y, self.tick)
        self.actors[a.id] = a
        for yy in range(y, y + a.h):
            for xx in range(x, x + a.w):
                if self.in_map(xx, yy):
                    self.occ[yy][xx] = a.id
        p = self.players[seat]
        p.first_built.setdefault(typ, self.tick)
        if event:
            self._event(seat, "building_created", id=a.id, type=typ, x=x, y=y)
        if typ == "proc":                  # FreeActor: a harvester next to the refinery
            self._spawn_unit(seat, "harv", x + a.w / 2.0, y + a.h + 0.5)
        self._update_power()
        return a

    def _remove(self, a: Actor) -> None:
        self.actors.pop(a.id, None)
        if a.bld:
            for yy in range(int(a.y), int(a.y) + a.h):
                for xx in range(int(a.x), int(a.x) + a.w):
                    if self.in_map(xx, yy) and self.occ[yy][xx] == a.id:
                        self.occ[yy][xx] = 0

    def own(self, seat: int, *, units: bool = True, buildings: bool = True, wrecks: bool = False):
        for a in self.actors.values():
            if a.owner != seat or (a.wreck and not wrecks):
                continue
            if (a.bld and buildings) or (not a.bld and units):
                yield a

    def owned_types(self, seat: int) -> set[str]:
        return {a.type for a in self.actors.values() if a.owner == seat and a.bld and a.sell_tick is None}

    def fact(self, seat: int) -> Actor | None:
        facts = [a for a in self.actors.values() if a.owner == seat and a.type == "fact"]
        return min(facts, key=lambda a: a.id) if facts else None

    def base_center(self, seat: int) -> tuple[int, int]:
        f = self.fact(seat)
        if f is not None:
            return int(f.x), int(f.y)              # the yard's cell (its actor location, the top-left)
        own = [a for a in self.own(seat)]
        blds = [a for a in own if a.bld]
        pts = [a.center() for a in (blds or own)]
        if not pts:
            return self.spawns[seat]
        return int(sum(p[0] for p in pts) / len(pts)), int(sum(p[1] for p in pts) / len(pts))

    def enemy_base(self, seat: int) -> tuple[int, int]:
        bx, by = self.base_center(seat)
        return max(self.spawns, key=lambda s: ((s[0] - bx) ** 2 + (s[1] - by) ** 2, -self.spawns.index(s)))

    # ---- production ---------------------------------------------------------------------------------------------
    def available(self, seat: int) -> list[str]:
        key = (seat, frozenset(self.owned_types(seat)))
        out = self._avail_cache.get(key)
        if out is None:
            out = self._avail_cache[key] = self.rules.available(set(key[1]), self.players[seat].country)
        return list(out)

    def _producers(self, seat: int, item: str) -> list[Actor]:
        kinds = self.rules.producers(item)
        return sorted((a for a in self.actors.values() if a.owner == seat and a.type in kinds and not a.wreck
                       and a.sell_tick is None), key=lambda a: a.id)

    def _producer_for(self, seat: int, item: str) -> Actor | None:
        prods = self._producers(seat, item)
        if not prods:
            return None
        q = self.rules.queue(item) or ""
        prim = self.players[seat].primary.get("Dog" if item == "dog" else q)
        for a in prods:
            if a.id == prim:
                return a
        return prods[0]

    def _build_ticks(self, seat: int, item: str) -> int:
        it = self.rules.item(item) or {}
        base = float(it.get("build_time_s") or 1.0) * TICKS_PER_SECOND
        n = len(self._producers(seat, item))
        q = self.rules.queue(item) or ""
        table = SPEEDUP.get(q, SPEEDUP_DEFAULT)
        pct = table[min(max(n, 1), len(table)) - 1]
        return max(1, int(round(base * pct / 100.0)))

    def _tick_production(self) -> None:
        for p in self.players:
            seat = p.seat
            avail = None
            for q in QUEUES:
                items = p.queues[q]
                if not items:
                    continue
                # an item nobody can produce any more is cancelled and refunded (OpenRA ProductionQueue)
                if avail is None:
                    avail = set(self.available(seat))
                keep = []
                for it in items:
                    if it.name in avail or (it.done and self._producer_for(seat, it.name) is not None):
                        keep.append(it)
                    else:
                        p.refund(it.paid)
                p.queues[q] = items = keep
                if not items:
                    continue
                head = items[0]
                if head.done:
                    if not self.rules.is_building(head.name):
                        self._finish_unit(seat, head)
                    continue
                if head.paused:
                    continue
                if head.ticks is None:
                    head.ticks = self._build_ticks(seat, head.name)
                rate = STEP / head.ticks / (LOW_POWER_MODIFIER if p.low_power else 1)
                dprog = min(rate, 1.0 - head.progress)
                finish = head.progress + dprog >= 1.0 - 1e-9
                cost_now = (head.cost if finish else int(head.cost * (head.progress + dprog))) - head.paid
                if cost_now > 0 and not p.take(cost_now):
                    continue                   # no money: the item stalls (OpenRA: production waits)
                head.paid += max(0, cost_now)
                head.progress += dprog
                if head.progress >= 1.0 - 1e-9:
                    head.progress = 1.0
                    head.done = True
                    if self.rules.is_building(head.name):
                        self._event(seat, "production_ready", type=head.name, queue=q)
                    else:
                        self._finish_unit(seat, head)

    def _finish_unit(self, seat: int, it: Item) -> None:
        prod = self._producer_for(seat, it.name)
        if prod is None:
            return
        n = sum(1 for a in self.actors.values() if a.owner == seat and a.created == self.tick)
        ex = prod.x + prod.w / 2.0 + ((n % 3) - 1) * 0.6
        ey = prod.y + prod.h + 0.5 + (n // 3) * 0.6
        u = self._spawn_unit(seat, it.name, ex, ey)
        self.players[seat].queues[it.queue].remove(it)
        if prod.rally is not None and u.type != "harv":
            u.order = {"kind": "move", "to": (prod.rally[0] + 0.5, prod.rally[1] + 0.5), "target": None}

    # ---- placement ----------------------------------------------------------------------------------------------
    def _fits(self, x: int, y: int, w: int, h: int, gap: int = 0) -> bool:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if not self.buildable(xx, yy):
                    return False
        if gap:
            for yy in range(y - gap, y + h + gap):
                for xx in range(x - gap, x + w + gap):
                    if self.in_map(xx, yy) and self.occ[yy][xx]:
                        return False
        return True

    def _adjacent(self, seat: int, x: int, y: int, w: int, h: int) -> bool:
        for a in self.actors.values():
            if a.owner != seat or not a.bld or a.type in DEFENSE_TYPES or a.type in WALLS:
                continue
            dx = max(a.x - (x + w), 0, x - (a.x + a.w))
            dy = max(a.y - (y + h), 0, y - (a.y + a.h))
            if max(dx, dy) <= ADJACENT:
                return True
        return False

    def find_spot(self, seat: int, typ: str, at=None) -> tuple[int, int] | None:
        """Where `place` puts a building: `at` if it is a legal spot, else the nearest free spot (keeping a one-cell
        gap) around the construction yard, else None."""
        w, h = self.rules.footprint(typ)
        if at is not None:
            x, y = at
            if self._fits(x, y, w, h) and self._adjacent(seat, x, y, w, h):
                return x, y
        f = self.fact(seat)
        cx, cy = (f.center() if f is not None else self.base_center(seat))
        ox, oy = int(cx) - w // 2, int(cy) - h // 2
        for r in range(0, AUTO_PLACE_RADIUS + 1):
            ring = []
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) == r:
                        ring.append((abs(dx) + abs(dy), dy, dx))
            for _, dy, dx in sorted(ring):
                x, y = ox + dx, oy + dy
                if self._fits(x, y, w, h, gap=1) and self._adjacent(seat, x, y, w, h):
                    return x, y
        return None

    def _deploy(self, mcv: Actor) -> bool:
        cx, cy = mcv.cell()
        x, y = cx - 1, cy - 1
        w, h = self.rules.footprint("fact")
        if not self._fits(x, y, w, h):
            return False
        seat = mcv.owner
        self._remove(mcv)                  # no unit_lost: the MCV becomes the yard (a building_created event only)
        self._add_building(seat, "fact", x, y)
        return True

    # ---- power --------------------------------------------------------------------------------------------------
    def _update_power(self) -> None:
        for p in self.players:
            prov = drain = 0
            for a in self.actors.values():
                if a.owner != p.seat or not a.bld or a.powered_down:
                    continue
                pw = self.rules.power(a.type)
                if pw > 0:
                    prov += int(pw * a.hp / a.max_hp)      # ScalePowerWithHealth
                else:
                    drain -= pw
            low = drain > prov
            if low and not p.low_power:
                self._event(p.seat, "low_power", power_provided=prov, power_drained=drain)
            elif p.low_power and not low:
                self._event(p.seat, "power_restored", power_provided=prov, power_drained=drain)
            p.power_provided, p.power_drained, p.low_power = prov, drain, low

    def powered(self, a: Actor) -> bool:
        if a.powered_down:
            return False
        return not (self.players[a.owner].low_power and a.type in DISABLED_ON_LOW_POWER)

    # ---- orders: runner-side checks ------------------------------------------------------------------------------
    def check_order(self, seat: int, order) -> dict:
        """The runner's checks of one order (redalert.md section 5): {status: pending|rejected|ok, reason, detail,
        commands, failed, units (the ids that will run)}."""
        def rej(reason, detail, failed=None, cmd=None):
            return {"status": "rejected", "reason": reason, "detail": detail, "commands": 0,
                    "failed": failed or [], "cmd": cmd}

        if not isinstance(order, dict):
            return rej("invalid", "an order must be an object")
        cmd = order.get("cmd")
        if cmd not in ORDER_FIELDS:
            return rej("invalid", f"unknown cmd {cmd!r}", cmd=cmd if isinstance(cmd, str) else None)
        required, optional = ORDER_FIELDS[cmd]
        keys = set(order) - {"cmd"}
        unknown = sorted(keys - required - optional)
        if unknown:
            return rej("invalid", f"unknown field {unknown[0]!r}", cmd=cmd)
        missing = sorted(required - keys)
        if missing:
            return rej("invalid", f"missing field {missing[0]!r}", cmd=cmd)
        if "queued" in order and not isinstance(order["queued"], bool):
            return rej("invalid", "queued must be true or false", cmd=cmd)
        for f in ("to", "at"):
            if f in order:
                c = order[f]
                if not (isinstance(c, list) and len(c) == 2 and all(_is_int(v) for v in c)):
                    return rej("invalid", f"{f} must be [x, y] integers", cmd=cmd)
                if not self.in_map(c[0], c[1]):
                    return rej("invalid", f"cell {c} is outside the map ({self.width} x {self.height})", cmd=cmd)
        p = self.players[seat]
        if cmd in UNIT_CMDS:
            units = order.get("units")
            if not (isinstance(units, list) and 1 <= len(units) <= MAX_UNITS_PER_ORDER
                    and all(_is_int(u) for u in units) and len(set(units)) == len(units)):
                return rej("invalid", f"units must be 1-{MAX_UNITS_PER_ORDER} distinct integer ids", cmd=cmd)
            if cmd == "attack":
                if ("target" in order) == ("to" in order):
                    return rej("invalid", "attack takes exactly one of target / to", cmd=cmd)
                if "target" in order:
                    t = order["target"]
                    if not _is_int(t):
                        return rej("invalid", "target must be an actor id", cmd=cmd)
                    ta = self.actors.get(t)
                    if ta is None or ta.owner == seat or ta.id not in p.visible:
                        return rej("target_not_visible", f"enemy {t} is not visible", cmd=cmd)
            if cmd in ("guard", "enter_transport"):
                t = order.get("target")
                ta = self.actors.get(t) if _is_int(t) else None
                if ta is None or ta.owner != seat or ta.wreck:
                    return rej("invalid", f"target {t} is not one of your actors", cmd=cmd)
            if cmd == "set_stance" and order.get("stance") not in STANCES:
                return rej("invalid", f"stance must be one of {', '.join(STANCES)}", cmd=cmd)
            ok_ids, failed = [], []
            for u in units:
                a = self.actors.get(u)
                if a is None or a.owner != seat:
                    failed.append({"unit": u, "reason": "not_owned"})
                elif a.wreck:
                    failed.append({"unit": u, "reason": "wreck"})
                elif a.bld and cmd not in BUILDING_OK_CMDS:
                    failed.append({"unit": u, "reason": "is_building"})
                else:
                    ok_ids.append(u)
            if not ok_ids:
                return rej(failed[0]["reason"], "no unit of this order can take it", failed, cmd)
            return {"status": "pending", "reason": "pending", "detail": None, "commands": len(ok_ids),
                    "failed": failed, "units": ok_ids, "cmd": cmd}
        if cmd in ("build", "train", "place", "cancel"):
            item = order.get("item")
            if not (isinstance(item, str) and item.strip()):
                return rej("invalid", "item must be a name", cmd=cmd)
            item = item.strip().lower()
            avail = self.available(seat)
            if cmd in ("build", "train"):
                if item not in avail:
                    return rej("not_available", f"{item} is not in available_production", cmd=cmd)
                is_b = self.rules.is_building(item)
                if (cmd == "build") != is_b:
                    return rej("invalid", f"{item} is {'a building: build' if is_b else 'a unit: train'} it",
                               cmd=cmd)
                n = 1
                if cmd == "train" and "count" in order:
                    n = order["count"]
                    if not (_is_int(n) and 1 <= n <= MAX_TRAIN_COUNT):
                        return rej("invalid", f"count must be 1-{MAX_TRAIN_COUNT}", cmd=cmd)
                return {"status": "pending", "reason": "pending", "detail": None, "commands": n, "failed": [],
                        "item": item, "cmd": cmd}
            if cmd == "place":
                if not self.rules.is_building(item):
                    return rej("invalid", f"{item} is not a building", cmd=cmd)
                q = self.rules.queue(item)
                head = p.queues.get(q, [None])[0] if p.queues.get(q) else None
                if head is None or head.name != item or not head.done:
                    return rej("not_ready", f"{item} is not ready to place", cmd=cmd)
                return {"status": "pending", "reason": "pending", "detail": None, "commands": 1, "failed": [],
                        "item": item, "cmd": cmd}
            q = self.rules.queue(item)
            if not any(it.name == item for it in p.queues.get(q or "", [])):
                return rej("not_available", f"no {item} queued to cancel", cmd=cmd)
            return {"status": "pending", "reason": "pending", "detail": None, "commands": 1, "failed": [],
                    "item": item, "cmd": cmd}
        # building orders
        b = order.get("building")
        a = self.actors.get(b) if _is_int(b) else None
        if not _is_int(b):
            return rej("invalid", "building must be an id", cmd=cmd)
        if a is None or a.owner != seat:
            return rej("not_owned", f"building {b} is not yours or no longer exists", cmd=cmd)
        if not a.bld:
            return rej("invalid", f"{b} is a unit, not a building", cmd=cmd)
        if cmd in ("repair", "power_toggle") and "on" in order and not isinstance(order["on"], bool):
            return rej("invalid", "on must be true or false", cmd=cmd)
        if cmd in ("set_primary", "rally") and a.type not in PRODUCTION_TYPES:
            return rej("invalid", f"{a.type} {b} is not a production building", cmd=cmd)
        recent = self.tick - 2 * TICKS_PER_SECOND

        def noop(detail):
            return {"status": "ok", "reason": "ok", "detail": detail, "commands": 0, "failed": [], "cmd": cmd}

        if cmd == "repair":                         # idempotent (also within 2 s of the last repair order)
            on = order.get("on", True)
            if on and (a.repairing or a.repair_cmd_tick > recent):
                return noop("already_repairing")
            if not on and not a.repairing:
                return noop("not_repairing")
        if cmd == "power_toggle" and "on" in order:
            on = order["on"]
            if on and not a.powered_down:
                return noop("already_on")
            if not on and a.powered_down:
                return noop("already_off")
            if a.toggle_tick > recent:
                return noop("already_on" if on else "already_off")
        return {"status": "pending", "reason": "pending", "detail": None, "commands": 1, "failed": [], "cmd": cmd}

    def enqueue(self, seat: int, batch: int, index: int, order: dict, check: dict, apply_tick: int) -> None:
        """An accepted order, applied by the engine at `apply_tick`."""
        if order.get("cmd") in ("repair", "power_toggle"):
            b = self.actors.get(order.get("building"))
            if b is not None:
                if order["cmd"] == "repair":
                    b.repair_cmd_tick = self.tick if order.get("on", True) else -10 ** 9
                else:
                    b.toggle_tick = self.tick
        self.pending.append((apply_tick, seat, batch, index, order, check))

    # ---- orders: the engine applies them ---------------------------------------------------------------------------
    def _apply_due(self) -> None:
        due = sorted((e for e in self.pending if e[0] <= self.tick), key=lambda e: (e[0], e[1], e[2], e[3]))
        if not due:
            return
        self.pending = [e for e in self.pending if e[0] > self.tick]
        for _t, seat, batch, index, order, check in due:
            refused, accepted = self._apply(seat, order, check)
            skipped = list(check.get("failed") or [])
            failed = skipped + refused
            total = accepted + len(refused)
            if accepted == 0 and total > 0:
                status, reason = "rejected", refused[0]["reason"]
            elif failed:
                status, reason = "partial", failed[0]["reason"]
            else:
                status, reason = "ok", "ok"
            detail = refused[0].get("detail") if refused else None
            self.verdicts.append((seat, batch, index, status, reason, detail,
                                  [{k: v for k, v in f.items() if k != "detail"} for f in failed], accepted))

    def _apply(self, seat: int, order: dict, check: dict) -> tuple[list, int]:
        """Run one checked order. Returns (refused commands [{unit, reason, detail}], accepted command count)."""
        cmd = order["cmd"]
        p = self.players[seat]
        refused: list = []
        if cmd in UNIT_CMDS:
            to = order.get("to")
            if to is not None and not self.in_bounds(to[0], to[1]):
                return [{"unit": u, "reason": "invalid", "detail": f"cell {to} is outside map.bounds"}
                        for u in check["units"]], 0
            ok = 0
            for u in check["units"]:
                a = self.actors.get(u)
                if a is None or a.owner != seat or a.wreck:
                    refused.append({"unit": u, "reason": "unknown_actor", "detail": f"actor {u} no longer exists"})
                    continue
                why = self._unit_command(a, order)
                if why:
                    refused.append({"unit": u, "reason": "invalid", "detail": why})
                else:
                    ok += 1
            return refused, ok
        if cmd in ("build", "train"):
            item = check["item"]
            if item not in self.available(seat):
                return [{"unit": None, "reason": "not_available", "detail": f"{item} is no longer available"}], 0
            q = self.rules.queue(item)
            n = check["commands"]
            for _ in range(n):
                p.queues[q].append(Item(item, q, self.rules.cost(item)))
            return [], n
        if cmd == "place":
            item = check["item"]
            q = self.rules.queue(item)
            items = p.queues.get(q) or []
            if not items or items[0].name != item or not items[0].done:
                return [{"unit": None, "reason": "not_ready", "detail": f"{item} is not ready to place"}], 0
            at = order.get("at")
            at = tuple(at) if at is not None else None
            spot = self.find_spot(seat, item, at)
            if spot is None:
                return [{"unit": None, "reason": "invalid", "detail": f"no free cell near your base for {item}"}], 0
            items.pop(0)
            self._add_building(seat, item, spot[0], spot[1])
            return [], 1
        if cmd == "cancel":
            item = check["item"]
            q = self.rules.queue(item)
            items = p.queues.get(q) or []
            for i in range(len(items) - 1, -1, -1):
                if items[i].name == item:
                    p.refund(items[i].paid)
                    items.pop(i)
                    return [], 1
            return [{"unit": None, "reason": "not_available", "detail": f"no {item} queued"}], 0
        b = self.actors.get(order.get("building"))
        if b is None or b.owner != seat or not b.bld:
            return [{"unit": None, "reason": "unknown_actor", "detail": "building no longer exists"}], 0
        if cmd == "sell":
            if b.sell_tick is None:
                b.sell_tick = self.tick + SELL_TICKS
            return [], 1
        if cmd == "repair":
            on = order.get("on", True)
            b.repairing = bool(on) and b.hp < b.max_hp
            return [], 1
        if cmd == "power_toggle":
            if b.type not in POWER_TOGGLE:
                return [{"unit": None, "reason": "invalid", "detail": f"{b.type} cannot be powered down"}], 0
            on = order.get("on")
            b.powered_down = (not b.powered_down) if on is None else (not on)
            self._update_power()
            return [], 1
        if cmd == "set_primary":
            q = "Dog" if b.type == "kenn" else {"fact": "Building", "tent": "Infantry", "barr": "Infantry",
                                                 "weap": "Vehicle", "syrd": "Ship", "spen": "Ship"}.get(b.type,
                                                                                                         "Aircraft")
            p.primary[q] = b.id
            return [], 1
        if cmd == "rally":
            to = order["to"]
            if not self.in_bounds(to[0], to[1]):
                return [{"unit": None, "reason": "invalid", "detail": f"cell {to} is outside map.bounds"}], 0
            b.rally = (to[0], to[1])
            return [], 1
        return [{"unit": None, "reason": "invalid", "detail": f"unknown cmd {cmd}"}], 0

    def _unit_command(self, a: Actor, order: dict) -> str | None:
        """Apply one engine command to one actor; a refusal reason text, or None."""
        cmd = order["cmd"]
        queued = bool(order.get("queued"))
        to = order.get("to")
        pt = (to[0] + 0.5, to[1] + 0.5) if to is not None else None
        if cmd == "set_stance":
            a.stance = order["stance"]
            if a.stance == "hold_fire":
                a.engage = None
            return None
        if cmd == "stop":
            a.order, a.oq, a.engage = None, [], None
            if a.type == "harv":
                a.hstate = None
            return None
        if a.bld:
            if cmd == "attack":
                t = order.get("target")
                if t is None:
                    return "a building cannot attack the ground"
                a.order = {"kind": "attack", "to": None, "target": t}
                return None
            if cmd == "guard":
                return None
            return "a building cannot do that"
        new = None
        if cmd == "move":
            new = {"kind": "move", "to": pt, "target": None}
        elif cmd == "attack_move":
            if a.weapon is None:
                new = {"kind": "move", "to": pt, "target": None}
            else:
                new = {"kind": "attack_move", "to": pt, "target": None}
        elif cmd == "attack":
            if a.weapon is None:
                return f"{a.type} has no weapon"
            new = {"kind": "attack", "to": pt, "target": order.get("target")}
        elif cmd == "guard":
            new = {"kind": "guard", "to": None, "target": order["target"]}
        elif cmd == "deploy":
            if a.type != "mcv":
                return f"{a.type} cannot deploy"
            cx, cy = a.cell()
            if not self._fits(cx - 1, cy - 1, *self.rules.footprint("fact")):
                return "no room to deploy here"
            if a.order is not None and a.order["kind"] == "deploy" and a.deploy_tick is not None:
                return None                        # already unfolding: a repeated deploy changes nothing
            a.order, a.oq = {"kind": "deploy", "to": None, "target": None}, []
            a.deploy_tick = self.tick + DEPLOY_TICKS
            return None
        elif cmd == "harvest":
            if a.type != "harv":
                return f"{a.type} cannot harvest"
            a.order, a.oq = None, []
            a.hstate = "seek"
            a.hcell = tuple(to) if to is not None and tuple(to) in self.ore else None
            if a.hcell is not None:
                a.hstate = "to_ore"
            return None
        elif cmd == "enter_transport":
            t = self.actors.get(order["target"])
            if t is None or t.type not in TRANSPORTS or not self.rules.is_infantry(a.type):
                return "only infantry can enter a transport"
            new = {"kind": "enter", "to": None, "target": t.id}
        elif cmd == "unload":
            if a.type not in TRANSPORTS:
                return f"{a.type} carries nobody"
            self._unload(a)
            return None
        if new is None:
            return "unsupported"
        if a.type == "harv":
            a.hstate = None
        if queued and (a.order is not None):
            a.oq.append(new)
        else:
            a.order, a.oq, a.engage = new, [], None
        return None

    def _unload(self, t: Actor) -> None:
        for i, (typ, hp) in enumerate(t.passengers):
            u = self._spawn_unit(t.owner, typ, t.x + (i % 3) - 1, t.y + 1 + i // 3, event=False)
            u.hp = hp
        t.passengers = []

    # ---- the step --------------------------------------------------------------------------------------------------
    def step(self) -> None:
        """Advance one view (STEP ticks)."""
        if self.over:
            return
        self.tick += STEP
        self._apply_due()
        self._tick_production()
        self._update_power()
        buckets = self._buckets()
        self._tick_units(buckets)
        self._tick_combat(buckets)
        self._tick_buildings()
        if self.tick % ORE_GROW_EVERY == 0:
            self._grow_ore()
        self._update_visibility(force_explore=self.tick % EXPLORE_EVERY == 0)
        self._update_stats()
        self._check_victory()

    def _buckets(self) -> list[dict]:
        """Each side's live actors by BUCKET x BUCKET square, and (self._reach) the squares within REACH of them:
        an actor whose square is outside the other side's reach has nothing of it within 2 * BUCKET - 4 cells."""
        out = [{}, {}]
        for a in self.actors.values():
            if a.wreck:
                continue
            cx, cy = a.center()
            out[a.owner].setdefault((int(cx) // BUCKET, int(cy) // BUCKET), []).append(a)
        self._reach = []
        for b in out:
            s = set()
            for bx, by in b:
                for dy in (-2, -1, 0, 1, 2):
                    for dx in (-2, -1, 0, 1, 2):
                        s.add((bx + dx, by + dy))
            self._reach.append(s)
        return out

    def _near(self, bucket: dict, x: float, y: float, r: float):
        k = int(math.ceil((r + 4) / BUCKET))
        bx, by = int(x) // BUCKET, int(y) // BUCKET
        for dy in range(-k, k + 1):
            for dx in range(-k, k + 1):
                for a in bucket.get((bx + dx, by + dy), ()):
                    yield a

    # ---- units -----------------------------------------------------------------------------------------------------
    def _move_toward(self, a: Actor, tx: float, ty: float, factor: float = 1.0) -> bool:
        """Move a unit toward a point; True when it got there."""
        step = a.speed * DT * factor
        dx, dy = tx - a.x, ty - a.y
        d = math.hypot(dx, dy)
        if d <= max(step, 0.05):
            a.x, a.y = tx, ty
            return True
        a.x += dx / d * step
        a.y += dy / d * step
        a.x = min(max(a.x, self.bx0 + 0.01), self.bx1 + 0.99)
        a.y = min(max(a.y, self.by0 + 0.01), self.by1 + 0.99)
        return False

    def _next_order(self, a: Actor) -> None:
        a.order = a.oq.pop(0) if a.oq else None

    def _tick_units(self, buckets) -> None:
        for a in sorted(self.actors.values(), key=lambda x: x.id):
            if a.bld:
                continue
            if a.wreck:
                continue
            o = a.order
            if a.type == "harv" and o is None and a.hstate is not None:
                self._tick_harvester(a)
                continue
            if o is None:
                continue
            kind = o["kind"]
            if kind == "move":
                if self._move_toward(a, *o["to"]):
                    self._next_order(a)
            elif kind == "deploy":
                if a.deploy_tick is not None and self.tick >= a.deploy_tick:
                    a.deploy_tick = None
                    if not self._deploy(a):
                        a.order = None
            elif kind == "guard":
                t = self.actors.get(o["target"])
                if t is None or t.owner != a.owner or t.wreck:
                    self._next_order(a)
                elif a.engage is None and a.dist_from(*t.center()) > 2.5:
                    self._move_toward(a, *t.nearest_point(a.x, a.y))
            elif kind == "enter":
                t = self.actors.get(o["target"])
                if t is None or t.owner != a.owner or t.wreck:
                    self._next_order(a)
                elif a.dist_from(*t.center()) <= 1.5:
                    if len(t.passengers) < TRANSPORTS.get(t.type, 0):
                        t.passengers.append((a.type, a.hp))
                        self._remove(a)
                        self._event(a.owner, "unit_lost", id=a.id, type=a.type, x=a.cell()[0], y=a.cell()[1])
                    else:
                        self._next_order(a)
                else:
                    self._move_toward(a, t.x, t.y)
            # attack / attack_move movement happens in _tick_combat (they depend on targets)

    def _tick_harvester(self, a: Actor) -> None:
        seat = a.owner
        p = self.players[seat]
        procs = [b for b in self.actors.values() if b.owner == seat and b.type == "proc" and b.sell_tick is None]
        st = a.hstate
        if st == "seek":
            if a.cargo >= HARVESTER_CAPACITY:
                a.hstate = "to_proc"
                return
            cell = self._find_ore(a)
            if cell is None:
                if a.cargo > 0 and procs:
                    a.hstate = "to_proc"
                return                                     # nothing to harvest: waits (act Harvest, not idle)
            a.hcell, a.hstate = cell, "to_ore"
            return
        if st == "to_ore":
            if a.hcell not in self.ore or self.ore[a.hcell][1] <= 0:
                a.hstate = "seek"
                return
            if self._move_toward(a, a.hcell[0] + 0.5, a.hcell[1] + 0.5):
                a.hstate, a.htick = "loading", 0
            return
        if st == "loading":
            a.htick += STEP
            while a.htick >= BALE_TICKS and a.cargo < HARVESTER_CAPACITY:
                cell = self.ore.get(a.hcell)
                if cell is None or cell[1] <= 0:
                    self.ore.pop(a.hcell, None)
                    a.hstate = "seek"
                    return
                a.htick -= BALE_TICKS
                cell[1] -= 1
                a.cargo += 1
                a.cargo_value += cell[0]
                if cell[1] <= 0:
                    self.ore.pop(a.hcell, None)
            if a.cargo >= HARVESTER_CAPACITY:
                a.hstate = "to_proc"
            return
        if st == "to_proc":
            if not procs:
                return
            proc = min(procs, key=lambda b: (b.dist_from(a.x, a.y), b.id))
            dock = (proc.x + proc.w / 2.0, proc.y + proc.h - 0.5)
            if self._move_toward(a, dock[0], dock[1], HARV_FULL_SPEED if a.cargo else 1.0):
                a.hstate, a.htick = "unloading", -DOCK_TICKS
            return
        if st == "unloading":
            a.htick += STEP
            cap = self.capacity(seat)
            while a.htick >= BALE_UNLOAD_TICKS and a.cargo > 0:
                per = a.cargo_value // a.cargo if a.cargo > 1 else a.cargo_value
                if p.ore + per > cap:
                    p.storage_full = True
                    return                                 # storage full: the harvester waits with its load
                a.htick -= BALE_UNLOAD_TICKS
                a.cargo -= 1
                a.cargo_value -= per
                p.ore += per
            if a.cargo <= 0:
                a.cargo, a.cargo_value = 0, 0
                a.hstate = "seek"
            return

    def capacity(self, seat: int) -> int:
        return sum(STORAGE.get(b.type, 0) for b in self.actors.values() if b.owner == seat and b.bld)

    def _find_ore(self, a: Actor) -> tuple[int, int] | None:
        best, bd = None, None
        ax, ay = a.x, a.y
        for c, (_v, dens) in self.ore.items():
            if dens <= 0:
                continue
            d = (c[0] + 0.5 - ax) ** 2 + (c[1] + 0.5 - ay) ** 2
            if bd is None or d < bd or (d == bd and c < best):
                best, bd = c, d
        return best

    def _grow_ore(self) -> None:
        for mx, my in self.mines:
            cells = [(mx + dx, my + dy) for dy in (-2, -1, 0, 1, 2) for dx in (-2, -1, 0, 1, 2) if dx or dy]
            cells = [c for c in cells if self.in_bounds(*c) and self.terrain[c[1]][c[0]] not in BLOCKED
                     and self.occ[c[1]][c[0]] == 0]
            if not cells:
                continue
            c = cells[self.rng.randrange(len(cells))]
            cur = self.ore.get(c)
            if cur is None:
                self.ore[c] = [ORE_VALUE, 1]
            elif cur[1] < MAX_ORE_DENSITY:
                cur[1] += 1

    # ---- combat ----------------------------------------------------------------------------------------------------
    def _can_hit(self, a: Actor, t: Actor) -> bool:
        only = a.weapon[4]
        if only == "infantry":
            return not t.bld and self.rules.is_infantry(t.type)
        return True

    def _auto_ok(self, a: Actor, t: Actor, structures: bool) -> bool:
        """OpenRA AutoTargetPriority: units and defenses; plain buildings only under attack_anything."""
        if t.wreck or not self._can_hit(a, t):
            return False
        if t.bld and t.type not in DEFENSE_TYPES and not structures:
            return False
        return True

    def _scan(self, a: Actor, buckets, radius: float, structures: bool) -> Actor | None:
        enemy = buckets[1 - a.owner]
        vis = self.players[a.owner].visible
        cx, cy = a.center()
        if radius <= 2 * BUCKET - 4 and (int(cx) // BUCKET, int(cy) // BUCKET) not in self._reach[1 - a.owner]:
            return None
        best, bd = None, None
        for t in self._near(enemy, cx, cy, radius):
            if t.id not in vis or not self._auto_ok(a, t, structures):
                continue
            d = t.dist_from(cx, cy)
            if d <= radius and (bd is None or d < bd or (d == bd and t.id < best.id)):
                best, bd = t, d
        return best

    def _valid_enemy(self, a: Actor, t: Actor | None) -> bool:
        return t is not None and t.id in self.actors and not t.wreck and t.hp > 0 and t.owner != a.owner \
            and t.id in self.players[a.owner].visible

    def _tick_combat(self, buckets) -> None:
        kills: list[tuple[Actor, Actor]] = []
        recent = 2 * TICKS_PER_SECOND
        for a in sorted(self.actors.values(), key=lambda x: x.id):
            if a.wreck or a.weapon is None or a.hp <= 0 or a.id not in self.actors:
                continue
            if a.bld and (not self.powered(a) or a.sell_tick is not None):
                continue
            rng = a.weapon[0]
            o = a.order
            kind = o["kind"] if o is not None else None
            target, chase = None, False
            if kind == "attack" and o["target"] is not None:
                t = self.actors.get(o["target"])
                if self._valid_enemy(a, t):
                    target, chase = t, not a.bld
                else:                                   # dead or out of sight: the attack ends
                    self._next_order(a)
                    o = a.order
                    kind = o["kind"] if o is not None else None
            elif kind == "attack":                      # attack the ground
                tx, ty = o["to"]
                if math.hypot(tx - a.x, ty - a.y) > rng:
                    self._move_toward(a, tx, ty)
                else:
                    t = self.actors.get(self.occ[int(ty)][int(tx)]) if self.in_map(int(tx), int(ty)) else None
                    if t is not None and t.owner != a.owner and t.hp > 0:
                        target = t
                if target is None:
                    a.cd = max(a.cd - STEP, 0)
                    continue
            if target is None and kind in (None, "attack_move", "guard") and a.stance != "hold_fire":
                structures = a.stance == "attack_anything"
                allow_move = not a.bld and (kind == "attack_move" or (kind is None and structures))
                cx, cy = a.center()
                cur = self.actors.get(a.engage) if a.engage is not None else None
                if not (self._valid_enemy(a, cur) and self._auto_ok(a, cur, structures)) or \
                        (not allow_move and cur.dist_from(cx, cy) > rng):
                    cur = None
                if cur is None and a.stance != "return_fire":
                    cur = self._scan(a, buckets, rng, structures)
                if cur is None and a.hit_by is not None and self.tick - a.hit_tick < recent:
                    att = self.actors.get(a.hit_by)    # fire back at whoever shot us
                    if self._valid_enemy(a, att) and self._can_hit(a, att) and \
                            (allow_move or att.dist_from(cx, cy) <= rng):
                        cur = att
                a.engage = cur.id if cur is not None else None
                target, chase = cur, cur is not None and allow_move
            if target is None:
                a.cd = max(a.cd - STEP, 0)
                if kind == "attack_move" and self._move_toward(a, *o["to"]):
                    self._next_order(a)
                continue
            cx, cy = a.center()
            a.cd -= STEP
            if target.dist_from(cx, cy) > rng:
                a.cd = max(a.cd, 0)
                if chase:
                    self._move_toward(a, *target.nearest_point(a.x, a.y))
                elif kind == "attack_move":
                    self._move_toward(a, *o["to"])
                continue
            while a.cd <= 0 and target.hp > 0:
                self._damage(a, target)
                a.cd += a.weapon[2]
            if target.hp <= 0:
                kills.append((a, target))
                a.engage = None
                if kind == "attack" and o.get("target") == target.id:
                    self._next_order(a)
        for killer, victim in kills:
            if victim.id in self.actors and not victim.wreck:
                self._kill(victim, killer)
        for p in self.players:
            if p.kills_this_step:
                self._event(p.seat, "enemy_killed", count=len(p.kills_this_step), types=list(p.kills_this_step))
                p.kills_this_step = []

    def _damage(self, a: Actor, t: Actor) -> None:
        dmg = a.weapon[1] * a.weapon[3].get(t.armor, 100) / 100.0
        if dmg <= 0:
            return
        t.hp -= dmg
        t.hit_by, t.hit_tick = a.id, self.tick
        if self.tick - t.ua_tick >= UNDER_ATTACK_EVERY:
            t.ua_tick = self.tick
            cx, cy = (int(t.x), int(t.y))
            self._event(t.owner, "under_attack", id=t.id, type=t.type, x=cx, y=cy, attacker_type=a.type)

    def _kill(self, victim: Actor, killer: Actor | None) -> None:
        seat = victim.owner
        p = self.players[seat]
        cx, cy = int(victim.x), int(victim.y)
        self._remove(victim)
        p.mil["deaths_cost"] += victim.cost
        if killer is not None and killer.owner != seat:
            k = self.players[killer.owner]
            k.mil["kills_cost"] += victim.cost
            k.mil["buildings_killed" if victim.bld else "units_killed"] += 1
            k.kills_this_step.append(victim.type)
        p.mil["buildings_lost" if victim.bld else "units_lost"] += 1
        if victim.bld:
            self._event(seat, "building_lost", id=victim.id, type=victim.type, x=cx, y=cy)
            self._update_power()
        else:
            extra = {}
            if self.rules.husk(victim.type):
                h = Actor(self._new_id(), seat, victim.type + ".husk", self.rules, victim.x, victim.y, self.tick)
                h.wreck, h.expire, h.weapon, h.cost, h.speed = True, self.tick + HUSK_TICKS, None, 0, 0.0
                h.max_hp = h.hp = 28000
                self.actors[h.id] = h
                extra["wreck_id"] = h.id
            self._event(seat, "unit_lost", id=victim.id, type=victim.type, x=cx, y=cy, **extra)
            for typ, _hp in victim.passengers:          # passengers die with their transport
                p.mil["units_lost"] += 1
                p.mil["deaths_cost"] += self.rules.cost(typ)

    def _tick_buildings(self) -> None:
        for a in sorted(self.actors.values(), key=lambda x: x.id):
            if a.wreck:
                if a.expire is not None and self.tick >= a.expire:
                    self._remove(a)
                continue
            if not a.bld:
                continue
            if a.sell_tick is not None and self.tick >= a.sell_tick:
                p = self.players[a.owner]
                p.refund(int(a.cost * SELL_REFUND * a.hp / a.max_hp))
                self._remove(a)
                self._event(a.owner, "building_sold", id=a.id, type=a.type)
                self._spawn_unit(a.owner, "e1", a.x + a.w / 2.0, a.y + a.h + 0.5)
                self._update_power()
                continue
            if a.repairing:
                if a.hp >= a.max_hp:
                    a.repairing = False
                    continue
                a.repair_tick += STEP
                if a.repair_tick >= REPAIR_INTERVAL_TICKS:
                    a.repair_tick -= REPAIR_INTERVAL_TICKS
                    step_hp = min(REPAIR_STEP_HP, a.max_hp - a.hp)
                    cost = max(1, int(round(a.cost * REPAIR_COST_PERCENT / 100.0 * step_hp / a.max_hp)))
                    if self.players[a.owner].take(cost):
                        a.hp = min(a.max_hp, a.hp + step_hp)

    # ---- fog of war --------------------------------------------------------------------------------------------------
    def _update_visibility(self, force_explore: bool = False) -> None:
        buckets = self._buckets()
        for p in self.players:
            seat = p.seat
            mine = buckets[seat]
            reach = self._reach[seat]
            vis = set()
            for e in self.actors.values():
                if e.owner == seat:
                    continue
                ex, ey = e.center()
                if (int(ex) // BUCKET, int(ey) // BUCKET) not in reach:
                    continue                           # no actor of ours within 12 cells (sight is at most 10)
                for a in self._near(mine, ex, ey, 10.5):
                    ax, ay = a.center()
                    if e.dist_from(ax, ay) <= a.sight:
                        vis.add(e.id)
                        break
            for eid in sorted(vis - p.spotted):
                e = self.actors[eid]
                if e.wreck:
                    continue
                p.spotted.add(eid)
                self._event(seat, "enemy_spotted", id=eid, type=e.type, x=int(e.x), y=int(e.y))
            p.visible = vis
            if force_explore and not self.explored_map:
                self._explore(p)

    def _explore(self, p: Player) -> None:
        w = self.width
        for a in self.actors.values():
            if a.owner != p.seat or a.wreck:
                continue
            cx, cy = a.center()
            key = (int(cx), int(cy))
            if a.mark == key:
                continue
            a.mark = key
            r = int(math.ceil(a.sight))
            r2 = a.sight * a.sight
            for dy in range(-r, r + 1):
                y = key[1] + dy
                if not self.by0 <= y <= self.by1:
                    continue
                for dx in range(-r, r + 1):
                    x = key[0] + dx
                    if self.bx0 <= x <= self.bx1 and dx * dx + dy * dy <= r2 and not p.explored[y * w + x]:
                        p.explored[y * w + x] = 1
                        p.explored_count += 1

    def explored_percent(self, seat: int) -> float:
        p = self.players[seat]
        if self.explored_map:
            return 100.0
        tot = (self.bx1 - self.bx0 + 1) * (self.by1 - self.by0 + 1)
        return round(100.0 * p.explored_count / max(1, tot), 1)

    # ---- stats / victory -------------------------------------------------------------------------------------------
    def _update_stats(self) -> None:
        for p in self.players:
            assets = army = 0
            for a in self.actors.values():
                if a.owner != p.seat or a.wreck:
                    continue
                assets += a.cost
                if not a.bld and self.rules.can_fight(a.type):
                    army += a.cost
            p.mil["assets_value"], p.mil["army_value"] = assets, army
            p.peak_army = max(p.peak_army, army)

    def alive(self, seat: int) -> bool:
        for a in self.actors.values():
            if a.owner == seat and not a.wreck and ((a.bld and a.type not in WALLS) or a.type == "mcv"):
                return True
        return False

    def _check_victory(self) -> None:
        dead = [not self.alive(s) for s in (0, 1)]
        for s in (0, 1):
            self.players[s].defeated = dead[s]
        if dead[0] and dead[1]:
            self.over, self.winner = True, -1
        elif dead[0] or dead[1]:
            self.over, self.winner = True, 1 if dead[0] else 0

    def tiebreak(self, seat: int) -> tuple[int, int]:
        m = self.players[seat].mil
        return m["kills_cost"] - m["deaths_cost"], m["assets_value"]

    # ---- the seat's view ----------------------------------------------------------------------------------------------
    def _unit_view(self, a: Actor) -> dict:
        cx, cy = a.cell()
        if a.wreck:
            return {"id": a.id, "type": a.type, "x": cx, "y": cy, "hp": max(1, int(100 * a.hp / a.max_hp)),
                    "act": None, "idle": True, "stance": a.stance, "wreck": True}
        return {"id": a.id, "type": a.type, "x": cx, "y": cy, "hp": max(1, int(math.ceil(100 * a.hp / a.max_hp))),
                "act": self._act(a), "idle": self.idle(a), "stance": a.stance}

    def _act(self, a: Actor) -> str | None:
        o = a.order
        if o is not None:
            return {"move": "Move", "attack_move": "AttackMove", "attack": "Attack", "guard": "Guard",
                    "deploy": "Deploy", "enter": "EnterTransport"}.get(o["kind"], o["kind"])
        if a.type == "harv" and a.hstate is not None:
            return "Harvest"
        if a.engage is not None:
            return "Attack"
        return None

    def idle(self, a: Actor) -> bool:
        return a.order is None and a.engage is None and not (a.type == "harv" and a.hstate is not None)

    def _producing(self, seat: int) -> dict[int, str]:
        out = {}
        p = self.players[seat]
        for q in QUEUES:
            items = p.queues[q]
            if items and not items[0].done:
                prod = self._producer_for(seat, items[0].name)
                if prod is not None and prod.id not in out:
                    out[prod.id] = items[0].name
        return out

    def observation(self, seat: int) -> dict:
        p = self.players[seat]
        e = self.players[1 - seat]
        producing = self._producing(seat)
        units, buildings = [], []
        for a in sorted(self.actors.values(), key=lambda x: x.id):
            if a.owner != seat:
                continue
            if a.bld:
                buildings.append({"id": a.id, "type": a.type, "x": int(a.x), "y": int(a.y),
                                  "hp": max(1, int(math.ceil(100 * a.hp / a.max_hp))), "powered": self.powered(a),
                                  "powered_down": a.powered_down, "repairing": a.repairing,
                                  "producing": producing.get(a.id),
                                  "rally": list(a.rally) if a.rally is not None else None})
            else:
                units.append(self._unit_view(a))
        production = []
        for q in QUEUES:
            for i, it in enumerate(p.queues[q]):
                ticks = it.ticks or self._build_ticks(seat, it.name)
                slow = LOW_POWER_MODIFIER if p.low_power else 1
                rem = 0.0 if it.done else (1.0 - it.progress) * ticks * slow / TICKS_PER_SECOND
                production.append({"queue": q, "item": it.name, "progress": round(it.progress, 3),
                                   "remaining_s": round(rem, 1), "paused": it.paused, "ready": bool(it.done)})
        vis_units, vis_blds = [], []
        for eid in sorted(p.visible):
            a = self.actors.get(eid)
            if a is None:
                continue
            row = {"id": a.id, "type": a.type, "x": int(a.x), "y": int(a.y),
                   "hp": max(1, int(math.ceil(100 * a.hp / a.max_hp)))}
            if a.wreck:
                row["wreck"] = True
            (vis_blds if a.bld else vis_units).append(row)
        bc = self.base_center(seat)
        harvs = sum(1 for a in self.actors.values() if a.owner == seat and a.type == "harv" and not a.wreck)
        return {
            "tick": self.tick, "faction": p.country, "enemy_faction": e.country,
            "economy": {"cash": int(p.cash), "ore": int(p.ore), "power_provided": p.power_provided,
                        "power_drained": p.power_drained, "resource_capacity": self.capacity(seat),
                        "harvesters": harvs},
            "military": dict(p.mil), "units": units, "buildings": buildings, "production": production,
            "available_production": self.available(seat), "visible_enemies": vis_units,
            "visible_enemy_buildings": vis_blds, "map": {"id": self.map_id, "width": self.width,
                                                         "height": self.height},
            "base_center": [bc[0], bc[1]], "explored_percent": self.explored_percent(seat),
            "events": [dict(ev) for ev in p.events[-EVENT_WINDOW:]],
        }

    def legal_actions(self, seat: int, obs: dict | None = None) -> dict:
        obs = obs if obs is not None else self.observation(seat)
        p = self.players[seat]
        avail = obs["available_production"]
        place = []
        for q in ("Building", "Defense"):
            items = p.queues[q]
            if items and items[0].done:
                place.append(items[0].name)
        own_units = [a for a in self.own(seat, buildings=False)]
        idle = [a for a in own_units if self.idle(a)]
        fighters = [a for a in own_units if self.rules.can_fight(a.type)]
        combat_idle = [a.id for a in idle if self.rules.can_fight(a.type)]
        damaged = sorted((a for a in self.own(seat, units=False) if a.hp < a.max_hp and not a.repairing),
                         key=lambda a: (a.hp / a.max_hp, a.id))
        attack = [r["id"] for r in obs["visible_enemies"] if not r.get("wreck")] + \
                 [r["id"] for r in obs["visible_enemy_buildings"] if not r.get("wreck")]
        eb = self.enemy_base(seat)
        attack_now = None
        if fighters:
            group = [a for a in fighters if self.idle(a)] or fighters
            group = sorted(group, key=lambda a: a.id)[:MAX_UNITS_PER_ORDER]
            gx = sum(a.x for a in group) / len(group)
            gy = sum(a.y for a in group) / len(group)
            enemies = [self.actors[i] for i in attack if i in self.actors]
            if enemies:
                t = min(enemies, key=lambda e: (e.dist_from(gx, gy), e.id))
                to = [int(t.x), int(t.y)]
            else:
                to = [eb[0], eb[1]]
            attack_now = {"cmd": "attack_move", "units": [a.id for a in group], "to": to}
        return {
            "build": [i for i in avail if self.rules.is_building(i)],
            "train": [i for i in avail if not self.rules.is_building(i)],
            "place": place,
            "deploy": [a.id for a in own_units if a.type == "mcv"],
            "repair": [a.id for a in damaged],
            "attack": attack,
            "attack_now": attack_now,
            "enemy_base": [eb[0], eb[1]],
            "combat_units": combat_idle,
            "idle_units": [a.id for a in idle],
            "harvesters": [a.id for a in own_units if a.type == "harv"],
            "production_buildings": [{"id": a.id, "type": a.type} for a in self.own(seat, units=False)
                                     if a.type in PRODUCTION_TYPES],
            "how_to_send": "play_action(session_id, state_version=<this state_version>, action={\"type\": \"orders\", "
                           "\"orders\": [<1-20 orders>]}); formats and examples: get_game_config('red_alert')"
                           ".order_schema",
        }
