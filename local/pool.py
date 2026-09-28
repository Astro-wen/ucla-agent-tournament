"""The VGC draft pool used by the local environment.

Preferred source: data/vgc_cards.json — the real platform cards, harvested from
recorded draft payloads (`python -m local.pool --harvest`). The platform sends
`ivs: {}`; locally we fill 31s (Showdown's validator needs them for legendaries).
Fallback: HANDWRITTEN, common Reg I sets written before we had real data.
`python -m local.pool` checks every card with Showdown's TeamValidator.

Pool sampling (`sample_pool`). The platform does NOT draw its 18 cards uniformly: over the 39 recorded VGC draft
pools (data/vgc_pools.json, filled by the same --harvest) Rillaboom was in 35 and Wo-Chien in 33, while Sneasler,
Toxapex and Ursaluna were in 3 each (the variance of the counts is ~7x a uniform draw's). The mix also drifts: the
09-28 live A/B pools (the last 20) hold the newest cards far more often (Tyranitar 7/20, after 1/19 before). So the
local pool gives every catalog card the inclusion probability it has on the platform, smoothed toward uniform:

    pi[c] = 18 * (count[c] + POOL_SMOOTH) / sum_d (count[d] + POOL_SMOOTH), capped at POOL_PI_MAX

and draws exactly 18 cards with those marginals (systematic PPS over a fresh random order, then shuffled: the
platform's `available_cards` order is random too). `ARENA_POOL=legacy` restores the old environment exactly
(uniform `rng.sample` of 18 over the 43-card catalog of before 09-28, `LEGACY_CARD_IDS`, same random stream,
item-blocked cards hidden), so pre-09-28 ladder / benchmark / panel numbers can be reproduced; `ARENA_POOL=uniform`
samples uniformly from the whole catalog but keeps the platform's item-blocked listing.

Only VGC doubles draft recordings are read (`_draft_states`, filtered by game type): the singles OU draft
(pokemon_gen9ou_draft, `ou-` cards) uses the same "draft" phase and must not leak into data/vgc_cards.json /
data/vgc_pools.json.
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[1] / "data" / "vgc_cards.json"
POOLS = Path(__file__).resolve().parents[1] / "data" / "vgc_pools.json"
POOL_SIZE = 18
POOL_SMOOTH = 0.5        # pseudo-count per card (Jeffreys-like): an unseen catalog card still shows up now and then
POOL_PI_MAX = 0.97       # no card is in every local pool

# Catalog cards the shipped draft model (agent/pokemon/draft_model.json) was NOT fitted on: harvested from real logs
# after the v3 fit (09-28; toxapex from the 09-28 live A/B logs). The drafter scores them with its unseen-card prior,
# as it would any new platform card. The model tests require catalog - model == exactly this set, so a new harvest
# cannot drift in silently: list the new cards here, or refit (scripts/fit_draft_model.py) on games that contain them
# and remove them. The fitter only writes cards that occur in its training games, so a card without games stays out
# of the model (and on this list) whatever the catalog holds.
NOT_IN_DRAFT_MODEL = frozenset({"vgc-pelipper", "vgc-scream-tail", "vgc-ursaluna", "vgc-tyranitar", "vgc-toxapex"})

# The catalog (data/vgc_cards.json) before 09-28: the 43 cards every earlier local number was measured on.
# ARENA_POOL=legacy samples from exactly these (in catalog order), as the old env's rng.sample(POOL, 18) did.
LEGACY_CARD_IDS = frozenset({
    "vgc-amoonguss", "vgc-arcanine", "vgc-archaludon", "vgc-basculegion", "vgc-baxcalibur", "vgc-chi-yu",
    "vgc-chien-pao", "vgc-cresselia", "vgc-dondozo", "vgc-dragonite", "vgc-farigiraf", "vgc-flutter-mane",
    "vgc-garchomp", "vgc-gholdengo", "vgc-great-tusk", "vgc-grimmsnarl", "vgc-gyarados", "vgc-hatterene",
    "vgc-incineroar", "vgc-indeedee-f", "vgc-iron-bundle", "vgc-iron-hands", "vgc-iron-treads", "vgc-iron-valiant",
    "vgc-kingambit", "vgc-klefki", "vgc-landorus-therian", "vgc-lucario", "vgc-milotic", "vgc-porygon2",
    "vgc-rillaboom", "vgc-sableye", "vgc-salamence", "vgc-sneasler", "vgc-sylveon", "vgc-talonflame",
    "vgc-thundurus", "vgc-ting-lu", "vgc-torkoal", "vgc-tornadus", "vgc-urshifu-rapid-strike", "vgc-whimsicott",
    "vgc-wo-chien"})

RESTRICTED = {"Kyogre", "Miraidon"}


ALL31 = {"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31}
# Event-locked IVs, exactly as Showdown's validator requires them.
EVENT_IVS = {
    "Raging Bolt": {"hp": 20, "atk": 20, "def": 20, "spa": 20, "spd": 20, "spe": 20},
    "Ogerpon-Wellspring": {"hp": 31, "atk": 31, "def": 20, "spa": 20, "spd": 20, "spe": 31},
    "Miraidon": {"hp": 31, "atk": 31, "def": 28, "spa": 31, "spd": 28, "spe": 31},
}


def card(cid, species, item, ability, nature, evs, moves, level=50):
    return {"card_id": cid, "species": species, "item": item, "ability": ability, "nature": nature,
            "level": level, "evs": evs, "ivs": dict(EVENT_IVS.get(species, ALL31)), "moves": moves}


HANDWRITTEN = [
    card("vgc-incineroar", "Incineroar", "Safety Goggles", "Intimidate", "Careful",
         {"hp": 252, "atk": 4, "def": 100, "spd": 148, "spe": 4}, ["Fake Out", "Flare Blitz", "Knock Off", "Parting Shot"]),
    card("vgc-rillaboom", "Rillaboom", "Assault Vest", "Grassy Surge", "Adamant",
         {"hp": 252, "atk": 116, "def": 44, "spd": 92, "spe": 4}, ["Fake Out", "Grassy Glide", "Wood Hammer", "U-turn"]),
    card("vgc-urshifu-rapid-strike", "Urshifu-Rapid-Strike", "Mystic Water", "Unseen Fist", "Adamant",
         {"hp": 4, "atk": 252, "spe": 252}, ["Surging Strikes", "Close Combat", "Aqua Jet", "Protect"]),
    card("vgc-flutter-mane", "Flutter Mane", "Booster Energy", "Protosynthesis", "Timid",
         {"hp": 4, "spa": 252, "spe": 252}, ["Moonblast", "Shadow Ball", "Icy Wind", "Protect"]),
    card("vgc-amoonguss", "Amoonguss", "Rocky Helmet", "Regenerator", "Calm",
         {"hp": 252, "def": 100, "spd": 156}, ["Spore", "Rage Powder", "Pollen Puff", "Protect"]),
    card("vgc-tornadus", "Tornadus", "Covert Cloak", "Prankster", "Timid",
         {"hp": 252, "spa": 4, "spe": 252}, ["Tailwind", "Bleakwind Storm", "Rain Dance", "Taunt"]),
    card("vgc-chien-pao", "Chien-Pao", "Focus Sash", "Sword of Ruin", "Jolly",
         {"hp": 4, "atk": 252, "spe": 252}, ["Icicle Crash", "Sucker Punch", "Sacred Sword", "Protect"]),
    card("vgc-chi-yu", "Chi-Yu", "Choice Specs", "Beads of Ruin", "Timid",
         {"hp": 4, "spa": 252, "spe": 252}, ["Heat Wave", "Dark Pulse", "Overheat", "Snarl"]),
    card("vgc-raging-bolt", "Raging Bolt", "Life Orb", "Protosynthesis", "Modest",
         {"hp": 252, "spa": 252, "spe": 4}, ["Thunderclap", "Draco Meteor", "Thunderbolt", "Protect"]),
    card("vgc-farigiraf", "Farigiraf", "Sitrus Berry", "Armor Tail", "Sassy",
         {"hp": 252, "def": 100, "spd": 156}, ["Trick Room", "Psychic", "Foul Play", "Helping Hand"]),
    card("vgc-kingambit", "Kingambit", "Black Glasses", "Defiant", "Adamant",
         {"hp": 252, "atk": 252, "spd": 4}, ["Kowtow Cleave", "Sucker Punch", "Iron Head", "Protect"]),
    card("vgc-gholdengo", "Gholdengo", "Leftovers", "Good as Gold", "Modest",
         {"hp": 252, "spa": 252, "spe": 4}, ["Make It Rain", "Shadow Ball", "Nasty Plot", "Protect"]),
    card("vgc-iron-hands", "Iron Hands", "Clear Amulet", "Quark Drive", "Adamant",
         {"hp": 252, "atk": 252, "spd": 4}, ["Fake Out", "Drain Punch", "Wild Charge", "Low Kick"]),
    card("vgc-archaludon", "Archaludon", "Power Herb", "Stamina", "Modest",
         {"hp": 252, "spa": 252, "spd": 4}, ["Electro Shot", "Draco Meteor", "Flash Cannon", "Body Press"]),
    card("vgc-grimmsnarl", "Grimmsnarl", "Light Clay", "Prankster", "Careful",
         {"hp": 252, "def": 4, "spd": 252}, ["Reflect", "Light Screen", "Spirit Break", "Thunder Wave"]),
    card("vgc-indeedee-f", "Indeedee-F", "Psychic Seed", "Psychic Surge", "Bold",
         {"hp": 252, "def": 252, "spd": 4}, ["Follow Me", "Helping Hand", "Psychic", "Trick Room"]),
    card("vgc-whimsicott", "Whimsicott", "Focus Sash", "Prankster", "Timid",
         {"hp": 4, "spa": 252, "spe": 252}, ["Tailwind", "Moonblast", "Encore", "Protect"]),
    card("vgc-pelipper", "Pelipper", "Damp Rock", "Drizzle", "Modest",
         {"hp": 252, "spa": 252, "spe": 4}, ["Hurricane", "Weather Ball", "Tailwind", "Protect"]),
    card("vgc-ting-lu", "Ting-Lu", "Assault Vest", "Vessel of Ruin", "Impish",
         {"hp": 252, "atk": 4, "def": 252}, ["Ruination", "Stomping Tantrum", "Rock Slide", "Heavy Slam"]),
    card("vgc-landorus", "Landorus", "Life Orb", "Sheer Force", "Timid",
         {"hp": 4, "spa": 252, "spe": 252}, ["Earth Power", "Sludge Bomb", "Sandsear Storm", "Protect"]),
    card("vgc-ursaluna-bloodmoon", "Ursaluna-Bloodmoon", "Assault Vest", "Mind's Eye", "Quiet",
         {"hp": 252, "spa": 252, "spd": 4}, ["Blood Moon", "Earth Power", "Hyper Voice", "Vacuum Wave"]),
    card("vgc-ogerpon-wellspring", "Ogerpon-Wellspring", "Wellspring Mask", "Water Absorb", "Jolly",
         {"hp": 4, "atk": 252, "spe": 252}, ["Ivy Cudgel", "Horn Leech", "Follow Me", "Spiky Shield"]),
    card("vgc-dragonite", "Dragonite", "Loaded Dice", "Multiscale", "Adamant",
         {"hp": 4, "atk": 252, "spe": 252}, ["Scale Shot", "Extreme Speed", "Low Kick", "Protect"]),
    card("vgc-porygon2", "Porygon2", "Eviolite", "Download", "Sassy",
         {"hp": 252, "def": 4, "spd": 252}, ["Trick Room", "Ice Beam", "Tera Blast", "Recover"]),
    card("vgc-kyogre", "Kyogre", "Choice Scarf", "Drizzle", "Modest",
         {"hp": 4, "spa": 252, "spe": 252}, ["Water Spout", "Origin Pulse", "Ice Beam", "Thunder"]),
    card("vgc-miraidon", "Miraidon", "Choice Specs", "Hadron Engine", "Timid",
         {"hp": 4, "spa": 252, "spe": 252}, ["Electro Drift", "Draco Meteor", "Volt Switch", "Dazzling Gleam"]),
]


def load_real() -> list[dict]:
    if not CATALOG.exists():
        return []
    return [{**c, "ivs": dict(c.get("ivs") or EVENT_IVS.get(c["species"], ALL31))}
            for c in json.loads(CATALOG.read_text())]


VGC_DRAFT_GAME = "pokemon_vgc_doubles_draft"


def is_vgc_draft(raw: dict, obs: dict) -> bool:
    """A VGC doubles draft state, by the platform's game type (every recorded draft state carries game_type
    pokemon_vgc_doubles_draft and battle_format gen9vgc2025regi); a record without game_type falls back to its
    battle_format. The singles OU draft (pokemon_gen9ou_draft, battle_format gen9ou, `ou-` cards) is not one."""
    game = raw.get("game_type")
    if game:
        return game == VGC_DRAFT_GAME
    return str(obs.get("battle_format") or "").lower().startswith("gen9vgc")


def _draft_states(log_glob: str):
    """(session_id, observation) of every recorded VGC draft decision (both agents' logs)."""
    for f in sorted(glob.glob(log_glob)):
        for line in open(f):
            try:
                raw = json.loads(line).get("raw", {})
            except ValueError:
                continue
            o = raw.get("observation") if isinstance(raw, dict) else None
            if isinstance(o, dict) and o.get("phase") == "draft" and is_vgc_draft(raw, o):
                yield str(raw.get("session_id") or f), o


def harvest(log_glob: str = "logs/*/*.jsonl") -> int:
    """Merge every card seen in recorded draft payloads into data/vgc_cards.json, and every match's full 18-card
    pool (union of the cards offered at any pick + the cards already picked) into data/vgc_pools.json."""
    cards = {c["card_id"]: c for c in (json.loads(CATALOG.read_text()) if CATALOG.exists() else [])}
    pools: dict[str, set] = {}
    for sid, o in _draft_states(log_glob):
        seen = pools.setdefault(sid, set())
        for c in o.get("available_cards", []):
            cards[c["card_id"]] = c
            seen.add(c["card_id"])
        for p in o.get("picks") or []:
            if p.get("card_id"):
                seen.add(p["card_id"])
    CATALOG.parent.mkdir(exist_ok=True)
    CATALOG.write_text(json.dumps(sorted(cards.values(), key=lambda c: c["card_id"]), indent=1))
    old = json.loads(POOLS.read_text()).get("pools", {}) if POOLS.exists() else {}
    for sid, ids in pools.items():
        if len(ids) == POOL_SIZE:                   # only complete pools (a partial log would bias the counts)
            old[sid] = sorted(ids)
    POOLS.write_text(json.dumps({"about": "full 18-card pool of every recorded real VGC draft (session_id -> "
                                          "card_ids); python -m local.pool --harvest", "pools": dict(sorted(old.items()))},
                                indent=1))
    return len(cards)


def real_pools() -> dict[str, list[str]]:
    try:
        return {str(k): list(v) for k, v in json.loads(POOLS.read_text()).get("pools", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def inclusion_probs(cards: list[dict], pools: dict[str, list[str]] | None = None, k: int = POOL_SIZE,
                    smooth: float = POOL_SMOOTH, cap: float = POOL_PI_MAX) -> list[float]:
    """Per-card probability of being in a k-card pool: the real pool frequency, smoothed toward uniform, summing to
    k, none above `cap`. Uniform k/n when there are no recorded pools."""
    n = len(cards)
    if n == 0:
        return []
    if n <= k:
        return [1.0] * n
    pools = real_pools() if pools is None else pools
    counts = {}
    for ids in pools.values():
        for cid in set(ids):
            counts[cid] = counts.get(cid, 0) + 1
    raw = [counts.get(c["card_id"], 0) + smooth for c in cards]
    if not pools or sum(raw) <= 0:
        return [k / n] * n
    cap = max(cap, k / n)
    pi = [0.0] * n
    free = list(range(n))
    budget = float(k)
    while free:                                      # proportional to raw, capped cards pinned at `cap`
        tot = sum(raw[i] for i in free)
        over = [i for i in free if budget * raw[i] / tot > cap]
        if not over:
            for i in free:
                pi[i] = budget * raw[i] / tot
            break
        for i in over:
            pi[i] = cap
            budget -= cap
        free = [i for i in free if i not in over]
    return pi


def sample_by_inclusion(items: list, pi: list[float], rng, k: int) -> list:
    """Exactly k items with P(item i chosen) = pi[i] (sum(pi) == k, each <= 1): systematic PPS over a random order."""
    order = list(range(len(items)))
    rng.shuffle(order)
    u = rng.random()
    out, cum = [], 0.0
    for i in order:
        lo, cum = cum, cum + pi[i]
        # chosen when an integer point u + j falls in [lo, cum)
        if int(cum - u + 1.0) - int(lo - u + 1.0) >= 1:
            out.append(i)
    if len(out) != k:                               # float edge (sum(pi) = k - 1e-12): top up / trim at random
        rest = [i for i in order if i not in out]
        while len(out) < k and rest:
            out.append(rest.pop(rng.randrange(len(rest))))
        while len(out) > k:
            out.pop(rng.randrange(len(out)))
    rng.shuffle(out)
    return [items[i] for i in out]


def pool_mode() -> str:
    """'real' (default: platform frequencies + item-blocked cards listed), 'uniform', or 'legacy' (the old env)."""
    m = (os.environ.get("ARENA_POOL") or "real").strip().lower()
    return m if m in ("real", "uniform", "legacy") else "real"


def legacy_pool(pool: list[dict]) -> list[dict]:
    """The pre-09-28 catalog inside `pool` (catalog order kept). A pool without all of those cards (the handwritten
    fallback, a hand-made test pool) is returned as it is, which is what the old env sampled from too."""
    old = [c for c in pool if c.get("card_id") in LEGACY_CARD_IDS]
    return old if len(old) == len(LEGACY_CARD_IDS) else pool


_PI_CACHE: dict = {}


def sample_pool(rng, pool: list[dict] | None = None, k: int = POOL_SIZE) -> list[dict]:
    """One match's k-card pool. `rng` is the match's random.Random."""
    pool = POOL if pool is None else pool
    mode = pool_mode()
    if mode == "legacy":
        pool = legacy_pool(pool)
    k = min(k, len(pool))
    if mode != "real":
        return rng.sample(pool, k)
    key = (id(pool), len(pool), k)
    if key not in _PI_CACHE:
        _PI_CACHE[key] = inclusion_probs(pool, k=k)
    return sample_by_inclusion(pool, _PI_CACHE[key], rng, k)


POOL = load_real() or HANDWRITTEN


def to_showdown_set(c: dict) -> dict:
    return {"name": c["species"], "species": c["species"], "item": c["item"], "ability": c["ability"],
            "nature": c["nature"], "level": c.get("level", 50), "evs": dict(c["evs"]), "ivs": dict(c.get("ivs") or {}),
            "moves": list(c["moves"]), "gender": ""}


if __name__ == "__main__":
    import sys
    if "--harvest" in sys.argv:
        print(f"{harvest()} cards in {CATALOG}; {len(real_pools())} real pools in {POOLS}")
        POOL = load_real()
    print(f"pool source: {'real platform cards' if load_real() else 'handwritten'} ({len(POOL)} cards); "
          f"sampling mode {pool_mode()}")
    if "--freq" in sys.argv:
        pools = real_pools()
        pi = inclusion_probs(POOL)
        cnt = {}
        for ids in pools.values():
            for cid in ids:
                cnt[cid] = cnt.get(cid, 0) + 1
        for c, p in sorted(zip(POOL, pi), key=lambda t: -t[1]):
            print(f"  {c['card_id']:28s} real {cnt.get(c['card_id'], 0):2d}/{len(pools)}  local pi {p:.3f}")
    from .showdown import Showdown
    with Showdown() as sd:
        probs = sd.validate([to_showdown_set(c) for c in POOL])
    bad = [(c["card_id"], p) for c, p in zip(POOL, probs) if p]
    for cid, p in bad:
        print("ILLEGAL", cid, p)
    print(f"{len(POOL) - len(bad)}/{len(POOL)} cards legal in gen9vgc2025regi")
