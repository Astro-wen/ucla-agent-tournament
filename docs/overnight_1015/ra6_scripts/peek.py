import json, sys, collections
path = sys.argv[1]
recs = [json.loads(x) for x in open(path) if x.strip()]
print(len(recs))
kc = collections.Counter()
for r in recs:
    kc[tuple(sorted(r.keys()))] += 1
for k, v in kc.most_common(10):
    print(v, k)
r = recs[5]
for k, v in r.items():
    s = json.dumps(v)
    print(k, s[:600])
# any record with "events" in our log
for r in recs:
    s = json.dumps(r)
    if "attack wave" in s or "retreat at" in s:
        print("FOUND", s[:300])
        break
