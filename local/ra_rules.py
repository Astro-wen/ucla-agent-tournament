"""Red Alert rules for the local simulator (local/ra_sim.py): the platform's reference (data/red_alert_config.json,
get_game_config("red_alert").rules: cost, build_time_s, power, prerequisites, factions, queue, hp, speed, weapon,
footprint, maps with bounds / spawns / layout) plus the OpenRA facts the reference leaves out, taken from the RA mod at
the engine commit the platform names (rules.engine_commit 9b271c1a; mods/ra/rules/*.yaml, mods/ra/weapons/*.yaml):
armor classes, sight ranges, weapon reload / burst / damage-versus-armor tables, which buildings stop on low power,
which vehicles leave a husk, the construction yard (`fact`, not in the reference) and who produces what.

Everything here is data; nothing touches the network. Numbers are the engine's where we have them and labelled
guesses where we do not (marked GUESS).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parents[1] / "data" / "red_alert_config.json"
TICKS_PER_SECOND = 25

# Countries (observation.faction) and their side (the reference's `factions` lists use both).
SIDES = {"england": "allies", "france": "allies", "germany": "allies", "russia": "soviet", "ukraine": "soviet"}
COUNTRIES = tuple(SIDES)

# The construction yard is not in the reference (it is only ever made by deploying an MCV). OpenRA FACT.
FACT = {"cost": 2000, "build_time_s": 0.0, "power": 0, "prerequisites": ["~disabled"], "queue": "Building",
        "hp": 150000, "footprint": [3, 4], "speed": None, "weapon": None}

# Armor classes (OpenRA Armor.Type). Anything not listed: infantry None, buildings Wood, vehicles Heavy.
ARMOR = {
    "mcv": "Light", "jeep": "Light", "ftrk": "Light", "arty": "Light", "v2rl": "Light", "truk": "Light",
    "dtrk": "Light", "mgg": "Light", "mrj": "Light", "heli": "Light", "mh60": "Light", "tran": "Light",
    "yak": "Light", "mig": "Light", "pt": "Light", "lst": "Heavy",
    "1tnk": "Heavy", "2tnk": "Heavy", "3tnk": "Heavy", "4tnk": "Heavy", "apc": "Heavy", "harv": "Heavy",
    "ctnk": "Heavy", "stnk": "Heavy", "ttnk": "Heavy", "qtnk": "Heavy", "mnly": "Heavy", "ca": "Heavy",
    "dd": "Heavy", "ss": "Heavy", "msub": "Heavy",
    "pbox": "Heavy", "hbox": "Heavy", "gun": "Heavy", "ftur": "Heavy", "tsla": "Heavy", "agun": "Heavy",
    "sam": "Heavy", "gap": "Heavy", "brik": "Concrete",
}

# Sight in cells (OpenRA RevealsShroud.Range). Not listed: infantry 4, vehicles 5, buildings 4 (5 when 3 wide).
SIGHT = {
    "fact": 5, "mcv": 4, "harv": 4, "proc": 5, "silo": 4, "powr": 4, "apwr": 5, "tent": 5, "barr": 5, "weap": 5,
    "fix": 5, "dome": 10, "pbox": 6, "hbox": 6, "gun": 6.5, "ftur": 6, "tsla": 7, "agun": 6, "sam": 8, "kenn": 4,
    "spen": 5, "syrd": 5, "atek": 5, "stek": 5, "hpad": 5, "afld": 5,
    "e1": 4, "e2": 4, "e3": 4, "e4": 4, "e6": 4, "e7": 6, "dog": 5.5, "medi": 3, "spy": 5, "thf": 4, "shok": 4,
    "1tnk": 5, "2tnk": 6, "3tnk": 6, "4tnk": 6, "jeep": 7, "apc": 5, "ftrk": 6, "arty": 5, "v2rl": 5,
}

# Ground weapons: name -> (range cells, damage per hit, reload ticks, burst, burst delay ticks, versus %, only).
# `only`: the target kinds it can hit at all ("infantry", or None for any ground target). AA-only weapons (RedEye,
# Nike, ZSU-23, FLAK-23-AA) are mapped to the actor's ground weapon or to nothing (see GROUND_WEAPON).
_ALL = {"None": 100, "Wood": 100, "Light": 100, "Heavy": 100, "Concrete": 100}


def _vs(none: int | None = None, **kw) -> dict:
    """A versus table (percent per armor class); `none` is the "None" (infantry) class."""
    out = dict(_ALL, **kw)
    if none is not None:
        out["None"] = none
    return out


WEAPONS = {
    "M1Carbine": (5.0, 1000, 20, 1, 0, _vs(none=150, Wood=30, Light=40, Heavy=10, Concrete=10), None),
    "Vulcan": (6.0, 6000, 35, 1, 0, _vs(none=200, Wood=50, Light=50, Heavy=20, Concrete=20), None),
    "Grenade": (4.0, 6000, 60, 1, 0, _vs(none=60, Wood=100, Light=25, Heavy=25, Concrete=100), None),
    "Dragon": (5.0, 5000, 50, 1, 0, _vs(none=10, Wood=74, Light=34, Heavy=100, Concrete=50), None),
    "Flamer": (5.0, 1000, 50, 15, 1, _vs(none=70, Wood=80, Light=40, Heavy=20, Concrete=10), None),
    "DogJaw": (3.0, 100000, 10, 1, 0, _vs(), "infantry"),
    "Colt45": (7.0, 10000, 7, 1, 0, _vs(), "infantry"),
    "SilencedPPK": (2.5, 15000, 80, 1, 0, _vs(), "infantry"),                     # GUESS reload
    "25mm": (4.75, 2500, 21, 1, 0, _vs(none=32, Wood=52, Light=116, Heavy=48, Concrete=32), None),
    "90mm": (4.75, 4000, 50, 1, 0, _vs(none=30, Wood=75, Light=75, Heavy=115, Concrete=50), None),
    "105mm": (4.75, 4000, 70, 2, 5, _vs(none=30, Wood=75, Light=75, Heavy=115, Concrete=50), None),
    "120mm": (4.75, 6000, 90, 2, 5, _vs(none=30, Wood=75, Light=75, Heavy=115, Concrete=50), None),
    "M60mg": (4.0, 1000, 30, 5, 5, _vs(none=150, Wood=10, Light=30, Heavy=10, Concrete=10), None),
    "FLAK-23-AG": (6.0, 2000, 10, 1, 0, _vs(none=40, Wood=10, Light=60, Heavy=10, Concrete=20), None),
    "155mm": (12.0, 23000, 85, 1, 0, _vs(none=60, Wood=40, Light=60, Heavy=25, Concrete=50), None),
    "SCUD": (10.0, 4500, 215, 1, 0, _vs(none=90, Wood=75, Light=70, Heavy=40, Concrete=100), None),
    "TurretGun": (6.5, 6000, 30, 1, 0, _vs(none=20, Wood=50, Light=75, Concrete=50), None),
    "FireballLauncher": (5.0, 15000, 70, 2, 20, _vs(none=90, Wood=50, Light=50, Heavy=25, Concrete=20), None),
    # The engine's tesla coil charges between zaps (AttackCharges); one zap about every 2.4 s. GUESS period.
    "TeslaZap": (7.0, 10000, 60, 1, 0, _vs(none=1000, Wood=60), None),
    "PortaTesla": (6.0, 4500, 70, 1, 0, _vs(none=1000, Wood=73, Heavy=60), None),
    "TTankZap": (7.0, 10000, 120, 1, 0, _vs(none=1000), None),
    "APTusk": (6.0, 5000, 60, 2, 0, _vs(none=10, Wood=74, Light=34, Heavy=100, Concrete=50), None),
    "APTusk.stnk": (6.0, 5000, 60, 2, 0, _vs(none=10, Wood=74, Light=34, Heavy=100, Concrete=50), None),
    "ChainGun": (5.0, 2500, 10, 2, 0, _vs(none=144, Wood=60, Light=72, Heavy=28, Concrete=28), None),
    "ChainGun.Yak": (5.0, 4000, 10, 2, 0, _vs(none=144, Wood=60, Light=72, Heavy=28, Concrete=28), None),
    "HellfireAA": (4.0, 3000, 60, 2, 0, _vs(none=30, Wood=90, Light=90, Heavy=115), None),   # GUESS (ground use)
    "Maverick": (9.0, 7000, 50, 2, 7, _vs(none=30, Wood=90, Light=90, Heavy=115), None),
    "2Inch": (5.5, 2500, 60, 1, 0, _vs(none=30, Wood=75, Light=75, Heavy=100, Concrete=50), None),  # GUESS
    "Stinger": (7.5, 2500, 60, 2, 5, _vs(none=30, Wood=75, Light=75, Heavy=100, Concrete=50), None),  # GUESS
    "8Inch": (20.0, 2500, 100, 2, 5, _vs(none=60, Wood=75, Light=75, Heavy=100, Concrete=100), None),  # GUESS
    "TorpTube": (9.0, 18000, 100, 1, 0, _vs(), None),                            # GUESS (ships only, in fact)
    "SubMissile": (20.0, 2500, 100, 2, 5, _vs(), None),                          # GUESS
    "mandible": (1.5, 6000, 20, 1, 0, _vs(), None),
    "claw": (1.5, 3000, 20, 1, 0, _vs(), None),
}

# An actor's ground weapon where the reference's `weapon` is AA-only, garrison-only or missing.
GROUND_WEAPON = {"e3": "Dragon", "pbox": "Vulcan", "hbox": "Vulcan", "ftrk": "FLAK-23-AG", "4tnk": "120mm",
                 "agun": None, "sam": None, "medi": None, "mech": None, "dtrk": None}

# OpenRA ^DisableOnLowPower(OrPowerDown): these stop working on low power (observation `powered: false`).
DISABLED_ON_LOW_POWER = frozenset({"mslo", "gap", "iron", "pdox", "tsla", "agun", "dome", "sam", "atek"})
# The ones that can be powered down (ToggleConditionOnOrder PowerDown).
POWER_TOGGLE = frozenset({"mslo", "gap", "iron", "pdox", "tsla", "agun", "dome", "sam"})
# Vehicles that leave a burning husk (OpenRA SpawnActorOnDeath *.Husk); a husk burns out in about 45 s.
HUSKS = frozenset({"2tnk", "3tnk", "4tnk", "mcv", "harv"})
HUSK_TICKS = 1120
# Walls never have to be destroyed for victory (no MustBeDestroyed).
WALLS = frozenset({"brik", "sbag", "fenc"})

# Who produces each queue. Infantry soldiers come from barracks, dogs from the kennel.
PRODUCERS = {"Building": ("fact",), "Defense": ("fact",), "Infantry": ("tent", "barr"), "Dog": ("kenn",),
             "Vehicle": ("weap",), "Ship": ("syrd", "spen"), "Aircraft": ("hpad", "afld", "afld.ukraine")}
# ClassicProductionQueue SpeedUp: build time with N producers of the queue (BuildTimeSpeedReduction, percent).
SPEEDUP = {"Vehicle": (100, 75, 60, 50)}
SPEEDUP_DEFAULT = (100, 86, 75, 67, 60, 55, 50)
LOW_POWER_MODIFIER = 3                 # LowPowerModifier 300: production runs at a third of the speed

# Not combat units (legal_actions.combat_units / attack_now leave them out).
NON_COMBAT = frozenset({"mcv", "harv", "medi", "mech", "e6", "spy", "spy.england", "thf", "truk", "mnly", "mgg",
                        "mrj", "lst", "tran", "dtrk", "qtnk"})
SHIP_QUEUE = "Ship"

# Economy (OpenRA PlayerResources / Harvester / StoresPlayerResources).
ORE_VALUE, GEM_VALUE = 25, 50          # credits per bale
HARVESTER_CAPACITY = 20                # bales per load
BALE_LOAD_TICKS = 4
BALE_UNLOAD_TICKS = 1
STORAGE = {"proc": 2000, "silo": 3000}
ORE_DENSITY, GEM_DENSITY, MAX_ORE_DENSITY = 8, 3, 12   # GUESS starting density (the layout shows none)
SELL_REFUND = 0.5
REPAIR_STEP_HP, REPAIR_INTERVAL_TICKS, REPAIR_COST_PERCENT = 700, 24, 20

# Layout characters: buildable ground, passable ground, ore.
BUILDABLE = frozenset(".,=S_")
ORE_CHARS = {"$": "ore", "*": "gems"}
BLOCKED = frozenset("~^T#M")


def load_config(path: Path | None = None) -> dict:
    """get_game_config("red_alert") as saved in data/red_alert_config.json."""
    try:
        return json.loads(Path(path or CONFIG_FILE).read_text())
    except (OSError, ValueError):
        return {"game_type": "red_alert", "rules": {}}


def _prereq_groups(prereqs) -> list[tuple[str, ...]]:
    """A reference prerequisite list as groups of alternatives: "any power plant (powr/apwr)" -> ("powr", "apwr")."""
    groups = []
    for p in prereqs or []:
        p = str(p)
        m = re.search(r"\(([^)]*)\)", p)
        names = tuple(s.strip().lower() for s in m.group(1).split("/")) if m else (p.strip().lower(),)
        groups.append(names)
    return groups


class Rules:
    """The rules one match uses: items (buildings + units) by lowercase name, maps by id."""

    def __init__(self, config: dict | None = None):
        self.config = config if config is not None else load_config()
        rules = self.config.get("rules") or {}
        self.buildings: dict[str, dict] = {k.lower(): dict(v) for k, v in (rules.get("buildings") or {}).items()}
        self.units: dict[str, dict] = {k.lower(): dict(v) for k, v in (rules.get("units") or {}).items()}
        self.buildings.setdefault("fact", dict(FACT))
        self.maps: dict[str, dict] = dict(rules.get("maps") or {})
        self.prereqs = {k: _prereq_groups(v.get("prerequisites")) for k, v in {**self.buildings, **self.units}.items()}
        self._weapon: dict[str, tuple | None] = {}
        self._fight: dict[str, bool] = {}

    # ---- items ----
    def item(self, name: str) -> dict | None:
        name = (name or "").lower()
        return self.buildings.get(name) or self.units.get(name)

    def is_building(self, name: str) -> bool:
        return (name or "").lower() in self.buildings

    def cost(self, name: str) -> int:
        it = self.item(name)
        return int(it.get("cost") or 0) if it else 0

    def hp(self, name: str) -> int:
        it = self.item(name)
        return int(it.get("hp") or 1000) if it else 1000

    def queue(self, name: str) -> str | None:
        it = self.item(name)
        return it.get("queue") if it else None

    def footprint(self, name: str) -> tuple[int, int]:
        it = self.buildings.get((name or "").lower()) or {}
        fp = it.get("footprint") or [1, 1]
        return int(fp[0]), int(fp[1])

    def power(self, name: str) -> int:
        it = self.item(name)
        return int(it.get("power") or 0) if it else 0

    def speed(self, name: str) -> float:
        it = self.units.get((name or "").lower()) or {}
        return float(it.get("speed") or 1.0)

    def armor(self, name: str) -> str:
        n = (name or "").lower()
        if n in ARMOR:
            return ARMOR[n]
        if n in self.buildings:
            return "Wood"
        return "None" if self.queue(n) == "Infantry" else "Heavy"

    def sight(self, name: str) -> float:
        n = (name or "").lower()
        if n in SIGHT:
            return float(SIGHT[n])
        if n in self.buildings:
            return 5.0 if self.footprint(n)[0] >= 3 else 4.0
        return 4.0 if self.queue(n) == "Infantry" else 5.0

    def is_infantry(self, name: str) -> bool:
        return self.queue(name) == "Infantry"

    def weapon(self, name: str) -> tuple | None:
        """(range, damage, period ticks, versus, only) of the actor's ground weapon, or None if it has none."""
        n = (name or "").lower()
        if n not in self._weapon:
            self._weapon[n] = self._weapon_of(n)
        return self._weapon[n]

    def _weapon_of(self, n: str) -> tuple | None:
        if n in GROUND_WEAPON:
            wname = GROUND_WEAPON[n]
        else:
            w = (self.item(n) or {}).get("weapon")
            wname = w.get("weapon") if isinstance(w, dict) else None
        if not wname:
            return None
        spec = WEAPONS.get(wname)
        if spec is None:
            w = (self.item(n) or {}).get("weapon") or {}
            dmg = int(w.get("damage") or 0)
            if dmg <= 0:
                return None
            return (float(w.get("range_cells") or 4.0), dmg, 40, _vs(), None)
        rng, dmg, reload_t, burst, bdelay, versus, only = spec
        if dmg <= 0:
            return None
        return (rng, dmg * burst, reload_t + max(0, burst - 1) * bdelay, versus, only)

    def can_fight(self, name: str) -> bool:
        n = (name or "").lower()
        out = self._fight.get(n)
        if out is None:
            out = self._fight[n] = n not in NON_COMBAT and self.queue(n) != SHIP_QUEUE and self.weapon(n) is not None
        return out

    def husk(self, name: str) -> bool:
        return (name or "").lower() in HUSKS

    def producers(self, name: str) -> tuple[str, ...]:
        n = (name or "").lower()
        if n == "dog":
            return PRODUCERS["Dog"]
        return PRODUCERS.get(self.queue(n) or "", ())

    # ---- availability ----
    def faction_ok(self, name: str, country: str) -> bool:
        facs = (self.item(name) or {}).get("factions")
        if not facs:
            return True
        return country in facs or SIDES.get(country) in facs

    def available(self, owned_types: set[str], country: str) -> list[str]:
        """available_production: what a player owning `owned_types` (buildings) can start right now."""
        out = []
        for n in list(self.buildings) + list(self.units):
            if "." in n or n == "fact":
                continue
            groups = self.prereqs.get(n, [])
            if any(g == ("~disabled",) or g == ("bio",) for g in groups):
                continue
            if not self.faction_ok(n, country):
                continue
            if not all(any(x in owned_types for x in g) for g in groups):
                continue
            if not any(p in owned_types for p in self.producers(n)):
                continue
            out.append(n)
        return out

    # ---- maps ----
    def map_info(self, map_id: str) -> dict:
        m = self.maps.get(map_id)
        if m is None:
            raise KeyError(f"unknown map {map_id!r}; this config has {sorted(self.maps)}")
        return m


@lru_cache(maxsize=4)
def default_rules() -> Rules:
    return Rules()
