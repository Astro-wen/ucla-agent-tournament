"""A local stand-in for the platform's 18-card VGC draft pool.

The platform's real pool is not public. These are common Regulation I sets,
written by hand and checked for legality by Showdown's TeamValidator
(`python -m local.pool` runs the check). Only two restricted Pokémon are in
the pool so a roster can never exceed the format's "Limit Two Restricted".
"""
from __future__ import annotations

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


POOL = [
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


def to_showdown_set(c: dict) -> dict:
    return {"name": c["species"], "species": c["species"], "item": c["item"], "ability": c["ability"],
            "nature": c["nature"], "level": c.get("level", 50), "evs": dict(c["evs"]), "ivs": dict(c.get("ivs") or {}),
            "moves": list(c["moves"]), "gender": ""}


if __name__ == "__main__":
    from .showdown import Showdown
    with Showdown() as sd:
        probs = sd.validate([to_showdown_set(c) for c in POOL])
    bad = [(c["card_id"], p) for c, p in zip(POOL, probs) if p]
    for cid, p in bad:
        print("ILLEGAL", cid, p)
    print(f"{len(POOL) - len(bad)}/{len(POOL)} cards legal in gen9vgc2025regi")
