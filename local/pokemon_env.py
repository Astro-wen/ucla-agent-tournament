"""Local pokemon_vgc_doubles_draft environment.

Reproduces the platform flow — 18-card snake draft with Item Clause, team
preview (bring 4 / 2 leads), VGC doubles battle — on the real Showdown engine
(local/js/bridge.js, format gen9vgc2025regi + Terastal Clause). States and
action templates are built in the shape documented in docs/platform/pokemon.md
so the deployed `agent.agent.Contestant` runs here unchanged.

Anything the platform rejects (bad lineup, illegal doubles choice) is what the
platform's 300 s timer would do: a random legal choice, counted as `invalid`.
"""
from __future__ import annotations

import logging
import os
import random
import re
from dataclasses import dataclass, field

from altruagent import DecisionContext, GameState, LegalAction

from .pool import POOL, to_showdown_set
from .showdown import Showdown

os.environ["ARENA_RECORD"] = "0"      # never record simulated games (agent/agent.py record())
log = logging.getLogger("local.pokemon")
GAME = "pokemon_vgc_doubles_draft"
SNAKE = "ABBAABBAABBA"
NO_TARGET = {"self", "allySide", "foeSide", "all", "allAdjacent", "allAdjacentFoes", "allyTeam",
             "randomNormal", "scripted"}


# ---------------------------------------------------------------- players
class Player:
    """Anything with choose(raw_state) -> LegalAction | dict | action_id str."""
    name = "player"

    def choose(self, raw: dict):
        raise NotImplementedError


class ContestantPlayer(Player):
    """Runs our deployed agent (agent/agent.py) exactly as the official runner would."""

    def __init__(self, factory, name="ours"):
        self.factory, self.name = factory, name
        self.agent = None

    def reset(self):
        self.agent = self.factory()

    def choose(self, raw: dict):
        gs = GameState.from_mcp_state(raw)
        ctx = DecisionContext(session_id=raw["session_id"], tournament_id=None, game_type=GAME, agent_id="ours")
        return self.agent.choose_action(gs, ctx)


class OfficialBaseline(Player):
    """The starter's `return state.legal_actions[0]`.

    Draft: first offered card. Team preview / doubles turn: that LegalAction is a
    template the server rejects, the match process dies, and the platform plays
    randomly for the seat — so those phases are random here.
    """
    name = "official_baseline"

    def reset(self):
        pass

    def choose(self, raw: dict):
        if raw["phase"] == "draft":
            return raw["legal_actions"]["actions"][0]["action_id"]
        return random_action(raw)


class RandomPlayer(Player):
    name = "random"

    def reset(self):
        pass

    def choose(self, raw: dict):
        return random_action(raw)


def random_action(raw: dict):
    obs, la = raw["observation"], raw["legal_actions"]["actions"]
    if raw["phase"] == "draft":
        return random.choice(la)["action_id"]
    if raw["phase"] == "team_preview":
        ids = [p["species"] for p in obs["your_roster"]]
        bring = random.sample(ids, 4)
        return {"type": "select_lineup", "bring": bring, "leads": bring[:2]}
    tmpl = la[0]["input"]["action"]
    pick, used = {}, set()
    for slot in tmpl["slots"]:
        opts = [o for o in slot["options"] if not (o["type"] == "switch" and o["species"] in used)]
        o = random.choice(opts) if opts else {"type": "pass"}
        if o["type"] == "move":
            pick[f"slot_{slot['slot']}"] = {"type": "move", "move_id": o["move_id"], "target": random.choice(o["targets"])}
        elif o["type"] == "switch":
            pick[f"slot_{slot['slot']}"] = {"type": "switch", "species": o["species"]}
            used.add(o["species"])
        else:
            pick[f"slot_{slot['slot']}"] = {"type": "pass"}
    return {"type": "doubles_turn", **pick}


# ---------------------------------------------------------------- match
@dataclass
class MatchResult:
    winner: int | None            # 0 / 1 / None (tie)
    turns: int
    invalid: list[int] = field(default_factory=lambda: [0, 0])
    rosters: list[list[str]] = field(default_factory=list)
    brought: list[list[str]] = field(default_factory=list)


def _sid(x: str) -> str:
    return "".join(ch for ch in x.lower() if ch.isalnum())


class Match:
    def __init__(self, sd: Showdown, players: list[Player], rng: random.Random, pool: list[dict] | None = None):
        self.sd, self.players, self.rng = sd, players, rng
        self.pool = pool or rng.sample(POOL, min(18, len(POOL)))
        self.ids = ["agent-a", "agent-b"]
        self.version = 0
        self.invalid = [0, 0]
        self.picks: list[dict] = []
        self.rosters: list[list[dict]] = [[], []]
        for p in players:
            p.reset()

    # ---- envelope helpers -------------------------------------------
    def _state(self, seat: int, phase: str, obs: dict, actions: list[dict]) -> dict:
        return {"session_id": "local", "game_type": GAME, "runtime_adapter": "pokemon", "status": "in_progress",
                "state_version": self.version, "phase": phase, "observation": obs, "is_terminal": False,
                "is_current_actor": bool(actions), "current_actor": {"agent_id": self.ids[seat], "position": seat},
                "legal_actions": {"session_id": "local", "state_version": self.version, "actions": actions},
                "next_actions": [{"tool": "play_action"}]}

    def _act(self, la: dict, action: dict, label: str = "") -> dict:
        return {"action_id": la, "label": label,
                "input": {"session_id": "local", "action_id": la, "state_version": self.version, "action": action}}

    def _choose(self, seat: int, raw: dict):
        try:
            out = self.players[seat].choose(raw)
        except Exception:
            out = None
        if isinstance(out, LegalAction):
            out = out.input.get("action") or out.action_id
        if isinstance(out, str) and out.startswith("draft_pick:"):
            out = {"type": "draft_pick", "card_id": out.split(":", 1)[1]}
        return out

    # ---- draft ---------------------------------------------------------
    def draft(self) -> None:
        first = self.rng.randrange(2)
        taken: set[str] = set()
        for n, letter in enumerate(SNAKE, 1):
            seat = first if letter == "A" else 1 - first
            my_items = {c["item"] for c in self.rosters[seat]}
            offered = [c for c in self.pool if c["card_id"] not in taken and c["item"] not in my_items]
            obs = {"phase": "draft", "draft_complete": False, "battle_format": "gen9vgc2025regi",
                   "current_seat": self.ids[seat], "pick_number": n, "picks_remaining": 13 - n,
                   "first_drafter": self.ids[first],
                   "available_card_ids": [c["card_id"] for c in offered], "available_cards": offered,
                   "rosters": {self.ids[i]: [{"card_id": c["card_id"], "species": c["species"]} for c in self.rosters[i]]
                               for i in range(2)},
                   "picks": list(self.picks)}
            actions = [self._act(f"draft_pick:{c['card_id']}", {"type": "draft_pick", "card_id": c["card_id"]},
                                 f"Draft {c['species']}") for c in offered]
            out = self._choose(seat, self._state(seat, "draft", obs, actions))
            cid = (out or {}).get("card_id") if isinstance(out, dict) else None
            card = next((c for c in offered if c["card_id"] == cid), None)
            if card is None:
                self.invalid[seat] += 1
                card = self.rng.choice(offered)
            taken.add(card["card_id"])
            self.rosters[seat].append(card)
            self.picks.append({"pick_number": n, "seat_id": self.ids[seat], "card_id": card["card_id"],
                               "species": card["species"]})
            self.version += 1

    # ---- team preview ----------------------------------------------------
    def preview(self) -> list[str]:
        orders = []
        for seat in range(2):
            mine = [preview_view(c, hidden=False) for c in self.rosters[seat]]
            obs = {"phase": "team_preview", "battle_format": "gen9vgc2025regi", "waiting_for_action": True,
                   "your_roster": mine, "opponent_roster": [preview_view(c, hidden=True) for c in self.rosters[1 - seat]]}
            tmpl = {"type": "select_lineup", "roster": [p["species"] for p in mine],
                    "instructions": "bring 4 of your 6, leads = 2 of those 4"}
            out = self._choose(seat, self._state(seat, "team_preview", obs, [self._act("select_lineup", tmpl)]))
            ids = [p["species"] for p in mine]
            ok = (isinstance(out, dict) and out.get("type") == "select_lineup"
                  and len(set(map(_sid, out.get("bring", [])))) == 4
                  and all(_sid(b) in ids for b in out["bring"])
                  and len(out.get("leads", [])) == 2 and all(_sid(l) in map(_sid, out["bring"]) for l in out["leads"])
                  and _sid(out["leads"][0]) != _sid(out["leads"][1]))
            if not ok:
                self.invalid[seat] += 1
                out = random_action({"phase": "team_preview", "observation": obs, "legal_actions": {"actions": []}})
            leads = [_sid(x) for x in out["leads"]]
            rest = [_sid(x) for x in out["bring"] if _sid(x) not in leads]
            orders.append("team " + "".join(str(ids.index(s) + 1) for s in leads + rest))
            self.version += 1
        return orders

    # ---- battle ----------------------------------------------------------
    def battle(self, orders: list[str]) -> MatchResult:
        teams = [[to_showdown_set(c) for c in r] for r in self.rosters]
        snap = self.sd.start(teams[0], teams[1], seed=[self.rng.randrange(1, 65535) for _ in range(4)])
        snap = self.sd.choose(orders[0], orders[1])
        versions = [0, 0]
        turns = 0
        stuck = 0
        while not snap["ended"]:
            before = (snap["state"]["turn"], len(snap["log"]))
            pending = [sid for sid in ("p1", "p2") if snap["requests"].get(sid)]
            for sid in pending:
                seat = 0 if sid == "p1" else 1
                req = snap["requests"][sid]
                raw, tmpl = self._battle_state(seat, sid, req, snap["state"], versions[seat])
                out = self._choose(seat, raw)
                choice = _to_choice(out, req, tmpl)
                if choice is None:
                    self.invalid[seat] += 1
                    choice = _to_choice(random_action(raw), req, tmpl)
                versions[seat] += 1
                snap = self.sd.choose(**{sid: choice})
                for _retry in range(4):            # engine refused it: random, like the platform's 300 s timer
                    if "error" not in snap:
                        break
                    self.invalid[seat] += 1
                    snap = self.sd.choose(**{sid: _to_choice(random_action(raw), req, tmpl)})
                if "error" in snap:                # template bug on our side: let Showdown pick, but make it loud
                    log.warning("engine refused every choice for %s: %s", sid, snap.get("detail"))
                    snap = self.sd.choose(**{sid: "default"})
                if snap["ended"]:
                    break
            turns = snap["state"]["turn"]
            stuck = stuck + 1 if (turns, len(snap["log"])) == before and snap["log"] == [] else 0
            if turns > 200 or stuck > 20 or not pending:
                raise RuntimeError(f"battle stalled at turn {turns}: requests={list(snap['requests'])} "
                                   f"error={snap.get('detail')}")
        w = snap.get("winner")
        winner = 0 if w == "p1" else 1 if w == "p2" else None
        return MatchResult(winner, turns, list(self.invalid),
                           rosters=[[c["species"] for c in r] for r in self.rosters], brought=orders)

    def _battle_state(self, seat: int, sid: str, req: dict, st: dict, version: int):
        me, opp = st[sid], st["p2" if sid == "p1" else "p1"]
        my_side = req["side"]["pokemon"]
        force = req.get("forceSwitch") or [False, False]
        active_views = [_pv(p) for p in me["active"]]
        opp_views = [_foe_view(p) for p in opp["active"]]
        slots, moves_out, switches_out = [], [], []
        bench = [(j, p) for j, p in enumerate(my_side) if not p["active"] and "fnt" not in p["condition"]]
        n_slots = len(req["active"]) if req.get("active") else len(force)
        for i in range(n_slots):
            options, mv, sw = [], [], []
            if req.get("active"):
                pk = my_side[i]
                if "fnt" in pk["condition"]:
                    options.append({"type": "pass"})
                else:
                    for k, m in enumerate(req["active"][i]["moves"]):
                        if m.get("disabled"):
                            continue
                        # A locked move (charging, Choice-locked, Encore...) has no `target` key: no target may be chosen.
                        targets = _targets(m["target"], i, opp_views) if "target" in m else [0]
                        options.append({"type": "move", "move_id": m["id"], "targets": targets, "_k": k})
                        mv.append({"id": m["id"], "targets": targets, "current_pp": m.get("pp"), "max_pp": m.get("maxpp")})
                    for j, p in ([] if req["active"][i].get("trapped") else bench):
                        options.append({"type": "switch", "species": _sid(p["details"].split(",")[0]), "_j": j})
                        sw.append({"species": _sid(p["details"].split(",")[0]), "condition": p["condition"]})
            else:  # force switch request
                if force[i] and bench:
                    for j, p in bench:
                        options.append({"type": "switch", "species": _sid(p["details"].split(",")[0]), "_j": j})
                        sw.append({"species": _sid(p["details"].split(",")[0]), "condition": p["condition"]})
                    if sum(force) > len(bench):
                        options.append({"type": "pass"})
                else:
                    options.append({"type": "pass"})
            slots.append({"slot": i, "species": (active_views[i] or {}).get("species"), "options": options})
            moves_out.append(mv)
            switches_out.append(sw)
        tmpl = {"type": "doubles_turn", "slots": slots,
                "target_legend": {"1": "opponent A", "2": "opponent B", "-1": "your slot 0", "-2": "your slot 1", "0": "none"}}
        obs = {"phase": "moving", "is_doubles": True, "turn": st["turn"], "format": "gen9vgc2025regi",
               "weather": st["weather"], "fields": st["pseudo_weather"] + ([st["terrain"]] if st["terrain"] else []),
               "active_pokemon": active_views, "opponent_active_pokemon": opp_views,
               "team": {f"{sid}: {p['species']}": p for p in map(_pv, me["team"]) if p},
               "opponent_team": {f"opp: {p['species']}": p for p in map(_foe_view, opp["team"]) if p and p.get("revealed", True)},
               "available_moves": moves_out, "available_switches": switches_out,
               "force_switch": force if not req.get("active") else [False] * n_slots,
               "side_conditions": {c: 1 for c in me["side_conditions"]},
               "opponent_side_conditions": {c: 1 for c in opp["side_conditions"]}}
        public_tmpl = {"type": "doubles_turn", "target_legend": tmpl["target_legend"],
                       "slots": [{"slot": s["slot"], "species": s["species"],
                                  "options": [{k: v for k, v in o.items() if not k.startswith("_")} for o in s["options"]]}
                                 for s in slots]}
        raw = self._state(seat, "moving", obs, [self._act("doubles_turn", public_tmpl)])
        raw["state_version"] = version
        raw["legal_actions"]["state_version"] = version
        raw["legal_actions"]["actions"][0]["input"]["state_version"] = version
        return raw, tmpl


MASKED_FORMES = re.compile(r"^(Arceus|Gourgeist|Pumpkaboo|Xerneas|Silvally|Urshifu|Dudunsparce)(-.*)?$")
ZERO_BOOSTS = {"accuracy": 0, "atk": 0, "def": 0, "evasion": 0, "spa": 0, "spd": 0, "spe": 0}


def preview_view(c: dict, hidden: bool) -> dict:
    """A roster entry as the real platform shows it at team preview (docs/real_payloads.md, logs/*/): our own
    Pokemon in full (ids, not display names); the opponent's with item "unknown_item", ability None, no moves,
    no HP, and Showdown's masked formes ("Urshifu-*" -> species "urshifu" with the BASE forme's types/stats)."""
    from agent.pokemon import dex
    shown = c["species"]
    if hidden:
        m = MASKED_FORMES.match(shown)
        shown = m.group(1) if m else shown
    v = {"species": _sid(shown), "name": shown, "types": dex.types_of(shown), "base_stats": dex.base_stats(shown),
         "boosts": dict(ZERO_BOOSTS), "status": None, "fainted": False, "active": False, "revealed": False,
         "is_terastallized": False}
    if hidden:
        v.update({"item": "unknown_item", "ability": None, "moves": [], "current_hp": 0, "max_hp": 0,
                  "current_hp_fraction": 0})
    else:
        v.update({"item": _sid(c["item"]), "ability": _sid(c["ability"]), "moves": [_sid(m) for m in c["moves"]],
                  "current_hp_fraction": 1.0})
    return v


def _pv(p: dict | None) -> dict | None:
    if not p:
        return None
    return {"species": p["id"], "name": p["species"], "types": p["types"], "current_hp": p["current_hp"],
            "max_hp": p["max_hp"], "current_hp_fraction": p["current_hp_fraction"], "status": p["status"],
            "fainted": p["fainted"], "active": p["active"], "boosts": p["boosts"], "base_stats": p["base_stats"]}


def _foe_view(p: dict | None) -> dict | None:
    """Opponent Pokémon as the real platform shows it: HP out of 100 (docs/real_payloads.md), no item/ability/moves."""
    v = _pv(p)
    if v is None:
        return None
    pct = 0 if v["fainted"] else max(1, round(100 * v["current_hp_fraction"]))
    return {**v, "current_hp": pct, "max_hp": 100, "current_hp_fraction": pct / 100,
            "item": "unknown_item", "ability": None, "moves": []}


def _targets(kind: str, slot: int, opp_views: list) -> list[int]:
    other = -(2 - slot)          # the ally's slot as a negative target (-1 = slot 0, -2 = slot 1)
    if kind in NO_TARGET:
        return [0]
    foes = [t for t in (1, 2) if t - 1 < len(opp_views) and opp_views[t - 1] and not opp_views[t - 1]["fainted"]] or [1, 2]
    if kind in ("normal", "any"):
        return foes + [other]
    if kind == "adjacentFoe":
        return foes
    if kind == "adjacentAlly":
        return [other]
    if kind == "adjacentAllyOrSelf":
        return [-(slot + 1), other]
    return [0]


def _to_choice(out, req: dict, tmpl: dict) -> str | None:
    """doubles_turn dict -> Showdown choice string, or None if not legal per the template."""
    if not isinstance(out, dict) or out.get("type") != "doubles_turn":
        return None
    parts, switched = [], set()
    for slot in tmpl["slots"]:
        c = out.get(f"slot_{slot['slot']}")
        if not isinstance(c, dict):
            return None
        opts = slot["options"]
        if c.get("type") == "move":
            o = next((o for o in opts if o["type"] == "move" and o["move_id"] == _sid(str(c.get("move_id")))), None)
            if not o or c.get("target") not in o["targets"]:
                return None
            parts.append(f"move {o['_k'] + 1}" + (f" {c['target']}" if c["target"] != 0 else ""))
        elif c.get("type") == "switch":
            sp = _sid(str(c.get("species")))
            o = next((o for o in opts if o["type"] == "switch" and o["species"] == sp), None)
            if not o or sp in switched:
                return None
            switched.add(sp)
            parts.append(f"switch {o['_j'] + 1}")
        elif c.get("type") == "pass":
            if not any(o["type"] == "pass" for o in opts):
                return None
            parts.append("pass")
        else:
            return None
    return ", ".join(parts)


def play_match(sd: Showdown, a: Player, b: Player, seed: int) -> MatchResult:
    rng = random.Random(seed)
    random.seed(seed)
    m = Match(sd, [a, b], rng)
    m.draft()
    orders = m.preview()
    return m.battle(orders)
