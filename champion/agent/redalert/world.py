"""What the Red Alert bot knows: the per-match static data (rules, map grid, spawns, the ground path between the bases,
waypoints, ore) and a parsed snapshot of one state (`View`). Pure data, never raises on odd payloads: missing fields
read as empty, unknown types get default numbers."""
from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path

TICKS_PER_SECOND = 25
CONFIG_FILE = Path(__file__).resolve().parents[2] / "data" / "red_alert_config.json"

ALLIES = frozenset({"allies", "england", "france", "germany"})
SOVIET = frozenset({"soviet", "russia", "ukraine"})
# Not combat units (redalert.md: legal_actions.combat_units leaves them out), plus transports / special units.
NON_COMBAT = frozenset({"mcv", "harv", "medi", "mech", "e6", "spy", "spy.england", "thf", "truk", "mnly", "mgg", "mrj",
                        "lst", "tran", "dtrk", "qtnk"})
AIR = frozenset({"heli", "mh60", "yak", "mig", "hind", "badr", "u2", "tran"})
INFANTRY = frozenset({"e1", "e2", "e3", "e4", "e6", "e7", "dog", "shok", "medi", "mech", "spy", "spy.england", "thf",
                      "ivan", "zombie", "ant"})
# Enemy defenses that shoot ground units (agun / sam are anti-air only).
GROUND_DEFENSES = frozenset({"pbox", "hbox", "gun", "ftur", "tsla", "gap_never"})
DEFENSES = frozenset({"pbox", "hbox", "gun", "ftur", "tsla", "agun", "sam", "gap", "pdox", "iron", "mslo"})
WALLS = frozenset({"sbag", "fenc", "brik", "cycl", "barb", "wood"})
PRODUCTION = frozenset({"tent", "barr", "weap", "kenn", "syrd", "spen", "hpad", "afld", "afld.ukraine"})
POWER_DOWNABLE = frozenset({"tsla", "agun", "sam", "gap", "dome"})
# Layout characters: ground units cross these (M = ore mine actor: treated as blocked), and these take buildings.
PASSABLE = frozenset(".,=$*S_")
BUILDABLE = frozenset(".,=S_")
ORE = frozenset("$*")
DEFAULT_COST = 500
COVER_STEP = 10                 # spacing of the map-wide search points (units see 4-5 cells around them)
# Built-in fallbacks when the reference is missing (game_config None and no local copy).
FALLBACK_ITEMS = {
    "powr": (300, 100, 7.2, "Building", (2, 3)), "apwr": (500, 200, 12.0, "Building", (3, 3)),
    "proc": (1400, -30, 33.6, "Building", (3, 4)), "tent": (500, -20, 12.0, "Building", (2, 3)),
    "barr": (500, -20, 12.0, "Building", (2, 3)), "weap": (2000, -30, 48.0, "Building", (3, 3)),
    "fix": (1200, -30, 28.8, "Building", (3, 3)), "dome": (1500, -40, 36.0, "Building", (2, 2)),
    "silo": (150, -10, 3.6, "Defense", (1, 1)), "pbox": (600, -20, 14.4, "Defense", (1, 1)),
    "gun": (800, -40, 19.2, "Defense", (1, 1)), "ftur": (600, -20, 14.4, "Defense", (1, 1)),
    "tsla": (1200, -80, 28.8, "Defense", (1, 1)), "fact": (2000, 0, 0.0, "Building", (3, 4)),
    "e1": (100, 0, 2.4, "Infantry", None), "e2": (150, 0, 3.6, "Infantry", None),
    "e3": (300, 0, 7.2, "Infantry", None), "1tnk": (700, 0, 16.8, "Vehicle", None),
    "2tnk": (850, 0, 20.4, "Vehicle", None), "3tnk": (1150, 0, 27.6, "Vehicle", None),
    "harv": (1100, 0, 26.4, "Vehicle", None), "mcv": (2000, 0, 40.0, "Vehicle", None),
    "ftrk": (600, 0, 14.4, "Vehicle", None), "apc": (850, 0, 20.4, "Vehicle", None),
    "jeep": (500, 0, 12.0, "Vehicle", None),
}
_LOCAL_CONFIG: dict | None = None


def local_config() -> dict:
    """data/red_alert_config.json (the saved get_game_config), read once; {} when missing."""
    global _LOCAL_CONFIG
    if _LOCAL_CONFIG is None:
        try:
            _LOCAL_CONFIG = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            _LOCAL_CONFIG = {}
    return _LOCAL_CONFIG


def _int(v, default=0) -> int:
    try:
        if isinstance(v, bool):
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


def _num(v, default=0.0) -> float:
    try:
        if isinstance(v, bool):
            return default
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _cell(obj) -> tuple[int, int] | None:
    if isinstance(obj, (list, tuple)) and len(obj) == 2:
        x, y = obj
    elif isinstance(obj, dict):
        x, y = obj.get("x"), obj.get("y")
    else:
        return None
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    try:
        return int(x), int(y)
    except (TypeError, ValueError):
        return None


def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def side_of(faction) -> str:
    f = str(faction or "").lower()
    return "soviet" if f in SOVIET else "allies"


class Rules:
    """Item numbers from the reference (game_config.rules), the local copy, then FALLBACK_ITEMS."""

    def __init__(self, config: dict | None):
        rules = (config or {}).get("rules") if isinstance(config, dict) else None
        if not isinstance(rules, dict) or not rules.get("buildings"):
            rules = local_config().get("rules") or {}
        self.buildings = {str(k).lower(): v for k, v in (rules.get("buildings") or {}).items() if isinstance(v, dict)}
        self.units = {str(k).lower(): v for k, v in (rules.get("units") or {}).items() if isinstance(v, dict)}
        self.maps = rules.get("maps") if isinstance(rules.get("maps"), dict) else {}
        if not self.maps:
            self.maps = (local_config().get("rules") or {}).get("maps") or {}

    def item(self, name) -> dict:
        n = str(name or "").lower()
        return self.buildings.get(n) or self.units.get(n) or {}

    def cost(self, name) -> int:
        it = self.item(name)
        if it.get("cost") is not None:
            return max(0, _int(it.get("cost"), DEFAULT_COST))
        fb = FALLBACK_ITEMS.get(str(name or "").lower())
        return fb[0] if fb else DEFAULT_COST

    def power(self, name) -> int:
        it = self.item(name)
        if it.get("power") is not None:
            return _int(it.get("power"))
        fb = FALLBACK_ITEMS.get(str(name or "").lower())
        return fb[1] if fb else 0

    def build_time(self, name) -> float:
        it = self.item(name)
        if it.get("build_time_s") is not None:
            return _num(it.get("build_time_s"), 10.0)
        fb = FALLBACK_ITEMS.get(str(name or "").lower())
        return fb[2] if fb else 10.0

    def queue(self, name) -> str:
        it = self.item(name)
        if it.get("queue"):
            return str(it["queue"])
        fb = FALLBACK_ITEMS.get(str(name or "").lower())
        if fb:
            return fb[3]
        n = str(name or "").lower()
        return "Infantry" if n in INFANTRY else "Vehicle"

    def is_building(self, name) -> bool:
        n = str(name or "").lower()
        if n in self.buildings:
            return True
        fb = FALLBACK_ITEMS.get(n)
        return bool(fb and fb[3] in ("Building", "Defense"))

    def footprint(self, name) -> tuple[int, int]:
        it = self.buildings.get(str(name or "").lower()) or {}
        fp = it.get("footprint")
        if isinstance(fp, (list, tuple)) and len(fp) == 2:
            return max(1, _int(fp[0], 1)), max(1, _int(fp[1], 1))
        fb = FALLBACK_ITEMS.get(str(name or "").lower())
        return fb[4] if fb and fb[4] else (1, 1)

    def has_weapon(self, name) -> bool:
        n = str(name or "").lower()
        it = self.item(n)
        if not it:
            return n not in NON_COMBAT
        return bool(it.get("weapon")) or n == "e3"

    def speed(self, name) -> float:
        return _num(self.item(name).get("speed"), 1.5) or 1.5


def is_infantry(t: str, rules: Rules | None = None) -> bool:
    if t in INFANTRY:
        return True
    return rules is not None and rules.queue(t) == "Infantry"


def is_combat(t: str, rules: Rules) -> bool:
    if t in NON_COMBAT or t.endswith(".husk"):
        return False
    q = rules.queue(t)
    if q in ("Ship", "Building", "Defense"):
        return False
    return rules.has_weapon(t)


class Static:
    """Per-match data: rules, map grid, spawns, the ground path from our spawn to the enemy's, waypoints, ore."""

    def __init__(self, raw: dict, config: dict | None):
        self.rules = Rules(config)
        m = raw.get("map") if isinstance(raw.get("map"), dict) else {}
        obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
        om = obs.get("map") if isinstance(obs.get("map"), dict) else {}
        self.map_id = str(m.get("id") or om.get("id") or "")
        info = self.rules.maps.get(self.map_id) if isinstance(self.rules.maps.get(self.map_id), dict) else {}
        self.width = _int(m.get("width") or om.get("width") or info.get("width"), 128)
        self.height = _int(m.get("height") or om.get("height") or info.get("height"), 128)
        b = m.get("bounds") or info.get("bounds")
        if isinstance(b, (list, tuple)) and len(b) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                                for v in b):
            bx, by, bw, bh = (int(v) for v in b)
        else:
            bx, by, bw, bh = 1, 1, max(1, self.width - 2), max(1, self.height - 2)
        self.bx0, self.by0 = bx, by
        self.bx1, self.by1 = bx + max(1, bw) - 1, by + max(1, bh) - 1
        self.spawns = [c for c in (_cell(s) for s in (info.get("spawns") or [])) if c is not None]
        self.grid_ok = False
        self.passable = None
        self.buildable = None
        self.ore_cells: list[tuple[int, int]] = []
        self.gem_cells: list[tuple[int, int]] = []
        self._load_layout(info.get("layout"))
        self.ready = False
        self.my_spawn = self.enemy_spawn = None
        self.path: list[tuple[int, int]] = []
        self.waypoints: list[tuple[int, int]] = []
        self.rally = self.guard = self.picket = self.staging = None
        self.sweep: list[tuple[int, int]] = []
        self._coverage: list[tuple[int, int]] | None = None

    # ---- map picture -----------------------------------------------------------------------------------------------
    def _load_layout(self, lay) -> None:
        if not isinstance(lay, dict) or not isinstance(lay.get("rows"), list):
            return
        rows = lay["rows"]
        origin = _cell(lay.get("origin")) or (self.bx0, self.by0)
        w, h = self.width, self.height
        pas = bytearray(w * h)
        bld = bytearray(w * h)
        ore = []
        gems = []
        for i, row in enumerate(rows):
            if not isinstance(row, str):
                continue
            y = origin[1] + i
            if not 0 <= y < h:
                continue
            base = y * w
            for j, ch in enumerate(row):
                x = origin[0] + j
                if not 0 <= x < w:
                    continue
                if ch in PASSABLE:
                    pas[base + x] = 1
                if ch in BUILDABLE:
                    bld[base + x] = 1
                if ch in ORE:
                    ore.append((x, y))
                    if ch == "*":
                        gems.append((x, y))
        if sum(pas) < 50:
            return
        self.passable, self.buildable, self.ore_cells, self.grid_ok = pas, bld, ore, True
        self.gem_cells = gems

    def in_bounds(self, c) -> bool:
        return self.bx0 <= c[0] <= self.bx1 and self.by0 <= c[1] <= self.by1

    def clamp(self, c) -> tuple[int, int]:
        return (min(max(int(round(c[0])), self.bx0), self.bx1), min(max(int(round(c[1])), self.by0), self.by1))

    def can_pass(self, x: int, y: int) -> bool:
        if not (self.bx0 <= x <= self.bx1 and self.by0 <= y <= self.by1):
            return False
        return self.passable is None or bool(self.passable[y * self.width + x])

    def can_build(self, x: int, y: int) -> bool:
        if not (self.bx0 <= x <= self.bx1 and self.by0 <= y <= self.by1):
            return False
        return self.buildable is None or bool(self.buildable[y * self.width + x])

    # ---- the route between the bases -------------------------------------------------------------------------------
    def setup(self, base_center, enemy_hint=None) -> None:
        """Once per match, from the first view: which spawn is ours, the enemy's, the BFS path, waypoints."""
        bc = base_center or (self.spawns[0] if self.spawns else ((self.bx0 + self.bx1) // 2, (self.by0 + self.by1) // 2))
        if self.spawns:
            self.my_spawn = min(self.spawns, key=lambda s: (dist(s, bc), s))
            others = [s for s in self.spawns if s != self.my_spawn]
            self.enemy_spawn = max(others, key=lambda s: (dist(s, bc), s)) if others else None
        else:
            self.my_spawn = tuple(bc)
        if enemy_hint is not None and (self.enemy_spawn is None or dist(enemy_hint, self.enemy_spawn) > 6):
            self.enemy_spawn = tuple(enemy_hint)
        if self.enemy_spawn is None:
            self.enemy_spawn = ((self.bx0 + self.bx1) - self.my_spawn[0], (self.by0 + self.by1) - self.my_spawn[1])
        self.my_spawn, self.enemy_spawn = self.clamp(self.my_spawn), self.clamp(self.enemy_spawn)
        path = self._bfs(self.my_spawn, self.enemy_spawn) if self.grid_ok else None
        self.path = path or self._line(self.my_spawn, self.enemy_spawn)
        n = len(self.path)

        def at(k):
            return self.path[max(0, min(n - 1, k))]
        # home guard and the attack group's gathering point a few cells apart, both covering the base's front
        self.guard = at(min(5, n // 6))
        self.rally = at(min(8, n // 4))
        self.picket = at(min(max(12, int(n * 0.45)), n - 8))
        self.staging = at(n - 1 - min(18, n // 3))
        self.waypoints = [at(k) for k in range(min(10, n // 4) + 12, n - 1, 12)]
        if not self.waypoints or dist(self.waypoints[-1], self.enemy_spawn) > 3:
            self.waypoints.append(self.enemy_spawn)
        self.sweep = self._sweep_points()
        self.ready = True

    def _line(self, a, b) -> list[tuple[int, int]]:
        n = max(1, int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))))
        return [self.clamp((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)) for i in range(n + 1)]

    def _nearest_passable(self, c) -> tuple[int, int] | None:
        if self.can_pass(*c):
            return c
        for r in range(1, 8):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) == r and self.can_pass(c[0] + dx, c[1] + dy):
                        return c[0] + dx, c[1] + dy
        return None

    def _bfs(self, a, b) -> list[tuple[int, int]] | None:
        """8-direction BFS on the layout (no corner cutting) from a to b, as a list of cells, or None."""
        s, t = self._nearest_passable(a), self._nearest_passable(b)
        if s is None or t is None:
            return None
        w = self.width
        prev = {s[1] * w + s[0]: -1}
        q = deque([s])
        goal = t[1] * w + t[0]
        steps = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
        found = False
        while q:
            x, y = q.popleft()
            if y * w + x == goal:
                found = True
                break
            for dx, dy in steps:
                nx, ny = x + dx, y + dy
                k = ny * w + nx
                if k in prev or not self.can_pass(nx, ny):
                    continue
                if dx and dy and not (self.can_pass(x + dx, y) and self.can_pass(x, y + dy)):
                    continue
                prev[k] = y * w + x
                q.append((nx, ny))
        if not found:
            return None
        out = []
        k = goal
        while k != -1:
            out.append((k % w, k // w))
            k = prev[k]
        out.reverse()
        return out

    def _sweep_points(self) -> list[tuple[int, int]]:
        """Where to look when the enemy base is not where we expected: around its spawn, distant ore, quadrants."""
        e = self.enemy_spawn
        pts = [self.clamp((e[0] + dx, e[1] + dy)) for dx, dy in ((8, 0), (0, 8), (-8, 0), (0, -8))]
        far_ore = sorted(self.ore_cells, key=lambda c: -dist(c, self.my_spawn))
        picked: list[tuple[int, int]] = []
        for c in far_ore:
            if dist(c, self.my_spawn) < 25:
                break
            if all(dist(c, p) > 15 for p in picked):
                picked.append(c)
            if len(picked) >= 4:
                break
        cx, cy = (self.bx0 + self.bx1) / 2, (self.by0 + self.by1) / 2
        qw, qh = (self.bx1 - self.bx0) / 4, (self.by1 - self.by0) / 4
        quads = [self.clamp((cx + sx * qw, cy + sy * qh)) for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1))]
        quads.sort(key=lambda c: dist(c, self.my_spawn), reverse=True)
        out = []
        for c in pts + picked + quads + [self.clamp((cx, cy))]:
            c = self._nearest_passable(c) or c
            if all(dist(c, p) > 4 for p in out):
                out.append(c)
        return out

    def coverage(self) -> list[tuple[int, int]]:
        """Search points over the whole map, one every COVER_STEP cells (edges and corners included) on ground our
        units can reach from our spawn, computed on first use: where HUNT looks once the sweep points showed
        nothing (a base built away from both spawns)."""
        if self._coverage is not None:
            return self._coverage
        reach = self._reachable(self.my_spawn) if (self.grid_ok and self.my_spawn is not None) else None
        pts: list[tuple[int, int]] = []
        half = COVER_STEP // 2
        y = self.by0 + half
        while y <= self.by1 + half:
            x = self.bx0 + half
            while x <= self.bx1 + half:
                c = self.clamp((x, y))
                if reach is not None:
                    c = self._nearest_in(c, reach)
                if c is not None and all(dist(c, p) > half for p in pts):
                    pts.append(c)
                x += COVER_STEP
            y += COVER_STEP
        self._coverage = pts
        return pts

    def _reachable(self, start) -> set | None:
        """Cells reachable from `start` (8 directions, no corner cutting), as y * width + x keys."""
        s = self._nearest_passable(start)
        if s is None:
            return None
        w = self.width
        seen = {s[1] * w + s[0]}
        q = deque([s])
        steps = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
        while q:
            x, y = q.popleft()
            for dx, dy in steps:
                nx, ny = x + dx, y + dy
                k = ny * w + nx
                if k in seen or not self.can_pass(nx, ny):
                    continue
                if dx and dy and not (self.can_pass(x + dx, y) and self.can_pass(x, y + dy)):
                    continue
                seen.add(k)
                q.append((nx, ny))
        return seen

    def _nearest_in(self, c, reach: set) -> tuple[int, int] | None:
        w = self.width
        for r in range(0, COVER_STEP // 2 + 1):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) == r and (c[1] + dy) * w + (c[0] + dx) in reach \
                            and self.in_bounds((c[0] + dx, c[1] + dy)):
                        return c[0] + dx, c[1] + dy
        return None

    def step_toward(self, a, b, k: float) -> tuple[int, int]:
        d = dist(a, b)
        if d < 1e-6:
            return self.clamp(a)
        return self.clamp((a[0] + (b[0] - a[0]) * k / d, a[1] + (b[1] - a[1]) * k / d))

    def ore_near(self, c, radius: float = 15.0) -> tuple[int, int] | None:
        """The centre of the ore patch nearest to c (within radius), from the starting map picture."""
        best = None
        for o in self.ore_cells:
            d = dist(o, c)
            if d <= radius and (best is None or d < best[0]):
                best = (d, o)
        if best is None:
            return None
        o = best[1]
        patch = [p for p in self.ore_cells if dist(p, o) <= 4]
        return (round(sum(p[0] for p in patch) / len(patch)), round(sum(p[1] for p in patch) / len(patch)))


class View:
    """One state, parsed. Own units/buildings exclude wrecks; enemies exclude wrecks."""

    def __init__(self, raw: dict, st: Static):
        obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
        self.raw = raw
        self.st = st
        rules = st.rules
        self.sv = raw.get("state_version")
        self.tick = _int(obs.get("tick"), _int(self.sv))
        t = raw.get("time") if isinstance(raw.get("time"), dict) else {}
        self.game_s = _num(t.get("game_seconds"), self.tick / TICKS_PER_SECOND)
        mm = _num(t.get("max_minutes"), 20.0) or 20.0
        rem = t.get("remaining_seconds")
        self.remaining = _num(rem, max(0.0, mm * 60 - self.game_s)) if rem is not None else max(0.0, mm * 60 - self.game_s)
        pacing = raw.get("pacing") if isinstance(raw.get("pacing"), dict) else {}
        self.windows = pacing.get("mode") == "windows"
        mr = raw.get("match_rules") if isinstance(raw.get("match_rules"), dict) else {}
        self.starting_cash = _int(mr.get("starting_cash"), 5000) or 5000
        self.faction = str(obs.get("faction") or "").lower()
        self.enemy_faction = str(obs.get("enemy_faction") or "").lower()
        self.side = side_of(self.faction)
        e = obs.get("economy") if isinstance(obs.get("economy"), dict) else {}
        self.cash = _int(e.get("cash"))
        self.ore = _int(e.get("ore"))
        self.money = self.cash + self.ore
        self.power_provided = _int(e.get("power_provided"))
        self.power_drained = _int(e.get("power_drained"))
        self.surplus = self.power_provided - self.power_drained
        self.capacity = _int(e.get("resource_capacity"))
        m = obs.get("military") if isinstance(obs.get("military"), dict) else {}
        self.kills = _int(m.get("kills_cost"))
        self.deaths = _int(m.get("deaths_cost"))
        self.assets = _int(m.get("assets_value"))
        self.army_value = _int(m.get("army_value"))
        self.buildings_killed = _int(m.get("buildings_killed"))
        # own units
        self.units: list[dict] = []
        self.unit_by_id: dict[int, dict] = {}
        self.unit_count: dict[str, int] = {}
        for u in obs.get("units") or []:
            if not isinstance(u, dict) or u.get("wreck") or isinstance(u.get("id"), bool):
                continue
            uid = u.get("id")
            if not isinstance(uid, int):
                continue
            typ = str(u.get("type") or "").lower()
            if typ.endswith(".husk"):
                continue
            c = _cell(u) or (0, 0)
            rec = {"id": uid, "type": typ, "x": c[0], "y": c[1], "hp": _num(u.get("hp"), 100.0),
                   "idle": bool(u.get("idle")), "act": u.get("act"), "stance": u.get("stance")}
            self.units.append(rec)
            self.unit_by_id[uid] = rec
            self.unit_count[typ] = self.unit_count.get(typ, 0) + 1
        # own buildings
        self.buildings: list[dict] = []
        self.bld_by_id: dict[int, dict] = {}
        self.have: dict[str, int] = {}
        for b in obs.get("buildings") or []:
            if not isinstance(b, dict) or not isinstance(b.get("id"), int) or isinstance(b.get("id"), bool):
                continue
            typ = str(b.get("type") or "").lower()
            c = _cell(b) or (0, 0)
            rec = {"id": b["id"], "type": typ, "x": c[0], "y": c[1], "hp": _num(b.get("hp"), 100.0),
                   "powered": b.get("powered", True), "powered_down": bool(b.get("powered_down")),
                   "repairing": bool(b.get("repairing")), "producing": b.get("producing"),
                   "rally": _cell(b.get("rally")) if b.get("rally") is not None else None}
            self.buildings.append(rec)
            self.bld_by_id[rec["id"]] = rec
            self.have[typ] = self.have.get(typ, 0) + 1
        self.fact = next((b for b in self.buildings if b["type"] == "fact"), None)
        # production
        self.queues: dict[str, list[dict]] = {}
        self.in_prod: dict[str, int] = {}
        self.ready: list[tuple[str, str]] = []
        for p in obs.get("production") or []:
            if not isinstance(p, dict):
                continue
            item = str(p.get("item") or "").lower()
            q = str(p.get("queue") or rules.queue(item))
            rec = {"queue": q, "item": item, "progress": _num(p.get("progress")), "ready": bool(p.get("ready")),
                   "remaining": _num(p.get("remaining_s")), "paused": bool(p.get("paused"))}
            self.queues.setdefault(q, []).append(rec)
            self.in_prod[item] = self.in_prod.get(item, 0) + 1
            if rec["ready"] and q in ("Building", "Defense"):
                self.ready.append((q, item))
        self.avail = {str(a).lower() for a in (obs.get("available_production") or []) if isinstance(a, str)}
        # enemies
        self.enemies: list[dict] = []
        self.enemy_blds: list[dict] = []
        for src, dst in ((obs.get("visible_enemies"), self.enemies), (obs.get("visible_enemy_buildings"), self.enemy_blds)):
            for e_ in src or []:
                if not isinstance(e_, dict) or e_.get("wreck") or not isinstance(e_.get("id"), int):
                    continue
                typ = str(e_.get("type") or "").lower()
                if typ.endswith(".husk"):
                    continue
                c = _cell(e_)
                if c is None:
                    continue
                dst.append({"id": e_["id"], "type": typ, "x": c[0], "y": c[1], "hp": _num(e_.get("hp"), 100.0)})
        # a building reported among the units (or the reverse) is still what its type says
        moved = [e_ for e_ in self.enemies if rules.is_building(e_["type"]) and e_["type"] not in rules.units]
        if moved:
            self.enemies = [e_ for e_ in self.enemies if e_ not in moved]
            self.enemy_blds += moved
        self.enemy_ids = {e_["id"] for e_ in self.enemies} | {e_["id"] for e_ in self.enemy_blds}
        bc = _cell(obs.get("base_center"))
        if bc is None:
            pts = [(b["x"], b["y"]) for b in self.buildings] or [(u["x"], u["y"]) for u in self.units]
            bc = (sum(p[0] for p in pts) // len(pts), sum(p[1] for p in pts) // len(pts)) if pts else None
        self.base = bc
        la = raw.get("legal_actions") if isinstance(raw.get("legal_actions"), dict) else {}
        self.la = la
        self.enemy_hint = _cell(la.get("enemy_base"))
        self.events = [ev for ev in (obs.get("events") or []) if isinstance(ev, dict)]
        self.last_orders = raw.get("last_orders") if isinstance(raw.get("last_orders"), dict) else None
        self.problems = [r for r in (raw.get("recent_order_problems") or []) if isinstance(r, dict)]

    # ---- helpers ----
    def count(self, typ: str) -> int:
        """Owned (buildings or units) plus queued."""
        return self.have.get(typ, 0) + self.unit_count.get(typ, 0) + self.in_prod.get(typ, 0)

    def queue_items(self, q: str) -> list[dict]:
        return self.queues.get(q, [])

    def own_ids(self) -> set[int]:
        return set(self.unit_by_id)
