"""Harvest-income calibration: the same estimator over real recordings (logs/<session>-*.jsonl, read only) and over
simulated frames of our bot on the same map.

Estimator: invested wealth W(t) = cash + ore + cost of our buildings (not the yard) + cost of our units (not the MCV)
+ paid part of every queue item (progress x cost) + deaths_cost - 1100 x refineries ever built (their free harvester).
W(t2) - W(t1) = income in between (repairs and sales ignored: small early). Income per harvester-minute over windows.

    python calib.py real <session>...            # real recordings
    python calib.py sim <map> <opp> <seed> <seat> [ENV=VAL ...]   # one simulated match (code root: cwd / PYTHONPATH)
"""
import glob
import json
import os
import sys

ROOT = os.environ.get("PYTHONPATH", "/home/linmengfdm/wt/ra3").split(":")[0]
CFG = json.load(open(os.path.join(ROOT, "data/red_alert_config.json")))
R = CFG["rules"]


def cost(t):
    it = R["units"].get(t) or R["buildings"].get(t) or {}
    return it.get("cost", 0) or 0


def frames_real(sess):
    path = glob.glob(f"/home/linmengfdm/ucla-agent-tournament/logs/{sess}-*.jsonl")[0]
    out = []
    for line in open(path):
        if not line.strip():
            continue
        r = json.loads(line)
        st = r.get("raw") or {}
        if isinstance(st.get("observation"), dict):
            out.append(st)
    return out


def wealth(raw, procs_ever):
    ob = raw["observation"]
    eco = ob.get("economy") or {}
    mil = ob.get("military") or {}
    w = (eco.get("cash") or 0) + (eco.get("ore") or 0)
    for b in ob.get("buildings") or []:
        if b.get("type") != "fact":
            w += cost(b.get("type"))
            if b.get("type") == "proc":
                procs_ever.add(b.get("id"))
    for u in ob.get("units") or []:
        if u.get("type") != "mcv" and not u.get("wreck"):
            w += cost(u.get("type"))
    for p in ob.get("production") or []:
        w += cost(p.get("item")) * (1.0 if p.get("ready") else float(p.get("progress") or 0))
    w += mil.get("deaths_cost") or 0
    w -= 1100 * len(procs_ever)
    return w


def series(frames, label, window_s=60.0):
    procs = set()
    pts = []
    for raw in frames:
        ob = raw["observation"]
        t = (raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0
        eco = ob.get("economy") or {}
        harv = sum(1 for u in ob.get("units") or [] if u.get("type") == "harv")
        pts.append((t, wealth(raw, procs), harv, len(procs), (eco.get("cash") or 0) + (eco.get("ore") or 0),
                    len(ob.get("buildings") or [])))
    print(f"== {label}: {len(pts)} frames, {pts[-1][0]:.0f} s")
    print("   window      income  /min  harv(avg)  per-harv-min  cash_end  procs")
    i = 0
    rows = []
    while i < len(pts):
        j = i
        while j < len(pts) and pts[j][0] - pts[i][0] < window_s:
            j += 1
        if j >= len(pts):
            break
        t0, w0, h0, p0, c0, _ = pts[i]
        t1, w1, h1, p1, c1, _ = pts[j]
        hs = [pts[k][2] for k in range(i, j + 1)]
        hav = sum(hs) / len(hs)
        inc = w1 - w0
        pm = inc / ((t1 - t0) / 60.0)
        phm = pm / hav if hav > 0 else float("nan")
        print(f"   {t0:5.0f}-{t1:5.0f}  {inc:7.0f} {pm:6.0f}  {hav:5.2f}      {phm:7.0f}     {c1:6d}   {p1}")
        rows.append((t0, t1, inc, hav, phm, c1))
        i = j
    return rows


def sim(map_id, opp, seed, seat, envs):
    for e in envs:
        k, val = e.split("=", 1)
        os.environ[k] = val
    os.environ["ARENA_RECORD"] = "0"
    sys.path.insert(0, ROOT)
    import local.ra_platform as P
    import agent.redalert as RA
    import local.ra_sim as S
    frames = []
    orig = RA.RedAlertPolicy.decide
    last = [-10 ** 9]

    def decide(self, raw, config=None):
        try:
            tick = raw["observation"]["tick"]
            if tick - last[0] >= 250:
                last[0] = tick
                frames.append(json.loads(json.dumps(raw)))
        except Exception:
            pass
        return orig(self, raw, config)
    RA.RedAlertPolicy.decide = decide
    # direct instrument: harvester unload events (seat, tick, value)
    loads = []
    orig_tick = S.World._tick_harvester

    def tick_h(self, a):
        before = (a.hstate, a.cargo)
        orig_tick(self, a)
        if before[0] == "unloading" and a.hstate == "seek" and a.owner == want_seat[0]:
            loads.append((self.tick, a.id))
    want_seat = [seat]
    S.World._tick_harvester = tick_h
    import agent.agent as AA
    row = P.play_match(AA.create_agent(), P.make_factory(opp)(seed), seed=seed, map_id=map_id, a_seat=seat)
    print("result", row["result_a"], row["reason"], f"{row['game_s']:.0f}s", "first weap", row["a"].get("first_weap_s"))
    series(frames, f"sim {map_id} vs {opp} seed {seed} seat {seat} {' '.join(envs)}")
    if os.environ.get("CALIB_POS"):
        import math
        lastp = -999
        for raw in frames:
            ob = raw["observation"]
            t = ob.get("tick", 0) / 25.0
            if t - lastp < float(os.environ["CALIB_POS"]):
                continue
            lastp = t
            base = ob.get("base_center")
            procs = [(b["x"], b["y"]) for b in ob.get("buildings") or [] if b.get("type") == "proc"]
            hv = [(u["x"], u["y"], u.get("act"), u.get("idle")) for u in ob.get("units") or [] if u.get("type") == "harv"]
            print(f"   t={t:5.0f} procs {procs} harv {hv} dist "
                  f"{[round(math.hypot(x - base[0], y - base[1])) for x, y, _a, _i in hv]}")
    # cycle times per harvester
    by = {}
    for t, hid in loads:
        by.setdefault(hid, []).append(t / 25.0)
    cyc = []
    for hid, ts in sorted(by.items()):
        d = [b - a for a, b in zip(ts, ts[1:]) if b < 400]
        cyc += d
        print(f"   harv {hid}: {len(ts)} loads, first {ts[0]:.0f}s, cycle med "
              f"{sorted(d)[len(d)//2] if d else float('nan'):.1f}s")
    if cyc:
        cyc.sort()
        print(f"   all cycles (before 400 s): n={len(cyc)} median {cyc[len(cyc)//2]:.1f}s p25 {cyc[len(cyc)//4]:.1f} "
              f"p75 {cyc[3*len(cyc)//4]:.1f}")
    return frames


if __name__ == "__main__":
    if sys.argv[1] == "real":
        for s in sys.argv[2:]:
            fr = frames_real(s)
            ob0 = fr[1]["observation"] if len(fr) > 1 else fr[0]["observation"]
            series(fr, f"real {s} map {ob0.get('map', {}).get('id')} base {ob0.get('base_center')} "
                       f"{ob0.get('faction')} vs {ob0.get('enemy_faction')}")
    else:
        sim(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), sys.argv[6:])
