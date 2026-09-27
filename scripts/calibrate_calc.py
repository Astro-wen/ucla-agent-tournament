"""Check the damage calculator against real platform battles.

For every non-crit hit in the recorded protocol logs (logs/*/*.jsonl), rebuild the attacker and defender from
the drafted cards (both sides' full sets are known after the draft), replay stat boosts / terrain / weather /
screens / Helping Hand from the log, ask @smogon/calc for the damage range and compare it with the HP the
defender actually lost. Our HP is exact; foe HP is out of 100, so foe hits are compared in percent (±1).

    python scripts/calibrate_calc.py            # summary + every mismatch
"""
from __future__ import annotations

import glob
import json
import re
from collections import defaultdict

from agent.pokemon import dex
from agent.pokemon.calc import DamageCalc, _terrain_name, _weather_name
from agent.pokemon.dex import to_id

BOOSTABLE = ("atk", "def", "spa", "spd", "spe")


def cards_for(path: str) -> tuple[dict, dict, str | None]:
    """species id -> card, for each side (p1/p2), from the draft records of one session file."""
    seen: dict[str, dict] = {c["card_id"]: c for c in json.load(open("data/vgc_cards.json"))}
    rosters: dict[str, list] = {}
    me = None
    for line in open(path):
        raw = json.loads(line)["raw"]
        obs = raw.get("observation")
        if raw.get("phase") == "draft" and isinstance(obs, dict):
            for c in obs.get("available_cards", []):
                seen[c["card_id"]] = c
            rosters = obs.get("rosters") or rosters
            me = (raw.get("current_actor") or {}).get("agent_id") or me
    by_side = {"me": {}, "opp": {}}
    for seat, ro in rosters.items():
        for r in ro:
            c = seen.get(r["card_id"])
            if c:
                by_side["me" if seat == me else "opp"][to_id(c["species"])] = c
    return by_side["me"], by_side["opp"], me


def final_log(path: str) -> tuple[list[str], str | None]:
    """Longest protocol log in the file and which side (p1/p2) we are."""
    best, side = [], None
    for line in open(path):
        obs = json.loads(line)["raw"].get("observation")
        if isinstance(obs, dict) and isinstance(obs.get("protocol_log"), list):
            if len(obs["protocol_log"]) > len(best):
                best = obs["protocol_log"]
            for k in (obs.get("team") or {}):
                side = k.split(":")[0]
                break
    return best, side


def mon_input(card: dict, boosts: dict, hp_frac: float) -> dict:
    out = {"species": dex.species(card["species"]).get("name") or card["species"], "level": card.get("level", 50),
           "item": dex.item_name(card["item"]), "ability": dex.ability_name(card["ability"]),
           "nature": card.get("nature", "Hardy"), "evs": card.get("evs") or {}, "ivs": card.get("ivs") or {},
           "hpFraction": hp_frac}
    b = {k: v for k, v in boosts.items() if v}
    if b:
        out["boosts"] = b
    return out


def analyse(path: str, calc: DamageCalc) -> list[dict]:
    mine, theirs, _ = cards_for(path)
    log, my_side = final_log(path)
    if not log or not my_side or not mine:
        return []
    active: dict[str, str] = {}                      # "p1a" -> species id
    hp: dict[str, tuple[int, int]] = {}              # species id+side -> (cur, max) as shown
    boosts: dict[str, dict] = defaultdict(lambda: dict.fromkeys(BOOSTABLE, 0))
    field = {"weather": None, "terrain": None, "screens": defaultdict(set)}
    items_gone: set[str] = set()                     # consumed / knocked-off items
    pending = None                                   # the current |move| line context
    helping: set[str] = set()
    out = []

    def key(pos: str) -> str:
        return pos[:2] + ":" + active.get(pos[:3], "?")

    for line in log:
        parts = line.split("|")
        if len(parts) < 2:
            continue
        tag = parts[1]
        if tag in ("switch", "drag"):
            pos = parts[2].split(":")[0]
            sp = to_id(parts[3].split(",")[0])
            active[pos] = sp
            cur, mx = map(int, re.match(r"(\d+)/(\d+)", parts[4]).groups())
            hp[pos[:2] + ":" + sp] = (cur, mx)
            boosts.pop(pos[:2] + ":" + sp, None)
        elif tag == "turn":
            helping.clear()
            pending = None
        elif tag in ("-boost", "-unboost"):
            k = key(parts[2].split(":")[0])
            stat, n = parts[3], int(parts[4])
            if stat in BOOSTABLE:
                boosts[k][stat] = max(-6, min(6, boosts[k][stat] + (n if tag == "-boost" else -n)))
        elif tag == "-weather":
            field["weather"] = None if parts[2] == "none" else parts[2]
        elif tag == "-fieldstart":
            field["terrain"] = parts[2].replace("move: ", "")
        elif tag == "-fieldend":
            field["terrain"] = None
        elif tag == "-sidestart":
            field["screens"][parts[2][:2]].add(to_id(parts[3].replace("move: ", "")))
        elif tag == "-sideend":
            field["screens"][parts[2][:2]].discard(to_id(parts[3].replace("move: ", "")))
        elif tag in ("-enditem",):
            items_gone.add(key(parts[2].split(":")[0]))
        elif tag == "-singleturn" and "Helping Hand" in line:
            helping.add(parts[2].split(":")[0][:3])
        elif tag == "move":
            src = parts[2].split(":")[0][:3]
            pending = {"src": src, "move": parts[3], "spread": "[spread]" in line, "crit": set()}
        elif tag == "-crit":
            if pending:
                pending["crit"].add(parts[2].split(":")[0][:3])
        elif tag in ("-damage", "-heal", "-sethp") and len(parts) >= 4:
            tgt = parts[2].split(":")[0][:3]
            m = re.match(r"(\d+)(?:/(\d+))?", parts[3])
            new = int(m.group(1)) if m else 0
            tk = key(tgt)
            before, mx = hp.get(tk, (None, None))
            if before is not None:
                hp[tk] = (new, mx)                    # every HP change (recoil, heals, items) keeps tracking exact
            if tag != "-damage" or not pending or "[from]" in line:
                continue                              # only direct move damage is a calibration event
            src = pending["src"]
            sk = key(src)
            mv = dex.move(pending["move"])
            multi = mv.get("multihit")
            if (before is None or tgt in pending["crit"] or multi or src[:2] == tgt[:2]
                    or not mv.get("basePower")):
                continue
            a_side = "me" if src[:2] == my_side else "opp"
            atk_card = (mine if a_side == "me" else theirs).get(active.get(src))
            def_card = (theirs if a_side == "me" else mine).get(active.get(tgt))
            if not atk_card or not def_card:
                continue
            a_cur, a_max = hp.get(sk, (1, 1))
            atk = mon_input(atk_card, boosts[sk], a_cur / a_max if a_max else 1)
            dfn = mon_input(def_card, boosts[tk], before / mx if mx else 1)
            if sk in items_gone:
                atk.pop("item", None)
            if tk in items_gone:
                dfn.pop("item", None)
            f = {"gameType": "Doubles" if pending["spread"] else "Singles",
                 "weather": _weather_name(field["weather"]), "terrain": _terrain_name(field["terrain"]),
                 "attackerSide": {"isHelpingHand": src in helping},
                 "defenderSide": {"isReflect": "reflect" in field["screens"][tgt[:2]],
                                  "isLightScreen": "lightscreen" in field["screens"][tgt[:2]],
                                  "isAuroraVeil": "auroraveil" in field["screens"][tgt[:2]]}}
            out.append({"file": path.split("/")[-1][:8], "move": pending["move"], "atk": atk_card["species"],
                        "def": def_card["species"], "lost": before - new, "mx": mx, "calc_in": (atk, dfn, f),
                        "foe_scale": tgt[:2] != my_side, "ko": new == 0})
    res = calc.batch([{"attacker": a, "defender": d, "move": o["move"], "field": f}
                      for o in out for a, d, f in [o["calc_in"]]]) or []
    for o, r in zip(out, res):
        o["pred"] = r
    return out


def main() -> None:
    calc = DamageCalc.get()
    hits = []
    for path in sorted(glob.glob("logs/yoriko/*.jsonl")):
        hits += analyse(path, calc)
    ok = bad = skipped = 0
    for h in hits:
        r = h["pred"]
        if not r:
            skipped += 1
            continue
        lo, hi, dhp = r["min"], r["max"], r["defender_hp"]
        if h["foe_scale"]:                                    # percent scale, ±1 rounding
            lo_p, hi_p = 100 * lo / dhp - 1, 100 * hi / dhp + 1
            good = lo_p <= h["lost"] <= hi_p or (h["ko"] and hi_p >= h["lost"])
            shown = f"lost {h['lost']}%  pred {100 * lo / dhp:.0f}-{100 * hi / dhp:.0f}%"
        else:
            good = lo <= h["lost"] <= hi or (h["ko"] and hi >= h["lost"])
            shown = f"lost {h['lost']}/{h['mx']}  pred {lo}-{hi} (calc max HP {dhp})"
        if good:
            ok += 1
        else:
            bad += 1
            print(f"MISMATCH {h['file']} {h['atk']:>20s} {h['move']:<16s} -> {h['def']:<20s} {shown}")
    print(f"\n{len(hits)} non-crit single-hit damage events: {ok} inside the calc range, {bad} outside, {skipped} not computable")


if __name__ == "__main__":
    main()
