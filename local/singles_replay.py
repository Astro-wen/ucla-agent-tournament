"""Real-log check of the singles policy: every recorded singles match (logs/*/*.jsonl of a game type other than the VGC
doubles draft; so far one gen9same game, session c8efbea9) replayed through the current agent and the champion.

    python -m local.singles_replay          # summary
    python -m local.singles_replay -v       # one line per decision

1. legality: every answer is one of that state's legal actions (team preview: a valid select_lineup);
2. decisions: the candidate's choice (with the singles policy's reason) next to the champion's and the one actually
   played on the platform;
3. damage model: every direct hit in the recorded protocol (no crit, multi-hit, boosts, weather, terrain or screens)
   against the calculator's [min, max] for the sets the singles policy assumes in that format (gen9same: level 100,
   252 EVs in every stat, neutral nature, the items / abilities of the shared team).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import time

os.environ["ARENA_RECORD"] = "0"          # replaying must not re-record the sessions it reads

from altruagent import DecisionContext, GameState, LegalAction  # noqa: E402

VGC = "pokemon_vgc_doubles_draft"


def singles_logs(pattern: str = "logs/*/*.jsonl") -> list[str]:
    out = []
    for f in sorted(glob.glob(pattern)):
        with open(f) as fh:
            first = fh.readline()
        try:
            gt = json.loads(first).get("game_type") or json.loads(first)["raw"].get("game_type")
        except (ValueError, KeyError):
            continue
        if gt and gt != VGC:
            out.append(f)
    return out


def _label(raw: dict, out) -> str:
    """A readable choice: move id / switch species / lineup."""
    if isinstance(out, dict) and out.get("type") == "select_lineup":
        return "lead " + ",".join(out.get("leads") or []) + " bring " + ",".join(out.get("bring") or [])
    aid = out.action_id if isinstance(out, LegalAction) else out.get("action_id") if isinstance(out, dict) else out
    for a in (raw.get("legal_actions") or {}).get("actions") or []:
        if a.get("action_id") == aid:
            act = (a.get("input") or {}).get("action") or {}
            return act.get("move_id") or ("-> " + str(act.get("species"))) if act.get("type") in ("move", "switch") \
                else str(aid)
    return str(aid)


def _legal(raw: dict, out) -> bool:
    obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
    if raw.get("phase") == "team_preview":
        roster = {p["species"] for p in obs.get("your_roster", [])}
        return isinstance(out, dict) and len(set(out.get("bring", []))) == 4 and set(out["bring"]) <= roster \
            and len(out.get("leads", [])) == 2 and set(out["leads"]) <= set(out["bring"])
    ids = {a["action_id"] for a in (raw.get("legal_actions") or {}).get("actions", [])}
    return isinstance(out, LegalAction) and out.action_id in ids


# ---------------------------------------------------------------- 1 + 2: decisions
def replay_decisions(f: str, verbose: bool = False) -> dict:
    from agent.agent import Contestant
    from agent.reasoning import unwrap        # moves may come back wrapped in WithReasoning (public reasoning)
    from champion.agent.agent import Contestant as Champion
    cand, champ = Contestant(), Champion()
    n = illegal = same_champ = same_played = by_singles = 0
    worst = 0.0
    for line in open(f):
        rec = json.loads(line)
        raw = rec["raw"]
        if rec.get("kind") != "action":
            continue
        ctx = DecisionContext(session_id=raw.get("session_id", "x"), tournament_id=None,
                              game_type=raw.get("game_type"), agent_id="replay")
        cand.pk.singles_why = None
        t = time.perf_counter()
        a = unwrap(cand.choose_action(GameState.from_mcp_state(raw), ctx))
        worst = max(worst, time.perf_counter() - t)
        b = unwrap(champ.choose_action(GameState.from_mcp_state(raw), ctx))
        n += 1
        ok = _legal(raw, a)
        illegal += not ok
        la, lb, lp = _label(raw, a), _label(raw, b), _label(raw, rec.get("out"))
        same_champ += la == lb
        same_played += la == lp
        by_singles += cand.pk.singles_why is not None
        if verbose or not ok:
            obs = raw.get("observation") or {}
            A, O = obs.get("active_pokemon") or {}, obs.get("opponent_active_pokemon") or {}
            where = f"t{obs.get('turn')} {A.get('species')} {100 * (A.get('current_hp_fraction') or 0):.0f}% vs " \
                    f"{O.get('species')} {100 * (O.get('current_hp_fraction') or 0):.0f}%" if raw.get("phase") == "moving" \
                else raw.get("phase")
            print(f"  {'' if ok else 'ILLEGAL '}{where:42s} ours {la:22s} v3 {lb:22s} played {lp:22s} "
                  f"| {cand.pk.singles_why or ''}")
    return {"file": f, "decisions": n, "illegal": illegal, "same_as_champion": same_champ, "same_as_played": same_played,
            "singles_policy": by_singles, "max_ms": 1000 * worst}


# ---------------------------------------------------------------- 3: damage model
def protocol_hits(lines: list[str]) -> list[dict]:
    """Direct, single, non-critical hits from a singles protocol log: attacker / defender (side, nickname, species),
    the move, the defender's HP before and the damage, plus both sides' status. Hits under boosts, weather, terrain,
    screens, multi-hit moves or a Substitute are left out (the check compares plain calcs)."""
    hp: dict = {}
    species: dict = {}
    boosted: set = set()
    status: dict = {}
    items_gone: set = set()
    field: set = set()
    active: dict = {}
    hits: list[dict] = []
    cur = None

    def who(tok: str):
        tok = tok.strip()
        return (tok[:2], tok.split(":", 1)[1].strip()) if len(tok) > 3 and tok[0] == "p" and ":" in tok else (None, None)

    def parse_hp(s: str):
        s = s.split(" ")[0]
        if s in ("0", "0fnt") or "/" not in s:
            return (0, None)
        a, b = s.split("/")
        return (int(a), int(b))

    for ln in lines:
        parts = ln.split("|")
        if len(parts) < 2:
            continue
        tag = parts[1]
        if tag in ("move", "turn", "", "upkeep") and cur is not None:
            if cur.get("damage") is not None and not cur.get("skip"):
                hits.append(cur)
            cur = None
        if tag in ("switch", "drag", "replace") and len(parts) > 4:
            k = who(parts[2])
            species[k] = parts[3].split(",")[0]
            active[k[0]] = k
            boosted.discard(k)
            h = parse_hp(parts[4])
            hp[k] = (h[0], h[1] or (hp.get(k) or (0, 0))[1])
            st = parts[4].split(" ")[1] if " " in parts[4] else None
            status[k] = st
        elif tag in ("-boost", "-unboost", "-setboost", "-clearnegativeboost", "-swapboost", "-copyboost",
                     "-invertboost"):
            boosted.add(who(parts[2]))
        elif tag in ("-clearboost",):
            boosted.discard(who(parts[2]))
        elif tag == "-clearallboost":
            boosted.clear()
        elif tag == "-status" and len(parts) > 3:
            status[who(parts[2])] = parts[3]
        elif tag == "-curestatus":
            status[who(parts[2])] = None
        elif tag == "-enditem":
            items_gone.add(who(parts[2]))
        elif tag == "-weather" and len(parts) > 2:
            field.discard("weather")
            if parts[2] != "none":
                field.add("weather")
        elif tag in ("-fieldstart", "-fieldend") and len(parts) > 2:
            (field.add if tag == "-fieldstart" else field.discard)(parts[2])
        elif tag in ("-sidestart", "-sideend") and len(parts) > 3:
            name = parts[3].replace("move: ", "")
            if name in ("Reflect", "Light Screen", "Aurora Veil"):
                (field.add if tag == "-sidestart" else field.discard)(parts[2][:2] + name)
        elif tag == "move" and len(parts) > 4:
            atk, dfn = who(parts[2]), who(parts[4])
            skip = any(p.startswith("[") and p not in ("[notarget]",) for p in parts[5:]) or not dfn[0] \
                or atk[0] == dfn[0] or atk in boosted or dfn in boosted or bool(field)
            cur = {"attacker": atk, "defender": dfn, "atk_species": species.get(atk), "def_species": species.get(dfn),
                   "move": parts[3], "hp_before": hp.get(dfn), "damage": None, "skip": skip,
                   "atk_status": status.get(atk), "def_status": status.get(dfn),
                   "atk_item_gone": atk in items_gone, "def_item_gone": dfn in items_gone}
        elif tag in ("-crit", "-hitcount") and cur is not None:
            cur["skip"] = True
        elif tag == "-activate" and cur is not None and "Substitute" in ln:
            cur["skip"] = True
        if tag in ("-damage", "-heal", "-sethp") and len(parts) > 3:
            k = who(parts[2])
            before = hp.get(k)
            h = parse_hp(parts[3])
            hp[k] = (h[0], h[1] or (before or (0, 0))[1])
            if tag == "-damage" and cur is not None and cur["defender"] == k and cur["damage"] is None \
                    and not any(p.startswith("[from]") for p in parts[4:]) and before:
                cur["damage"] = before[0] - h[0]
                cur["hp_before"] = before
    if cur is not None and cur.get("damage") is not None and not cur.get("skip"):
        hits.append(cur)
    return hits


def damage_check(f: str, verbose: bool = False) -> dict:
    """Observed damage of each clean direct hit vs the calculator range for the singles policy's assumed sets."""
    from agent.pokemon import dex
    from agent.pokemon import singles as S
    from agent.pokemon.calc import DamageCalc, calc_pokemon, move_name
    from agent.pokemon.dex import to_id
    from agent.pokemon.policy import PokemonPolicy
    recs = [json.loads(line) for line in open(f)]
    moving = [r for r in recs if (r["raw"].get("observation") or {}).get("protocol_log")]
    if not moving:
        return {"file": f, "hits": 0, "inside": 0}
    last = max(moving, key=lambda r: len(r["raw"]["observation"]["protocol_log"]))
    st, obs = last["raw"], last["raw"]["observation"]
    mode = S.mode_of(st, obs)
    mine = S.my_side_id(obs)
    levels = S.log_levels(obs)
    pol = PokemonPolicy()
    team = {to_id(v.get("species", "")): v for v in (obs.get("team") or {}).values() if isinstance(v, dict)}
    hits = protocol_hits(obs["protocol_log"])
    calcs, keep = [], []
    for h in hits:
        if not h["hp_before"] or not h["hp_before"][1] or not h["atk_species"] or not h["def_species"]:
            continue
        if not dex.move(to_id(h["move"])) or dex.move(to_id(h["move"])).get("category") == "Status":
            continue
        views, sets = [], []
        for side, sp, stat, gone, hpb in ((h["attacker"][0], h["atk_species"], h["atk_status"], h["atk_item_gone"], None),
                                          (h["defender"][0], h["def_species"], h["def_status"], h["def_item_gone"],
                                           h["hp_before"])):
            sid = to_id(sp)
            base = team.get(sid) or {"species": sid}
            v = {"species": sid, "boosts": {}, "status": stat,
                 "current_hp_fraction": (hpb[0] / hpb[1]) if hpb else 1.0}
            if side == mine:
                v.update(item=None if gone else base.get("item"), ability=base.get("ability"), moves=base.get("moves"))
                s = S.own_set(pol, mode, v, levels)
            else:
                v.update(item=None if gone else "unknown_item", ability=None, moves=[])
                s, _ = S.foe_set(pol, mode, v, levels, obs)
                if gone:
                    s = dict(s, item=None)
            views.append(v)
            sets.append(s)
        calcs.append({"attacker": calc_pokemon(S._cv(views[0]), sets[0]), "defender": calc_pokemon(S._cv(views[1]), sets[1]),
                      "move": move_name(to_id(h["move"])), "field": {"gameType": "Singles"}})
        keep.append(h)
    res = DamageCalc.get().batch(calcs) if calcs else []
    rows = []
    for h, r in zip(keep, res or []):
        if not r or not r.get("rolls"):
            continue
        lo, hi = min(r["rolls"]), max(r["rolls"])
        cap = h["hp_before"][0]
        ko = h["damage"] >= cap                      # a KO only shows the HP that was left: max roll must reach it
        ok = hi >= cap if ko else lo <= h["damage"] <= hi
        rows.append((ok, ko, f"{h['atk_species']}->{h['def_species']} {h['move']}: {h['damage']}"
                             f"{' (KO)' if ko else ''} vs [{lo},{hi}]"))
    if verbose:
        for ok, ko, s in rows:
            print(f"  {'ok ' if ok else 'OUT'} {s}")
    exact = [ok for ok, ko, _ in rows if not ko]
    kos = [ok for ok, ko, _ in rows if ko]
    return {"file": f, "mode": mode, "hits": len(exact), "inside": sum(exact), "ko_hits": len(kos),
            "ko_consistent": sum(kos), "outside": [s for ok, _, s in rows if not ok]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    files = singles_logs()
    bad = 0
    for f in files:
        print(f)
        d = replay_decisions(f, a.verbose)
        print(f"decisions {d['decisions']}: illegal {d['illegal']} | by the singles policy {d['singles_policy']} | "
              f"same as v3 {d['same_as_champion']} | same as played {d['same_as_played']} | max {d['max_ms']:.0f} ms")
        c = damage_check(f, a.verbose)
        print(f"damage model ({c.get('mode')}): {c['inside']}/{c['hits']} recorded non-KO hits inside the calc's "
              f"[min, max]; {c.get('ko_consistent', 0)}/{c.get('ko_hits', 0)} KOs reachable"
              + (f"; outside: {c['outside']}" if c.get("outside") else ""))
        bad += d["illegal"]
    print(f"{len(files)} singles logs, {bad} illegal answers")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
