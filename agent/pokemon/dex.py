"""Showdown dex/move data (run scripts/fetch_showdown_data.sh first). Degrades to {} if missing."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"


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


def types_of(name: str) -> list[str]:
    return [t.upper() for t in species(name).get("types", [])]


def base_stats(name: str) -> dict:
    return species(name).get("baseStats", {})
