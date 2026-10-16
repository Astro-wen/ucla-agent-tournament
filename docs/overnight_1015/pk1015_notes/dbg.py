import json
from tests.strategy.test_search_sucker import sucker_state, FAST
from agent.pokemon import engine
e = engine.SearchEngine()
assert e.warm(60)
st, tmpl = sucker_state()
print(json.dumps(st["options"]))
mons = st["sides"][st["me"]]["mons"]
print([ (m["set"]["species"], m["set"].get("moves")) for m in mons])
for keep in (8, 30):
    ans = e.search(st, dict(FAST, samples=1, rng=3, mode="blend", fixed=1, hard_ms=60000, keep=keep, slot_keep=6, sucker_q=1.0), 60)
    print(keep, ans and ans["action"], ans and {k: ans["info"].get(k) for k in ("rows", "cols", "n_my", "sucker_rows", "mix")}, e.last_error)
e._kill()
