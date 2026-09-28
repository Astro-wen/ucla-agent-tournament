"""Parse a Werewolf get_game_state payload into a structured view (docs/platform/werewolf.md §2–3)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ROLE_RE = re.compile(r"Your role:\s*(WOLF|SEER|VILLAGER)", re.I)
ALLY_RE = re.compile(r"Your fellow wolves:\s*(.+)")
INVEST_RE = re.compile(r"Player(\d+) is (wolf|villager)", re.I)
SEAT_RE = re.compile(r"Player(\d+)")
ACCUSE_RE = re.compile(r"Player(\d+)\s+(?:is|=|was)\s+(?:a\s+|the\s+)?(?:wolf|werewolf)", re.I)
SEER_CLAIM_RE = re.compile(r"\b(i am|i'm|im)\s+(the\s+)?seer\b", re.I)


@dataclass
class View:
    me: int
    role: str                                   # wolf | seer | villager
    day: int | None
    sub_phase: str | None                       # night_wolf | night_seer | day_vote
    alive: list[int]
    dead: dict[int, str]                        # seat -> revealed role
    dead_cause: dict[int, str]                  # seat -> night_kill | lynch
    allies: list[int] = field(default_factory=list)
    investigations: dict[int, str] = field(default_factory=dict)
    votes: list[tuple[int, dict[int, int | None], int | None]] = field(default_factory=list)
    # (day, {voter_seat: target_seat or None=abstain}, eliminated_seat)
    messages: list[dict] = field(default_factory=list)

    @property
    def others_alive(self) -> list[int]:
        return [s for s in self.alive if s != self.me]

    @property
    def wolves_left(self) -> int:
        return 2 - sum(1 for r in self.dead.values() if r == "wolf")


def parse(st: dict, messages: list[dict] | None = None) -> View:
    obs = st.get("observation") or ""
    if not isinstance(obs, str):
        obs = str(obs)
    players = st.get("players") or []
    name2seat = {p["name"]: p["position"] for p in players}
    gs = st.get("game_state") or {}

    m = ROLE_RE.search(obs)
    role = m.group(1).lower() if m else "villager"

    allies: list[int] = []
    m = ALLY_RE.search(obs)
    if m:
        allies = [int(x) for x in SEAT_RE.findall(m.group(1))]

    invest: dict[int, str] = {}
    m = re.search(r"Your investigations:\s*(.+)", obs)
    if m:
        invest = {int(s): r.lower() for s, r in INVEST_RE.findall(m.group(1))}

    def seat(name: str | None) -> int | None:
        if name is None or name == "abstain":
            return None
        if name in name2seat:
            return name2seat[name]
        m = SEAT_RE.fullmatch(name)
        return int(m.group(1)) if m else None

    alive = sorted(s for s in (seat(n) for n in gs.get("alive") or []) if s is not None)
    dead, cause = {}, {}
    for d in gs.get("dead") or []:
        s = seat(d.get("player"))
        if s is not None:
            dead[s] = d.get("role", "?")
            cause[s] = d.get("cause", "?")

    votes = []
    for vh in gs.get("vote_history") or []:
        vv = {}
        for voter, tgt in (vh.get("votes") or {}).items():
            vs = seat(voter)
            if vs is not None:
                vv[vs] = seat(tgt)
        votes.append((vh.get("day"), vv, seat(vh.get("eliminated"))))

    return View(
        me=st.get("your_position", -1), role=role, day=gs.get("day"), sub_phase=gs.get("phase"),
        alive=alive, dead=dead, dead_cause=cause, allies=allies, investigations=invest,
        votes=votes, messages=messages if messages is not None else list(st.get("new_messages") or []),
    )
