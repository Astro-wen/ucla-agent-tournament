"""Find invalid / rejected action records in real RA recordings (read only)."""
import glob
import json
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
for s in sys.argv[1:]:
    p = glob.glob(f"{LOG}/{s}*.jsonl")[0]
    lines = open(p).read().splitlines()
    print(s, "records", len(lines))
    first = json.loads(lines[0])
    print("  keys", list(first.keys()))
    for i, l in enumerate(lines):
        low = l.lower()
        if "invalid" in low:
            r = json.loads(l)
            for k, v in r.items():
                vs = json.dumps(v)
                j = vs.lower().find("invalid")
                if j >= 0:
                    print(f"  rec {i} key {k}: ...{vs[max(0, j - 1200):j + 500]}")
