"""Local environments for the platform's SINGLES Pokémon game types (the VGC doubles draft lives in pokemon_env.py).

    pokemon_gen9same          both seats get the same team; team preview; Gen 9 singles (format gen9customgame)
    pokemon_gen9random        Showdown random-battle teams; no team preview (gen9randombattle)
    pokemon_gen9ou_teambuild  each seat submits a 6-Pokémon OU team; team preview; gen9ou
    pokemon_gen9ou_draft      18-card snake draft (Item Clause), team preview, OU singles

Shapes follow what the real platform sent in the one recorded singles match (logs/*/c8efbea9-*.jsonl,
docs/real_payloads.md) and docs/platform/pokemon.md:
  * team preview is `select_lineup` (bring 4, 2 leads) even in singles, but ALL SIX Pokémon battle: the lineup only
    orders the team (leads, then the rest of `bring`, then the others) -- the recorded match switched into Pokémon that
    were not in `bring`;
  * battle legal actions are `move:<i>` (index into `available_moves`) and `switch:<i>` (index into
    `available_switches`); `active_pokemon` / `opponent_active_pokemon` are dicts, `force_switch` a bool;
  * the foe's item is "unknown_item", ability None and moves [] until revealed in the protocol;
  * gen9same sets carry no EVs: Showdown's validator fills 252 in every stat for a custom game (fits all 13 damage
    rolls of the recorded match at level 100, neutral nature), so the local teams do the same.
Unknowns, modelled as the doubles env does: the draft pool of pokemon_gen9ou_draft (never seen for real; built from
Showdown's Gen 9 Battle Factory OU/UU sets at level 100), the platform's teambuild fallback sets (its six species
with our DEFAULT_OU_TEAM sets), whether foe HP is exact or out of 100 (exact in gen9same as recorded, else /100).

An illegal answer is replaced by a random legal one and counted in `invalid`, like the platform's timer would.
"""
from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

from altruagent import DecisionContext, GameState, LegalAction

from .pokemon_env import SNAKE, _sid
from .showdown import JS_DIR, Showdown

os.environ["ARENA_RECORD"] = "0"      # never record simulated games (agent/agent.py record())
log = logging.getLogger("local.singles")

FORMATS = {
    "same": {"game": "pokemon_gen9same", "sd": "gen9customgame", "fmt": "gen9customgame", "preview": True},
    "random": {"game": "pokemon_gen9random", "sd": "gen9randombattle", "fmt": "gen9randombattle", "preview": False},
    "teambuild": {"game": "pokemon_gen9ou_teambuild", "sd": "gen9ou", "fmt": "gen9ou", "preview": True},
    "draft": {"game": "pokemon_gen9ou_draft", "sd": "gen9customgame", "fmt": "gen9ou", "preview": True},
}
MAX_TURNS = 300                   # a stall war past this is scored as a tie
MAX_SUBMITS = 8                   # teambuild attempts before the platform's fallback team

# The one real gen9same team (session c8efbea9): the platform's curated sets, no EVs -> 252 everywhere, level 100.
REAL_SAME_TEAM = [
    ("Pikachu", "Light Ball", "Static", ["Thunderbolt", "Volt Switch", "Grass Knot", "Surf"]),
    ("Charizard", "Life Orb", "Blaze", ["Flamethrower", "Air Slash", "Dragon Pulse", "Roost"]),
    ("Garchomp", "Leftovers", "Rough Skin", ["Earthquake", "Dragon Claw", "Swords Dance", "Fire Fang"]),
    ("Clefable", "Leftovers", "Magic Guard", ["Moonblast", "Soft-Boiled", "Calm Mind", "Thunder Wave"]),
    ("Ferrothorn", "Leftovers", "Iron Barbs", ["Leech Seed", "Power Whip", "Spikes", "Gyro Ball"]),
    ("Rotom-Wash", "Leftovers", "Levitate", ["Hydro Pump", "Volt Switch", "Pain Split", "Will-O-Wisp"]),
]
ALL252 = {s: 252 for s in ("hp", "atk", "def", "spa", "spd", "spe")}
ALL31 = {s: 31 for s in ("hp", "atk", "def", "spa", "spd", "spe")}
FACTORY_SETS = JS_DIR / "node_modules" / "pokemon-showdown" / "data" / "random-battles" / "gen9" / "factory-sets.json"


def _sd_set(species, item, ability, moves, nature="Serious", evs=None, ivs=None, level=100) -> dict:
    return {"name": species, "species": species, "item": item, "ability": ability, "nature": nature,
            "evs": dict(evs if evs is not None else ALL252), "ivs": dict(ivs or ALL31), "level": level,
            "moves": list(moves), "gender": ""}


def real_same_team() -> list[dict]:
    return [_sd_set(sp, it, ab, mv) for sp, it, ab, mv in REAL_SAME_TEAM]


def fallback_team() -> list[dict]:
    """The platform's teambuild fallback species (pokemon.md section 5) with our DEFAULT_OU_TEAM sets."""
    from agent.pokemon.policy import DEFAULT_OU_TEAM
    return [submitted_set(s) for s in DEFAULT_OU_TEAM]


def submitted_set(s: dict) -> dict:
    """A platform submit_team entry -> Showdown set (level 100 unless given, IVs 31 unless given)."""
    return {"name": s.get("species", ""), "species": s.get("species", ""), "item": s.get("item", ""),
            "ability": s.get("ability", ""), "nature": s.get("nature", "Serious"), "evs": dict(s.get("evs") or {}),
            "ivs": {**ALL31, **(s.get("ivs") or {})}, "level": int(s.get("level") or 100),
            "moves": list(s.get("moves") or []), "gender": ""}


# ---------------------------------------------------------------- card / team sources
_FACTORY: dict | None = None


def factory_sets() -> dict:
    global _FACTORY
    if _FACTORY is None:
        _FACTORY = json.loads(FACTORY_SETS.read_text()) if FACTORY_SETS.exists() else {}
    return _FACTORY


def factory_card(rng: random.Random, species_key: str, entry: dict, tier: str) -> dict | None:
    """One concrete set from a Battle Factory entry (weighted set, first-or-random option per slot)."""
    sets = [s for s in entry.get("sets", []) if s.get("species")]
    if not sets:
        return None
    s = rng.choices(sets, weights=[max(1, int(x.get("weight", 1))) for x in sets])[0]
    moves = []
    for slot in s.get("moves", []):
        opts = [m for m in slot if m not in moves and _sid(m) != "terablast"]
        if opts:
            moves.append(rng.choice(opts))
    if len(moves) < 2:
        return None
    pick = lambda xs, d="": rng.choice(xs) if isinstance(xs, list) and xs else (xs or d)   # noqa: E731
    return {"card_id": f"{tier.lower()}-{_sid(s['species'])}", "species": s["species"], "item": pick(s.get("item")),
            "ability": pick(s.get("ability")), "nature": pick(s.get("nature"), "Serious"), "level": 100,
            "evs": dict(s.get("evs") or {}), "ivs": {**ALL31, **(s.get("ivs") or {})}, "moves": moves}


def factory_pool(rng: random.Random, n: int = 18, tiers=("OU", "UU")) -> list[dict]:
    """n cards with distinct species AND distinct items (so the Item Clause never empties a seat's offer) from the
    Gen 9 Battle Factory sets (the real OU draft pool is unknown)."""
    fs = factory_sets()
    keys = [(t, k) for t in tiers for k in sorted(fs.get(t, {}))]
    rng.shuffle(keys)
    out, seen, items = [], set(), set()
    for t, k in keys:
        c = None
        for _try in range(4):
            c = factory_card(rng, k, fs[t][k], t)
            if c is None or c["item"] not in items:
                break
        if c is None or c["item"] in items:
            continue
        base = _sid(c["species"].split("-")[0])
        if base in seen:
            continue
        seen.add(base)
        items.add(c["item"])
        out.append(c)
        if len(out) == n:
            break
    return out


def factory_team(rng: random.Random) -> list[dict]:
    """A random 6-Pokémon OU team from the factory sets, distinct species and items (for varied teambuild opponents)."""
    out, items = [], set()
    for c in factory_pool(rng, 24, ("OU",)):
        if c["item"] in items:
            continue
        items.add(c["item"])
        out.append(c)
        if len(out) == 6:
            break
    return [_sd_set(c["species"], c["item"], c["ability"], c["moves"], c["nature"], c["evs"], c["ivs"], 100)
            for c in out]


# ---------------------------------------------------------------- players
class Player:
    name = "player"

    def reset(self):
        pass

    def choose(self, raw: dict):
        raise NotImplementedError


class ContestantPlayer(Player):
    """Our deployed agent (or an archived version) exactly as the official runner calls it."""

    def __init__(self, factory, name="ours"):
        self.factory, self.name = factory, name
        self.agent = None

    def reset(self):
        self.agent = self.factory()

    def choose(self, raw: dict):
        gs = GameState.from_mcp_state(raw)
        ctx = DecisionContext(session_id=raw["session_id"], tournament_id=None, game_type=raw["game_type"],
                              agent_id=raw["current_actor"]["agent_id"])
        from .pokemon_env import runner_view   # public reasoning: checked like the real runner, then the move inside
        return runner_view(self.agent.choose_action(gs, ctx), gs)


FALLBACK = "__fallback_team__"


class OfficialBaseline(Player):
    """The starter's `return state.legal_actions[0]`: first card, first move / switch. Its team-preview and teambuild
    answers are the templates the server rejects: the runner dies and the platform takes over (random lineup,
    fallback team after 8 rejected teams)."""
    name = "official_baseline"

    def choose(self, raw: dict):
        la = raw["legal_actions"]["actions"]
        if raw["phase"] == "team_preview":
            return random_lineup(raw)
        if raw["phase"] == "teambuild":
            return FALLBACK
        return la[0]["action_id"]


class RandomPlayer(Player):
    name = "random"

    def choose(self, raw: dict):
        if raw["phase"] == "team_preview":
            return random_lineup(raw)
        if raw["phase"] == "teambuild":
            return FALLBACK
        return random.choice(raw["legal_actions"]["actions"])["action_id"]


def random_lineup(raw: dict) -> dict:
    ids = [p["species"] for p in raw["observation"]["your_roster"]]
    bring = random.sample(ids, min(4, len(ids)))
    return {"type": "select_lineup", "bring": bring, "leads": bring[:2]}


# ---------------------------------------------------------------- views
def _upper_snake(s: str | None) -> str:
    out = ""
    for ch in str(s or ""):
        out += ("_" + ch) if ch.isupper() and out else ch.upper()
    return out.upper()


def _conditions(side: dict) -> dict:
    """poke-env style side conditions: {NAME: layers} (Spikes 1-3, Toxic Spikes 1-2, else 1)."""
    layers = side.get("side_layers") or {}
    return {c.upper(): int(layers.get(c) or 1) for c in side.get("side_conditions") or []}


def _status(p: dict) -> str | None:
    if p.get("fainted"):
        return "FNT"
    return str(p["status"]).upper() if p.get("status") else None


def own_view(p: dict | None) -> dict | None:
    if not p:
        return None
    return {"species": p["id"], "name": p["species"], "current_hp": p["current_hp"], "max_hp": p["max_hp"],
            "current_hp_fraction": p["current_hp_fraction"], "status": _status(p), "ability": p.get("ability"),
            "item": p.get("item") or "", "types": p["types"], "base_stats": p["base_stats"], "boosts": p["boosts"],
            "moves": list(p.get("moves") or []), "fainted": p["fainted"], "active": p["active"],
            "revealed": bool(p["active"]), "is_terastallized": False}


def foe_view(p: dict | None, rev: dict, exact_hp: bool) -> dict | None:
    """What the real platform shows of an opposing Pokémon: HP (exact in gen9same, else out of 100), item / ability /
    moves only once revealed in the protocol, a consumed or knocked-off item as None."""
    if not p:
        return None
    frac = p["current_hp_fraction"]
    if exact_hp:
        cur, mx = p["current_hp"], p["max_hp"]
    else:
        cur = 0 if p["fainted"] else max(1, round(100 * frac))
        mx, frac = 100, (0 if p["fainted"] else max(1, round(100 * frac)) / 100)
    r = rev.get(p.get("name") or p["species"], {})
    item = r.get("item", "unknown_item")
    return {"species": p["id"], "name": p["species"], "current_hp": cur, "max_hp": mx, "current_hp_fraction": frac,
            "status": _status(p), "ability": r.get("ability"), "item": item, "types": p["types"],
            "base_stats": p["base_stats"], "boosts": p["boosts"], "moves": list(r.get("moves", [])),
            "fainted": p["fainted"], "active": p["active"], "revealed": True, "is_terastallized": False}


def preview_entry(s: dict, full: bool) -> dict:
    from agent.pokemon import dex
    v = {"species": _sid(s["species"]), "name": s["species"], "types": dex.types_of(s["species"]),
         "base_stats": dex.base_stats(s["species"]),
         "boosts": {"accuracy": 0, "atk": 0, "def": 0, "evasion": 0, "spa": 0, "spd": 0, "spe": 0}, "status": None,
         "fainted": False, "active": False, "revealed": False, "is_terastallized": False}
    if full:
        v.update({"ability": _sid(s["ability"]), "item": _sid(s["item"]), "moves": [_sid(m) for m in s["moves"]],
                  "current_hp_fraction": 1.0})
    else:
        v.update({"ability": None, "item": "unknown_item", "moves": [], "current_hp": 0, "max_hp": 0,
                  "current_hp_fraction": 0})
    return v


class Reveals:
    """Per side: what the protocol has revealed of each Pokémon (keyed by its nickname = species display name)."""

    def __init__(self):
        self.side = {"p1": {}, "p2": {}}

    @staticmethod
    def _who(tok: str):
        tok = tok.strip()
        if len(tok) < 3 or tok[0] != "p" or ":" not in tok:
            return None, None
        return tok[:2], tok.split(":", 1)[1].strip()

    def feed(self, line: str) -> None:
        parts = line.split("|")
        if len(parts) < 3:
            return
        tag = parts[1]
        side, nick = self._who(parts[2])
        if side is None:
            return
        of = next((p[5:] for p in parts if p.startswith("[of] ")), None)
        frm = next((p[7:] for p in parts if p.startswith("[from] ")), None)
        if tag in ("switch", "drag", "replace"):
            self.side[side].setdefault(nick, {})
        if tag == "move" and len(parts) > 3 and not frm:
            self.side[side].setdefault(nick, {}).setdefault("moves", [])
            mid = _sid(parts[3])
            if mid not in self.side[side][nick]["moves"] and mid != "struggle":
                self.side[side][nick]["moves"].append(mid)
        elif tag == "-item" and len(parts) > 3:
            self.side[side].setdefault(nick, {})["item"] = _sid(parts[3])
        elif tag == "-enditem" and len(parts) > 3:
            self.side[side].setdefault(nick, {})["item"] = None
        elif tag == "-ability" and len(parts) > 3:
            self.side[side].setdefault(nick, {})["ability"] = _sid(parts[3])
        if frm and (frm.startswith("item: ") or frm.startswith("ability: ")):
            s2, n2 = (self._who(of) if of else (side, nick))
            if s2:
                kind, name = frm.split(": ", 1)
                rec = self.side[s2].setdefault(n2, {})
                if kind == "item" and tag != "-enditem":
                    rec["item"] = _sid(name)
                elif kind == "ability":
                    rec["ability"] = _sid(name)


# ---------------------------------------------------------------- match
@dataclass
class SinglesResult:
    winner: int | None
    turns: int
    invalid: list[int] = field(default_factory=lambda: [0, 0])
    teams: list[list[str]] = field(default_factory=list)
    decisions: list[int] = field(default_factory=lambda: [0, 0])
    errors: list[tuple] = field(default_factory=list)


class SinglesMatch:
    def __init__(self, sd: Showdown, fmt: str, players: list[Player], rng: random.Random, opp_team: str = "platform"):
        self.sd, self.fmt, self.players, self.rng = sd, fmt, players, rng
        self.cfg = FORMATS[fmt]
        # teambuild, what a team-less seat gets: "platform" (the fallback six), "factory" (a Battle-Factory OU team: the
        # same source as the agent's teambuild guess of foe sets, so flattering) or "randbats" (a random-battle team
        # re-set to lv 100 / 252 EVs, as in gen9same: sets the agent's teambuild guess did not come from)
        self.opp_team = opp_team
        self.ids = ["agent-a", "agent-b"]
        self.version = 0
        self.invalid = [0, 0]
        self.decisions = [0, 0]
        self.teams: list[list[dict] | None] = [None, None]
        self.order = ["", ""]
        self.errors: list[tuple] = []           # (seat, refused choice, engine detail) for debugging
        for p in players:
            p.reset()

    # ---- envelope ------------------------------------------------------
    def _state(self, seat: int, phase: str, obs: dict, actions: list[dict], version: int | None = None) -> dict:
        v = self.version if version is None else version
        return {"session_id": "local", "game_type": self.cfg["game"], "runtime_adapter": "pokemon",
                "status": "in_progress", "state_version": v, "phase": phase, "observation": obs,
                "is_terminal": False, "is_current_actor": bool(actions),
                "current_actor": {"agent_id": self.ids[seat], "position": seat},
                "legal_actions": {"session_id": "local", "state_version": v, "actions": actions},
                "next_actions": [{"tool": "play_action"}]}

    @staticmethod
    def _act(aid: str, action: dict, label: str, version: int) -> dict:
        return {"action_id": aid, "label": label,
                "input": {"session_id": "local", "action": action, "action_id": aid, "state_version": version}}

    def _choose(self, seat: int, raw: dict):
        self.decisions[seat] += 1
        try:
            out = self.players[seat].choose(raw)
        except Exception:
            log.warning("player %s crashed", self.players[seat].name, exc_info=True)
            out = None
        return out

    # ---- teams ---------------------------------------------------------
    def setup_teams(self) -> None:
        mode = self.fmt
        if mode == "same":
            if self.rng.random() < 1 / 3:
                team = real_same_team()
            else:
                gen = self.sd.gen_team("gen9randombattle", [self.rng.randrange(1, 65535) for _ in range(4)])
                team = [_sd_set(s["species"], s.get("item", ""), s.get("ability", ""), s["moves"]) for s in gen]
            self.teams = [team, [dict(s, evs=dict(s["evs"]), ivs=dict(s["ivs"])) for s in team]]
        elif mode == "random":
            self.teams = [self.sd.gen_team("gen9randombattle", [self.rng.randrange(1, 65535) for _ in range(4)])
                          for _ in range(2)]
        elif mode == "teambuild":
            self.teams = [self.teambuild(seat) for seat in range(2)]
        elif mode == "draft":
            self.teams = self.draft()

    def teambuild(self, seat: int) -> list[dict]:
        failed = 0
        while failed < MAX_SUBMITS:
            obs = {"phase": "teambuild", "battle_format": self.cfg["fmt"], "you_submitted": False,
                   "opponent_submitted": self.teams[1 - seat] is not None, "fallback_used": False,
                   "submit_attempts_failed": failed, "submit_attempts_max": MAX_SUBMITS,
                   "submit_attempts_remaining": MAX_SUBMITS - failed,
                   "message": "Submit a structured 6-Pokémon team via play_action. Showdown must accept it."}
            tmpl = {"type": "submit_team", "team": [], "instructions": "6 sets: species, item, ability, nature, evs, moves"}
            out = self._choose(seat, self._state(seat, "teambuild", obs, [self._act("submit_team", tmpl, "Submit team",
                                                                                      self.version)]))
            if out == FALLBACK:
                break
            if isinstance(out, LegalAction):
                out = out.input.get("action")
            team = out.get("team") if isinstance(out, dict) and out.get("type") == "submit_team" else None
            ok = isinstance(team, list) and len(team) == 6 and all(isinstance(s, dict) for s in team)
            if ok:
                sets = [submitted_set(s) for s in team]
                problems = self.sd.validate(sets, self.cfg["sd"])
                ok = not any(problems) and len({_sid(s["species"]) for s in sets}) == 6
                if not ok:
                    log.info("seat %d team rejected: %s", seat, [p for p in problems if p])
            if ok:
                self.version += 1
                return sets
            self.invalid[seat] += 1
            failed += 1
        self.version += 1
        if self.opp_team == "factory":
            return factory_team(self.rng)
        if self.opp_team == "randbats":
            gen = self.sd.gen_team("gen9randombattle", [self.rng.randrange(1, 65535) for _ in range(4)])
            return [_sd_set(s["species"], s.get("item", ""), s.get("ability", ""), s["moves"]) for s in gen]
        return fallback_team()

    def draft(self) -> list[list[dict]]:
        pool = factory_pool(self.rng, 18)
        first = self.rng.randrange(2)
        rosters: list[list[dict]] = [[], []]
        picks, taken = [], set()
        for n, letter in enumerate(SNAKE, 1):
            seat = first if letter == "A" else 1 - first
            my_items = {c["item"] for c in rosters[seat]}
            offered = [c for c in pool if c["card_id"] not in taken and c["item"] not in my_items]
            obs = {"phase": "draft", "draft_complete": False, "battle_format": self.cfg["fmt"],
                   "current_seat": self.ids[seat], "pick_number": n, "picks_remaining": 13 - n,
                   "first_drafter": self.ids[first], "available_card_ids": [c["card_id"] for c in offered],
                   "available_cards": offered,
                   "rosters": {self.ids[i]: [{"card_id": c["card_id"], "species": c["species"]} for c in rosters[i]]
                               for i in range(2)}, "picks": list(picks)}
            actions = [self._act(f"draft_pick:{c['card_id']}", {"type": "draft_pick", "card_id": c["card_id"]},
                                 f"Draft {c['species']}", self.version) for c in offered]
            out = self._choose(seat, self._state(seat, "draft", obs, actions))
            if isinstance(out, LegalAction):
                out = out.action_id
            if isinstance(out, str) and out.startswith("draft_pick:"):
                out = {"type": "draft_pick", "card_id": out.split(":", 1)[1]}
            cid = out.get("card_id") if isinstance(out, dict) else None
            card = next((c for c in offered if c["card_id"] == cid), None)
            if card is None:
                self.invalid[seat] += 1
                card = self.rng.choice(offered)
            taken.add(card["card_id"])
            rosters[seat].append(card)
            picks.append({"pick_number": n, "seat_id": self.ids[seat], "card_id": card["card_id"],
                          "species": card["species"]})
            self.version += 1
        return [[_sd_set(c["species"], c["item"], c["ability"], c["moves"], c["nature"], c["evs"], c["ivs"],
                         c.get("level", 100)) for c in r] for r in rosters]

    # ---- team preview ----------------------------------------------------
    def preview(self) -> list[str]:
        orders = []
        for seat in range(2):
            mine = [preview_entry(s, True) for s in self.teams[seat]]
            obs = {"phase": "team_preview", "battle_format": self.cfg["fmt"], "battle_tag": "battle-local",
                   "waiting_for_action": True, "your_roster": mine,
                   "opponent_roster": [preview_entry(s, False) for s in self.teams[1 - seat]]}
            ids = [p["species"] for p in mine]
            tmpl = {"type": "select_lineup", "roster": ids, "bring_count": 4, "lead_count": 2,
                    "instructions": "Choose exactly 4 species from `roster` to bring into battle, and which 2 of "
                                    "those 4 lead."}
            out = self._choose(seat, self._state(seat, "team_preview", obs,
                                                 [self._act("select_lineup", tmpl, "Choose 4 of 6 and designate 2 leads",
                                                            self.version)]))
            if isinstance(out, LegalAction):
                out = out.input.get("action")
            ok = False
            try:
                bring = [_sid(b) for b in out.get("bring", [])]
                leads = [_sid(x) for x in out.get("leads", [])]
                ok = (out.get("type") == "select_lineup" and len(set(bring)) == 4 and all(b in ids for b in bring)
                      and len(set(leads)) == 2 and set(leads) <= set(bring))
            except Exception:
                ok = False
            if not ok:
                self.invalid[seat] += 1
                r = random_lineup({"observation": obs})
                bring, leads = [_sid(b) for b in r["bring"]], [_sid(x) for x in r["leads"]]
            seq = leads + [b for b in bring if b not in leads] + [i for i in ids if i not in bring]
            orders.append("team " + "".join(str(ids.index(s) + 1) for s in seq))
            self.version += 1
        self.order = orders
        return orders

    # ---- battle ----------------------------------------------------------
    def battle(self) -> SinglesResult:
        seed = [self.rng.randrange(1, 65535) for _ in range(4)]
        snap = self.sd.start(self.teams[0], self.teams[1], seed=seed, fmt=self.cfg["sd"])
        rev = Reveals()
        plog = {"p1": [], "p2": []}
        pending_split = None

        def feed(lines):
            nonlocal pending_split
            for ln in lines:
                if ln.startswith("|split|"):
                    pending_split = [ln.split("|")[2], None]
                    continue
                if pending_split is not None:
                    if pending_split[1] is None:          # the secret (exact) line for that side
                        pending_split[1] = ln
                        continue
                    owner, secret = pending_split
                    pending_split = None
                    for s in ("p1", "p2"):
                        plog[s].append(secret if s == owner else ln)
                    rev.feed(ln)
                    continue
                for s in ("p1", "p2"):
                    plog[s].append(ln)
                rev.feed(ln)

        feed(snap.get("log", []))
        if self.cfg["preview"]:
            orders = self.preview()
            snap = self.sd.choose(orders[0], orders[1])
            feed(snap.get("log", []))
        versions = [0, 0]
        turns = stuck = 0
        while not snap["ended"]:
            before = (snap["state"]["turn"], len(snap["log"]))
            pending = [s for s in ("p1", "p2") if snap["requests"].get(s)]
            for sid in pending:
                seat = 0 if sid == "p1" else 1
                req = snap["requests"][sid]
                if req.get("teamPreview"):              # random battles have none; a stray one: default order
                    snap = self.sd.choose(**{sid: "default"})
                    feed(snap.get("log", []))
                    continue
                raw, choices = self._battle_state(seat, sid, req, snap["state"], versions[seat], rev, plog[sid])
                out = self._choose(seat, raw)
                choice = self._to_choice(out, raw, choices)
                if choice is None:
                    self.invalid[seat] += 1
                    choice = self.rng.choice(list(choices.values()))
                versions[seat] += 1
                snap = self.sd.choose(**{sid: choice})
                for _retry in range(4):
                    if "error" not in snap:
                        break
                    self.invalid[seat] += 1
                    self.errors.append((seat, choice, snap.get("detail")))
                    snap = self.sd.choose(**{sid: self.rng.choice(list(choices.values()))})
                if "error" in snap:
                    log.warning("engine refused every choice for %s: %s", sid, snap.get("detail"))
                    snap = self.sd.choose(**{sid: "default"})
                feed(snap.get("log", []))
                if snap["ended"]:
                    break
            turns = snap["state"]["turn"]
            stuck = stuck + 1 if (turns, len(snap["log"])) == before and snap["log"] == [] else 0
            if turns > MAX_TURNS:
                return SinglesResult(None, turns, list(self.invalid), self._names(), list(self.decisions),
                                     list(self.errors))
            if stuck > 20 or not pending:
                raise RuntimeError(f"battle stalled at turn {turns}: requests={list(snap['requests'])} "
                                   f"error={snap.get('detail')}")
        w = snap.get("winner")
        winner = 0 if w == "p1" else 1 if w == "p2" else None
        return SinglesResult(winner, turns, list(self.invalid), self._names(), list(self.decisions),
                             list(self.errors))

    def _names(self) -> list[list[str]]:
        return [[s["species"] for s in (t or [])] for t in self.teams]

    def _battle_state(self, seat: int, sid: str, req: dict, st: dict, version: int, rev: Reveals, plog: list):
        from agent.pokemon import dex
        osid = "p2" if sid == "p1" else "p1"
        me, opp = st[sid], st[osid]
        side = req["side"]["pokemon"]
        force = bool((req.get("forceSwitch") or [False])[0])
        exact = self.fmt == "same"
        active = own_view(next((p for p in me["active"] if p), None))
        foe_raw = next((p for p in opp["active"] if p), None)
        foe = foe_view(foe_raw, rev.side[osid], exact) if foe_raw else None
        la, choices, moves, switches = [], {}, [], []
        if not force and req.get("active"):
            a0 = req["active"][0]
            for k, m in enumerate(a0.get("moves", [])):
                if m.get("disabled"):
                    continue
                md = dex.move(m["id"])
                acc = md.get("accuracy", 100)
                acc = 1.0 if acc is True else round(float(acc) / 100, 2)
                mv = {"id": m["id"], "type": str(md.get("type", "")).upper(), "category": str(md.get("category", "")).upper(),
                      "base_power": md.get("basePower", 0), "accuracy": acc, "priority": md.get("priority", 0),
                      "current_pp": m.get("pp"), "max_pp": m.get("maxpp"),
                      "target": _upper_snake(m.get("target") or md.get("target"))}
                i = len(moves)
                moves.append(mv)
                aid = f"move:{i}"
                la.append(self._act(aid, {"type": "move", "slot": i, "move_id": m["id"], "base_power": mv["base_power"],
                                          "category": mv["category"], "move_type": mv["type"],
                                          "current_pp": mv["current_pp"], "accuracy": acc}, f"Use {m['id']}", version))
                choices[aid] = f"move {k + 1}"
        trapped = bool(req.get("active") and req["active"][0].get("trapped")) and not force
        if not trapped:
            for j, p in enumerate(side):
                if p.get("active") or "fnt" in p.get("condition", ""):
                    continue
                name = p["details"].split(",")[0]
                full = me["team"][j] if j < len(me["team"]) else None      # same side.pokemon order as the request
                if not full or _sid(full["species"]) != _sid(name):
                    full = next((q for q in me["team"] if q and _sid(q["species"]) == _sid(name)), None)
                view = own_view(full) or {"species": _sid(name), "name": name}
                i = len(switches)
                switches.append(view)
                aid = f"switch:{i}"
                la.append(self._act(aid, {"type": "switch", "slot": i, "species": view["species"]},
                                    f"Switch to {view['species']}", version))
                choices[aid] = f"switch {j + 1}"
        if not la:                                   # nothing legal (should not happen): let Showdown decide
            la.append(self._act("move:0", {"type": "move", "slot": 0}, "default", version))
            choices["move:0"] = "default"
        team = {f"{sid}: {p.get('name') or p['species']}": own_view(p) for p in me["team"] if p}
        opp_team = {f"{osid}: {p.get('name') or p['species']}": foe_view(p, rev.side[osid], exact) for p in opp["team"]
                    if p and ((p.get("name") or p["species"]) in rev.side[osid] or p["active"])}
        obs = {"battle_tag": "battle-local", "turn": st["turn"], "format": self.cfg["fmt"], "finished": False,
               "won": None, "lost": None,
               "weather": {st["weather"].upper(): 0} if st.get("weather") else {},
               "fields": {**{_upper_snake(x): 0 for x in st.get("pseudo_weather", [])},
                          **({_upper_snake(st["terrain"]): 0} if st.get("terrain") else {})},
               "side_conditions": _conditions(me), "opponent_side_conditions": _conditions(opp),
               "team": team, "opponent_team": opp_team, "is_doubles": False, "force_switch": force, "can_tera": False,
               "active_pokemon": active,
               "opponent_active_pokemon": foe, "available_moves": moves, "available_switches": switches,
               "protocol_log": list(plog), "waiting_for_action": True}
        raw = self._state(seat, "moving", obs, la, version)
        return raw, choices

    @staticmethod
    def _to_choice(out, raw: dict, choices: dict) -> str | None:
        la = raw["legal_actions"]["actions"]
        aid = None
        if isinstance(out, LegalAction):
            aid = out.action_id
        elif isinstance(out, str):
            aid = out
        elif isinstance(out, int) and not isinstance(out, bool):
            aid = la[out]["action_id"] if 0 <= out < len(la) else None
        elif isinstance(out, dict):
            t = out.get("type")
            for a in la:
                act = a["input"]["action"]
                if act.get("type") != t:
                    continue
                if "slot" in out and out.get("slot") == act.get("slot"):
                    aid = a["action_id"]
                    break
                if "slot" not in out and t == "move" and _sid(out.get("move_id", "")) == _sid(act.get("move_id", "")):
                    aid = a["action_id"]
                    break
                if "slot" not in out and t == "switch" and _sid(out.get("species", "")) == _sid(act.get("species", "")):
                    aid = a["action_id"]
                    break
        return choices.get(aid) if aid else None


def play_singles(sd: Showdown, fmt: str, a: Player, b: Player, seed: int, opp_team: str = "platform") -> SinglesResult:
    rng = random.Random(seed)
    random.seed(seed)
    m = SinglesMatch(sd, fmt, [a, b], rng, opp_team=opp_team)
    m.setup_teams()
    return m.battle()
