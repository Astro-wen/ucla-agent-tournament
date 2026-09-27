"""Exact damage via Smogon's calculator (agent/js/calc.js), batched: one round trip per turn.

`DamageCalc.batch()` returns None when node or node_modules are missing, so callers fall back to the
base-stat heuristic instead of crashing (a crash = the platform random-plays our seat).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from . import dex
from .dex import to_id

log = logging.getLogger("agent.calc")


def _js_dir() -> Path | None:
    here = Path(__file__).resolve().parent.parent / "js"          # agent/js next to this package
    root = dex.repo_root() / "agent" / "js"                         # the real one when we run as a champion copy
    for d in (here, root):
        if (d / "calc.js").exists() and (d / "node_modules").exists():
            return d
    return None


class DamageCalc:
    _inst: "DamageCalc | None" = None

    def __init__(self):
        self.proc = None
        self.enabled = shutil.which("node") is not None and _js_dir() is not None
        self._id = 0
        self.cache: dict[str, dict] = {}

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
            self.proc = subprocess.Popen(["node", str(_js_dir() / "calc.js")], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            return True
        except Exception:
            log.warning("could not start damage calc", exc_info=True)
            self.enabled = False
            return False

    def batch(self, calcs: list[dict]) -> list[dict | None] | None:
        """Each calc: {attacker, defender, move, field?}. Returns per-calc dicts (or None on per-calc error)."""
        if not calcs:
            return []
        keys = [json.dumps(c, sort_keys=True) for c in calcs]
        todo = [(i, k) for i, k in enumerate(keys) if k not in self.cache]
        if todo:
            if not self._start():
                return None
            self._id += 1
            try:
                self.proc.stdin.write(json.dumps({"id": self._id, "calcs": [calcs[i] for i, _ in todo]}) + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                res = json.loads(line)["results"]
            except Exception:
                log.warning("damage calc round trip failed", exc_info=True)
                self.enabled = False
                return None
            for (i, k), r in zip(todo, res):
                self.cache[k] = r
            if len(self.cache) > 20000:
                self.cache.clear()
        out = []
        for k in keys:
            r = self.cache.get(k)
            out.append(None if not r or "error" in r else r)
        return out


# ---------------------------------------------------------------- building calc inputs from platform data
def calc_pokemon(mon: dict | None, card: dict | None, species: str | None = None) -> dict:
    """Merge a battle-state Pokémon view with its drafted card into a calculator input."""
    mon = mon or {}
    sp = species or mon.get("species") or (card or {}).get("species") or ""
    entry = dex.species(sp)
    out = {"species": entry.get("name") or sp, "level": (card or {}).get("level", 50)}
    item = mon.get("item") or (card or {}).get("item")
    if item and item != "unknown_item":
        out["item"] = dex.item_name(item)
    ability = mon.get("ability") or (card or {}).get("ability")
    if ability:
        out["ability"] = dex.ability_name(ability)
    if card:
        out["nature"] = card.get("nature", "Hardy")
        out["evs"] = card.get("evs") or {}
        out["ivs"] = card.get("ivs") or {}
    boosts = {k: v for k, v in (mon.get("boosts") or {}).items() if k in ("atk", "def", "spa", "spd", "spe") and v}
    if boosts:
        out["boosts"] = boosts
    if mon.get("max_hp") and mon.get("current_hp") is not None:
        # the calc needs curHP in ITS stat scale; platform HP is Showdown's real HP, same scale when the set is known
        out["curHP"] = int(mon["current_hp"])
    if mon.get("status"):
        out["status"] = mon["status"]
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
