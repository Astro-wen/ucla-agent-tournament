"""Real Gen 9 stat formulas and speed ordering (the platform's cards give EVs/nature/level, so these are exact)."""
from __future__ import annotations

from . import dex
from .dex import to_id

STATS = ("hp", "atk", "def", "spa", "spd", "spe")
NATURES = {  # name -> (plus, minus)
    "adamant": ("atk", "spa"), "bold": ("def", "atk"), "brave": ("atk", "spe"), "calm": ("spd", "atk"),
    "careful": ("spd", "spa"), "gentle": ("spd", "def"), "hasty": ("spe", "def"), "impish": ("def", "spa"),
    "jolly": ("spe", "spa"), "lax": ("def", "spd"), "lonely": ("atk", "def"), "mild": ("spa", "def"),
    "modest": ("spa", "atk"), "naive": ("spe", "spd"), "naughty": ("atk", "spd"), "quiet": ("spa", "spe"),
    "rash": ("spa", "spd"), "relaxed": ("def", "spe"), "sassy": ("spd", "spe"), "timid": ("spe", "atk"),
    "hardy": (None, None), "docile": (None, None), "serious": (None, None), "bashful": (None, None), "quirky": (None, None),
}
STAGE = {s: (2 + s) / 2 if s >= 0 else 2 / (2 - s) for s in range(-6, 7)}


def calc_stats(base: dict, evs: dict | None = None, ivs: dict | None = None, nature: str = "hardy",
               level: int = 50) -> dict:
    evs, ivs = evs or {}, ivs or {}
    plus, minus = NATURES.get(to_id(nature or ""), (None, None))
    out = {}
    for s in STATS:
        b, ev, iv = base.get(s, 80), evs.get(s, 0), ivs.get(s, 31)
        core = (2 * b + iv + ev // 4) * level // 100
        if s == "hp":
            out[s] = core + level + 10 if b != 1 else 1          # Shedinja
        else:
            v = core + 5
            v = int(v * 1.1) if s == plus else int(v * 0.9) if s == minus else v
            out[s] = v
    return out


def stats_for(card: dict | None, species: str) -> dict:
    """Stats of a Pokémon: exact from its drafted card, else base stats with a generic 252/252 spread."""
    base = dex.base_stats(species)
    if card and card.get("evs") is not None:
        return calc_stats(base, card.get("evs"), card.get("ivs"), card.get("nature", "hardy"), card.get("level", 50))
    return calc_stats(base, {"hp": 4, "atk": 252, "spa": 252, "spe": 252}, {}, "hardy", 50)


def effective_speed(mon: dict, card: dict | None, side: dict | None = None, field: dict | None = None) -> float:
    """Speed after boosts, paralysis, Tailwind, Choice Scarf and paradox-ability speed boosts."""
    sp = to_id(mon.get("species", ""))
    spe = float(stats_for(card, sp)["spe"])
    spe *= STAGE.get((mon.get("boosts") or {}).get("spe", 0), 1.0)
    item = to_id(mon.get("item") or (card or {}).get("item") or "")
    ability = to_id(mon.get("ability") or (card or {}).get("ability") or "")
    if item == "choicescarf":
        spe *= 1.5
    if ability in ("protosynthesis", "quarkdrive"):
        st = stats_for(card, sp)
        boosted_stat = max(("atk", "def", "spa", "spd", "spe"), key=lambda k: st[k])
        weather = to_id(str((field or {}).get("weather") or ""))
        terrain = to_id(str((field or {}).get("terrain") or ""))
        active = item == "boosterenergy" or (ability == "protosynthesis" and "sun" in weather) \
            or (ability == "quarkdrive" and "electric" in terrain)
        if active and boosted_stat == "spe":
            spe *= 1.5
    if (mon.get("status") or "") == "par":
        spe *= 0.5
    if "tailwind" in {to_id(k) for k in (side or {})}:
        spe *= 2
    return spe


def move_priority(move_id: str, mon: dict | None = None, card: dict | None = None, terrain: str | None = None) -> int:
    """Priority bracket of a move, with Prankster / Gale Wings / Triage and Grassy Glide in Grassy Terrain."""
    m = dex.move(move_id)
    pr = int(m.get("priority", 0) or 0)
    ability = to_id((mon or {}).get("ability") or (card or {}).get("ability") or "")
    if ability == "prankster" and m.get("category") == "Status":
        pr += 1
    hp = (mon or {}).get("current_hp_fraction")
    if ability == "galewings" and m.get("type") == "Flying" and (hp is None or float(hp) >= 1.0):
        pr += 1
    if to_id(move_id) == "grassyglide" and "grassy" in to_id(str(terrain or "")):
        pr += 1
    if ability == "triage" and m.get("flags", {}).get("heal"):
        pr += 3
    return pr


def acts_first(a_speed: float, a_prio: int, b_speed: float, b_prio: int, trick_room: bool = False) -> bool | None:
    """True if A acts before B, False if after, None for an exact speed tie (50/50)."""
    if a_prio != b_prio:
        return a_prio > b_prio
    if a_speed == b_speed:
        return None
    return (a_speed < b_speed) if trick_room else (a_speed > b_speed)
