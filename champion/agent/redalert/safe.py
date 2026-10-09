"""Safe mode: the minimal policy the bot falls back to for 60 s after repeated exceptions (docs/redalert_design.md
11.6). Deploy, place what is ready, power when short, a refinery when there is none, the main tank when the Vehicle
queue is empty, and legal_actions.attack_now every 10 s once the army is worth more than 3000."""
from __future__ import annotations

from .orders import Cand

TANKS = ("3tnk", "2tnk", "1tnk")


def decide(v, pol) -> list[Cand]:
    out: list[Cand] = []
    led = pol.ledger
    if v.fact is None:
        mcv = next((u for u in v.units if u["type"] == "mcv"), None)
        if mcv is not None and not led.is_pending(("deploy",)):
            out.append(Cand({"cmd": "deploy", "units": [mcv["id"]]}, key=("deploy",), urgent=True))
    for _q, item in v.ready:
        if not led.is_pending(("place", item)):
            out.append(Cand({"cmd": "place", "item": item}, key=("place", item), urgent=True))
    if v.fact is not None and not v.queue_items("Building"):
        item = None
        if v.surplus < 20 and "powr" in v.avail:
            item = "powr"
        elif v.have.get("proc", 0) == 0 and "proc" in v.avail:
            item = "proc"
        if item and not led.is_pending(("build", item)):
            out.append(Cand({"cmd": "build", "item": item}, key=("build", item), urgent=True,
                            base=v.in_prod.get(item, 0)))
    if v.have.get("weap") and not v.queue_items("Vehicle"):
        tank = next((t for t in TANKS if t in v.avail), None)
        if tank and v.money >= 700 and not led.is_pending(("train", tank)):
            out.append(Cand({"cmd": "train", "item": tank, "count": 1}, key=("train", tank), urgent=True,
                            base=v.in_prod.get(tank, 0)))
    an = v.la.get("attack_now")
    if isinstance(an, dict) and v.army_value > 3000 and v.tick - pol.safe_attack_tick >= 250:
        order = {k: an[k] for k in ("cmd", "units", "to") if k in an}
        if order.get("cmd") == "attack_move" and isinstance(order.get("units"), list):
            pol.safe_attack_tick = v.tick
            out.append(Cand(dict(order, units=list(order["units"])[:50]), urgent=True))
    return out
