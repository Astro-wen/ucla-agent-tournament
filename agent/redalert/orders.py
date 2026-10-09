"""Order building and local validation (docs/platform/redalert.md section 5, get_game_config order_schema): every
order that leaves the bot has only known fields, ids that are ours right now, cells inside map.bounds, items that are
available, and the batch respects 20 orders and (with our margin) 180 engine commands. An order that fails is dropped
here, so a batch can never be refused as a whole by our own mistake."""
from __future__ import annotations

FIELDS = {
    "move": ({"units", "to"}, {"queued"}), "attack_move": ({"units", "to"}, {"queued"}),
    "attack": ({"units"}, {"target", "to", "queued"}), "stop": ({"units"}, set()), "deploy": ({"units"}, set()),
    "unload": ({"units"}, set()), "guard": ({"units", "target"}, {"queued"}),
    "enter_transport": ({"units", "target"}, {"queued"}), "harvest": ({"units"}, {"to", "queued"}),
    "set_stance": ({"units", "stance"}, set()), "build": ({"item"}, set()), "train": ({"item"}, {"count"}),
    "place": ({"item"}, {"at"}), "cancel": ({"item"}, set()), "sell": ({"building"}, set()),
    "repair": ({"building"}, {"on"}), "power_toggle": ({"building"}, {"on"}), "set_primary": ({"building"}, set()),
    "rally": ({"building", "to"}, set()),
}
UNIT_CMDS = frozenset({"move", "attack_move", "attack", "stop", "deploy", "unload", "guard", "enter_transport",
                       "harvest", "set_stance"})
TAKES_BUILDINGS = frozenset({"attack", "stop", "set_stance", "guard"})
STANCES = ("hold_fire", "return_fire", "defend", "attack_anything")
MAX_ORDERS = 20
MAX_UNITS = 50
MAX_TRAIN = 10
COMMAND_CAP = 180                    # our margin under the platform's 200 engine commands per batch


class Cand:
    """A candidate order plus its bookkeeping: `key` (the ledger's pending entry), `sig` (per-unit command
    signature), `base` (the item's production count when sent), `urgent` (skips the cadence throttle)."""
    __slots__ = ("order", "key", "urgent", "sig", "base", "tag")

    def __init__(self, order: dict, key=None, urgent: bool = False, sig=None, base: int = 0, tag: str = ""):
        self.order, self.key, self.urgent, self.sig, self.base, self.tag = order, key, urgent, sig, base, tag


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def commands(order: dict) -> int:
    if order.get("cmd") in UNIT_CMDS:
        return len(order.get("units") or [])
    if order.get("cmd") == "train":
        return int(order.get("count", 1))
    return 1


def check(order, view) -> str | None:
    """Why the platform would refuse this order (a short reason), or None when it is fine. May prune dead / foreign
    ids out of a unit order in place (the platform would only skip them)."""
    if not isinstance(order, dict):
        return "not an object"
    cmd = order.get("cmd")
    if cmd not in FIELDS:
        return "unknown cmd"
    req, opt = FIELDS[cmd]
    keys = set(order) - {"cmd"}
    if keys - req - opt:
        return "unknown field"
    if req - keys:
        return "missing field"
    if "queued" in order and not isinstance(order["queued"], bool):
        return "queued not bool"
    st = view.st
    for f in ("to", "at"):
        if f in order:
            c = order[f]
            if not (isinstance(c, list) and len(c) == 2 and all(_int(v) for v in c)):
                return f"{f} not a cell"
            if not st.in_bounds(c):
                return f"{f} outside bounds"
    if cmd in UNIT_CMDS:
        units = order.get("units")
        if not isinstance(units, list) or not all(_int(u) for u in units):
            return "units not ids"
        ok_ids = set(view.unit_by_id)
        if cmd in TAKES_BUILDINGS:
            ok_ids |= set(view.bld_by_id)
        kept = []
        for u in units:
            if u in ok_ids and u not in kept:
                kept.append(u)
        if cmd == "deploy":
            kept = [u for u in kept if view.unit_by_id.get(u, {}).get("type") == "mcv"]
        elif cmd == "harvest":
            kept = [u for u in kept if view.unit_by_id.get(u, {}).get("type") == "harv"]
        if not kept:
            return "no unit left"
        order["units"] = kept[:MAX_UNITS]
        if cmd == "attack":
            if ("target" in order) == ("to" in order):
                return "attack needs one of target / to"
            if "target" in order and (not _int(order["target"]) or order["target"] not in view.enemy_ids):
                return "target not visible"
        if cmd in ("guard", "enter_transport"):
            t = order.get("target")
            if not _int(t) or (t not in view.unit_by_id and t not in view.bld_by_id):
                return "target not ours"
        if cmd == "set_stance" and order.get("stance") not in STANCES:
            return "bad stance"
        return None
    if cmd in ("build", "train", "place", "cancel"):
        item = order.get("item")
        if not isinstance(item, str) or not item:
            return "no item"
        item = item.lower()
        rules = st.rules
        if cmd in ("build", "train"):
            if item not in view.avail:
                return "not available"
            if (cmd == "build") != rules.is_building(item):
                return "build / train mismatch"
            if cmd == "train" and "count" in order and not (_int(order["count"]) and 1 <= order["count"] <= MAX_TRAIN):
                return "bad count"
            return None
        if cmd == "place":
            if not any(i == item for _q, i in view.ready):
                return "not ready"
            return None
        if view.in_prod.get(item, 0) <= 0:
            return "nothing to cancel"
        return None
    b = order.get("building")
    if not _int(b) or b not in view.bld_by_id:
        return "not our building"
    if cmd in ("repair", "power_toggle") and "on" in order and not isinstance(order["on"], bool):
        return "on not bool"
    if cmd in ("set_primary", "rally") and view.bld_by_id[b]["type"] not in PRODUCTION_TYPES:
        return "not a production building"
    return None


PRODUCTION_TYPES = frozenset({"tent", "barr", "weap", "kenn", "syrd", "spen", "hpad", "afld", "afld.ukraine", "fact"})


def finalize(cands: list[Cand], view, ledger, *, urgent_only: bool = False) -> tuple[list[dict], list[Cand], int]:
    """The batch to send: candidates in priority order that pass `check`, are not blacklisted and not duplicates,
    within MAX_ORDERS and COMMAND_CAP. Returns (orders, the candidates kept, how many were dropped)."""
    out: list[dict] = []
    kept: list[Cand] = []
    dropped = 0
    total = 0
    seen: set = set()
    claimed: set = set()                     # a unit takes one order per batch (the first, highest priority)
    for c in cands:
        if urgent_only and not c.urgent:
            continue
        o = c.order
        if check(o, view) is not None:
            dropped += 1
            continue
        if o.get("cmd") in UNIT_CMDS and o.get("cmd") != "set_stance":
            o["units"] = [u for u in o["units"] if u not in claimed]
            if not o["units"]:
                dropped += 1
                continue
        if ledger.blocked(o, view):
            dropped += 1
            continue
        k = (o.get("cmd"), o.get("item"), o.get("building")) if o.get("cmd") not in UNIT_CMDS else None
        if k is not None and k in seen:
            dropped += 1
            continue
        n = commands(o)
        if len(out) >= MAX_ORDERS:
            break
        if total + n > COMMAND_CAP:
            if o.get("cmd") in UNIT_CMDS and COMMAND_CAP - total >= 1:
                o["units"] = o["units"][:COMMAND_CAP - total]
                n = len(o["units"])
            else:
                continue
        if k is not None:
            seen.add(k)
        if o.get("cmd") in UNIT_CMDS and o.get("cmd") != "set_stance":
            claimed.update(o["units"])
        total += n
        out.append(o)
        kept.append(c)
    return out, kept, dropped


def fragile(order: dict) -> bool:
    """An order the platform may refuse because the world moved between our view and its check (a target out of
    sight, a building no longer ready, one or two units that just died)."""
    cmd = order.get("cmd")
    if cmd in ("attack", "place", "cancel", "guard", "enter_transport", "repair", "rally", "set_primary",
               "power_toggle", "sell"):
        return True                                  # (a building order: the building may have just been destroyed)
    return cmd in UNIT_CMDS and len(order.get("units") or []) <= 2


def anchor(view) -> dict | None:
    """An order that is valid on any live state and costs no engine command: power on a building that is not powered
    down (`ok already_on`). Sent along with a batch of fragile orders so the batch is never refused as a whole."""
    blds = [b for b in view.buildings if not b.get("powered_down")]
    if not blds:
        return None
    b = max(blds, key=lambda b: (b["type"] == "fact", b["hp"], -b["id"]))
    return {"cmd": "power_toggle", "building": b["id"], "on": True}


def noop(view) -> dict | None:
    """A batch member that is always valid and changes nothing (windows mode needs one valid order per window):
    re-set a unit's current stance, else a power-on of a building that is not powered down (`ok already_on`)."""
    for u in view.units:
        if u.get("stance") in STANCES:
            return {"cmd": "set_stance", "units": [u["id"]], "stance": u["stance"]}
    for b in view.buildings:
        if not b.get("powered_down"):
            return {"cmd": "power_toggle", "building": b["id"], "on": True}
    for u in view.units:
        if u["type"] not in ("harv", "mcv"):
            return {"cmd": "stop", "units": [u["id"]]}
    if view.units:
        return {"cmd": "set_stance", "units": [view.units[0]["id"]], "stance": "defend"}
    return None
