"""Loss analysis of panel runs saved with --save-logs (local.panel ... --save-logs): what costs the candidate games.

    python scripts/extopp_analyze.py logs/panel/runs/v32-vs-simbot-s71.jsonl [more runs...] [--md out.md]

Reads <run>.jsonl (results) and <run>.jsonl.logs/<opp>-g<g>.json (Showdown protocol log per game, omniscient: the
`|split|` secret lines carry exact HP). For every game and both sides ("cand" = the candidate, "opp") it counts, from
the log alone:
  protect      Protect-family moves used; blocks = attacks that hit that Protect (wasted by the other side)
  fakeout      Fake Out used / flinches caused
  speed        Tailwind / Trick Room set, foe Speed drops (Icy Wind, Electroweb...), paralysis inflicted
  spread       spread attacks and the damage they dealt
  support      Follow Me / Rage Powder, Helping Hand, Spore/sleep, Will-O-Wisp burns, Intimidate
  switches     voluntary switches (before any move of the turn), pivots (U-turn...), forced replacements
  waste        own moves that failed / missed / hit an immunity / hit Protect, turns lost to flinch / sleep / para
  damage, KOs  direct damage dealt (fractions of max HP) and KOs, per turn and in total
and per game: turns, first KO, the remaining-Pokemon trajectory (was the candidate ever ahead and still lost; the
turn it fell behind for good; endgame = both sides <= 2 left), brought / lead species.
Then: win rates split by each feature (Wilson CI), candidate-vs-opponent behaviour per game, per-card win rates of
the candidate's picks and the opponent's picks, and (same-team runs) the same Pokemon played by each side:
per (species, move) usage rates, largest differences first. Example lost games are listed per theme as g numbers
(match seed = seed * 100000 + g, candidate seat = g % 2; re-play with the same panel command).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROTECT = {"Protect", "Detect", "Spiky Shield", "Baneful Bunker", "Silk Trap", "Burning Bulwark", "King's Shield",
           "Obstruct"}
PIVOT = {"U-turn", "Volt Switch", "Flip Turn", "Parting Shot", "Teleport", "Baton Pass", "Shed Tail", "Chilly Reception"}
REDIRECT = {"Follow Me", "Rage Powder"}
SPEED_MOVES = {"Tailwind", "Trick Room", "Icy Wind", "Electroweb", "Thunder Wave", "Bulldoze", "Rock Tomb",
               "Scary Face", "Glare", "Nuzzle"}
SLEEP_MOVES = {"Spore", "Sleep Powder", "Hypnosis", "Yawn", "Dark Void", "Lovely Kiss", "Sing"}
PRIORITY = {"Fake Out", "Extreme Speed", "Aqua Jet", "Grassy Glide", "Sucker Punch", "Vacuum Wave", "Quick Attack",
            "Bullet Punch", "Ice Shard", "Shadow Sneak", "First Impression", "Thunderclap", "Mach Punch", "Jet Punch",
            "Accelerock", "Feint", "Upper Hand"}
Z = 1.96
ROOT = Path(__file__).resolve().parents[1]
NATURE_SPE = {"timid": 1.1, "hasty": 1.1, "jolly": 1.1, "naive": 1.1, "brave": 0.9, "relaxed": 0.9, "quiet": 0.9,
              "sassy": 0.9}


def _speeds() -> dict:
    """Level-50 Speed stat of every catalog card by species name (Choice Scarf x1.5), from data/vgc_cards.json and the
    Showdown base stats the agent ships (agent/pokemon/dex.py). {} if either is missing."""
    try:
        sys.path.insert(0, str(ROOT))
        from agent.pokemon import dex
        out = {}
        for c in json.loads((ROOT / "data" / "vgc_cards.json").read_text()):
            base = (dex.base_stats(c["species"]) or {}).get("spe")
            if not base:
                continue
            ev = (c.get("evs") or {}).get("spe", 0)
            iv = (c.get("ivs") or {}).get("spe", 31)
            st = math.floor(math.floor((2 * base + iv + ev // 4) * 50 / 100 + 5) * NATURE_SPE.get(c["nature"].lower(), 1.0))
            if (c.get("item") or "").lower() == "choice scarf":
                st = math.floor(st * 1.5)
            out[c["species"]] = st
        return out
    except Exception:  # noqa: BLE001 - the speed tags are optional
        return {}


SPEED = _speeds()


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    h = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return c - h, c + h


def _side(ident: str) -> str:
    return ident.split(":")[0][:2]           # "p1a: Name" -> "p1"


def _name(ident: str) -> str:
    return ident.split(": ", 1)[1] if ": " in ident else ident


def _hp(tok: str) -> tuple[float, bool]:
    """'151/227 par' -> (fraction, fainted)."""
    tok = tok.split()[0] if tok else tok
    if tok.startswith("0") and ("fnt" in tok or tok == "0"):
        return 0.0, True
    if "/" in tok:
        a, b = tok.split("/")[:2]
        try:
            return float(a) / float(b.split()[0]), False
        except ValueError:
            return 1.0, False
    return 1.0, False


def parse(lines: list[str]) -> dict:
    """Per-side counters + game trajectory from one omniscient Showdown log."""
    # drop the public duplicate after every |split| (keep the exact-HP secret line)
    clean, skip = [], 0
    for i, ln in enumerate(lines):
        if skip:
            skip -= 1
            continue
        if ln.startswith("|split|"):
            if i + 1 < len(lines):
                clean.append(lines[i + 1])
            skip = 2
            continue
        clean.append(ln)
    S = {s: Counter() for s in ("p1", "p2")}
    moves_by = {s: Counter() for s in ("p1", "p2")}           # (species, move) -> uses
    turns_active = {s: Counter() for s in ("p1", "p2")}       # species -> turns it acted (moved or was stopped)
    hp: dict = {}                                              # (side, name) -> fraction
    alive = {"p1": set(), "p2": set()}
    fainted = {"p1": 0, "p2": 0}
    size = {"p1": 4, "p2": 4}
    brought = {"p1": [], "p2": []}
    leads = {"p1": [], "p2": []}
    turn = 0
    switched_in: set = set()                                   # (side, name) switched in voluntarily this turn
    phase = "pre"                                              # pre / turn / upkeep
    moved_this_turn = False
    last_move = None                                           # (side, name, move, spread)
    traj = []                                                  # (turn, alive p1, alive p2, hp p1, hp p2) at each turn start
    first_ko = None
    winner = None
    tw_turns = {"p1": [], "p2": []}
    tr_by = []
    per_turn_dmg = defaultdict(lambda: {"p1": 0.0, "p2": 0.0})
    # per-turn situation tags "p1:<tag>" (E[turn]); see TAGS
    E: dict = defaultdict(set)
    active: dict = {}                                          # "p1a" -> name
    start_active: set = set()
    moved: set = set()
    targets: dict = defaultdict(list)
    tw_up = {"p1": False, "p2": False}
    tr_setter = None
    hits_on: dict = defaultdict(list)                          # (side, name) -> [(attacker side, name, move, spread)]
    species_of: dict = {}                                      # (side, name) -> species (details)
    start_slots: dict = {}                                     # "p1a" -> name at the start of the turn
    kob: list = []                                             # Pokemon KO'd before they moved (see parse)

    def state():
        return (size["p1"] - fainted["p1"], size["p2"] - fainted["p2"],
                sum(hp.get(("p1", n), 0) for n in alive["p1"]) + size["p1"] - len(brought["p1"]),
                sum(hp.get(("p2", n), 0) for n in alive["p2"]) + size["p2"] - len(brought["p2"]))

    for ln in clean:
        parts = ln.split("|")
        if len(parts) < 2:
            continue
        tag = parts[1]
        if tag == "teamsize":
            size[parts[2]] = int(parts[3])
        elif tag == "turn":
            turn = int(parts[2])
            phase = "turn"
            moved_this_turn = False
            last_move = None
            switched_in = set()
            # Pokemon left = brought - fainted; HP sum counts the not yet seen bench at full HP
            traj.append((turn, *state()))
            start_active = {(k[:2], v) for k, v in active.items()}
            start_slots = dict(active)
            moved = set()
            targets = defaultdict(list)
            hits_on = defaultdict(list)
            for s in ("p1", "p2"):
                if tw_up[s]:
                    E[turn].add(f"{s}:tailwind_up")
            if tr_setter:
                E[turn].add(f"{tr_setter}:trickroom_up")
        elif tag == "upkeep":
            phase = "upkeep"
        elif tag in ("switch", "drag", "replace"):
            s, n = _side(parts[2]), _name(parts[2])
            slot_key = parts[2].split(":")[0][:3]
            if phase == "turn" and tag == "switch" and not moved_this_turn and active.get(slot_key):
                moves_by[s][(active[slot_key], "(switched out)")] += 1
                turns_active[s][active[slot_key]] += 1
            active[slot_key] = n
            species_of[(s, n)] = (parts[3] if len(parts) > 3 else n).split(",")[0]
            if phase == "turn" and tag == "switch" and not moved_this_turn:
                E[turn].add(f"{s}:switch")
            frac, _ = _hp(parts[4] if len(parts) > 4 else "")
            if n not in brought[s]:
                brought[s].append(n)
                if phase == "pre":
                    leads[s].append(n)
            hp[(s, n)] = frac
            if frac > 0:
                alive[s].add(n)
            if phase == "turn" and tag == "switch":
                if not moved_this_turn:
                    S[s]["switch_voluntary"] += 1
                    switched_in.add((s, n))
                elif last_move and last_move[0] == s and last_move[2] in PIVOT:
                    S[s]["switch_pivot"] += 1
                else:
                    S[s]["switch_other"] += 1
            elif phase == "upkeep" or phase == "pre":
                if phase == "upkeep":
                    S[s]["switch_forced"] += 1
        elif tag == "move":
            s, n, mv = _side(parts[2]), _name(parts[2]), parts[3]
            moved_this_turn = True
            spread = any(p.startswith("[spread]") for p in parts[4:])
            last_move = (s, n, mv, spread)
            S[s]["moves"] += 1
            moves_by[s][(n, mv)] += 1
            turns_active[s][n] += 1
            if phase == "turn":
                moved.add((s, n))
                if spread:
                    E[turn].add(f"{s}:spread")
                if mv in PROTECT:
                    E[turn].add(f"{s}:protect")
                elif mv == "Fake Out":
                    E[turn].add(f"{s}:fakeout")
                elif mv in REDIRECT:
                    E[turn].add(f"{s}:redirect")
                elif mv == "Helping Hand":
                    E[turn].add(f"{s}:helpinghand")
                tgt = parts[4] if len(parts) > 4 else ""
                if not spread and re.match(r"^p[12][ab]: ", tgt) and _side(tgt) != s:
                    if _name(tgt) in targets[s]:
                        E[turn].add(f"{s}:double_target")
                    targets[s].append(_name(tgt))
            if mv in PROTECT:
                S[s]["protect_uses"] += 1
            elif mv == "Fake Out":
                S[s]["fakeout_uses"] += 1
            elif mv in REDIRECT:
                S[s]["redirect_uses"] += 1
            elif mv == "Helping Hand":
                S[s]["helpinghand_uses"] += 1
            elif mv == "Tailwind":
                S[s]["tailwind_uses"] += 1
            elif mv == "Trick Room":
                S[s]["trickroom_uses"] += 1
            if mv in SPEED_MOVES:
                S[s]["speed_moves"] += 1
            if spread:
                S[s]["spread_moves"] += 1
        elif tag == "cant":
            s, n = _side(parts[2]), _name(parts[2])
            why = parts[3] if len(parts) > 3 else ""
            turns_active[s][n] += 1
            S[s]["cant"] += 1
            moved.add((s, n))
            E[turn].add(f"{s}:flinched" if why == "flinch" else f"{s}:cant_move")
            if why == "flinch":
                S[s]["flinched"] += 1
                S["p2" if s == "p1" else "p1"]["flinches_caused"] += 1
            elif why in ("slp", "par", "frz"):
                S[s][f"cant_{why}"] += 1
            elif why == "recharge":
                S[s]["cant_recharge"] += 1
        elif tag == "-activate":
            s = _side(parts[2])
            what = parts[3] if len(parts) > 3 else ""
            if what in ("move: Protect", "Protect", "move: Detect", "move: Wide Guard", "move: Quick Guard"):
                S[s]["protect_blocks"] += 1
                if last_move and last_move[0] != s:
                    S[last_move[0]]["waste_into_protect"] += 1
                    E[turn].add(f"{last_move[0]}:into_protect")
        elif tag == "-fail":
            if last_move:
                S[last_move[0]]["waste_fail"] += 1
                E[turn].add(f"{last_move[0]}:fail")
        elif tag == "-miss":
            s = _side(parts[2])
            S[s]["waste_miss"] += 1
            E[turn].add(f"{s}:miss")
        elif tag == "-crit":
            if last_move:
                E[turn].add(f"{last_move[0]}:crit")
        elif tag == "-immune":
            if last_move and last_move[0] != _side(parts[2]):
                S[last_move[0]]["waste_immune"] += 1
                E[turn].add(f"{last_move[0]}:into_immune")
                if (_side(parts[2]), _name(parts[2])) in switched_in:
                    S[_side(parts[2])]["switchin_immune"] += 1
                    E[turn].add(f"{_side(parts[2])}:switch_absorb")
        elif tag == "-resisted":
            if last_move and last_move[0] != _side(parts[2]) and (_side(parts[2]), _name(parts[2])) in switched_in:
                S[_side(parts[2])]["switchin_resist"] += 1
                E[turn].add(f"{_side(parts[2])}:switch_absorb")
        elif tag in ("-damage", "-heal"):
            s, n = _side(parts[2]), _name(parts[2])
            frac, fnt = _hp(parts[3] if len(parts) > 3 else "")
            before = hp.get((s, n), 1.0)
            hp[(s, n)] = frac
            indirect = any(p.startswith("[from]") for p in parts[4:])
            if tag == "-damage" and not indirect and last_move and last_move[0] != s:
                d = max(0.0, before - frac)
                a = last_move[0]
                hits_on[(s, n)].append(last_move)
                S[a]["damage_dealt"] += d
                per_turn_dmg[turn][a] += d
                if last_move[3]:
                    S[a]["spread_damage"] += d
            elif tag == "-damage" and not indirect and last_move and last_move[0] == s and last_move[1] != n:
                S[s]["ally_damage"] += max(0.0, before - frac)
        elif tag == "faint":
            s, n = _side(parts[2]), _name(parts[2])
            alive[s].discard(n)
            hp[(s, n)] = 0.0
            fainted[s] += 1
            other = "p2" if s == "p1" else "p1"
            S[other]["kos"] += 1
            if first_ko is None:
                first_ko = (turn, other)
            if phase == "turn":
                E[turn].add(f"{s}:lost_mon")
                hits = [h for h in hits_on.get((s, n), []) if h[0] != s]
                if len({h[1] for h in hits}) >= 2:
                    E[turn].add(f"{s}:ko_double_target")
                if any(h[2] in PRIORITY for h in hits):
                    E[turn].add(f"{s}:ko_priority")
                if any(h[3] for h in hits):
                    E[turn].add(f"{s}:ko_spread")
                if (s, n) in start_active and (s, n) not in moved:
                    E[turn].add(f"{s}:ko_before_moving")
                    mine = SPEED.get(species_of.get((s, n), n))
                    theirs = [SPEED.get(species_of.get((h[0], h[1]), h[1])) for h in hits if h[2] not in PRIORITY]
                    why = "?"
                    if mine and theirs and all(t for t in theirs):
                        why = "slower" if min(theirs) < mine else "faster"
                        E[turn].add(f"{s}:outsped_by_{why}")
                    slot = next((k[2] for k, v in start_slots.items() if k[:2] == s and v == n), None)
                    kob.append({"turn": turn, "side": s, "slot": "ab".index(slot) if slot else None, "name": n,
                                "by": why, "hits": [h[2] for h in hits]})
        elif tag == "-sidestart":
            s = parts[2][:2]
            what = parts[3]
            if "Tailwind" in what:
                S[s]["tailwind_set"] += 1
                tw_turns[s].append(turn)
                tw_up[s] = True
                E[turn].add(f"{s}:tailwind_set")
            elif any(x in what for x in ("Reflect", "Light Screen", "Aurora Veil")):
                S[s]["screens_set"] += 1
        elif tag == "-sideend" and "Tailwind" in (parts[3] if len(parts) > 3 else ""):
            tw_up[parts[2][:2]] = False
        elif tag == "-fieldstart" and "Trick Room" in parts[2]:
            of = next((p for p in parts[3:] if p.startswith("[of] ")), None)
            if of:
                s = _side(of[5:])
                S[s]["trickroom_set"] += 1
                tr_by.append((turn, s))
                tr_setter = s
                E[turn].add(f"{s}:trickroom_set")
        elif tag == "-fieldend" and "Trick Room" in parts[2]:
            tr_setter = None
        elif tag == "-unboost":
            s = _side(parts[2])
            stat = parts[3] if len(parts) > 3 else ""
            if stat == "spe" and last_move and last_move[0] != s:
                S[last_move[0]]["speed_drops_inflicted"] += 1
        elif tag == "-status":
            s = _side(parts[2])
            st = parts[3] if len(parts) > 3 else ""
            other = "p2" if s == "p1" else "p1"
            if not any(p.startswith("[from] item") for p in parts[4:]):     # Flame / Toxic Orb: self-inflicted
                S[other][f"status_{st}_inflicted"] += 1
        elif tag == "-ability" and len(parts) > 3 and parts[3] == "Intimidate":
            S[_side(parts[2])]["intimidate"] += 1
        elif tag == "win":
            winner = parts[2]
    final = (size["p1"] - fainted["p1"], size["p2"] - fainted["p2"])
    traj_end = (turn + 1, *state())
    return {"S": {s: dict(c) for s, c in S.items()}, "moves_by": moves_by, "turns_active": turns_active,
            "E": {t: set(v) for t, v in E.items()}, "traj_end": traj_end, "kob": kob,
            "brought": brought, "leads": leads, "traj": traj, "final": final, "first_ko": first_ko,
            "winner": winner, "turns": turn, "tw_turns": tw_turns, "tr_by": tr_by,
            "per_turn_dmg": {t: dict(v) for t, v in per_turn_dmg.items()}}


def load_run(path: Path) -> list[dict]:
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    keep = {}
    for r in rows:
        k = (r["opp"], r["seed"], r["g"])
        if k not in keep or "error" in keep[k]:
            keep[k] = r
    logs = Path(str(path) + ".logs")
    out = []
    for r in keep.values():
        if "error" in r:
            continue
        f = logs / f"{r['opp']}-g{r['g']}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text())
        p = parse(d["log"])
        cand = "p1" if r["seat"] == 0 else "p2"
        opp = "p2" if cand == "p1" else "p1"
        p.update(row=r, cand=cand, oppside=opp, win=bool(r["win"]), g=r["g"], seed=r["seed"],
                 same=bool(r.get("same_team")), trace=d.get("trace") or [], cand_trace=d.get("cand_trace") or [])
        out.append(p)
    return out


# ------------------------------------------------------------------------------------------------ game features
def features(p: dict) -> dict:
    c, o = p["cand"], p["oppside"]
    Sc, So = p["S"][c], p["S"][o]
    ci = 1 if c == "p1" else 2
    oi = 3 - ci
    ahead = any(t[ci] > t[oi] for t in p["traj"])
    behind_turn = None                      # the turn from which the candidate was behind in Pokemon for good
    for t in p["traj"]:
        if t[ci] < t[oi]:
            if behind_turn is None:
                behind_turn = t[0]
        else:
            behind_turn = None
    endgame = next((t for t in p["traj"] if t[ci] <= 2 and t[oi] <= 2), None)
    t1 = next((t for t in p["traj"] if t[0] == 2), None)      # state at the start of turn 2 = after turn 1
    lead_hp = (t1[2 + ci] - t1[2 + oi]) if t1 else 0.0       # (hp_c - hp_o) indices: traj = (turn, a1, a2, h1, h2)
    return {
        "opp_tailwind": So.get("tailwind_set", 0) > 0, "cand_tailwind": Sc.get("tailwind_set", 0) > 0,
        "opp_trickroom": So.get("trickroom_set", 0) > 0, "cand_trickroom": Sc.get("trickroom_set", 0) > 0,
        "opp_speed_drops": So.get("speed_drops_inflicted", 0) + So.get("status_par_inflicted", 0) > 0,
        "opp_fakeout_flinch": So.get("flinches_caused", 0) > 0,
        "opp_redirect": So.get("redirect_uses", 0) > 0,
        "cand_into_protect_2plus": Sc.get("waste_into_protect", 0) >= 2,
        "opp_spread_heavy": So.get("spread_damage", 0) >= 1.0,
        "opp_switches_2plus": So.get("switch_voluntary", 0) + So.get("switch_pivot", 0) >= 2,
        "cand_never_switched": Sc.get("switch_voluntary", 0) + Sc.get("switch_pivot", 0) == 0,
        "cand_first_ko": bool(p["first_ko"]) and p["first_ko"][1] == c,
        "cand_ahead_once": ahead,
        "cand_lost_turn1": lead_hp < -0.3,
        "cand_won_turn1": lead_hp > 0.3,
        "endgame_even": bool(endgame) and endgame[ci] == endgame[oi],
        "endgame_ahead": bool(endgame) and endgame[ci] > endgame[oi],
        "long_game": p["turns"] >= 10,
        "_behind_turn": behind_turn, "_lead_hp": lead_hp, "_endgame": endgame,
    }


BEHAVIOUR = [("protect_uses", "Protect used"), ("protect_blocks", "attacks blocked by own Protect"),
             ("waste_into_protect", "own attacks into foe Protect"), ("fakeout_uses", "Fake Out used"),
             ("flinches_caused", "flinches caused"), ("tailwind_set", "Tailwind set"),
             ("trickroom_set", "Trick Room set"), ("speed_drops_inflicted", "foe Speed drops"),
             ("status_par_inflicted", "paralysis inflicted"), ("status_slp_inflicted", "sleep inflicted"),
             ("status_brn_inflicted", "burns inflicted"), ("redirect_uses", "Follow Me / Rage Powder"),
             ("helpinghand_uses", "Helping Hand"), ("screens_set", "screens set"), ("intimidate", "Intimidate"),
             ("spread_moves", "spread attacks"), ("spread_damage", "spread damage (max-HP units)"),
             ("damage_dealt", "direct damage dealt (max-HP units)"), ("ally_damage", "damage to own ally"),
             ("kos", "KOs"), ("switch_voluntary", "voluntary switches"),
             ("switchin_immune", "foe attacks absorbed by an immune switch-in"),
             ("switchin_resist", "foe attacks resisted by a switch-in"), ("switch_pivot", "pivot switches"),
             ("switch_forced", "forced replacements"), ("waste_fail", "own moves that failed"),
             ("waste_miss", "misses"), ("waste_immune", "attacks into an immunity"), ("flinched", "flinched"),
             ("cant_par", "fully paralysed"), ("cant_slp", "asleep")]


def pct(a: float) -> str:
    return f"{100 * a:.1f}"


def split_table(games: list[dict], feats: list[dict]) -> list[str]:
    keys = [k for k in feats[0] if not k.startswith("_")]
    out = ["| feature (per game) | games with | cand win% with [95% CI] | games without | cand win% without |",
           "|---|---|---|---|---|"]
    for k in keys:
        yes = [g for g, f in zip(games, feats) if f[k]]
        no = [g for g, f in zip(games, feats) if not f[k]]
        wy, wn = sum(g["win"] for g in yes), sum(g["win"] for g in no)
        lo, hi = wilson(wy, len(yes))
        out.append(f"| {k} | {len(yes)} | {pct(wy / len(yes)) if yes else '-'} [{pct(lo)}, {pct(hi)}] | {len(no)} | "
                   f"{pct(wn / len(no)) if no else '-'} |")
    return out


def behaviour_table(games: list[dict]) -> list[str]:
    n = len(games)
    lost = [g for g in games if not g["win"]]
    out = ["| per game | cand (all) | opp (all) | cand in cand's losses | opp in cand's losses |", "|---|---|---|---|---|"]
    for key, label in BEHAVIOUR:
        a = sum(g["S"][g["cand"]].get(key, 0) for g in games) / n
        b = sum(g["S"][g["oppside"]].get(key, 0) for g in games) / n
        la = sum(g["S"][g["cand"]].get(key, 0) for g in lost) / max(1, len(lost))
        lb = sum(g["S"][g["oppside"]].get(key, 0) for g in lost) / max(1, len(lost))
        out.append(f"| {label} | {a:.2f} | {b:.2f} | {la:.2f} | {lb:.2f} |")
    return out


def card_table(games: list[dict], side_key: str, min_n: int = 12) -> list[str]:
    by = defaultdict(lambda: [0, 0])
    for g in games:
        for sp in set(g["brought"][g[side_key]]):
            by[sp][0] += 1
            by[sp][1] += g["win"]
    base = sum(g["win"] for g in games) / len(games)
    rows = sorted(((sp, n, w) for sp, (n, w) in by.items() if n >= min_n), key=lambda t: t[2] / t[1])
    out = [f"| species ({'cand' if side_key == 'cand' else 'opp'} brought it) | games | cand win% [95% CI] | vs all |",
           "|---|---|---|---|"]
    for sp, n, w in rows:
        lo, hi = wilson(w, n)
        out.append(f"| {sp} | {n} | {pct(w / n)} [{pct(lo)}, {pct(hi)}] | {100 * (w / n - base):+.1f} |")
    return out


def same_team_moves(games: list[dict], min_turns: int = 15) -> list[str]:
    """Same six on both sides: per (species, move) share of that species' acting turns, cand vs opp."""
    use = {"cand": Counter(), "opp": Counter()}
    act = {"cand": Counter(), "opp": Counter()}
    for g in games:
        for who, s in (("cand", g["cand"]), ("opp", g["oppside"])):
            use[who].update(g["moves_by"][s])
            act[who].update(g["turns_active"][s])
    rows = []
    for (sp, mv) in set(use["cand"]) | set(use["opp"]):
        ta, tb = act["cand"][sp], act["opp"][sp]
        if ta < min_turns or tb < min_turns:
            continue
        a, b = use["cand"][(sp, mv)] / ta, use["opp"][(sp, mv)] / tb
        rows.append((b - a, sp, mv, a, b, ta, tb))
    rows.sort(key=lambda t: -abs(t[0]))
    out = ["| species | move | cand: share of its turns | opp: share of its turns | opp - cand | turns (cand/opp) |",
           "|---|---|---|---|---|---|"]
    for d, sp, mv, a, b, ta, tb in rows[:30]:
        out.append(f"| {sp} | {mv} | {pct(a)} | {pct(b)} | {100 * d:+.1f} | {ta}/{tb} |")
    return out


def species_moves(games: list[dict], species: list[str]) -> list[str]:
    """Every move (and voluntary switch-out) of the named species, share of its acting turns, cand vs opp."""
    use = {"cand": Counter(), "opp": Counter()}
    act = {"cand": Counter(), "opp": Counter()}
    for g in games:
        for who, s in (("cand", g["cand"]), ("opp", g["oppside"])):
            use[who].update(g["moves_by"][s])
            act[who].update(g["turns_active"][s])
    out = ["| species | move | cand share | opp share | turns (cand/opp) |", "|---|---|---|---|---|"]
    for sp in species:
        ta, tb = act["cand"][sp], act["opp"][sp]
        mvs = sorted({m for (x, m) in list(use["cand"]) + list(use["opp"]) if x == sp},
                     key=lambda m: -(use["cand"][(sp, m)] + use["opp"][(sp, m)]))
        for m in mvs:
            out.append(f"| {sp} | {m} | {pct(use['cand'][(sp, m)] / max(1, ta))} | "
                       f"{pct(use['opp'][(sp, m)] / max(1, tb))} | {ta}/{tb} |")
    return out


def lead_table(games: list[dict]) -> list[str]:
    out = ["| state after turn 1 (HP sum difference, max-HP units) | games | cand win% |", "|---|---|---|"]
    bins = [(-9, -0.6, "cand behind by > 0.6"), (-0.6, -0.2, "behind 0.2-0.6"), (-0.2, 0.2, "even (+-0.2)"),
            (0.2, 0.6, "ahead 0.2-0.6"), (0.6, 9, "ahead by > 0.6")]
    for lo, hi, label in bins:
        gs = [g for g in games if lo <= g["_f"]["_lead_hp"] < hi]
        if gs:
            out.append(f"| {label} | {len(gs)} | {pct(sum(g['win'] for g in gs) / len(gs))} |")
    return out


def swings(p: dict) -> list[tuple[float, int]]:
    """(change of our position value over the turn, turn) for every turn; value = HP-sum lead + 0.5 x Pokemon lead."""
    ci = 1 if p["cand"] == "p1" else 2
    oi = 3 - ci
    pts = p["traj"] + [p["traj_end"]]

    def v(t):
        return (t[2 + ci] - t[2 + oi]) + 0.5 * (t[ci] - t[oi])
    return [(v(pts[k + 1]) - v(pts[k]), pts[k][0]) for k in range(len(pts) - 1)]


def turn_tags(p: dict, t: int) -> set:
    c = p["cand"]
    return {("cand:" if x[:2] == c else "opp:") + x[3:] for x in p["E"].get(t, set())}


def decisive_table(games: list[dict]) -> list[str]:
    """Tags of the worst turn of every lost game vs how often the tag shows up on any turn of any game."""
    base, nturns = Counter(), 0
    for g in games:
        for _, t in swings(g):
            base.update(turn_tags(g, t))
            nturns += 1
    dec, nd, when = Counter(), 0, Counter()
    for g in games:
        if g["win"]:
            continue
        sw = swings(g)
        if not sw:
            continue
        d, t = min(sw)
        g["_decisive"] = (t, d)
        dec.update(turn_tags(g, t))
        when[t] += 1
        nd += 1
    rows = []
    for tag in set(dec) | set(base):
        a, b = dec[tag] / max(1, nd), base[tag] / max(1, nturns)
        if dec[tag] >= 2:
            rows.append((a, tag, a, b))
    rows.sort(reverse=True)
    out = [f"worst turn of each loss ({nd} losses): turn " + ", ".join(f"t{t}: {c}" for t, c in sorted(when.items())),
           "", "| situation on that turn | share of worst turns | share of all turns | lift |", "|---|---|---|---|"]
    for _, tag, a, b in rows[:32]:
        out.append(f"| {tag} | {pct(a)} | {pct(b)} | {a / b if b else float('inf'):.2f} |")
    return out


SITUATIONS = [("lost_mon", "lost a Pokemon"), ("ko_before_moving", "lost one before it could move"),
              ("outsped_by_faster", "  ... hit first by naturally faster foes"),
              ("outsped_by_slower", "  ... hit first by a naturally slower foe (Tailwind / TR / drops / boosts)"),
              ("ko_double_target", "lost one to a double target"), ("ko_spread", "lost one to (incl.) a spread move"),
              ("ko_priority", "lost one to (incl.) a priority move"), ("double_target", "double-targeted a foe"),
              ("protect", "used Protect"), ("switch", "switched voluntarily"),
              ("switch_absorb", "a switch-in took an immune / resisted hit"), ("into_protect", "attacked into Protect"),
              ("flinched", "flinched"), ("tailwind_up", "had Tailwind up"), ("trickroom_up", "had its Trick Room up"),
              ("crit", "landed a crit")]


def situation_table(games: list[dict]) -> list[str]:
    """Turns per game with each situation, for each side (all games / the candidate's losses)."""
    n, lost = len(games), [g for g in games if not g["win"]]
    cnt = {k: Counter() for k in ("all", "lost")}
    for g in games:
        for t in g["E"]:
            for tag in turn_tags(g, t):
                cnt["all"][tag] += 1
                if not g["win"]:
                    cnt["lost"][tag] += 1
    out = ["| turns per game with ... | cand | opp | cand (cand's losses) | opp (cand's losses) |", "|---|---|---|---|---|"]
    for key, label in SITUATIONS:
        out.append(f"| {label} | {cnt['all']['cand:' + key] / n:.2f} | {cnt['all']['opp:' + key] / n:.2f} | "
                   f"{cnt['lost']['cand:' + key] / max(1, len(lost)):.2f} | "
                   f"{cnt['lost']['opp:' + key] / max(1, len(lost)):.2f} |")
    return out


PROTECT_IDS = {"".join(ch for ch in m.lower() if ch.isalnum()) for m in PROTECT}


def _category(c) -> str:
    if not isinstance(c, dict):
        return "unknown"
    t = c.get("type")
    if t in ("switch", "pass"):
        return t
    if t != "move":
        return "unknown"
    mid = str(c.get("move_id"))
    if mid in PROTECT_IDS:
        return "protect"
    try:
        from agent.pokemon import dex
        cat = dex.move(mid).get("category")
    except Exception:  # noqa: BLE001
        cat = None
    return "attack" if cat in ("Physical", "Special") else "status" if cat == "Status" else "move"


def decisions(g: dict) -> dict:
    """{side: {turn: [slot 0 answer, slot 1 answer]}} from the saved traces (first battle answer of each turn)."""
    out = {g["cand"]: {}, g["oppside"]: {}}
    for t in g.get("cand_trace") or []:
        if t.get("phase") == "moving" and isinstance(t.get("out"), dict) and t.get("turn") not in out[g["cand"]]:
            out[g["cand"]][t["turn"]] = [t["out"].get("slot_0"), t["out"].get("slot_1")]
    for t in g.get("trace") or []:
        if t.get("slots") and not t.get("forced") and t.get("turn") not in out[g["oppside"]]:
            out[g["oppside"]][t["turn"]] = list(t["slots"]) + [None]
    return out


def kob_table(games: list[dict]) -> list[str]:
    """Pokemon KO'd before they could move: what their side had told them to do that turn."""
    stats = {"cand": Counter(), "opp": Counter()}
    for g in games:
        dec = decisions(g)
        for ev in g["kob"]:
            who = "cand" if ev["side"] == g["cand"] else "opp"
            stats[who]["n"] += 1
            stats[who]["by_" + ev["by"]] += 1
            d = dec.get(ev["side"], {}).get(ev["turn"])
            if d is None or ev["slot"] is None:
                stats[who]["no_trace"] += 1
                continue
            stats[who]["traced"] += 1
            stats[who][_category(d[ev["slot"]])] += 1
    n = len(games)
    out = ["| KO'd before moving | per game | naturally faster KO-er | traced | told to attack | status move | "
           "Protect | switch |", "|---|---|---|---|---|---|---|---|"]
    for who in ("cand", "opp"):
        s = stats[who]
        tr = max(1, s["traced"])
        out.append(f"| {who} | {s['n'] / n:.2f} | {pct(s['by_faster'] / max(1, s['n']))} | {s['traced']} | "
                   f"{pct(s['attack'] / tr)} | {pct(s['status'] / tr)} | {pct(s['protect'] / tr)} | "
                   f"{pct(s['switch'] / tr)} |")
    return out


def examples(games: list[dict], pred, k: int = 6) -> str:
    gs = [g for g in games if not g["win"] and pred(g)]
    return ", ".join(f"g{g['g']}" for g in gs[:k]) + (f" (+{len(gs) - k})" if len(gs) > k else "") if gs else "-"


def report(path: Path, species: list[str] | None = None) -> list[str]:
    games = load_run(path)
    if not games:
        return [f"## {path.name}: no games with logs"]
    for g in games:
        g["_f"] = features(g)
    n, w = len(games), sum(g["win"] for g in games)
    lo, hi = wilson(w, n)
    same = sum(g["same"] for g in games)
    turns = sum(g["turns"] for g in games) / n
    L = [f"## {path.name} ({'same-team' if same else 'normal draft'}, {n} games)", "",
         f"candidate wins {w}/{n} = {pct(w / n)}% [95% CI {pct(lo)}, {pct(hi)}]; mean {turns:.1f} turns; "
         f"seat 0 (p1) {sum(g['win'] for g in games if g['cand'] == 'p1')}/{sum(g['cand'] == 'p1' for g in games)}, "
         f"seat 1 (p2) {sum(g['win'] for g in games if g['cand'] == 'p2')}/{sum(g['cand'] == 'p2' for g in games)}", ""]
    L += ["### Behaviour per game (both sides)", ""] + behaviour_table(games) + [""]
    L += ["### Situations per game (turns with ...)", ""] + situation_table(games) + [""]
    if any(g.get("cand_trace") for g in games):
        L += ["### Pokemon KO'd before they moved: what they had been told to do", ""] + kob_table(games) + [""]
    L += ["### Win rate split by game features", ""] + split_table(games, [g["_f"] for g in games]) + [""]
    L += ["### After turn 1", ""] + lead_table(games) + [""]
    L += ["### The turn that lost it (largest swing against the candidate in each loss)", ""]
    L += decisive_table(games) + [""]
    lost = [g for g in games if not g["win"]]
    thrown = [g for g in lost if g["_f"]["cand_ahead_once"]]
    bt = Counter(g["_f"]["_behind_turn"] for g in lost if g["_f"]["_behind_turn"] is not None)
    L += [f"losses: {len(lost)}; of them the candidate was ahead in Pokemon at some turn start in {len(thrown)} "
          f"({examples(games, lambda g: g['_f']['cand_ahead_once'])}); turn from which it stayed behind: "
          + ", ".join(f"t{t}: {c}" for t, c in sorted(bt.items())), ""]
    L += ["### Cards", ""] + card_table(games, "cand") + [""] + card_table(games, "oppside") + [""]
    if same:
        L += ["### Same Pokemon, different pilot: move usage (share of that species' acting turns)", ""]
        L += same_team_moves(games) + [""]
    if species:
        L += ["### Selected species: every move and voluntary switch-out", ""] + species_moves(games, species) + [""]
    themes = {
        "opp set Tailwind": lambda g: g["_f"]["opp_tailwind"],
        "opp set Trick Room": lambda g: g["_f"]["opp_trickroom"],
        "opp speed drops / paralysis": lambda g: g["_f"]["opp_speed_drops"],
        "flinched by Fake Out": lambda g: g["_f"]["opp_fakeout_flinch"],
        ">=2 own attacks into Protect": lambda g: g["_f"]["cand_into_protect_2plus"],
        "opp spread damage >= 1 max HP": lambda g: g["_f"]["opp_spread_heavy"],
        "opp switched >= 2": lambda g: g["_f"]["opp_switches_2plus"],
        "cand ahead at some point, lost": lambda g: g["_f"]["cand_ahead_once"],
        "even endgame (both <= 2, equal) lost": lambda g: g["_f"]["endgame_even"],
        "behind > 0.3 HP after turn 1": lambda g: g["_f"]["cand_lost_turn1"],
    }
    L += ["### Example lost games per theme (g = game index; match seed = seed*100000+g, cand seat = g%2)", ""]
    L += ["| theme | lost games | examples |", "|---|---|---|"]
    for name, pred in themes.items():
        k = sum(1 for g in lost if pred(g))
        L.append(f"| {name} | {k}/{len(lost)} | {examples(games, pred)} |")
    L.append("")
    tr = [t for g in games for t in g["trace"] if "ms" in t]
    if tr:
        ms = sorted(t["ms"] for t in tr)
        L.append(f"opponent decision time: mean {sum(ms) / len(ms):.0f} ms, p95 {ms[int(0.95 * (len(ms) - 1))]:.0f} ms, "
                 f"max {ms[-1]:.0f} ms over {len(ms)} decisions; fallbacks {sum(1 for t in tr if t.get('fallback'))}")
        L.append("")
    return L


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--md", help="also write the report to this Markdown file")
    ap.add_argument("--species", help="comma list of Pokemon (log names, e.g. Ting-Lu,Incineroar) to show every move of")
    a = ap.parse_args(argv)
    out = []
    sp = [s.strip() for s in a.species.split(",")] if a.species else None
    for r in a.runs:
        out += report(Path(r), sp)
    text = "\n".join(out)
    print(text)
    if a.md:
        Path(a.md).write_text(text + "\n")


if __name__ == "__main__":
    sys.exit(main())
