"""Offline HoK CONTRACT DIAGNOSTIC, not Tencent's engine or a game replica.

Evidence, checked 2026-10-16 (no credentials, no live games):
* https://api.altruagent-game.com/skill/hok returned HTTP 200.
* upstream/main at 686e7560708eaa595a409b330d0d1837d49ba9d5:
  GAMES.md, examples/llm/hok.py, tests/test_hok.py.
The accessible starter contains a client, example and fake payloads, NOT an
accessible local game engine. The public guide describes the API, not combat.

Confirmed contract: five seats per camp, 0.2-second steps, per-seat views,
pending seats, persistent/re-aimed macros, raw actions clear standing orders,
realtime WAIT/stale-version tolerance, lockstep version checks and timeout
fallback, crystal victory, then structure damage / kills at either limit.

EVERY numerical combat/map/visibility constant below is an independent,
uncalibrated diagnostic assumption. No candidate policy is imported here.
These parameters were fixed before running policy comparisons. Results can
test API use and sensitivity, not substantiate live strength or promotion.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

MODEL_VERSION = "hok-contract-diagnostic-v1"
OFFICIAL_REFERENCE = "https://api.altruagent-game.com/skill/hok"
UPSTREAM_REFERENCE = "686e7560708eaa595a409b330d0d1837d49ba9d5"
GAME_TYPE = "honor_of_kings_5v5"
MACROS = ("macro_advance", "macro_retreat", "macro_attack",
          "macro_skill1", "macro_skill2", "macro_skill3")
ASSUMPTIONS = (
    "Not a Tencent engine replica; all combat numbers and geometry are uncalibrated.",
    "One open corridor, one tower and one crystal per camp; five identical heroes.",
    "No minions, jungle, terrain, items, economy, leveling, mana, buffs or hero-specific skills.",
    "Per-hero circular vision; hidden enemies expose identity/camp only; structures are public.",
    "Nearest visible enemy hero or structure is the attack/skill target; no manual target selection.",
    "Skills are single-target damage with independent cooldowns; unusable standing orders do nothing.",
    "Advance only moves unless advance_attacks is enabled; no pathfinding or collision.",
    "All movement precedes simultaneous damage; towers target the nearest living enemy hero.",
    "Base healing, full-HP respawn, kill credit, death-order clearing and crystal shielding are assumptions.",
    "Raw controls implement only button_0 and discrete button_2 movement, not the full engine action space.",
    "Lockstep timeout is explicitly invoked after a camp finishes, not a wall-clock server timer.",
    "Simulated time limits use elapsed steps; no network, queue, start failure or live engine timing.",
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class SimConfig:
    # Step interval and default max_steps are from the official guide. Others
    # (including the default time limit) are diagnostic, not HoK measurements.
    mode: str = "realtime"
    step_s: float = 0.2
    max_steps: int = 3000
    time_limit_s: float = 600.0
    action_timeout_s: float = 5.0
    half_length: float = 30000.0
    tower_fraction: float = 0.45
    spawn_spread: float = 800.0
    spawn_jitter: float = 100.0
    hero_hp: float = 4000.0
    speed: float = 4500.0
    vision_range: float = 15000.0
    attack_range: float = 6500.0
    attack_damage: float = 180.0
    attack_cooldown_s: float = 0.8
    skill_ranges: tuple[float, ...] = (6000.0, 7500.0, 8000.0)
    skill_damage: tuple[float, ...] = (260.0, 340.0, 650.0)
    skill_cooldowns_s: tuple[float, ...] = (3.0, 5.0, 12.0)
    tower_hp: float = 8000.0
    crystal_hp: float = 10000.0
    tower_range: float = 7500.0
    tower_damage: float = 360.0
    tower_cooldown_s: float = 1.0
    base_heal_radius: float = 3000.0
    base_heal_fraction_per_s: float = 0.20
    respawn_s: float = 8.0
    crystal_shielded: bool = True
    advance_attacks: bool = False
    clear_orders_on_death: bool = True
    raw_move: bool = True

    def __post_init__(self):
        if self.mode not in ("realtime", "lockstep"):
            raise ValueError("mode must be realtime or lockstep")
        if type(self.max_steps) is not int or self.max_steps <= 0:
            raise ValueError("max_steps must be a positive integer")
        for name, value in asdict(self).items():
            if name in ("mode", "max_steps"):
                continue
            if name in ("crystal_shielded", "advance_attacks", "clear_orders_on_death", "raw_move"):
                if type(value) is not bool:
                    raise ValueError(f"{name} must be boolean")
                continue
            values = value if isinstance(value, (tuple, list)) else (value,)
            if isinstance(value, (tuple, list)) and len(value) != 3:
                raise ValueError(f"{name} must contain three numbers")
            if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
                raise ValueError(f"{name} must be finite and nonnegative")
        for name in ("step_s", "half_length", "hero_hp", "tower_hp", "crystal_hp",
                     "time_limit_s", "attack_cooldown_s", "tower_cooldown_s", "respawn_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 < self.tower_fraction < 1:
            raise ValueError("tower_fraction must lie between zero and one")
        # JSON config arrays become immutable too.
        for name in ("skill_ranges", "skill_damage", "skill_cooldowns_s"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def config_hash(self) -> str:
        return digest(asdict(self))


# A priori sensitivity cases; not selected using candidate outcomes. They vary
# assumptions with strong gameplay consequences rather than tweaking policy thresholds.
PROFILES: dict[str, dict[str, Any]] = {
    "default": {},
    "short-vision": {"vision_range": 4500.0, "attack_range": 4200.0,
                     "skill_ranges": (3500.0, 4200.0, 4500.0)},
    "punishing-towers": {"tower_damage": 800.0, "tower_range": 11000.0,
                         "base_heal_fraction_per_s": 0.08, "respawn_s": 16.0},
    "durable": {"hero_hp": 8000.0, "tower_hp": 16000.0, "crystal_hp": 24000.0,
                "respawn_s": 4.0, "base_heal_fraction_per_s": 0.35},
    "advance-attacks": {"advance_attacks": True, "crystal_shielded": False,
                        "clear_orders_on_death": False},
}


def profile_config(name: str = "default", **overrides) -> SimConfig:
    return replace(SimConfig(**PROFILES[name]), **overrides)


@dataclass
class Hero:
    seat: int
    camp: int
    x: float
    z: float
    hp: float
    max_hp: float
    kills: int = 0
    deaths: int = 0
    revive_at: float = 0.0
    ready: dict[str, float] = field(default_factory=dict)


@dataclass
class Structure:
    runtime_id: int
    camp: int
    type: str
    x: float
    z: float
    hp: float
    max_hp: float
    ready_at: float = 0.0


def distance(a, b) -> float:
    return math.hypot(a.x - b.x, a.z - b.z)


class DiagnosticGame:
    """Pure deterministic environment; never calls a policy or logs decisions.

    ``view(camp)`` is the only policy-facing state. ``submit`` validates against
    *current* offers. ``tick`` advances realtime without actions; in lockstep it
    requires all ten answers, or explicit timeout after one camp is complete.
    """

    def __init__(self, config: SimConfig | None = None, seed: int = 0):
        self.config = config or SimConfig()
        self.seed = seed
        c = self.config
        rng = random.Random(seed)
        # Identical seeded role geometry, mirrored between camps, for side pairing.
        self.spawn_z = [(i - 2) * c.spawn_spread + rng.uniform(-c.spawn_jitter, c.spawn_jitter)
                        for i in range(5)]
        self.heroes: dict[int, Hero] = {}
        self.structures: dict[int, Structure] = {}
        for camp in (1, 2):
            sign = -1 if camp == 1 else 1
            for role in range(5):
                seat = (camp - 1) * 5 + role
                self.heroes[seat] = Hero(seat, camp, sign * c.half_length,
                                         -sign * self.spawn_z[role], c.hero_hp, c.hero_hp)
            for kind, hp, x in (("ACTOR_TOWER", c.tower_hp, c.half_length * c.tower_fraction),
                                ("ACTOR_CRYSTAL", c.crystal_hp, c.half_length)):
                rid = camp * 100 + (0 if kind == "ACTOR_TOWER" else 1)
                self.structures[rid] = Structure(rid, camp, kind, sign * x, 0.0, hp, hp)
        self.step = 0
        self.pending = set(range(10))
        self.standing: dict[int, str] = {}
        self.fresh: dict[int, dict] = {}
        self.last_fallback_players: list[int] = []
        self.structure_damage = {1: 0.0, 2: 0.0}
        self.kills = {1: 0, 2: 0}
        self.healed = {1: 0.0, 2: 0.0}
        self.respawns = {1: 0, 2: 0}
        self.terminal = False
        self.winner_camp: int | None = None
        self.termination_reason: str | None = None

    @property
    def now(self) -> float:
        return self.step * self.config.step_s

    @staticmethod
    def seats(camp: int) -> list[int]:
        if type(camp) is not int or camp not in (1, 2):
            raise ValueError("camp must be 1 or 2")
        return list(range((camp - 1) * 5, camp * 5))

    def _shielded(self, st: Structure) -> bool:
        return (self.config.crystal_shielded and st.type == "ACTOR_CRYSTAL"
                and any(s.camp == st.camp and s.type == "ACTOR_TOWER" and s.hp > 0
                        for s in self.structures.values()))

    def _targets(self, hero: Hero) -> list[Hero | Structure]:
        targets: list[Hero | Structure] = [
            h for h in self.heroes.values() if h.camp != hero.camp and h.hp > 0
            and distance(hero, h) <= self.config.vision_range
        ]
        targets.extend(s for s in self.structures.values()
                       if s.camp != hero.camp and s.hp > 0 and not self._shielded(s))
        # Role-local tie break preserves camp symmetry.
        return sorted(targets, key=lambda t: (distance(hero, t), isinstance(t, Structure),
                                              t.seat % 5 if isinstance(t, Hero) else t.runtime_id % 100))

    def _nearest(self, hero: Hero):
        return next(iter(self._targets(hero)), None)

    def _can_use(self, hero: Hero, macro: str) -> bool:
        if hero.hp <= 0:
            return False
        if macro == "macro_advance":
            return any(s.hp > 0 and s.camp != hero.camp for s in self.structures.values())
        if macro == "macro_retreat":
            return True
        if hero.ready.get(macro, 0.0) > self.now + 1e-9:
            return False
        target = self._nearest(hero)
        if target is None:
            return False
        attack_range = self.config.attack_range if macro == "macro_attack" else \
            self.config.skill_ranges[int(macro[-1]) - 1]
        return distance(hero, target) <= attack_range

    def _entries(self, seat: int) -> list[dict]:
        hero = self.heroes[seat]
        entries = [{"action_id": f"s{seat}:button_0", "label": f"[seat {seat}] None",
                    "input": {"action": {"which_button": 0, "seat": seat}}}]
        if hero.hp > 0 and self.config.raw_move:
            entries.append({"action_id": f"s{seat}:button_2", "label": f"[seat {seat}] Move",
                            "input": {"action": {"which_button": 2, "seat": seat,
                                                "legal_move_x": [0, 8, 15], "legal_move_z": [0, 8, 15]}}})
        for macro in MACROS:
            if not self._can_use(hero, macro):
                continue
            label = {"macro_advance": "Advance to nearest enemy structure",
                     "macro_retreat": "Retreat home", "macro_attack": "Basic attack"}.get(macro, macro)
            if macro not in ("macro_advance", "macro_retreat"):
                target = self._nearest(hero)
                kind = "enemy hero" if isinstance(target, Hero) else target.type
                rid = target.seat if isinstance(target, Hero) else target.runtime_id
                label += f" (nearest: {kind} {rid}, {distance(hero, target):.0f} away)"
            entries.append({"action_id": f"s{seat}:{macro}", "label": f"[seat {seat}] {label}",
                            "input": {"action": {"macro": macro, "seat": seat}}})
        return entries

    def _hero_view(self, hero: Hero) -> dict:
        return {"runtime_id": hero.seat, "hero_id": 100 + hero.seat % 5, "camp": hero.camp,
                "hp": hero.hp, "max_hp": hero.max_hp, "x": hero.x, "z": hero.z, "level": 1,
                "kills": hero.kills, "deaths": hero.deaths, "money": 0,
                "revive_time": max(0.0, hero.revive_at - self.now) if hero.hp <= 0 else 0.0}

    def view(self, camp: int) -> dict:
        seats = self.seats(camp)
        pending = [s for s in seats if s in self.pending] if not self.terminal else []
        summaries = {}
        structures = [{"runtime_id": s.runtime_id, "camp": s.camp, "type": s.type,
                       "hp": s.hp, "max_hp": s.max_hp, "x": s.x, "z": s.z}
                      for s in self.structures.values()]
        for seat in seats:
            me = self.heroes[seat]
            others = []
            for h in self.heroes.values():
                if h.seat == seat:
                    continue
                visible = h.camp == camp or (me.hp > 0 and distance(me, h) <= self.config.vision_range)
                other = self._hero_view(h) if visible else {
                    "runtime_id": h.seat, "hero_id": 100 + h.seat % 5, "camp": h.camp}
                others.append({**other, "visible": visible})
            summaries[str(seat)] = {"summary": {"my_hero": self._hero_view(me),
                                                "other_heroes": others,
                                                "structures": [dict(s) for s in structures],
                                                "frame_no": self.step * 3}}
        return {
            "session_id": f"diagnostic-{self.seed}", "game_type": GAME_TYPE,
            "status": "finished" if self.terminal else "in_progress",
            "phase": "finished" if self.terminal else "moving",
            "is_terminal": self.terminal, "is_current_actor": bool(pending),
            "state_version": self.step, "camp": camp, "seats": seats,
            "max_steps": self.config.max_steps, "time_limit_s": self.config.time_limit_s,
            "seconds_left": max(0.0, self.config.time_limit_s - self.now),
            "action_timeout_s": self.config.action_timeout_s,
            "pacing": {"mode": self.config.mode, "step_interval_s": self.config.step_s},
            "simultaneous": {"your_pending_seats": pending,
                             "standing_orders": {str(s): self.standing[s] for s in seats if s in self.standing},
                             "waiting_for": sorted(self.pending) if not self.terminal else [],
                             "last_fallback_players": list(self.last_fallback_players)},
            "observation": {"seats": summaries},
            "legal_actions": {"actions": [e for s in pending for e in self._entries(s)]},
        }

    def _decode(self, action: Any) -> dict | None:
        # LegalAction is accepted without importing or modifying the SDK.
        if not isinstance(action, (str, dict)) and hasattr(action, "action_id"):
            action = action.action_id
        if isinstance(action, str):
            match = re.fullmatch(r"s(\d+):(macro_\w+|button_0)", action)
            if not match:
                return None
            seat, name = int(match[1]), match[2]
            return {"seat": seat, **({"which_button": 0} if name == "button_0" else {"macro": name})}
        return dict(action) if isinstance(action, dict) else None

    def submit(self, camp: int, action: Any, state_version: int | None = None) -> dict:
        seats = self.seats(camp)
        def refused(error="INVALID_ACTION"):
            return {"error": error, "state_version": self.step}
        if self.terminal:
            return refused()
        if state_version is not None and (type(state_version) is not int or state_version < 0
                                          or state_version > self.step):
            return refused("STALE_STATE")
        if self.config.mode == "lockstep" and state_version != self.step:
            return refused("STALE_STATE")
        act = self._decode(action)
        if act is None:
            return refused()
        seat = act.get("seat")
        if type(seat) is not int or seat not in seats or seat not in self.pending:
            return refused()
        macro = act.get("macro")
        if macro is not None:
            if set(act) != {"seat", "macro"} or macro not in MACROS or not self._can_use(self.heroes[seat], macro):
                return refused()
        else:
            button = act.get("which_button")
            if type(button) is not int:
                return refused()
            if button == 0:
                if set(act) != {"seat", "which_button"}:
                    return refused()
            elif button == 2:
                if (not self.config.raw_move or self.heroes[seat].hp <= 0
                        or set(act) != {"seat", "which_button", "move_x", "move_z"}
                        or any(type(act.get(k)) is not int or act[k] not in (0, 8, 15)
                               for k in ("move_x", "move_z"))):
                    return refused()
            else:
                return refused()
        self.pending.remove(seat)
        self.fresh[seat] = act
        if macro is not None:
            self.standing[seat] = macro
        else:
            self.standing.pop(seat, None)
        return {"status": "in_progress", "state_version": self.step, "stepped": False}

    def _move(self, hero: Hero, x: float, z: float):
        d = math.hypot(x - hero.x, z - hero.z)
        scale = min(1.0, self.config.speed * self.config.step_s / d) if d else 0.0
        hero.x += (x - hero.x) * scale
        hero.z += (z - hero.z) * scale

    def tick(self, *, timeout: bool = False):
        if self.terminal:
            raise RuntimeError("cannot step a terminal game")
        if self.config.mode == "lockstep" and self.pending:
            one_camp_complete = any(not (self.pending & set(self.seats(camp))) for camp in (1, 2))
            if not timeout or not one_camp_complete:
                raise RuntimeError("lockstep requires every seat, or timeout after one camp finishes")
        c = self.config
        self.last_fallback_players = sorted(self.pending)
        for h in self.heroes.values():
            if h.hp <= 0 and h.revive_at <= self.now + 1e-9:
                h.hp = h.max_hp
                sign = -1 if h.camp == 1 else 1
                h.x, h.z = sign * c.half_length, -sign * self.spawn_z[h.seat % 5]
                h.ready.clear()
                self.respawns[h.camp] += 1
        for h in self.heroes.values():
            if h.hp <= 0:
                continue
            fresh = self.fresh.get(h.seat, {})
            macro = self.standing.get(h.seat)
            if fresh.get("which_button") == 2:
                # 8 is neutral; 0/15 are negative/positive. Single-step raw action.
                dx = (fresh["move_x"] > 8) - (fresh["move_x"] < 8)
                dz = (fresh["move_z"] > 8) - (fresh["move_z"] < 8)
                self._move(h, h.x + dx * c.half_length, h.z + dz * c.half_length)
            elif macro == "macro_retreat":
                self._move(h, -c.half_length if h.camp == 1 else c.half_length, 0.0)
            elif macro == "macro_advance":
                targets = [s for s in self.structures.values() if s.camp != h.camp and s.hp > 0]
                if targets:
                    target = min(targets, key=lambda s: (distance(h, s), s.runtime_id % 100))
                    self._move(h, target.x, target.z)
        # Damage is collected before anyone dies: no first-camp attack advantage.
        hits: dict[tuple[str, int], list[tuple[int, int | None, float]]] = {}
        for h in self.heroes.values():
            macro = self.standing.get(h.seat)
            if macro == "macro_advance" and c.advance_attacks:
                macro = "macro_attack"
            if macro not in ("macro_attack", "macro_skill1", "macro_skill2", "macro_skill3") \
                    or not self._can_use(h, macro):
                continue
            target = self._nearest(h)
            if macro == "macro_attack":
                amount, cooldown = c.attack_damage, c.attack_cooldown_s
            else:
                i = int(macro[-1]) - 1
                amount, cooldown = c.skill_damage[i], c.skill_cooldowns_s[i]
            h.ready[macro] = self.now + cooldown
            key = ("hero", target.seat) if isinstance(target, Hero) else ("structure", target.runtime_id)
            hits.setdefault(key, []).append((h.camp, h.seat, amount))
        for tower in self.structures.values():
            if tower.type != "ACTOR_TOWER" or tower.hp <= 0 or tower.ready_at > self.now + 1e-9:
                continue
            targets = [h for h in self.heroes.values()
                       if h.camp != tower.camp and h.hp > 0 and distance(tower, h) <= c.tower_range]
            if targets:
                target = min(targets, key=lambda h: (distance(tower, h), h.seat % 5))
                hits.setdefault(("hero", target.seat), []).append((tower.camp, None, c.tower_damage))
                tower.ready_at = self.now + c.tower_cooldown_s
        for (kind, rid), sources in hits.items():
            target = self.heroes[rid] if kind == "hero" else self.structures[rid]
            amount = min(target.hp, sum(s[2] for s in sources))
            target.hp = max(0.0, target.hp - amount)
            if kind == "structure":
                self.structure_damage[3 - target.camp] += amount
            elif target.hp <= 0:
                target.deaths += 1
                target.revive_at = self.now + c.step_s + c.respawn_s
                if c.clear_orders_on_death:
                    self.standing.pop(target.seat, None)
                hero_sources = [s for s in sources if s[1] is not None and s[2] > 0]
                if hero_sources:
                    killer = max(hero_sources, key=lambda s: (s[2], -(s[1] % 5)))
                    self.heroes[killer[1]].kills += 1
                    self.kills[killer[0]] += 1
        for h in self.heroes.values():
            base_x = -c.half_length if h.camp == 1 else c.half_length
            if h.hp > 0 and math.hypot(h.x - base_x, h.z) <= c.base_heal_radius:
                healed = min(h.max_hp - h.hp, h.max_hp * c.base_heal_fraction_per_s * c.step_s)
                h.hp += healed
                self.healed[h.camp] += healed
        self.step += 1
        self.fresh.clear()
        self.pending = set(range(10))
        destroyed = {s.camp for s in self.structures.values() if s.type == "ACTOR_CRYSTAL" and s.hp <= 0}
        if destroyed:
            self.finish(3 - next(iter(destroyed)) if len(destroyed) == 1 else None, "crystal_destroyed")
        elif self.step >= c.max_steps or self.now + 1e-9 >= c.time_limit_s:
            scores = {camp: (round(self.structure_damage[camp], 8), self.kills[camp]) for camp in (1, 2)}
            winner = None if scores[1] == scores[2] else max(scores, key=scores.get)
            self.finish(winner, "step_limit" if self.step >= c.max_steps else "time_limit")

    def finish(self, winner: int | None, reason: str):
        self.terminal, self.winner_camp, self.termination_reason = True, winner, reason

    def result(self) -> dict:
        if not self.terminal:
            raise RuntimeError("game is not terminal")
        return {
            "termination_reason": self.termination_reason,
            "outcome": {"winner_camp": self.winner_camp,
                        "score": {str(c): {"enemy_structure_damage": self.structure_damage[c],
                                           "kills": self.kills[c]} for c in (1, 2)}},
            "returns": {str(c): 0 if self.winner_camp is None else (1 if self.winner_camp == c else -1)
                        for c in (1, 2)},
            "platform_scores": {str(c): 0.5 if self.winner_camp is None else float(self.winner_camp == c)
                                for c in (1, 2)},
        }

    def state_hash(self) -> str:
        """Full-state digest for reproducibility; never returns raw decisions."""
        return digest({"config": self.config.config_hash, "seed": self.seed, "step": self.step,
                       "heroes": [asdict(h) for h in self.heroes.values()],
                       "structures": [asdict(s) for s in self.structures.values()],
                       "standing": self.standing, "fresh": self.fresh, "pending": sorted(self.pending),
                       "damage": self.structure_damage, "kills": self.kills, "healed": self.healed,
                       "respawns": self.respawns, "fallback": self.last_fallback_players,
                       "terminal": self.terminal, "winner": self.winner_camp, "reason": self.termination_reason})
