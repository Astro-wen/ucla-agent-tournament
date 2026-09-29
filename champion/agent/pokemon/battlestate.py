"""Platform observation -> the battle description the engine search rebuilds (agent/js/search.js `build`).

Platform-legal information only (docs/real_payloads.md):
  * our four Pokemon: exact HP, status, boosts, item as shown in `team` / `active_pokemon`, full sets from our draft;
  * the foe: HP out of 100, status and boosts from `opponent_active_pokemon` / `opponent_team`, full sets from the
    draft cache (`PokemonPolicy.opp_cards`); which four they brought only as far as they have appeared (the protocol
    log's switch-ins, plus every foe we saw on the field); the rest is filled in from the team-preview prior
    (`lineup_prior`: the preview matrix game's equilibrium mix over their lineups, conditioned on what was revealed);
  * everything else from the protocol log (agent/pokemon/battlelog.py): items consumed, turns left of every field /
    side condition, Protect chains, Fake Out availability, Choice lock, Taunt, Encore, Yawn, confusion, Leech Seed,
    Protosynthesis / Quark Drive, Supreme Overlord, sleep / toxic counters, Wish.
Without a protocol log the conditions shown in the observation get default durations (half their length, or the
policy's own Tailwind / Trick Room tracking) and no volatile state.

`build()` raises ValueError when the observation cannot be turned into a consistent battle (the caller then plays its
classic pick); it never reads anything the platform does not send.
"""
from __future__ import annotations

import math

from . import battlelog, dex
from .dex import to_id

FORMAT = "gen9vgc2025regi@@@Terastal Clause"
BASE_TURNS = {"tailwind": 4, "trickroom": 5, "reflect": 5, "lightscreen": 5, "auroraveil": 5, "safeguard": 5,
              "mist": 5, "gravity": 5, "magicroom": 5, "wonderroom": 5, "luckychant": 5}
HAZARDS = {"spikes", "toxicspikes", "stealthrock", "stickyweb"}
EXTENDER = {"reflect": "lightclay", "lightscreen": "lightclay", "auroraveil": "lightclay"}
WEATHER_ROCK = {"raindance": "damprock", "sunnyday": "heatrock", "sandstorm": "smoothrock", "snowscape": "icyrock"}
PRIMAL = {"desolateland", "primordialsea", "deltastream"}
VOL_TURNS = {"taunt": 3, "encore": 3, "disable": 4, "yawn": 2, "healblock": 5, "throatchop": 2, "magnetrise": 5,
             "charge": 2, "embargo": 5}
SIMPLE_VOLATILES = {"substitute", "saltcure", "focusenergy", "torment", "aquaring", "ingrain", "flashfire", "curse",
                    "tarshot", "healblock", "throatchop", "magnetrise", "charge", "embargo"}
STATUSES = {"brn", "par", "slp", "frz", "psn", "tox"}
CHOICE_ITEMS = {"choicescarf", "choiceband", "choicespecs"}


def _status(x) -> str:
    s = to_id(str(x or ""))
    return s if s in STATUSES else ""


def _frac(v: dict | None) -> float | None:
    v = v or {}
    f = v.get("current_hp_fraction")
    try:
        if f is not None:
            return max(0.0, min(1.0, float(f)))
        if v.get("max_hp"):
            return max(0.0, min(1.0, float(v.get("current_hp") or 0) / float(v["max_hp"])))
    except (TypeError, ValueError):
        return None
    return None


def _fainted(v: dict | None) -> bool:
    v = v or {}
    return bool(v.get("fainted")) or to_id(str(v.get("status") or "")) == "fnt" \
        or (v.get("current_hp") == 0 and v.get("max_hp") not in (None, 0))


def _boosts(v: dict | None) -> dict:
    b = (v or {}).get("boosts")
    if not isinstance(b, dict):
        return {}
    out = {}
    for k in ("atk", "def", "spa", "spd", "spe", "accuracy", "evasion"):
        try:
            if b.get(k):
                out[k] = int(b[k])
        except (TypeError, ValueError):
            pass
    return out


def showdown_set(card: dict | None, species: str, view: dict | None = None) -> dict:
    """A drafted card (display names) -> a Showdown set; a species we hold no card for gets a generic set (its view's
    revealed moves, else one STAB attack per type; no item)."""
    card = card or {}
    view = view or {}
    name = card.get("species") or (dex.species(species).get("name") if species else None) or species
    moves = [m for m in (card.get("moves") or []) if m][:4]
    if not moves:
        moves = [m for m in (view.get("moves") or []) if m][:4]
    if not moves:
        from .policy import _guess_moves           # lazy: policy imports this module
        moves = _guess_moves(to_id(species))[:4] or ["tackle"]
    ability = card.get("ability") or view.get("ability")
    if not ability:
        abil = (dex.species(species).get("abilities") or {})
        ability = abil.get("0") if isinstance(abil, dict) else None
    out = {"name": name, "species": name, "item": card.get("item") or "", "ability": ability or "",
           "nature": card.get("nature") or "Hardy", "level": int(card.get("level") or 50),
           "evs": dict(card.get("evs") or ({} if card else {"hp": 4, "atk": 252, "spa": 252, "spe": 252}) or {}),
           "ivs": dict(card.get("ivs") or {}), "moves": list(moves), "gender": ""}
    return out


def _card_for(store: dict, sid: str) -> dict | None:
    if sid in store:
        return store[sid]
    from .preview import resolve_card
    return resolve_card(sid, store)


def _choose_unrevealed(revealed: list[str], roster: list[str], need: int, prior) -> list[str]:
    """The `need` unrevealed foe Pokemon: the most likely lineup of the preview prior that contains every revealed one,
    else by marginal probability of being brought, else roster order."""
    cands = [s for s in roster if s not in revealed]
    if need <= 0 or not cands:
        return []
    rev = set(revealed)
    best, best_p = None, -1.0
    marg: dict[str, float] = {}
    for lu, p in prior or []:
        s = [to_id(x) for x in lu]
        for x in s:
            marg[x] = marg.get(x, 0.0) + float(p)
        if rev <= set(s) and float(p) > best_p:
            best, best_p = s, float(p)
    out = [x for x in (best or []) if x not in rev and x in cands][:need]
    rest = sorted((c for c in cands if c not in out), key=lambda c: (-marg.get(c, 0.0), cands.index(c)))
    return (out + rest)[:need]


class _Ctx:
    def __init__(self, log: battlelog.Log | None):
        self.log = log
        self.upk = log.upkeeps if log is not None else 0


def _mon_state(ctx: _Ctx, side: str, sid: str, card: dict | None, view: dict | None, active: bool,
               exact_hp: bool, fresh: bool | None = None) -> dict:
    """One Pokemon's description (see search.js applyMon). `fresh` (no protocol log only): did it enter since our
    previous decision (Fake Out still works)?"""
    view = view or {}
    lm = ctx.log.mons.get((side, sid)) if ctx.log is not None else None
    fainted = _fainted(view) or bool(lm and lm.fainted and not view)
    st = {"fainted": fainted}
    if exact_hp and view.get("current_hp") is not None and view.get("max_hp"):
        try:
            st["hp"] = int(view["current_hp"])
        except (TypeError, ValueError):
            st["hp_frac"] = _frac(view)
    else:
        f = _frac(view)
        if f is None and lm is not None and lm.hp_frac is not None:
            f = lm.hp_frac
        st["hp_frac"] = 1.0 if f is None else f
    status = _status(view.get("status"))
    if not status and not view and lm is not None:
        status = _status(lm.status)
    st["status"] = status
    # item: what the view shows (ours; a foe's once revealed; the platform clears a lost one to "" / None), else the
    # log (consumed / knocked off / changed), else the drafted card's
    vi = view.get("item", "__absent__")
    if vi not in ("__absent__", "unknown_item"):
        item = to_id(vi or "")
    elif lm is not None and lm.item:
        item = lm.item
    elif lm is not None and lm.item_lost:
        item = ""
    else:
        item = to_id((card or {}).get("item") or "")
    st["item"] = item
    ab = view.get("ability") or (lm.ability if lm is not None else None)
    if ab and to_id(ab) != to_id((card or {}).get("ability") or ""):
        st["ability"] = to_id(ab)
    if status == "slp":
        k = lm.slp_cants if lm is not None else 0
        opts = [s - k for s in (2, 3, 4) if s - k >= 1] or [1]
        st["status_time"] = max(1, int(round(sum(opts) / len(opts))))
    if status == "tox" and active and lm is not None:
        st["tox_stage"] = lm.tox_ticks
    if active and not fainted:
        st["boosts"] = _boosts(view)
        if lm is not None and lm.pos is not None and lm.pos.startswith(side):
            st.update(_volatiles(ctx, lm, item, (card or {}).get("ability") or ab))
        elif ctx.log is None and fresh is not None:
            st["move_actions"] = 0 if fresh else 1
            st["active_turns"] = 0 if fresh else 1
    return st


def _left(total: int, up: int, now: int) -> int:
    return total - (now - up)


def _volatiles(ctx: _Ctx, lm: battlelog.Mon, item: str, ability) -> dict:
    upk = ctx.upk
    vols = []
    if lm.stall > 0 and upk - lm.stall_up <= 1:
        vols.append({"id": "stall", "counter": min(729, 3 ** lm.stall), "duration": 2 - (upk - lm.stall_up)})
    if item in CHOICE_ITEMS and lm.last_move:
        vols.append({"id": "choicelock", "move": lm.last_move})
    for vid, info in lm.volatiles.items():
        up = info.get("up", upk)
        if vid in ("protosynthesis", "quarkdrive"):
            v = {"id": vid, "fromBooster": bool(info.get("booster"))}
            if info.get("stat"):
                v["bestStat"] = info["stat"]
            vols.append(v)
        elif vid in ("taunt", "encore", "disable", "yawn"):
            total = VOL_TURNS[vid] + (1 if vid in ("taunt", "encore") and info.get("moved") else 0)
            left = _left(total, up, upk)
            if left < 1:
                continue
            v = {"id": vid, "duration": left}
            if vid in ("encore", "disable"):
                mv = info.get("move") or (lm.last_move if vid == "encore" else None)
                if not mv:
                    continue
                v["move"] = mv
            vols.append(v)
        elif vid == "confusion":
            vols.append({"id": "confusion", "time": max(1, 3 - int(info.get("turns") or 0))})
        elif vid == "leechseed":
            v = {"id": "leechseed"}
            if info.get("source_pos"):
                v["source_pos"] = info["source_pos"]
            vols.append(v)
        elif vid == "perishsong":
            vols.append({"id": "perishsong", "duration": max(1, int(info.get("left") or 3) - (upk - up))})
        elif vid in SIMPLE_VOLATILES:
            v = {"id": vid}
            if vid in VOL_TURNS:
                left = _left(VOL_TURNS[vid], up, upk)
                if left < 1:
                    continue
                v["duration"] = left
            vols.append(v)
    if to_id(ability or "") == "unburden" and item == "" and lm.item_lost:
        vols.append({"id": "unburden"})
    if lm.glaive_up >= 0 and lm.moved_up == lm.glaive_up and upk - lm.glaive_up <= 1:
        vols.append({"id": "glaiverush"})
    out = {"volatiles": vols, "move_actions": lm.actions, "active_turns": max(0, upk - lm.switch_up)}
    if lm.last_move:
        out["last_move"] = lm.last_move
    if lm.fallen:
        out["fallen"] = lm.fallen
    return out


def _conditions(ctx: _Ctx, side: str, items: dict, obs_conds, cond_left: dict | None, key: str) -> list[dict]:
    out = []
    if ctx.log is not None and ctx.log.lines:
        for cond, info in ctx.log.sides.get(side, {}).items():
            if cond in HAZARDS:
                out.append({"id": cond, "layers": int(info.get("layers") or 1)})
                continue
            base = BASE_TURNS.get(cond)
            if base is None:
                continue
            if EXTENDER.get(cond) and items.get(tuple(info.get("setter") or ())) == EXTENDER[cond]:
                base = 8
            left = _left(base, info.get("up", ctx.upk), ctx.upk)
            if left >= 1:
                out.append({"id": cond, "duration": left})
        return out
    for c in _ids(obs_conds):
        if c in HAZARDS:
            out.append({"id": c, "layers": 1})
        elif c in BASE_TURNS:
            left = (cond_left or {}).get(f"{key}_{c}") or math.ceil(BASE_TURNS[c] / 2)
            out.append({"id": c, "duration": int(left)})
    return out


def _ids(x) -> list[str]:
    if isinstance(x, dict):
        return [to_id(str(k)) for k in x]
    if isinstance(x, (list, tuple, set)):
        return [to_id(str(k)) for k in x]
    if isinstance(x, str) and x:
        return [to_id(x)]
    return []


def _field(ctx: _Ctx, obs: dict, items: dict, cond_left: dict | None) -> dict:
    out = {"weather": None, "terrain": None, "pseudo": []}
    log = ctx.log
    if log is not None and log.lines:
        if log.weather:
            w = log.weather["id"]
            if w in PRIMAL:
                out["weather"] = {"id": w, "duration": 0}
            else:
                base = 8 if WEATHER_ROCK.get(w) and items.get(tuple(log.weather.get("setter") or ())) == WEATHER_ROCK[w] else 5
                out["weather"] = {"id": w, "duration": max(1, _left(base, log.weather["up"], ctx.upk))}
        if log.terrain:
            base = 8 if items.get(tuple(log.terrain.get("setter") or ())) == "terrainextender" else 5
            out["terrain"] = {"id": log.terrain["id"], "duration": max(1, _left(base, log.terrain["up"], ctx.upk))}
        for pw, info in log.pseudo.items():
            base = BASE_TURNS.get(pw, 5)
            left = _left(base, info["up"], ctx.upk)
            if left >= 1:
                out["pseudo"].append({"id": pw, "duration": left})
        return out
    w = obs.get("weather")
    w = next(iter(w), None) if isinstance(w, dict) else w
    wid = battlelog.WEATHERS.get(to_id(str(w or "")))
    if wid:
        out["weather"] = {"id": wid, "duration": 0 if wid in PRIMAL else 3}
    for f in _ids(obs.get("fields")):
        if f in battlelog.TERRAINS:
            out["terrain"] = {"id": f, "duration": 3}
        elif f in BASE_TURNS:
            left = (cond_left or {}).get(f) or math.ceil(BASE_TURNS[f] / 2)
            out["pseudo"].append({"id": f, "duration": int(left)})
    return out


def build(obs: dict, tmpl: dict, my_cards: dict, opp_cards: dict, *, opp_roster: list | None = None,
          lineup_prior: list | None = None, seen_foes=(), classic: dict | None = None, seed: list | None = None,
          cond_left: dict | None = None, prev_mine=None, prev_foe=None) -> dict:
    """The search.js `state` for one doubles decision. Raises ValueError when inconsistent.

    opp_roster: the foe's six species (team preview); lineup_prior: [(four species, probability)] from the preview
    game; seen_foes: every foe species seen on the field this battle; classic: the classic policy's pick (always one
    of the searched rows); cond_left / prev_mine / prev_foe: the policy's own trackers, used only without a protocol
    log (Tailwind / Trick Room turns left; who was on the field at our previous decision, for Fake Out)."""
    me = battlelog.my_side(obs)
    lines = obs.get("protocol_log")
    log = battlelog.parse(lines) if isinstance(lines, list) and lines else None
    if me is None:
        raise ValueError("our side id is unknown (no `team` keys)")
    foe = "p2" if me == "p1" else "p1"
    ctx = _Ctx(log)

    # ---- our side: the battle team (4), actives first
    team = obs.get("team")
    if not isinstance(team, dict) or not team:
        raise ValueError("no `team` in the observation")
    ours = {}
    for k, v in team.items():
        if not isinstance(v, dict):
            continue
        sid = to_id(v.get("species") or str(k).split(":", 1)[-1])
        if sid:
            ours[sid] = v
    actives = list(obs.get("active_pokemon") or [None, None]) + [None, None]
    order = []
    for i in range(2):
        a = actives[i]
        sid = to_id((a or {}).get("species") or "")
        if not sid and log is not None:
            sid = (log.active.get(f"{me}{'ab'[i]}") or ("", ""))[1]
        if not sid or sid not in ours or sid in order:
            sid = next((s for s in ours if s not in order and _fainted(ours[s])), None)
        if not sid:
            raise ValueError(f"our position {i} is unknown")
        order.append(sid)
    order += [s for s in ours if s not in order]
    my_mons = []
    for n, sid in enumerate(order):
        card = _card_for(my_cards, sid)
        view = dict(ours[sid])
        if n < 2 and isinstance(actives[n], dict) and to_id(actives[n].get("species") or "") == sid:
            view.update({k: v for k, v in actives[n].items() if v is not None or k == "item"})
        st = _mon_state(ctx, me, sid, card, view, n < 2, exact_hp=True,
                        fresh=None if prev_mine is None else sid not in [to_id(x or "") for x in prev_mine])
        st["set"] = showdown_set(card, sid, view)
        my_mons.append(st)

    # ---- the foe: revealed first (actives in front), the rest from the preview prior
    foe_views = {}
    ot = obs.get("opponent_team")
    for k, v in (ot.items() if isinstance(ot, dict) else []):
        if isinstance(v, dict):
            foe_views[to_id(v.get("species") or str(k).split(":", 1)[-1])] = v
    opp_act = list(obs.get("opponent_active_pokemon") or [None, None]) + [None, None]
    revealed = list(log.revealed.get(foe, [])) if log is not None else []
    for s in list(seen_foes or []) + [to_id((o or {}).get("species") or "") for o in opp_act[:2]]:
        if s and s not in revealed:
            revealed.append(s)
    roster = [to_id(s) for s in (opp_roster or [])] or [to_id(k) for k in opp_cards]
    roster = [s for s in roster if s]
    for s in revealed:
        if s not in roster:
            roster.append(s)
    n_foe = 4
    need = n_foe - len(revealed)
    foe_list = revealed[:n_foe] + _choose_unrevealed(revealed, roster, need, lineup_prior)
    if len(foe_list) < 2:
        raise ValueError("fewer than two foe Pokemon known")
    f_order = []
    for i in range(2):
        o = opp_act[i]
        sid = to_id((o or {}).get("species") or "")
        if not sid and log is not None:
            sid = (log.active.get(f"{foe}{'ab'[i]}") or ("", ""))[1]
        if not sid or sid not in foe_list or sid in f_order:
            sid = next((s for s in foe_list if s not in f_order and _fainted(foe_views.get(s))), None) \
                or next((s for s in foe_list if s not in f_order), None)
        if not sid:
            raise ValueError(f"foe position {i} is unknown")
        f_order.append(sid)
    f_order += [s for s in foe_list if s not in f_order]
    opp_mons = []
    known = set(revealed)
    for n, sid in enumerate(f_order):
        card = _card_for(opp_cards, sid)
        view = dict(foe_views.get(sid) or {}) if sid in known else {}
        if n < 2 and isinstance(opp_act[n], dict) and to_id(opp_act[n].get("species") or "") == sid:
            view.update(opp_act[n])
        st = _mon_state(ctx, foe, sid, card, view if sid in known else None, n < 2, exact_hp=False,
                        fresh=None if prev_foe is None else sid not in [to_id(x or "") for x in prev_foe])
        st["set"] = showdown_set(card, sid, view)
        opp_mons.append(st)

    # ---- conditions (items of the setters decide Light Clay / Damp Rock extensions)
    items = {}
    for side, mons in ((me, my_mons), (foe, opp_mons)):
        for m in mons:
            items[(side, to_id(m["set"]["species"]))] = to_id(m["set"].get("item") or "")
    fs = obs.get("force_switch")
    forced = [bool(x) for x in fs] if isinstance(fs, list) else [bool(fs), False]
    forced = (forced + [False, False])[:2]
    request = "switch" if any(forced) else "move"
    for i in range(2):
        if forced[i]:
            my_mons[i]["switch_flag"] = True
    if request == "switch" and (log is None or log.after_upkeep):
        alive_bench = sum(1 for m in opp_mons[2:] if not m["fainted"])
        for i in range(2):
            if opp_mons[i]["fainted"] and alive_bench > 0:
                opp_mons[i]["switch_flag"] = True
    sides = {
        me: {"mons": my_mons, "conditions": _conditions(ctx, me, items, obs.get("side_conditions"), cond_left, "my")},
        foe: {"mons": opp_mons,
              "conditions": _conditions(ctx, foe, items, obs.get("opponent_side_conditions"), cond_left, "opp")},
    }
    if log is not None:
        for pos, info in log.wish.items():
            side = pos[:2]
            d = 2 - (ctx.upk - info["up"])
            if side in sides and d >= 1 and len(pos) == 3:
                sides[side].setdefault("wish", []).append({"position": "ab".index(pos[2]), "duration": d,
                                                          "wisher": info["wisher"][1]})
    options = []
    slots = {s.get("slot"): s for s in (tmpl.get("slots") or []) if isinstance(s, dict)}
    for i in range(2):
        s = slots.get(i)
        opts = []
        for o in (s or {}).get("options") or [{"type": "pass"}]:
            if not isinstance(o, dict):
                continue
            t = o.get("type")
            if t == "move":
                opts.append({"type": "move", "move_id": o.get("move_id"), "targets": list(o.get("targets") or [0])})
            elif t == "switch":
                opts.append({"type": "switch", "species": o.get("species")})
            elif t == "pass":
                opts.append({"type": "pass"})
        options.append(opts or [{"type": "pass"}])
    turn = obs.get("turn")
    try:
        turn = int(turn)
    except (TypeError, ValueError):
        turn = log.turn if log is not None else 1
    return {"format": FORMAT, "me": me, "turn": max(1, turn), "request": request, "sides": sides,
            "field": _field(ctx, obs, items, cond_left), "options": options, "classic": classic,
            "seed": seed or [11, 22, 33, 44],
            "meta": {"log_lines": log.lines if log is not None else 0, "revealed": len(revealed),
                     "filled": [s for s in f_order if s not in known]}}
