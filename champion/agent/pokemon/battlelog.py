"""What the Showdown protocol log tells about the battle beyond the per-turn observation (engine search input).

The platform forwards the whole battle protocol in `observation.protocol_log` (our player's view: exact HP for our
side, x/100 for the foe; docs/real_payloads.md). The observation itself carries HP, status, boosts and which
conditions are up, but not how many turns they have left, nor the per-Pokemon volatile state that decides what
happens next turn: a Protect used last turn (the next one succeeds 1/3 of the time), whether Fake Out still works
(first action since entering), a Choice lock, Taunt / Encore / Yawn / Leech Seed / confusion, a consumed item (Focus
Sash, Sitrus Berry, Booster Energy, Air Balloon), Protosynthesis / Quark Drive's boosted stat, Supreme Overlord's
count, sleep and toxic counters, Wish. `parse(lines)` reads all of that from the log; it never raises (a malformed
line is skipped). Only public information is used: exactly what a player sees in the battle log.

Counting time: every end-of-turn residual phase prints `|upkeep`, so `upkeeps` counts residual phases. A condition
set with duration D (Tailwind 4, Trick Room / screens / weather / terrain 5, 8 with the extending items) has
D - (upkeeps since it was set) turns left, which is exactly the engine's `duration` field at that point.
"""
from __future__ import annotations

from .dex import to_id

STALL_MOVES = {"protect", "detect", "spikyshield", "banefulbunker", "silktrap", "burningbulwark", "kingsshield",
               "obstruct", "wideguard", "quickguard", "endure", "maxguard"}
# -start / -end effect names -> engine volatile ids (anything else is ignored)
VOLATILES = {"substitute": "substitute", "confusion": "confusion", "taunt": "taunt", "encore": "encore",
             "disable": "disable", "leechseed": "leechseed", "yawn": "yawn", "healblock": "healblock",
             "saltcure": "saltcure", "focusenergy": "focusenergy", "torment": "torment", "throatchop": "throatchop",
             "charge": "charge", "magnetrise": "magnetrise", "aquaring": "aquaring", "ingrain": "ingrain",
             "perish3": "perishsong", "perish2": "perishsong", "perish1": "perishsong", "perish0": "perishsong",
             "flashfire": "flashfire", "curse": "curse", "tarshot": "tarshot"}
WEATHERS = {"raindance": "raindance", "rain": "raindance", "sunnyday": "sunnyday", "sun": "sunnyday",
            "sandstorm": "sandstorm", "sand": "sandstorm", "snowscape": "snowscape", "snow": "snowscape",
            "hail": "snowscape", "desolateland": "desolateland", "primordialsea": "primordialsea",
            "deltastream": "deltastream"}
TERRAINS = {"electricterrain", "grassyterrain", "psychicterrain", "mistyterrain"}
ITEM_TAKERS = {"trick", "switcheroo", "thief", "covet", "bestow", "magician", "pickpocket"}


def _pos(tok: str) -> tuple[str, str] | None:
    """'p2a: Flutter Mane' -> ('p2', 'p2a'); 'p1: Name' (a side) -> ('p1', 'p1'). None when not a position."""
    tok = (tok or "").strip()
    if len(tok) < 2 or tok[0] != "p" or not tok[1].isdigit():
        return None
    head = tok.split(":", 1)[0].strip()
    return head[:2], head


def _effect(tok: str) -> str:
    """'move: Tailwind' / 'ability: Grassy Surge' / 'item: Sitrus Berry' / 'Reflect' -> its id."""
    tok = (tok or "").strip()
    if ":" in tok and tok.split(":", 1)[0].strip() in ("move", "ability", "item"):
        tok = tok.split(":", 1)[1]
    return to_id(tok)


def _kw(parts: list[str], key: str) -> str | None:
    """The value of a '[from] ...' / '[of] ...' keyword argument, else None."""
    for p in parts:
        if p.startswith(key):
            return p[len(key):].strip()
    return None


class Mon:
    """One Pokemon's log-derived state; `species` is the id from its details ('Iron Hands, L50' -> ironhands)."""
    __slots__ = ("side", "species", "pos", "switch_up", "switch_turn", "actions", "last_move", "last_move_up",
                 "moves", "item_lost", "item", "status", "slp_cants", "tox_ticks", "volatiles", "stall", "stall_up",
                 "fallen", "ability", "fainted", "moved_up", "hp_frac", "glaive_up")

    def __init__(self, side: str, species: str):
        self.side, self.species = side, species
        self.pos: str | None = None
        self.switch_up = 0          # upkeeps at its latest switch-in
        self.switch_turn = 0
        self.actions = 0            # move attempts (|move| or |cant|) since the latest switch-in (Fake Out)
        self.last_move: str | None = None   # its own last move since the switch-in (Choice lock, Encore)
        self.last_move_up = -1
        self.moves: list[str] = []  # every move it was seen using
        self.item_lost = False      # |-enditem|: consumed, popped, knocked off, stolen
        self.item: str | None = None   # an item it was seen to hold / gain (|-item|)
        self.status: str | None = None
        self.slp_cants = 0          # |cant|..|slp| since it fell asleep
        self.tox_ticks = 0          # toxic damage ticks since max(switch-in, poisoning)
        self.volatiles: dict[str, dict] = {}   # engine volatile id -> {"up": upkeeps at start, ...}
        self.stall = 0              # consecutive successful stall moves ...
        self.stall_up = -9          # ... the last one before upkeep number stall_up + 1
        self.fallen = 0             # Supreme Overlord count
        self.ability: str | None = None        # revealed ability
        self.fainted = False
        self.moved_up = -9          # upkeeps at its latest move attempt (= in the turn before upkeep moved_up + 1)
        self.hp_frac: float | None = None      # last HP seen in the log (fraction)
        self.glaive_up = -9         # upkeeps when it last used Glaive Rush


class Log:
    """The parsed log. `mons[(side, species)]`; `active[position]` = (side, species) now there; conditions with the
    upkeep count at their start; `revealed[side]` = species in order of first appearance."""

    def __init__(self):
        self.turn = 0
        self.upkeeps = 0
        self.after_upkeep = False   # the latest |upkeep| came after the latest |turn| (an end-of-turn switch)
        self.mons: dict[tuple[str, str], Mon] = {}
        self.active: dict[str, tuple[str, str]] = {}
        self.revealed: dict[str, list[str]] = {"p1": [], "p2": []}
        self.weather: dict | None = None      # {"id", "up", "setter": (side, species) | None}
        self.terrain: dict | None = None
        self.pseudo: dict[str, dict] = {}     # trickroom / gravity ... -> {"up", "setter"}
        self.sides: dict[str, dict[str, dict]] = {"p1": {}, "p2": {}}   # side -> condition -> {"up", "setter", "layers"}
        self.wish: dict[str, dict] = {}       # position -> {"up", "wisher": (side, species)}
        self.names: dict[tuple[str, str], str] = {}   # (side, nickname) -> species id
        self.lines = 0
        self.last_mover: dict[str, tuple[str, str]] = {}   # side -> the Pokemon that last used a move
        self.pending_glaive: Mon | None = None             # a Glaive Rush user until the move is seen to hit
        self.errors = 0

    def mon(self, side: str, species: str) -> Mon:
        k = (side, species)
        m = self.mons.get(k)
        if m is None:
            m = self.mons[k] = Mon(side, species)
        return m

    def at(self, tok: str) -> Mon | None:
        """The Pokemon a 'p1a: Name' token refers to (by nickname; else whoever is at that position now)."""
        p = _pos(tok)
        if p is None:
            return None
        side, head = p
        name = tok.split(":", 1)[1].strip() if ":" in tok else ""
        sp = self.names.get((side, name)) if name else None
        if sp is not None:
            return self.mons.get((side, sp))
        if len(head) == 3 and head in self.active:
            return self.mons.get(self.active[head])
        return None

    # ---- line handlers --------------------------------------------------------------------------------------------
    def feed(self, line) -> None:
        if not isinstance(line, str) or not line.startswith("|"):
            return
        self.lines += 1
        parts = line.split("|")
        tag = parts[1] if len(parts) > 1 else ""
        if not tag:
            return
        h = getattr(self, "_h_" + tag.lstrip("-").replace("-", "_"), None)
        if h is None:
            return
        try:
            h(parts[2:])
        except Exception:                       # one odd line never costs the rest of the log
            self.errors += 1

    def _h_turn(self, a):
        try:
            self.turn = int(a[0])
        except (ValueError, IndexError):
            pass
        self.after_upkeep = False

    def _h_upkeep(self, a):
        self.upkeeps += 1
        self.after_upkeep = True
        self.pending_glaive = None

    def _h_switch(self, a):
        p = _pos(a[0]) if a else None
        if p is None or len(p[1]) != 3:
            return
        side, head = p
        species = to_id((a[1] if len(a) > 1 else "").split(",")[0])
        if not species:
            return
        name = a[0].split(":", 1)[1].strip() if ":" in a[0] else species
        self.names[(side, name)] = species
        old = self.active.get(head)
        if old is not None and old in self.mons and self.mons[old].pos == head:
            self.mons[old].pos = None
        m = self.mon(side, species)
        for other_head, k in list(self.active.items()):          # it left its old position (Ally Switch aside)
            if k == (side, species) and other_head != head:
                del self.active[other_head]
        m.pos = head
        m.switch_up, m.switch_turn = self.upkeeps, self.turn
        m.actions = 0
        m.last_move, m.last_move_up = None, -1
        m.volatiles = {}
        m.stall, m.stall_up = 0, -9
        m.fallen = 0
        m.tox_ticks = 0
        m.glaive_up = -9
        m.fainted = False
        self.active[head] = (side, species)
        if species not in self.revealed.setdefault(side, []):
            self.revealed[side].append(species)
        self._hp(m, a[2] if len(a) > 2 else "")

    _h_drag = _h_switch
    _h_replace = _h_switch

    def _h_detailschange(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        species = to_id((a[1] if len(a) > 1 else "").split(",")[0])
        if species and species != m.species:          # a forme change keeps the tracker under the new id
            k_old = (m.side, m.species)
            m.species = species
            self.mons[(m.side, species)] = m
            self.mons.pop(k_old, None)
            for key, sp in list(self.names.items()):
                if key[0] == m.side and sp == k_old[1]:
                    self.names[key] = species
            if m.pos:
                self.active[m.pos] = (m.side, species)

    def _hp(self, m: Mon, tok: str) -> None:
        bits = (tok or "").split()
        if not bits:
            return
        if bits[0] == "0" or "fnt" in bits:
            m.hp_frac = 0.0
            return
        if "/" in bits[0]:
            try:
                cur, mx = bits[0].split("/")
                m.hp_frac = max(0.0, min(1.0, int(cur) / max(1, int(mx))))
            except ValueError:
                pass
        st = bits[1] if len(bits) > 1 else ""
        if st in ("par", "brn", "slp", "frz", "psn", "tox") and m.status != st:
            m.status = st

    def _h_move(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        mid = to_id(a[1] if len(a) > 1 else "")
        rest = a[2:]
        if any(x.startswith("[from]") and "lockedmove" not in x for x in rest):
            return                                     # called by another move (Sleep Talk, Dancer...): no action
        m.actions += 1
        m.moved_up = self.upkeeps
        m.last_move, m.last_move_up = mid, self.upkeeps
        if mid and mid not in m.moves:
            m.moves.append(mid)
        m.glaive_up = -9
        self.pending_glaive = m if mid == "glaiverush" else None   # its drawback starts only if it hits
        self.last_mover[m.side] = (m.side, m.species)
        if mid == "wish" and m.pos:
            self.wish[m.pos] = {"up": self.upkeeps, "wisher": (m.side, m.species)}

    def _h_cant(self, a):
        self.pending_glaive = None
        m = self.at(a[0]) if a else None
        if m is None:
            return
        m.actions += 1
        m.moved_up = self.upkeeps
        if to_id(a[1] if len(a) > 1 else "") == "slp":
            m.slp_cants += 1

    def _h_singleturn(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        if _effect(a[1] if len(a) > 1 else "") in STALL_MOVES:
            if m.stall_up == self.upkeeps:
                return                                 # already counted this turn
            m.stall = m.stall + 1 if m.stall and m.stall_up == self.upkeeps - 1 else 1
            m.stall_up = self.upkeeps

    def _h_fail(self, a):
        m = self.at(a[0]) if a else None
        if m is not None and m.last_move in STALL_MOVES and m.last_move_up == self.upkeeps \
                and m.stall_up != self.upkeeps:
            m.stall, m.stall_up = 0, -9               # the Protect failed: the stall counter is gone

    def _h_faint(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        m.fainted = True
        m.hp_frac = 0.0
        m.volatiles = {}

    def _h_damage(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        self._hp(m, a[1] if len(a) > 1 else "")
        frm = _kw(a[2:], "[from]")
        g = self.pending_glaive
        if g is not None and frm is None and g is not m:
            g.glaive_up = self.upkeeps                # Glaive Rush hit: its user takes double damage until it moves
            self.pending_glaive = None
        if frm is not None and to_id(frm) == "psn" and m.status == "tox":
            m.tox_ticks += 1

    def _h_heal(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        self._hp(m, a[1] if len(a) > 1 else "")
        frm = _kw(a[2:], "[from]")
        if frm and _effect(frm) == "wish" and m.pos:
            self.wish.pop(m.pos, None)

    _h_sethp = _h_heal

    def _h_status(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        m.status = to_id(a[1] if len(a) > 1 else "") or None
        m.slp_cants = 0
        m.tox_ticks = 0

    def _h_curestatus(self, a):
        m = self.at(a[0]) if a else None
        if m is not None:
            m.status = None
            m.slp_cants = 0
            m.tox_ticks = 0

    def _h_cureteam(self, a):
        p = _pos(a[0]) if a else None
        if p is None:
            return
        for (side, _sp), m in self.mons.items():
            if side == p[0]:
                m.status = None

    def _h_enditem(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        m.item_lost = True
        m.item = None

    def _h_item(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        item = to_id(a[1] if len(a) > 1 else "")
        if item:
            m.item = item                      # Frisk-style reveal or a new item (Trick, Thief...)
            frm = _kw(a[2:], "[from]")
            if frm and _effect(frm) in ITEM_TAKERS:
                m.item_lost = False

    def _h_ability(self, a):
        m = self.at(a[0]) if a else None
        if m is not None and len(a) > 1 and to_id(a[1]):
            m.ability = to_id(a[1])

    def _h_start(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        eff = _effect(a[1] if len(a) > 1 else "")
        for base in ("protosynthesis", "quarkdrive"):
            if eff.startswith(base):
                m.volatiles[base] = {"up": self.upkeeps, "stat": eff[len(base):] or None,
                                     "booster": any("fromitem" in x for x in a[2:])
                                     or (m.item_lost and m.item is None)}
                return
        if eff.startswith("fallen"):
            digits = "".join(ch for ch in eff if ch.isdigit())
            m.fallen = min(5, int(digits or 0))
            return
        vid = VOLATILES.get(eff)
        if vid is None:
            return
        info = {"up": self.upkeeps, "moved": m.moved_up == self.upkeeps}   # target already acted this turn
        if vid == "encore":
            info["move"] = m.last_move
        elif vid == "disable":
            info["move"] = to_id(a[2]) if len(a) > 2 else None
        elif vid == "leechseed":
            src = self.last_mover.get("p1" if m.side == "p2" else "p2")
            src_mon = self.mons.get(src) if src else None
            info["source_pos"] = src_mon.pos if src_mon is not None else None
        elif vid == "perishsong":
            info["left"] = int("".join(ch for ch in eff if ch.isdigit()) or 3)
        elif vid == "confusion":
            info["turns"] = 0
        m.volatiles[vid] = info

    def _h_end(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        eff = _effect(a[1] if len(a) > 1 else "")
        if eff in ("protosynthesis", "quarkdrive"):
            m.volatiles.pop(eff, None)
            return
        vid = VOLATILES.get(eff)
        if vid is not None:
            m.volatiles.pop(vid, None)

    def _h_activate(self, a):
        m = self.at(a[0]) if a else None
        if m is None:
            return
        if _effect(a[1] if len(a) > 1 else "") == "confusion" and "confusion" in m.volatiles:
            m.volatiles["confusion"]["turns"] = m.volatiles["confusion"].get("turns", 0) + 1

    def _h_weather(self, a):
        w = to_id(a[0] if a else "")
        if not w or w == "none":
            self.weather = None
            return
        if any(x.strip() == "[upkeep]" for x in a[1:]):
            return
        of = _kw(a[1:], "[of]")
        mm = self.at(of) if of else None
        self.weather = {"id": WEATHERS.get(w, w), "up": self.upkeeps,
                        "setter": (mm.side, mm.species) if mm is not None else None}

    def _h_fieldstart(self, a):
        eff = _effect(a[0] if a else "")
        of = _kw(a[1:], "[of]")
        mm = self.at(of) if of else None
        setter = (mm.side, mm.species) if mm is not None else None
        if eff in TERRAINS:
            self.terrain = {"id": eff, "up": self.upkeeps, "setter": setter}
        elif eff:
            self.pseudo[eff] = {"up": self.upkeeps, "setter": setter}

    def _h_fieldend(self, a):
        eff = _effect(a[0] if a else "")
        if eff in TERRAINS:
            self.terrain = None
        else:
            self.pseudo.pop(eff, None)

    def _h_sidestart(self, a):
        p = _pos(a[0]) if a else None
        if p is None:
            return
        side, eff = p[0], _effect(a[1] if len(a) > 1 else "")
        if not eff:
            return
        cur = self.sides[side].get(eff)
        if cur is not None and eff in ("spikes", "toxicspikes"):
            cur["layers"] = cur.get("layers", 1) + 1
            return
        self.sides[side][eff] = {"up": self.upkeeps, "setter": self.last_mover.get(side), "layers": 1}

    def _h_sideend(self, a):
        p = _pos(a[0]) if a else None
        if p is not None:
            self.sides[p[0]].pop(_effect(a[1] if len(a) > 1 else ""), None)

    def _h_swapsideconditions(self, a):
        self.sides["p1"], self.sides["p2"] = self.sides["p2"], self.sides["p1"]


def parse(lines) -> Log:
    """Parse a protocol log (a list of lines). Never raises."""
    log = Log()
    if isinstance(lines, (list, tuple)):
        for line in lines:
            log.feed(line)
    return log


def my_side(obs: dict) -> str | None:
    """'p1' / 'p2' from the observation's `team` keys ("p2: Incineroar"), else None."""
    team = obs.get("team")
    if isinstance(team, dict):
        for k in team:
            s = str(k).split(":", 1)[0].strip()
            if s in ("p1", "p2"):
                return s
    return None
