"""Check the damage calculator against real platform battles.

For every non-crit hit in the recorded protocol logs (logs/*/*.jsonl), rebuild the attacker and defender from
the drafted cards (both sides' full sets are known after the draft), replay stat boosts / terrain / weather /
screens / Helping Hand from the log, ask @smogon/calc for the damage range and compare it with the HP the
defender actually lost. Our HP is exact; foe HP is out of 100, so foe hits are compared in percent (±1).

Multi-hit moves are checked separately: their hits are summed up to the |-hitcount| line and compared with the
calculator's per-outcome rolls for that hit count (what the policy scores with). Supreme Overlord's count comes
from |-start|<pos>|fallenN; a Focus Sash that breaks on a hit (|-enditem| precedes its |-damage|) still counts.

    python scripts/calibrate_calc.py              # summary + every mismatch
    python scripts/calibrate_calc.py --champion   # same events through champion/'s calculator (before/after)
"""
from __future__ import annotations

import glob
import json
import re
import sys
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


def mon_input(card: dict, boosts: dict, hp_frac: float, fallen: int = 0) -> dict:
    out = {"species": dex.species(card["species"]).get("name") or card["species"], "level": card.get("level", 50),
           "item": dex.item_name(card["item"]), "ability": dex.ability_name(card["ability"]),
           "nature": card.get("nature", "Hardy"), "evs": card.get("evs") or {}, "ivs": card.get("ivs") or {},
           "hpFraction": hp_frac}
    b = {k: v for k, v in boosts.items() if v}
    if b:
        out["boosts"] = b
    if fallen:
        out["alliesFainted"] = fallen                 # Supreme Overlord's count, from |-start|<pos>|fallenN
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
    fallen: dict[str, int] = {}                      # Supreme Overlord count locked in on entry
    pending = None                                   # the current |move| line context
    helping: set[str] = set()
    out = []

    def key(pos: str) -> str:
        return pos[:2] + ":" + active.get(pos[:3], "?")

    def event(src: str, tgt: str, before: int, new: int, mx: int, hits: int | None = None) -> None:
        sk, tk = key(src), key(tgt)
        a_side = "me" if src[:2] == my_side else "opp"
        atk_card = (mine if a_side == "me" else theirs).get(active.get(src))
        def_card = (theirs if a_side == "me" else mine).get(active.get(tgt))
        if not atk_card or not def_card:
            return
        a_cur, a_max = hp.get(sk, (1, 1))
        atk = mon_input(atk_card, boosts[sk], a_cur / a_max if a_max else 1, fallen.get(sk, 0))
        dfn = mon_input(def_card, boosts[tk], before / mx if mx else 1)
        if sk in items_gone:
            atk.pop("item", None)
        if tk in items_gone and tgt not in pending["sash"]:   # a Sash that broke on THIS hit still counted
            dfn.pop("item", None)
        f = {"gameType": "Doubles" if pending["spread"] else "Singles",
             "weather": _weather_name(field["weather"]), "terrain": _terrain_name(field["terrain"]),
             "attackerSide": {"isHelpingHand": src in helping},
             "defenderSide": {"isReflect": "reflect" in field["screens"][tgt[:2]],
                              "isLightScreen": "lightscreen" in field["screens"][tgt[:2]],
                              "isAuroraVeil": "auroraveil" in field["screens"][tgt[:2]]}}
        out.append({"file": path.split("/")[-1][:8], "move": pending["move"], "atk": atk_card["species"],
                    "def": def_card["species"], "lost": before - new, "mx": mx, "calc_in": (atk, dfn, f),
                    "foe_scale": tgt[:2] != my_side, "ko": new == 0, "hits": hits})

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
            fallen.pop(pos[:2] + ":" + sp, None)
        elif tag == "-start" and len(parts) >= 4 and parts[3].startswith("fallen"):
            fallen[key(parts[2].split(":")[0])] = int(parts[3][len("fallen"):] or 0)
        elif tag == "turn":
            helping.clear()
            pending = None
        elif tag == "-hitcount" and pending and pending["multi"]:
            n = int(re.match(r"\d+", parts[3]).group()) if len(parts) >= 4 and re.match(r"\d+", parts[3]) else 0
            for tgt, acc in pending["multi"].items():
                if n and not acc["skip"]:
                    event(pending["src"], tgt, acc["before"], acc["new"], acc["mx"], hits=n)
            pending["multi"] = {}
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
            if pending and len(parts) >= 4 and parts[3] == "Focus Sash":   # Showdown logs it BEFORE the -damage
                pending["sash"].add(parts[2].split(":")[0][:3])
        elif tag == "-singleturn" and "Helping Hand" in line:
            helping.add(parts[2].split(":")[0][:3])
        elif tag == "move":
            src = parts[2].split(":")[0][:3]
            pending = {"src": src, "move": parts[3], "spread": "[spread]" in line, "crit": set(), "sash": set(),
                       "multi": {}}
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
            mv = dex.move(pending["move"])
            if before is None or src[:2] == tgt[:2] or not mv.get("basePower"):
                continue
            if mv.get("multihit"):                    # one -damage per hit, then |-hitcount|: sum the hits
                acc = pending["multi"].setdefault(tgt, {"before": before, "mx": mx, "skip": False})
                acc["new"] = new
                acc["skip"] |= tgt in pending["crit"] and not mv.get("willCrit")
                continue
            if tgt in pending["crit"]:
                continue
            event(src, tgt, before, new, mx)
    calcs = []
    for o in out:
        a, d, f = o["calc_in"]
        c = {"attacker": a, "defender": d, "move": o["move"], "field": f}
        if o["hits"]:
            c["hits"] = o["hits"]
        calcs.append(c)
    res = calc.batch(calcs) or []
    for o, r in zip(out, res):
        o["pred"] = r
    return out


def check(h: dict, lo: int, hi: int) -> tuple[bool, str]:
    dhp = h["pred"]["defender_hp"]
    if h["foe_scale"]:                                        # percent scale, ±1 rounding (per hit for multi-hit)
        slack = h["hits"] or 1
        lo_p, hi_p = 100 * lo / dhp - slack, 100 * hi / dhp + slack
        good = lo_p <= h["lost"] <= hi_p or (h["ko"] and hi_p >= h["lost"])
        return good, f"lost {h['lost']}%  pred {100 * lo / dhp:.0f}-{100 * hi / dhp:.0f}%"
    good = lo <= h["lost"] <= hi or (h["ko"] and hi >= h["lost"])
    return good, f"lost {h['lost']}/{h['mx']}  pred {lo}-{hi} (calc max HP {dhp})"


def main() -> None:
    if "--champion" in sys.argv:                              # the frozen champion's calculator, for before/after
        from champion.agent.pokemon.calc import DamageCalc as ChampionCalc, _js_dir as champion_js_dir
        calc = ChampionCalc.get()
        print(f"calculator: champion ({champion_js_dir()})")
    else:
        from agent.pokemon.calc import _js_dir
        calc = DamageCalc.get()
        print(f"calculator: agent ({_js_dir()})")
    hits = []
    for path in sorted(glob.glob("logs/yoriko/*.jsonl")):
        hits += analyse(path, calc)
    single = [h for h in hits if not h["hits"]]
    multi = [h for h in hits if h["hits"]]
    ok = bad = skipped = 0
    for h in single:
        r = h["pred"]
        if not r:
            skipped += 1
            continue
        good, shown = check(h, r["min"], r["max"])
        if good:
            ok += 1
        else:
            bad += 1
            print(f"MISMATCH {h['file']} {h['atk']:>20s} {h['move']:<16s} -> {h['def']:<20s} {shown}"
                  + (f"  [{r['survives']}]" if r.get("survives") else ""))
    print(f"\n{len(single)} non-crit single-hit damage events: {ok} inside the calc range, {bad} outside, {skipped} not computable")
    # Multi-hit: the hit count comes from |-hitcount|. The observed total must lie in the calc's [min, max] AND the
    # ROLLS the policy scores with must lie inside that range (main's per-hit sums came out several times too big).
    ok = bad = skipped = 0
    for h in multi:
        r = h["pred"]
        if not r or not r.get("rolls"):
            skipped += 1
            continue
        good, shown = check(h, r["min"], r["max"])
        if not r["min"] <= min(r["rolls"]) <= max(r["rolls"]) <= r["max"]:
            good, shown = False, f"{shown}  rolls {min(r['rolls'])}-{max(r['rolls'])} outside [{r['min']}, {r['max']}]"
        ok, bad = ok + good, bad + (not good)
        print(f"{'ok      ' if good else 'MISMATCH'} {h['file']} {h['atk']:>20s} {h['move']:<16s} x{h['hits']} -> "
              f"{h['def']:<20s} {shown} (rolls)")
    print(f"{len(multi)} multi-hit events (all hits summed): {ok} inside the calc range (rolls inside it too), "
          f"{bad} outside, {skipped} not computable")


if __name__ == "__main__":
    main()
