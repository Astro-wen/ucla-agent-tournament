import glob, json, sys
LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
sess = sys.argv[1]
recs = [json.loads(l) for l in open(glob.glob(f"{LOG}/{sess}-*.jsonl")[0]) if l.strip()]
print(len(recs))
for i, r in enumerate(recs[:3] + recs[-2:]):
    print("KEYS", list(r.keys()))
    print({k: str(v)[:300] for k, v in r.items() if k != "raw"})
    raw = r.get("raw") or {}
    print("RAW", list(raw.keys()))
    obs = raw.get("observation")
    if isinstance(obs, dict):
        print("OBS", {k: str(v)[:200] for k, v in obs.items()})
