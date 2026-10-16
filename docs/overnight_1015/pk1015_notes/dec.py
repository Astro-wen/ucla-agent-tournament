import json, glob, sys, ast
LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
for sess in sys.argv[1:]:
    print("=====", sess)
    for l in open(glob.glob(f"{LOG}/{sess}-*.jsonl")[0]):
        r = json.loads(l)
        d = r.get("decision")
        o = r.get("out")
        raw = r.get("raw")
        if isinstance(raw, str):
            try: raw = ast.literal_eval(raw)
            except Exception: raw = {}
        ph = (raw or {}).get("observation", {}).get("phase") if isinstance((raw or {}).get("observation"), dict) else None
        if ph == "draft":
            print("DRAFT", str(o)[:80], "|", r.get("reasoning_summary", "")[:100])
            continue
        if isinstance(d, str):
            try: d = ast.literal_eval(d)
            except Exception: pass
        turn = (raw or {}).get("observation", {}).get("turn") if isinstance((raw or {}).get("observation"), dict) else None
        print(f"T{turn} {ph}", json.dumps(d, default=str)[:900])
        print("   OUT", str(o)[:200])
