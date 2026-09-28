"""Exact damage via Smogon's calculator (agent/js/calc.js), batched: one round trip per turn.

`DamageCalc.batch()` returns None when node or node_modules are missing, so callers fall back to the
base-stat heuristic instead of crashing (a crash = the platform random-plays our seat).

The node process starts lazily and its first answer pays for node + @smogon/calc start-up (0.3 s here, seconds on
a cold machine). `DamageCalc.warm()` pays that once with a tiny dummy calc; the policy calls it in the phases before
the first battle decision (draft / teambuild / team preview), so a fresh match process does not spend its first
turn's lookahead budget on it.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import dex
from .dex import to_id

log = logging.getLogger("agent.calc")

# One cheap calc that makes node load @smogon/calc and the gen-9 data; its answer is never cached or used.
WARM_CALC = {"attacker": {"species": "Pikachu", "level": 50}, "defender": {"species": "Pikachu", "level": 50},
             "move": "Thunderbolt", "field": {"gameType": "Doubles"}}


def _js_paths() -> tuple[Path, Path] | None:
    """(directory of the calc.js to run, node_modules directory to resolve @smogon/calc from).

    A champion copy (champion/agent/js) keeps its OWN calc.js but has no node_modules (freeze_champion.sh drops
    them): it must still run its own calc.js, borrowing the repo's node_modules via NODE_PATH. Falling back to the
    repo's agent/js/calc.js instead would silently give the champion the candidate's calculator on a ladder."""
    here = Path(__file__).resolve().parent.parent / "js"          # agent/js next to this package
    root = dex.repo_root() / "agent" / "js"                         # the real one when we run as a champion copy
    script = next((d for d in (here, root) if (d / "calc.js").exists()), None)
    modules = next((d / "node_modules" for d in (here, root) if (d / "node_modules").exists()), None)
    if script is None or modules is None:
        return None
    return script, modules


def _js_dir() -> Path | None:
    """Directory of the calc.js this package runs (None = calculator unavailable)."""
    p = _js_paths()
    return p[0] if p else None


CACHE_MAX = 20000                 # calc results kept across decisions (cleared, never partially evicted)


class DamageCalc:
    _inst: "DamageCalc | None" = None

    def __init__(self):
        self.proc = None
        self.enabled = shutil.which("node") is not None and _js_dir() is not None
        self._id = 0
        self.cache: dict[str, dict] = {}
        self.ready = False          # the running node process has answered at least once (start-up paid)
        self.starts = 0             # node processes spawned
        self.startup_s = None       # seconds the last spawn took until its first answer (None: never measured)

    @classmethod
    def get(cls) -> "DamageCalc":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def _start(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        if not self.enabled:
            return False
        try:
            self.ready = False
            script, modules = _js_paths()
            env = dict(os.environ)
            env["NODE_PATH"] = os.pathsep.join(x for x in (str(modules), env.get("NODE_PATH", "")) if x)
            self.proc = subprocess.Popen(["node", str(script / "calc.js")], stdin=subprocess.PIPE, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            self.starts += 1
            return True
        except Exception:
            log.warning("could not start damage calc", exc_info=True)
            self.enabled = False
            return False

    def _roundtrip(self, calcs: list[dict]) -> list | None:
        """One request / response with the node process (started if needed); None on any failure."""
        t = time.monotonic()
        if not self._start():
            return None
        cold = not self.ready
        self._id += 1
        try:
            self.proc.stdin.write(json.dumps({"id": self._id, "calcs": calcs}) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            res = json.loads(line)["results"]
        except Exception:
            log.warning("damage calc round trip failed", exc_info=True)
            self.enabled = False
            return None
        if cold:
            self.ready = True
            self.startup_s = time.monotonic() - t
        return res

    def warm(self) -> bool:
        """Start node and load the calculator now with one dummy calc, so the next real batch is a plain round trip.
        Idempotent (a no-op while the process is up and has answered); False when the calculator is unavailable.
        The dummy answer never enters the cache (batch()'s clear-before-insert cache order is untouched)."""
        if not self.enabled:
            return False
        if self.ready and self.proc is not None and self.proc.poll() is None:
            return True
        return self._roundtrip([WARM_CALC]) is not None

    def batch(self, calcs: list[dict]) -> list[dict | None] | None:
        """Each calc: {attacker, defender, move, field?}. Returns per-calc dicts (or None on per-calc error)."""
        if not calcs:
            return []
        keys = [json.dumps(c, sort_keys=True) for c in calcs]
        # This batch's answers are collected here, not re-read from the cache: the old code cleared an overflowing
        # cache AFTER inserting the batch and then read the batch back from the empty cache, so every calc of that
        # decision (a battle turn, the lookahead's table, a whole team-preview batch) silently came back None.
        got = {k: self.cache[k] for k in keys if k in self.cache}
        todo = [(i, k) for i, k in enumerate(keys) if k not in got]
        if todo:
            res = self._roundtrip([calcs[i] for i, _ in todo])
            if res is None:
                return None
            if len(self.cache) + len(todo) > CACHE_MAX:
                self.cache.clear()                       # clear BEFORE inserting the current batch
            for (i, k), r in zip(todo, res):
                self.cache[k] = r
                got[k] = r
        out = []
        for k in keys:
            r = got.get(k)
            out.append(None if not r or "error" in r else r)
        return out


# ---------------------------------------------------------------- building calc inputs from platform data
# Consumed items whose effect stays while the holder is in: the calculator only applies Protosynthesis / Quark Drive's
# Booster Energy boost while it sees the item, so a spent Booster Energy is still passed on (main's behaviour).
_EFFECT_OUTLIVES_ITEM = frozenset({"boosterenergy"})
# Major statuses @smogon/calc understands. The platform sends them upper case ("BRN", "PAR", ...), the local sim lower
# case; calc.js silently ignores anything else, so a platform burn used to leave physical damage un-halved.
CALC_STATUSES = frozenset({"brn", "par", "psn", "tox", "slp", "frz"})


def view_item(mon: dict | None, card: dict | None):
    """The item a battle view's Pokémon holds, for every model that reads items (calc input, lookahead, Sash checks).

    The platform hides a foe's item ("unknown_item") until revealed: the drafted card's item then. A view that
    explicitly shows an empty slot (foe None / ours "": the platform clears it on |-enditem|) has lost the card's
    item: a used Focus Sash, a knocked-off Loaded Dice (else still scored at 4-5 hits), a popped Air Balloon, a spent
    Electric Seed. It is not brought back, except an item whose effect outlives it (Booster Energy)."""
    mon = mon or {}
    item = mon.get("item")
    if not item or item == "unknown_item":
        card_item = (card or {}).get("item")
        gone = "item" in mon and item in (None, "")
        if not gone or to_id(card_item or "") in _EFFECT_OUTLIVES_ITEM:
            item = card_item
    return item if item and item != "unknown_item" else None


def calc_status(status) -> str | None:
    """A view's status as the calculator's id ("BRN" -> "brn"); None for no / unknown status (e.g. "fnt")."""
    s = to_id(str(status or ""))
    return s if s in CALC_STATUSES else None


def calc_pokemon(mon: dict | None, card: dict | None, species: str | None = None,
                 allies_fainted: int | None = None) -> dict:
    """Merge a battle-state Pokémon view with its drafted card into a calculator input.

    `allies_fainted`: the side's fainted count Supreme Overlord locked in when this Pokémon came in (only passed
    on for a Supreme Overlord holder)."""
    mon = mon or {}
    sp = species or mon.get("species") or (card or {}).get("species") or ""
    entry = dex.species(sp)
    out = {"species": entry.get("name") or sp, "level": (card or {}).get("level", 50)}
    # The platform hides a foe's item ("unknown_item") and ability (None) until revealed: use its drafted card then
    # (an item the view shows as gone stays gone, see view_item).
    item = view_item(mon, card)
    if item:
        out["item"] = dex.item_name(item)
    ability = mon.get("ability") or (card or {}).get("ability")
    if ability:
        out["ability"] = dex.ability_name(ability)
    if allies_fainted and to_id(ability or "") == "supremeoverlord":
        out["alliesFainted"] = max(0, min(5, int(allies_fainted)))
    if card:
        out["nature"] = card.get("nature", "Hardy")
        out["evs"] = card.get("evs") or {}
        out["ivs"] = card.get("ivs") or {}
    boosts = {k: v for k, v in (mon.get("boosts") or {}).items() if k in ("atk", "def", "spa", "spd", "spe") and v}
    if boosts:
        out["boosts"] = boosts
    frac = mon.get("current_hp_fraction")
    if frac is None and mon.get("max_hp"):
        frac = (mon.get("current_hp") or 0) / mon["max_hp"]
    if frac is not None:
        # Fraction, not raw HP: the platform reports foe HP out of 100, ours in real HP; calc.js rescales.
        out["hpFraction"] = round(float(frac), 4)
    status = calc_status(mon.get("status"))
    if status:
        out["status"] = status                   # lower case: calc.js ignores the platform's "BRN" / "PAR"
    return out


def calc_field(obs: dict, attacker_is_me: bool) -> dict:
    ours = {to_id(k) for k in (obs.get("side_conditions") or {})}
    theirs = {to_id(k) for k in (obs.get("opponent_side_conditions") or {})}
    my_side = {"isTailwind": "tailwind" in ours, "isReflect": "reflect" in ours, "isLightScreen": "lightscreen" in ours,
               "isAuroraVeil": "auroraveil" in ours}
    their_side = {"isTailwind": "tailwind" in theirs, "isReflect": "reflect" in theirs,
                  "isLightScreen": "lightscreen" in theirs, "isAuroraVeil": "auroraveil" in theirs}
    weather = obs.get("weather")
    weather = next(iter(weather), None) if isinstance(weather, dict) else weather
    fields = obs.get("fields")
    fields = list(fields) if isinstance(fields, dict) else (fields or [])
    terrain = next((f for f in fields if "terrain" in to_id(str(f))), None)
    return {"gameType": "Doubles" if obs.get("is_doubles") else "Singles",
            "weather": _weather_name(weather), "terrain": _terrain_name(terrain),
            "attackerSide": my_side if attacker_is_me else their_side,
            "defenderSide": their_side if attacker_is_me else my_side}


def _weather_name(w) -> str | None:
    w = to_id(str(w or ""))
    return {"sunnyday": "Sun", "sun": "Sun", "raindance": "Rain", "rain": "Rain", "sandstorm": "Sand", "sand": "Sand",
            "snow": "Snow", "hail": "Hail", "desolateland": "Harsh Sunshine", "primordialsea": "Heavy Rain"}.get(w)


def _terrain_name(t) -> str | None:
    t = to_id(str(t or ""))
    for name in ("electric", "grassy", "psychic", "misty"):
        if name in t:
            return name.capitalize()
    return None


def move_name(move_id: str) -> str:
    return dex.move(move_id).get("name") or move_id
