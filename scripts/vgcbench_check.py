"""Faithfulness check of the VGC-Bench adapter (local/vgcbench_opp.py + local/vgcbench_worker.py).

    python scripts/vgcbench_check.py --opp random --games 30 [--model vgcbench_sp] [--seed 8800] [--out f.json]

Plays whole matches VGC-Bench vs --opp (random, simbot, or an agent package: v0, v1, champion, ...) on the local
env (game g: match seed --seed + g, VGC-Bench in seat g % 2) and, at
every VGC-Bench battle decision, compares what the policy saw (the worker's poke-env DoubleBattle, VGCBENCH_DEBUG
view) with the engine's ground truth at that moment:
  request   the request the worker was fed (bridge sideview) == the env's request for that seat
  state     turn; own team HP / max HP / status / item / ability / moves / boosts / active slots; opponent active
            slots, HP (public, out of 100: +-1), status, boosts, item / ability (known from Open Team Sheets);
            weather, terrain / pseudo-weather, both sides' side conditions
  mask      per slot, the actions VGC-Bench's action mask allows == the actions the env's template allows
            (differences are listed by kind; 'fainted-foe target' = poke-env keeps a fainted foe's slot targetable,
            Showdown retargets such a move to the other foe, the adapter normalises it)
  decode    the policy's action pair -> VGC-Bench's choice string -> platform dict -> the env's Showdown choice
            equals the choice string read directly against the request (move id -> move index, switch name -> party
            index, target kept)
It also counts the adapter's own events (fallbacks, retargets, header mismatches, unknown vocab) and win rates.
Mismatches are printed with the first examples; --out writes everything as JSON.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["ARENA_RECORD"] = "0"
os.environ["VGCBENCH_DEBUG"] = "1"

from local import pokemon_env as pe  # noqa: E402
from local import vgcbench_opp as vb  # noqa: E402
from local.showdown import Showdown  # noqa: E402

PACKAGES = {"v0": "versions.v0_heuristic", "v1": "versions.v1_exact_damage", "v2": "versions.v2_joint_lookahead",
            "v3": "versions.v3_draft_preview", "champion": "champion"}


def norm(x) -> str:
    return re.sub(r"[^a-z0-9]", "", str(x or "").lower())


class RecSD(Showdown):
    """The bridge, remembering the last snapshot the match got (the engine's truth at each decision)."""

    last: dict | None = None

    def _rpc(self, msg):
        r = super()._rpc(msg)
        if msg.get("cmd") in ("start", "choose") and "state" in r:
            self.last = r
        return r


class Checker:
    def __init__(self):
        self.n = Counter()
        self.bad: dict[str, list] = defaultdict(list)

    def miss(self, kind: str, detail) -> None:
        self.n[f"mismatch:{kind}"] += 1
        if len(self.bad[kind]) < 6:
            self.bad[kind].append(detail)

    def check(self, player, raw, req, res, out) -> None:
        sd = player.match.sd
        snap = sd.last
        sid = player.sid
        seat = 0 if sid == "p1" else 1
        st = snap["state"]
        self.n["decisions"] += 1
        truth_req = snap["requests"].get(sid)
        if json.dumps(truth_req, sort_keys=True) != json.dumps(req, sort_keys=True):
            self.miss("request", {"turn": st["turn"]})
        v = res.get("view") or {}
        if not v:
            self.miss("no_view", {"turn": st["turn"]})
            return
        self.state(v, st, sid, req)
        self.mask(v, res.get("masks") or [], raw, st, sid)
        self.decode(player, res.get("choice") or "", out, req, raw, st, seat, sid)

    # ---- state -------------------------------------------------------------------------------------------------
    def state(self, v, st, sid, req) -> None:
        me, opp = st[sid], st["p2" if sid == "p1" else "p1"]
        t = st["turn"]
        self.n["checked:turn"] += 1
        if v["turn"] != t:
            self.miss("turn", {"view": v["turn"], "truth": t})
        vt = {m["species"]: m for m in v["team"]}
        for p in me["team"]:
            if p is None:
                continue
            m = vt.get(norm(p["id"])) or vt.get(norm(p["species"]))
            if m is None:
                if any(norm(p["species"]) in x["details"].lower().replace(" ", "").replace("-", "")
                       for x in req["side"]["pokemon"]):
                    self.miss("own_missing", {"turn": t, "species": p["id"]})
                continue
            self.n["checked:own_mon"] += 1
            for key, a, b in (("hp", m["hp"], p["current_hp"]), ("max_hp", m["max_hp"], p["max_hp"]),
                              ("status", norm(m["status"]) if not p["fainted"] else "", norm(p["status"])
                               if not p["fainted"] else ""),
                              ("fainted", m["fainted"], p["fainted"]),
                              ("item", norm(m["item"]), norm(p.get("item"))),
                              ("ability", norm(m["ability"]), norm(p.get("ability")))):
                if a != b:
                    self.miss(f"own_{key}", {"turn": t, "species": p["id"], "view": a, "truth": b})
            if set(m["moves"]) != {norm(x) for x in p.get("moves") or []}:
                self.miss("own_moves", {"turn": t, "species": p["id"], "view": sorted(m["moves"]),
                                        "truth": sorted(p.get("moves") or [])})
            if p["active"] and not p["fainted"]:
                if {k: v2 for k, v2 in m["boosts"].items() if v2} != {k: v2 for k, v2 in p["boosts"].items() if v2}:
                    self.miss("own_boosts", {"turn": t, "species": p["id"], "view": m["boosts"], "truth": p["boosts"]})
        truth_act = [norm(p["id"]) if p and not p["fainted"] else None for p in me["active"]]
        if [norm(x) if x else None for x in v["active"]] != truth_act:
            self.miss("own_active", {"turn": t, "view": v["active"], "truth": truth_act})
        truth_oact = [norm(p["id"]) if p and not p["fainted"] else None for p in opp["active"]]
        if [norm(x) if x else None for x in v["opp_active"]] != truth_oact:
            self.miss("opp_active", {"turn": t, "view": v["opp_active"], "truth": truth_oact})
        vo = {m["species"]: m for m in v["opp_team"]}
        for p in opp["active"]:
            if not p or p["fainted"]:
                continue
            m = vo.get(norm(p["id"]))
            self.n["checked:opp_active_mon"] += 1
            if m is None:
                self.miss("opp_mon_missing", {"turn": t, "species": p["id"]})
                continue
            if abs(100 * m["hp_frac"] - 100 * p["current_hp_fraction"]) > 1.01:
                self.miss("opp_hp", {"turn": t, "species": p["id"], "view": m["hp_frac"],
                                     "truth": round(p["current_hp_fraction"], 4)})
            if norm(m["status"]) != norm(p["status"]):
                self.miss("opp_status", {"turn": t, "species": p["id"], "view": m["status"], "truth": p["status"]})
            if {k: x for k, x in m["boosts"].items() if x} != {k: x for k, x in p["boosts"].items() if x}:
                self.miss("opp_boosts", {"turn": t, "species": p["id"], "view": m["boosts"], "truth": p["boosts"]})
            if norm(m["item"]) != norm(p.get("item")):
                self.miss("opp_item", {"turn": t, "species": p["id"], "view": m["item"], "truth": p.get("item")})
            if norm(m["ability"]) != norm(p.get("ability")):
                self.miss("opp_ability", {"turn": t, "species": p["id"], "view": m["ability"],
                                          "truth": p.get("ability")})
        self.n["checked:field"] += 1
        vw = {norm(k) for k in v["weather"]}
        tw = {norm(st["weather"])} if st["weather"] else set()
        if vw != tw:
            self.miss("weather", {"turn": t, "view": sorted(vw), "truth": sorted(tw)})
        vf = {norm(k) for k in v["fields"]}
        tf = {norm(x) for x in st["pseudo_weather"]} | ({norm(st["terrain"])} if st["terrain"] else set())
        if vf != tf:
            self.miss("fields", {"turn": t, "view": sorted(vf), "truth": sorted(tf)})
        for key, view_sc, side in (("side_conditions", v["side_conditions"], me),
                                   ("opp_side_conditions", v["opp_side_conditions"], opp)):
            a, b = {norm(k) for k in view_sc}, {norm(k) for k in side["side_conditions"]}
            if a != b:
                self.miss(key, {"turn": t, "view": sorted(a), "truth": sorted(b)})

    # ---- action mask -------------------------------------------------------------------------------------------
    def mask(self, v, masks, raw, st, sid) -> None:
        if not masks:
            self.miss("no_mask", {"turn": st["turn"]})
            return
        m = masks[-1]
        slots = raw["legal_actions"]["actions"][0]["input"]["action"]["slots"]
        team = [x["species"] for x in v["team"]]
        opp = st["p2" if sid == "p1" else "p1"]
        fainted_foe = {i + 1 for i, p in enumerate(opp["active"]) if not p or p["fainted"]}
        for slot in slots:
            i = slot["slot"]
            allowed = {a - 107 * i for a in m if 107 * i <= a < 107 * (i + 1)}
            act = v["active"][i] if i < len(v["active"]) else None
            mon = next((x for x in v["team"] if x["species"] == act), None)
            known = list(mon["moves"])[:4] if mon else []
            avail = v["available_moves"][i] if i < len(v["available_moves"]) else []
            if len(avail) == 1 and avail[0] not in known:
                known = avail
            got = set()
            for a in allowed:
                if a == 0:
                    got.add(("pass",))
                elif a <= 6:
                    got.add(("switch", team[a - 1] if a - 1 < len(team) else f"#{a}"))
                elif a <= 26:
                    k, tgt = (a - 7) // 5, (a - 7) % 5 - 2
                    got.add(("move", known[k] if k < len(known) else f"#{k}", tgt))
                else:
                    got.add(("gimmick", a))
            want = set()
            for o in slot["options"]:
                if o["type"] == "pass":
                    want.add(("pass",))
                elif o["type"] == "switch":
                    want.add(("switch", o["species"]))
                else:
                    for tgt in o["targets"]:
                        want.add(("move", o["move_id"], tgt))
            self.n["checked:mask_slot"] += 1
            extra, missing = got - want, want - got
            kinds = set()
            for e in extra:
                if e[0] == "move" and e[2] in fainted_foe:
                    kinds.add("mask_extra:fainted_foe_target")
                elif e[0] == "move" and any(w[0] == "move" and w[1] == e[1] and w[2] == 0 for w in want):
                    kinds.add("mask_extra:target_on_untargeted_move")
                else:
                    kinds.add("mask_extra:other")
            for e in missing:
                kinds.add(f"mask_missing:{e[0]}")
            for k in kinds:
                self.miss(k, {"turn": st["turn"], "slot": i, "extra": sorted(map(str, extra)),
                              "missing": sorted(map(str, missing))})

    # ---- decoding round trip -------------------------------------------------------------------------------------
    def decode(self, player, choice, out, req, raw, st, seat, sid) -> None:
        self.n["checked:decode"] += 1
        _, tmpl = player.match._battle_state(seat, sid, req, st, 0)
        env_choice = pe._to_choice(out, req, tmpl)
        s = choice[len("/choose "):] if choice.startswith("/choose ") else choice
        direct = []
        opp = st["p2" if sid == "p1" else "p1"]
        alive_foes = [i + 1 for i, p in enumerate(opp["active"]) if p and not p["fainted"]]
        for i, part in enumerate(x.strip() for x in s.split(",")):
            toks = part.split()
            if toks[0] == "pass":
                direct.append("pass")
            elif toks[0] == "switch":
                name = " ".join(toks[1:])
                j = next(j for j, p in enumerate(req["side"]["pokemon"]) if p["ident"].split(": ", 1)[1] == name)
                direct.append(f"switch {j + 1}")
            else:
                rest = [x for x in toks[1:] if x not in vb.GIMMICKS]
                tgt = int(rest[-1]) if len(rest) > 1 and re.fullmatch(r"[+-]?\d+", rest[-1]) else 0
                mid = rest[0]
                moves = req["active"][i]["moves"]
                k = int(mid) - 1 if mid.isdigit() else next(k for k, mv in enumerate(moves) if mv["id"] == mid)
                locked = "target" not in moves[k]
                if locked:
                    tgt = 0                                   # Showdown ignores a locked move's target
                elif tgt > 0 and tgt not in alive_foes and len(alive_foes) == 1:
                    tgt = alive_foes[0]                       # Showdown retargets from a fainted foe's slot
                direct.append(f"move {k + 1}" + (f" {tgt}" if tgt else ""))
        if env_choice != ", ".join(direct):
            self.miss("decode", {"turn": st["turn"], "choice": choice, "out": out, "env": env_choice,
                                 "direct": ", ".join(direct)})


def opponent(name: str):
    if name == "random":
        return pe.RandomPlayer()
    from local import ext_opp
    if name in ext_opp.NAMES:                   # simbot / simbot_v1 (engine search; they bind to the match too)
        return ext_opp.make(name)
    pkg = PACKAGES.get(name, name)
    mod = importlib.import_module(f"{pkg}.agent.agent")
    return pe.ContestantPlayer(mod.create_agent, name)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--opp", default="random")
    ap.add_argument("--model", default="vgcbench_sp", choices=vb.NAMES)
    ap.add_argument("--games", type=int, default=10)
    ap.add_argument("--seed", type=int, default=8800)
    ap.add_argument("--same-team", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ok, why = vb.available()
    if not ok:
        raise SystemExit(why)
    chk = Checker()
    bot = vb.VGCBench(a.model)
    t0 = time.time()
    print(f"worker start {bot.start()} in {time.time() - t0:.0f} s", flush=True)
    bot.hook = chk.check
    opp = opponent(a.opp)
    wins, turns, rows = 0, 0, []
    totals = Counter()
    ms = []
    with RecSD() as sd:
        for g in range(a.games):
            seat = g % 2
            players = [bot, opp] if seat == 0 else [opp, bot]
            r = pe.play_match(sd, players[0], players[1], seed=a.seed + g, same_team=a.same_team)
            win = r.winner == seat
            wins += win
            turns += r.turns
            totals.update({k: v for k, v in bot.stats.items() if isinstance(v, (int, float)) and k != "ms_max"})
            ms += [t["ms"] for t in bot.trace if t.get("phase") == "moving"]
            rows.append({"g": g, "seat": seat, "win": win, "turns": r.turns, "invalid": r.invalid,
                         "stats": dict(bot.stats)})
            print(f"g{g} seat {seat} {'WIN ' if win else 'loss'} turns {r.turns:3d} invalid {r.invalid} "
                  f"fallbacks {bot.stats['fallbacks']} retargeted {bot.stats['retargeted']} "
                  f"header_mismatch {bot.stats['header_mismatch']}", flush=True)
    vb.shutdown()
    ms.sort()
    lat = {"n": len(ms), "mean": round(sum(ms) / max(1, len(ms)), 1),
           "p95": ms[int(0.95 * (len(ms) - 1))] if ms else None, "max": ms[-1] if ms else None}
    print(f"\n{a.model} vs {a.opp}: {wins}/{a.games} wins, avg turns {turns / max(1, a.games):.1f}; "
          f"latency {lat}")
    print("adapter counters:", dict(totals))
    print("checked:", {k: v for k, v in chk.n.items() if not k.startswith("mismatch")})
    mm = {k: v for k, v in chk.n.items() if k.startswith("mismatch")}
    print("mismatches:", mm or "none")
    for k, ex in chk.bad.items():
        print(f"  {k}: {json.dumps(ex[:3])[:900]}")
    if a.out:
        Path(a.out).write_text(json.dumps({"model": a.model, "opp": a.opp, "games": a.games, "wins": wins,
                                           "turns": turns, "latency": lat, "counters": dict(totals),
                                           "checked": dict(chk.n), "examples": chk.bad, "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
