"""Build data/singles_sets.json: public Showdown set data the singles policy uses to guess unseen opponents.

    python scripts/build_singles_sets.py          # needs local/js/node_modules/pokemon-showdown (npm install)

  "random": {species id: {"level", "moves": [ids], "abilities": [ids]}}  -- Gen 9 Random Battle movepools
            (data/random-battles/gen9/sets.json): every move any role of that species can carry, its fixed level.
  "ou":     {species id: {"species", "item", "ability", "nature", "evs", "ivs", "moves": [ids]}} -- the heaviest
            Gen 9 Battle Factory set of each species (Uber..PU), first option per slot: a typical OU-style set for a
            team-building opponent whose set we have not seen.
Source: smogon/pokemon-showdown (MIT). Regenerate after `npm update` in local/js.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS = ROOT / "local" / "js" / "node_modules" / "pokemon-showdown" / "data" / "random-battles" / "gen9"
OUT = ROOT / "data" / "singles_sets.json"


def to_id(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def main() -> None:
    rnd = json.loads((PS / "sets.json").read_text())
    random_out = {}
    for sp, entry in sorted(rnd.items()):
        moves, abilities = [], []
        for s in entry.get("sets", []):
            for m in s.get("movepool", []):
                if to_id(m) not in moves:
                    moves.append(to_id(m))
            for a in s.get("abilities", []):
                if to_id(a) not in abilities:
                    abilities.append(to_id(a))
        random_out[to_id(sp)] = {"level": entry.get("level", 80), "moves": moves, "abilities": abilities}
    fac = json.loads((PS / "factory-sets.json").read_text())
    ou_out = {}
    for tier in ("PU", "NU", "RU", "UU", "OU", "Uber"):          # higher tiers overwrite: their sets are the usual ones
        for sp, entry in sorted(fac.get(tier, {}).items()):
            sets = [s for s in entry.get("sets", []) if s.get("species")]
            if not sets:
                continue
            s = max(sets, key=lambda x: x.get("weight", 0))
            first = lambda xs: (xs[0] if isinstance(xs, list) and xs else xs) or ""      # noqa: E731
            moves = []
            for slot in s.get("moves", []):
                m = next((to_id(x) for x in slot if to_id(x) not in moves and to_id(x) != "terablast"), None)
                if m:
                    moves.append(m)
            ou_out[to_id(s["species"])] = {"species": s["species"], "item": first(s.get("item")),
                                           "ability": first(s.get("ability")), "nature": first(s.get("nature")) or "Serious",
                                           "evs": s.get("evs") or {}, "ivs": s.get("ivs") or {}, "moves": moves}
    OUT.write_text(json.dumps({"source": "smogon/pokemon-showdown data/random-battles/gen9 (MIT)",
                               "random": random_out, "ou": ou_out}, separators=(",", ":"), sort_keys=True))
    print(f"{OUT}: {len(random_out)} random-battle species, {len(ou_out)} factory sets, {OUT.stat().st_size} bytes")


if __name__ == "__main__":
    main()
