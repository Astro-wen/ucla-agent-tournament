"""Showdown dex/move data (run scripts/fetch_showdown_data.sh first). Degrades to {} if missing."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path



def repo_root() -> Path:
    """Walk up until we find data/pokedex.json — works from agent/ and from a champion/agent/ copy."""
    p = Path(__file__).resolve()
    for d in p.parents:
        if (d / "data" / "pokedex.json").exists():
            return d
    return p.parents[2]


DATA = repo_root() / "data"


def to_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    p = DATA / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def species(name: str) -> dict:
    return _load("pokedex").get(to_id(name), {})


def move(name: str) -> dict:
    return _load("moves").get(to_id(name), {})


def item_name(item: str) -> str:
    return _load("items").get(to_id(item), {}).get("name") or item


def ability_name(ability: str) -> str:
    return _load("abilities").get(to_id(ability), {}).get("name") or ability


def types_of(name: str) -> list[str]:
    return [t.upper() for t in species(name).get("types", [])]


def base_stats(name: str) -> dict:
    return species(name).get("baseStats", {})


def card_catalog() -> dict[str, dict]:
    """Real platform draft cards harvested from logs (data/vgc_cards.json), keyed by card_id."""
    data = _load("vgc_cards")
    return {c["card_id"]: dict(c) for c in data} if isinstance(data, list) else {}
