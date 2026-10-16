// Engine-search battle policy (ARENA_BATTLE=search): the Showdown simulator in the agent's own Node process.
//
// JSON lines on stdin / stdout, one request per line, answered in order:
//   {"id": n, "cmd": "warm"}                         -> {"id": n, "ok": true, "ms": ...}   (loads the engine + data)
//   {"id": n, "cmd": "search", "state": {...}, "params": {...}}
//        -> {"id": n, "action": {slot_0: {...}, slot_1: {...}}, "info": {...}}  or  {"id": n, "error": "..."}
//   {"id": n, "cmd": "quit"}
//
// `state` is built by agent/pokemon/battlestate.py from PLATFORM-LEGAL information only: our four Pokemon exactly,
// the foe's sets from the draft cache (the four they brought as far as revealed, the rest filled in from the
// team-preview prior), HP (the foe's from its percentage), status, boosts, field / side conditions with the turns left,
// and per-Pokemon volatile state read from the protocol log (agent/pokemon/battlelog.py). build() turns it into a live
// Battle: the two teams are started with the current actives in front, then every piece of state is overwritten and
// fresh requests are made. search() is the matrix-game search of local/js/simbot.js (the local `simbot` opponent,
// which clones the real battle instead): prune both sides' joint options on one-turn probes, play the rows x columns
// matrix on clones with common random numbers (the chosen turn + forced switches + one greedy turn), score by a
// static evaluation, solve by regret matching and sample our mix. Our rows come from the platform's own template
// (so the answer is always one of its options); the classic policy's pick is always one of the rows.
//
// How much work: with `fixed` (the agent's default, agent/pokemon/policy.py SEARCH_CONFIG) the search always does the
// same work (every option probed, `samples` samples of the matrix), so its answer depends only on the state and the
// `rng` seed, never on the machine's speed or load; `hard_ms` is only a safety net (past it the search stops early and
// answers with what it has). Without `fixed` the original time budget applies: pruning, the sample count and the
// sampling loop stop on elapsed time against `budget_ms` (faster machine = more samples).
//
// Local ablation only: {"cmd": "search", "battle_json": ..., "state": {...}} searches the given serialized battle (the
// local simulator's true state) instead of the reconstruction; the platform never sends it.
'use strict';
const path = require('path');
const readline = require('readline');

function loadShowdown() {
  // agent/js/node_modules (bundle) or NODE_PATH, then the development checkout's local/js copy
  const tries = [];
  if (process.env.ARENA_SHOWDOWN) tries.push(process.env.ARENA_SHOWDOWN);
  tries.push('pokemon-showdown', path.join(__dirname, 'node_modules', 'pokemon-showdown'),
    path.join(__dirname, '..', '..', 'local', 'js', 'node_modules', 'pokemon-showdown'));
  let last = null;
  for (const t of tries) {
    try { return require(t); } catch (e) { last = e; }
  }
  throw last || new Error('pokemon-showdown not found');
}

let PS = null;
function ps() {
  if (!PS) PS = loadShowdown();
  return PS;
}

const FORMAT = 'gen9vgc2025regi@@@Terastal Clause';
const NEEDS_TARGET = new Set(['normal', 'any', 'adjacentFoe', 'adjacentAlly', 'adjacentAllyOrSelf']);
const SPREAD = new Set(['allAdjacentFoes', 'allAdjacent']);
const PROTECT = new Set(['protect', 'detect', 'spikyshield', 'banefulbunker', 'silktrap', 'burningbulwark',
  'kingsshield', 'obstruct']);
const FIRST_TURN = new Set(['fakeout', 'firstimpression', 'matblock']);
const ALLY_HEAL = new Set(['pollenpuff']);
// moves that work only when the target attacks this turn (Sucker Punch fails into a status move, a Protect, a switch,
// a redirection or a faster priority move): the greedy column (the target always attacks) overrates them
const NEEDS_FOE_ATTACK = new Set(['suckerpunch', 'thunderclap', 'upperhand']);
const WIN = 8;
const DEFAULTS = {samples: 2, rollout: 1, keep: 8, slot_keep: 3, probes: 2, budget_ms: 2500, mode: 'nash',
  min_prob: 0.05, iters: 1500, margin: 0.6, rng: 1, delta: 0, blend_w: 0.5, guard: 0, guard_delta: 0.08,
  free_delta: 0.4, fixed: 0, hard_ms: 0, sucker_q: 0};

// ------------------------------------------------------------------------------------------------ helpers
function mulberry(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function toID(x) {
  return ps().toID(x);
}

function serialize(battle) {
  const st = battle.toJSON();
  st.log = [];
  st.inputLog = [];
  return JSON.stringify(st);
}

function clone(base, seed) {
  const {Battle, PRNG} = ps();
  const b = Battle.fromJSON(base);
  b.prng = new PRNG(seed);
  b.send = () => {};
  for (const sd of b.sides) sd.clearChoice();     // never keep (or read) a choice the other side already sent
  return b;
}

function speciesKey(p) {
  return toID(p.set && p.set.species || p.species.name);
}

function reqSpecies(rp) {
  return toID(rp.details.split(',')[0]);
}

function fainted(rp) {
  return / fnt$/.test(rp.condition) || rp.condition === '0 fnt';
}

function live(p) {
  return !!p && !p.fainted && p.hp > 0;
}

// ------------------------------------------------------------------------------------------------ reconstruction
function effectState(b, obj) {
  return b.initEffectState(obj);
}

function applyField(b, F) {
  const f = b.field;
  const src = b.sides[0].active[0];
  f.weather = '';
  f.weatherState = effectState(b, {id: ''});
  f.terrain = '';
  f.terrainState = effectState(b, {id: ''});
  f.pseudoWeather = {};
  if (!F) return;
  if (F.weather && F.weather.id) {
    const id = toID(F.weather.id);
    if (b.dex.conditions.get(id).exists) {
      f.weather = id;
      f.weatherState = effectState(b, {id, source: src, sourceSlot: src.getSlot(), duration: F.weather.duration || 0});
      if (!F.weather.duration) delete f.weatherState.duration;      // an ability weather without an end (primal)
    }
  }
  if (F.terrain && F.terrain.id) {
    const id = toID(F.terrain.id);
    if (b.dex.conditions.get(id).exists) {
      f.terrain = id;
      f.terrainState = effectState(b, {id, source: src, sourceSlot: src.getSlot(), duration: F.terrain.duration || 5});
    }
  }
  for (const pw of F.pseudo || []) {
    const id = toID(pw.id);
    if (!b.dex.conditions.get(id).exists) continue;
    f.pseudoWeather[id] = effectState(b, {id, source: src, sourceSlot: src.getSlot(), duration: pw.duration || 5});
  }
}

function applySide(b, side, S) {
  side.sideConditions = {};
  for (const c of S.conditions || []) {
    const id = toID(c.id);
    if (!b.dex.conditions.get(id).exists) continue;
    const st = {id, target: side, source: side.active[0], sourceSlot: side.active[0].getSlot()};
    if (c.duration) st.duration = c.duration;
    if (c.layers) st.layers = c.layers;
    side.sideConditions[id] = effectState(b, st);
  }
  for (let i = 0; i < side.slotConditions.length; i++) side.slotConditions[i] = {};
  for (const w of S.wish || []) {
    const i = w.position;
    if (!(i >= 0 && i < side.slotConditions.length)) continue;
    const wisher = side.pokemon.find(p => speciesKey(p) === toID(w.wisher)) || side.active[i];
    side.slotConditions[i].wish = effectState(b, {id: 'wish', target: side.active[i], source: wisher,
      sourceSlot: side.active[i].getSlot(), duration: w.duration || 1, hp: Math.floor(wisher.maxhp / 2)});
  }
}

const BOOST_KEYS = ['atk', 'def', 'spa', 'spd', 'spe', 'accuracy', 'evasion'];

function applyMon(b, p, m, isActive) {
  if (m.item !== null && m.item !== undefined) {
    p.item = toID(m.item);
    p.itemState = effectState(b, {id: p.item, target: p});
  }
  if (m.ability) {
    const ab = toID(m.ability);
    if (b.dex.abilities.get(ab).exists) {
      p.ability = ab;
      p.baseAbility = ab;
      p.abilityState = effectState(b, {id: ab, target: p});
    }
  }
  p.volatiles = {};
  p.boosts = {atk: 0, def: 0, spa: 0, spd: 0, spe: 0, accuracy: 0, evasion: 0};
  if (m.fainted) {
    p.hp = 0;
    p.fainted = true;
    p.status = '';
    p.statusState = effectState(b, {id: ''});
    p.isActive = false;
    p.isStarted = false;
    p.switchFlag = !!m.switch_flag;
    return;
  }
  p.fainted = false;
  let hp = typeof m.hp === 'number' ? m.hp : Math.round((typeof m.hp_frac === 'number' ? m.hp_frac : 1) * p.maxhp);
  p.hp = Math.max(1, Math.min(p.maxhp, hp));
  const st = m.status ? toID(m.status) : '';
  p.status = st && b.dex.conditions.get(st).exists ? st : '';
  p.statusState = effectState(b, {id: p.status, target: p});
  if (p.status === 'slp') {
    const t = Math.max(1, m.status_time || 2);
    p.statusState.startTime = t + 1;
    p.statusState.time = t;
  } else if (p.status === 'tox') {
    p.statusState.stage = Math.max(0, m.tox_stage || 0);
  }
  if (isActive) {
    for (const k of BOOST_KEYS) {
      const v = m.boosts && typeof m.boosts[k] === 'number' ? m.boosts[k] : 0;
      p.boosts[k] = Math.max(-6, Math.min(6, v));
    }
    for (const v of m.volatiles || []) {
      const id = toID(v.id);
      if (!id || !b.dex.conditions.get(id).exists) continue;
      const s = Object.assign({}, v);
      delete s.id;
      if (s.source_pos) {
        s.sourceSlot = s.source_pos;
        delete s.source_pos;
      }
      if (id === 'substitute' && !(s.hp > 0)) s.hp = Math.floor(p.maxhp / 4);
      p.volatiles[id] = effectState(b, Object.assign({id, name: id, target: p}, s));
    }
    if (typeof m.fallen === 'number' && m.fallen > 0 && p.ability === 'supremeoverlord') p.abilityState.fallen = m.fallen;
    p.activeTurns = Math.max(0, m.active_turns || 0);
    p.activeMoveActions = Math.max(0, m.move_actions || 0);
    p.lastMove = m.last_move ? b.dex.getActiveMove(m.last_move) : null;
    p.lastMoveUsed = p.lastMove;
    p.newlySwitched = false;
  } else {
    p.activeTurns = 0;
    p.activeMoveActions = 0;
    p.lastMove = null;
  }
  p.switchFlag = !!m.switch_flag;
}

// The start-of-turn bookkeeping of Battle.endTurn for the actives: disabled moves (Choice lock, Taunt, Encore,
// Assault Vest...) and trapping, so the requests below match the state.
function refreshActives(b) {
  for (const side of b.sides) {
    for (const p of side.active) {
      if (!p || p.fainted) continue;
      p.moveThisTurn = '';
      p.maybeDisabled = false;
      p.maybeLocked = false;
      for (const ms of p.moveSlots) {
        ms.disabled = false;
        ms.disabledSource = '';
      }
      b.runEvent('DisableMove', p);
      for (const ms of p.moveSlots) {
        const am = b.dex.getActiveMove(ms.id);
        b.singleEvent('DisableMove', am, null, p);
        if (am.flags['cantusetwice'] && p.lastMove && p.lastMove.id === ms.id) p.disableMove(p.lastMove.id);
      }
      p.trapped = p.maybeTrapped = false;
      b.runEvent('TrapPokemon', p);
      if (!p.knownType || b.dex.getImmunity('trapped', p)) b.runEvent('MaybeTrapPokemon', p);
    }
  }
}

function build(st) {
  const {Battle} = ps();
  const b = new Battle({formatid: st.format || FORMAT, seed: st.seed || [11, 22, 33, 44]});
  b.send = () => {};
  for (const sid of ['p1', 'p2']) b.setPlayer(sid, {name: sid, team: st.sides[sid].mons.map(m => m.set)});
  for (const sid of ['p1', 'p2']) {
    const n = st.sides[sid].mons.length;
    let order = '';
    for (let i = 1; i <= n; i++) order += String(i);
    if (!b.choose(sid, `team ${order}`)) throw new Error(`team order refused for ${sid}`);
  }
  if (b.ended) throw new Error('battle ended at start');
  b.queue.clear();
  b.turn = Math.max(1, st.turn || 1);
  applyField(b, st.field);
  for (let si = 0; si < 2; si++) {
    const side = b.sides[si];
    const S = st.sides[side.id];
    const byKey = new Map(S.mons.map((m, i) => [i, m]));
    side.pokemon.forEach((p, i) => {
      // side.pokemon keeps team order: the first two are the actives (positions a / b)
      applyMon(b, p, byKey.get(i) || {}, p.isActive || i < side.active.length);
    });
    side.pokemonLeft = side.pokemon.filter(p => !p.fainted).length;
    side.totalFainted = side.pokemon.filter(p => p.fainted).length;
    side.faintedLastTurn = null;
    side.faintedThisTurn = null;
    applySide(b, side, S);
  }
  refreshActives(b);
  b.midTurn = false;
  b.makeRequest(st.request === 'switch' ? 'switch' : 'move');
  return b;
}

// ------------------------------------------------------------------------------------------------ search context
class Ctx {
  constructor(battle, sideIdx, params, t0) {
    const {Battle} = ps();
    this.p = Object.assign({}, DEFAULTS, params || {});
    this.t0 = t0 || Date.now();
    this.base = serialize(battle);
    this.me = sideIdx;
    this.opp = 1 - sideIdx;
    this.rng = mulberry((this.p.rng || 1) >>> 0);
    this.sims = 0;
    this.bad = 0;
    this.est = Battle.fromJSON(this.base);           // damage estimates: fixed 92.5% roll, no crits
    this.est.send = () => {};
    this.est.randomizer = d => Math.floor(Math.floor(d * 925 / 10) / 100);
    this.est.randomChance = () => false;
    this.est.random = (m, n) => (m === undefined ? 0.5 : n === undefined ? Math.floor((m - 1) / 2) : Math.floor((m + n - 1) / 2));
    this.dmgCache = new Map();
  }

  seed() {
    return [1 + Math.floor(this.rng() * 65534), 1 + Math.floor(this.rng() * 65534),
      1 + Math.floor(this.rng() * 65534), 1 + Math.floor(this.rng() * 65534)];
  }

  dmg(att, moveId, def) {
    const ka = `${att.side.n}:${speciesKey(att)}:${moveId}:${def.side.n}:${speciesKey(def)}`;
    let d = this.dmgCache.get(ka);
    if (d !== undefined) return d;
    d = 0;
    try {
      const ea = this.est.sides[att.side.n].pokemon.find(x => speciesKey(x) === speciesKey(att));
      const ed = this.est.sides[def.side.n].pokemon.find(x => speciesKey(x) === speciesKey(def));
      const mv = this.est.dex.moves.get(moveId);
      if (ea && ed && mv.exists && mv.category !== 'Status') {
        const r = this.est.actions.getDamage(ea, ed, this.est.dex.getActiveMove(moveId), true);
        if (typeof r === 'number' && r > 0) {
          let hits = 1;
          if (Array.isArray(mv.multihit)) {
            hits = ea.hasItem('loadeddice') ? 4 : ea.hasAbility('skilllink') ? mv.multihit[1] : 3.1;
          } else if (typeof mv.multihit === 'number') hits = mv.multihit;
          const acc = mv.accuracy === true ? 1 : (mv.accuracy || 100) / 100;
          d = r * hits * acc;
        }
      }
    } catch (e) {
      d = 0;
    }
    this.dmgCache.set(ka, d);
    return d;
  }

  hitValue(att, moveId, def) {
    if (!live(def)) return 0;
    const d = this.dmg(att, moveId, def);
    if (d <= 0) return 0;
    return Math.min(d, def.hp) / def.maxhp + (d >= def.hp ? 0.4 : 0);
  }

  elapsed() {
    return Date.now() - this.t0;
  }

  // the safety net: past it the search stops mid-sample (both budgets)
  hard() {
    return this.p.hard_ms || 1.8 * this.p.budget_ms;
  }

  // what the time-based stops (pruning, the sample count, the sampling loop) compare against: the time budget, or,
  // with a fixed amount of work, only the safety net
  soft() {
    return this.p.fixed ? this.hard() : this.p.budget_ms;
  }
}

// ------------------------------------------------------------------------------------------------ options
// Engine-side options of side `sd` (the opponent, and any side inside the play-outs).
function slotOptions(b, sd, req) {
  const res = [];
  const n = sd.active.length;
  const bench = [];
  req.side.pokemon.forEach((rp, j) => { if (!rp.active && !fainted(rp)) bench.push(j + 1); });
  const sw = j => ({str: `switch ${j}`, kind: 'switch', j, species: reqSpecies(req.side.pokemon[j - 1])});
  if (req.forceSwitch) {
    const need = req.forceSwitch.filter(Boolean).length;
    for (let i = 0; i < n; i++) {
      if (!req.forceSwitch[i]) { res.push([{str: 'pass', kind: 'pass'}]); continue; }
      const o = bench.map(sw);
      if (!o.length || bench.length < need) o.push({str: 'pass', kind: 'pass'});
      res.push(o);
    }
    return res;
  }
  for (let i = 0; i < n; i++) {
    const a = req.active && req.active[i];
    const rp = req.side.pokemon[i];
    const mon = sd.active[i];
    if (!a || !rp || !live(mon) || fainted(rp)) { res.push([{str: 'pass', kind: 'pass'}]); continue; }
    const o = [];
    const allyPos = 1 - i;
    const allyOk = live(sd.active[allyPos]);
    const foes = [0, 1].filter(j => live(sd.foe.active[j]));
    a.moves.forEach((m, k) => {
      if (m.disabled) return;
      const base = {kind: 'move', move: m.id, k, i};
      if (!('target' in m) || !NEEDS_TARGET.has(m.target)) {
        o.push(Object.assign({str: `move ${k + 1}`, target: 0}, base));
      } else if (m.target === 'adjacentAlly') {
        if (allyOk) o.push(Object.assign({str: `move ${k + 1} ${-(allyPos + 1)}`, target: -(allyPos + 1)}, base));
      } else if (m.target === 'adjacentAllyOrSelf') {
        const t = allyOk ? -(allyPos + 1) : -(i + 1);
        o.push(Object.assign({str: `move ${k + 1} ${t}`, target: t}, base));
      } else {
        for (const j of (foes.length ? foes : [0])) o.push(Object.assign({str: `move ${k + 1} ${j + 1}`, target: j + 1}, base));
      }
    });
    if (!a.trapped) for (const j of bench) o.push(Object.assign(sw(j), {i}));
    if (!o.length) o.push({str: 'pass', kind: 'pass'});
    res.push(o);
  }
  return res;
}

// One platform choice for our slot i -> an engine option (with `out`, the platform dict), or null.
function mapChoice(b, sd, req, i, c) {
  if (!c || !c.type) return null;
  if (c.type === 'pass') return {str: 'pass', kind: 'pass', i, out: {type: 'pass'}};
  if (c.type === 'switch') {
    const want = toID(c.species);
    let j = -1;
    req.side.pokemon.forEach((rp, k) => { if (j < 0 && !rp.active && !fainted(rp) && reqSpecies(rp) === want) j = k + 1; });
    if (j < 0) return null;
    return {str: `switch ${j}`, kind: 'switch', j, species: want, i, out: {type: 'switch', species: c.species}};
  }
  if (c.type !== 'move' || !req.active || !req.active[i]) return null;
  const moves = req.active[i].moves;
  const k = moves.findIndex(m => m.id === toID(c.move_id));
  if (k < 0 || moves[k].disabled) return null;
  const t = typeof c.target === 'number' ? c.target : 0;
  const locked = !('target' in moves[k]);
  const str = locked || !t ? `move ${k + 1}` : `move ${k + 1} ${t}`;
  return {str, kind: 'move', move: moves[k].id, k, i, target: locked ? 0 : t,
    out: {type: 'move', move_id: c.move_id, target: t}};
}

// Our per-slot options: the platform template's options, targets pruned like slotOptions (single-target attacks at
// live foes, ally moves at the ally), mapped to the engine's request.
function rootSlots(b, sd, req, tmplSlots) {
  const res = [];
  const n = sd.active.length;
  const foes = [0, 1].filter(j => live(sd.foe.active[j]));
  for (let i = 0; i < n; i++) {
    const opts = (tmplSlots[i] || []);
    const out = [];
    const seen = new Set();
    const add = (c) => {
      const o = mapChoice(b, sd, req, i, c);
      if (o && !seen.has(o.str)) { seen.add(o.str); out.push(o); }
    };
    for (const opt of opts) {
      if (opt.type === 'move') {
        const targets = Array.isArray(opt.targets) && opt.targets.length ? opt.targets : [0];
        const mv = b.dex.moves.get(opt.move_id);
        const tt = mv.exists ? mv.target : 'normal';
        const allyPos = 1 - i;
        const allyOk = live(sd.active[allyPos]);
        let want;
        if (!NEEDS_TARGET.has(tt) || targets.every(t => t === 0)) want = [targets[0]];
        else if (tt === 'adjacentAlly') want = targets.filter(t => t === -(allyPos + 1) && allyOk);
        else if (tt === 'adjacentAllyOrSelf') want = [allyOk ? -(allyPos + 1) : -(i + 1)].filter(t => targets.includes(t));
        else {
          want = targets.filter(t => t > 0 && foes.includes(t - 1));
          if (ALLY_HEAL.has(mv.id) && allyOk) {
            const ally = sd.active[allyPos];
            if (ally.hp < ally.maxhp * 0.75) want.push(...targets.filter(t => t === -(allyPos + 1)));
          }
          if (!want.length) want = targets.filter(t => t > 0).slice(0, 1);
        }
        if (!want.length) want = [targets[0]];
        for (const t of want) add({type: 'move', move_id: opt.move_id, target: t});
      } else if (opt.type === 'switch') {
        // A switch the platform offers but the rebuilt battle cannot make: our four Pokemon are wrong there, so
        // do not search that battle at all (the classic pick plays). Dropping the option instead left, e.g., only
        // "pass, pass" after a double faint (the platform refuses it) or searched without that switch.
        if (!mapChoice(b, sd, req, i, {type: 'switch', species: opt.species})) {
          throw new Error(`no engine Pokemon for slot ${i}'s switch to ${toID(opt.species)}`);
        }
        add({type: 'switch', species: opt.species});
      } else {
        add({type: 'pass'});
      }
    }
    if (!out.length) {
      const names = opts.map(o => (o.type === 'move' ? o.move_id : o.type));
      throw new Error(`no engine option for slot ${i} (${names.join(', ') || 'empty'})`);
    }
    res.push(out);
  }
  return res;
}

// Showdown's pair rule for OUR joint rows (the platform rejects anything else): never the same switch-in on both
// slots, and a slot passes only when no switch-in its platform template offers is left after the other slot's choice.
// The template decides, not the rebuilt request: a bench Pokemon the reconstruction lacks is still on the platform
// (09-29 f525d6ac: both actives fainted, the last Pokemon missing from the rebuilt team -> "pass, pass", rejected).
function jointOk(parts, tmplSlots) {
  if (parts.length < 2) return true;
  const [a, c] = parts;
  if (a.kind === 'switch' && c.kind === 'switch' && (a.j === c.j || a.species === c.species)) return false;
  for (let i = 0; i < 2; i++) {
    if (parts[i].kind !== 'pass') continue;
    const other = parts[1 - i];
    const taken = other.kind === 'switch' ? other.species : null;
    if (((tmplSlots || [])[i] || []).some(o => o.type === 'switch' && toID(o.species) !== taken)) return false;
  }
  return true;
}

// tmplSlots (our root rows only): drop the pairs jointOk rejects.
function jointOptions(slots, tmplSlots) {
  if (slots.length === 1) return slots[0].map(a => ({str: a.str, parts: [a]}));
  const out = [];
  for (const a of slots[0]) {
    for (const c of slots[1]) {
      if (a.kind === 'switch' && c.kind === 'switch' && a.j === c.j) continue;
      if (tmplSlots && !jointOk([a, c], tmplSlots)) continue;
      out.push({str: `${a.str}, ${c.str}`, parts: [a, c]});
    }
  }
  return out;
}

function optValue(ctx, b, sd, o) {
  if (o.kind !== 'move') return null;
  const mv = b.dex.moves.get(o.move);
  if (!mv.exists || mv.category === 'Status') return null;
  const att = sd.active[o.i];
  if (!live(att)) return 0;
  if (FIRST_TURN.has(o.move) && att.activeMoveActions > 0) return 0;
  const foes = sd.foe.active.filter(live);
  if (SPREAD.has(mv.target)) {
    const mult = foes.length > 1 ? 0.75 : 1;
    let v = 0;
    for (const x of foes) v += mult * ctx.hitValue(att, o.move, x);
    const ally = sd.active[1 - o.i];
    if (mv.target === 'allAdjacent' && live(ally)) v -= 1.2 * mult * ctx.hitValue(att, o.move, ally);
    return v;
  }
  if (o.target > 0) return ctx.hitValue(att, o.move, sd.foe.active[o.target - 1]);
  if (o.target === 0 && foes.length) {
    let v = 0;
    for (const x of foes) v += ctx.hitValue(att, o.move, x);
    return v / foes.length;
  }
  return 0;
}

function greedy(ctx, b, sd, req, slots, avoid, protectSlot) {
  slots = slots || slotOptions(b, sd, req);
  const parts = [];
  const used = new Set();
  for (let i = 0; i < slots.length; i++) {
    const opts = slots[i];
    let best = null;
    if (req.forceSwitch) {
      best = autoSwitchPick(ctx, b, sd, opts, used);
    } else {
      if (protectSlot === i) best = opts.find(o => o.kind === 'move' && PROTECT.has(o.move)) || null;
      if (!best && avoid) best = bestAttack(ctx, b, sd, opts, avoid);
      if (!best) best = bestAttack(ctx, b, sd, opts, null);
      if (!best) best = opts.find(o => o.kind === 'move' && neutralStatus(b, sd, o)) || opts.find(o => o.kind === 'move') || opts[0];
    }
    if (best.kind === 'switch') used.add(best.j);
    parts.push(best);
  }
  return {str: parts.map(x => x.str).join(', '), parts};
}

function bestAttack(ctx, b, sd, opts, avoid) {
  let best = null, bv = -1;
  for (const o of opts) {
    if (o.kind !== 'move' || (avoid && o.target === avoid)) continue;
    const v = optValue(ctx, b, sd, o);
    if (v !== null && v > bv) { bv = v; best = o; }
  }
  return best;
}

function neutralStatus(b, sd, o) {
  if (PROTECT.has(o.move)) return false;
  if (o.move === 'trickroom' && b.field.pseudoWeather.trickroom) return false;
  if (o.move === 'tailwind' && sd.sideConditions.tailwind) return false;
  return true;
}

function autoSwitchPick(ctx, b, sd, opts, used) {
  let best = null, bv = -Infinity;
  for (const o of opts) {
    if (o.kind !== 'switch' || used.has(o.j)) continue;
    const mon = sd.pokemon.find(p => speciesKey(p) === o.species);
    let off = 0, def = 0;
    if (mon) {
      for (const f of sd.foe.active.filter(live)) {
        for (const ms of mon.moveSlots) off = Math.max(off, ctx.hitValue(mon, ms.id, f));
        for (const ms of f.moveSlots) def = Math.max(def, ctx.dmg(f, ms.id, mon) / Math.max(1, mon.hp));
      }
    }
    const v = off - 0.7 * Math.min(def, 1.5);
    if (v > bv) { bv = v; best = o; }
  }
  return best || opts.find(o => o.kind === 'pass') || opts[0];
}

function threat(ctx, b, sd, i) {
  const me = sd.active[i];
  if (!live(me)) return 0;
  let t = 0;
  for (const f of sd.foe.active.filter(live)) {
    for (const ms of f.moveSlots) t = Math.max(t, ctx.dmg(f, ms.id, me) / Math.max(1, me.hp));
  }
  return t;
}

function probes(ctx, b, sd, req, slots) {
  const out = [];
  const add = j => { if (j && !out.some(x => x.str === j.str)) out.push(j); };
  const g = greedy(ctx, b, sd, req, slots);
  add(g);
  if (req.forceSwitch) return out;
  let ti = -1, tv = 0.45;
  for (let i = 0; i < slots.length; i++) {
    const can = slots[i].some(o => o.kind === 'move' && PROTECT.has(o.move));
    const th = threat(ctx, b, sd, i);
    if (can && th > tv) { tv = th; ti = i; }
  }
  if (ti >= 0) add(greedy(ctx, b, sd, req, slots, null, ti));
  const tg = g.parts.map(x => x.target).filter(t => t > 0);
  if (tg.length) add(greedy(ctx, b, sd, req, slots, tg[0], null));
  return out.slice(0, Math.max(1, ctx.p.probes));
}

// ------------------------------------------------------------------------------------------------ evaluation
function physical(p) {
  let ph = 0, sp = 0;
  for (const ms of p.moveSlots) {
    const m = p.battle.dex.moves.get(ms.id);
    if (m.category === 'Physical') ph++;
    else if (m.category === 'Special') sp++;
  }
  return ph >= sp;
}

const STATUS_COST = {slp: 0.25, frz: 0.3, par: 0.12, psn: 0.05, tox: 0.1};

function sideScore(b, sd) {
  let s = 0;
  for (const p of sd.pokemon) {
    if (!live(p)) continue;
    let v = 0.35 + 0.65 * p.hp / p.maxhp;
    if (p.status === 'brn') v -= physical(p) ? 0.15 : 0.05;
    else if (STATUS_COST[p.status]) v -= STATUS_COST[p.status];
    if (p.isActive) {
      const bo = p.boosts;
      const phys = physical(p);
      v += 0.08 * Math.max(-3, Math.min(3, phys ? bo.atk : bo.spa));
      v += 0.05 * Math.max(-3, Math.min(3, bo.spe));
      v += 0.03 * Math.max(-3, Math.min(3, bo.def)) + 0.03 * Math.max(-3, Math.min(3, bo.spd));
      v += 0.03 * Math.max(-3, Math.min(3, bo.accuracy)) + 0.03 * Math.max(-3, Math.min(3, bo.evasion));
      if (p.volatiles.substitute) v += 0.08;
      if (p.volatiles.leechseed) v -= 0.05;
      if (p.volatiles.confusion) v -= 0.05;
    }
    s += v;
  }
  const sc = sd.sideConditions;
  const dur = k => (sc[k] ? Math.max(0, sc[k].duration || 0) : 0);
  s += 0.07 * dur('tailwind') + 0.035 * dur('reflect') + 0.035 * dur('lightscreen') + 0.06 * dur('auroraveil');
  return s;
}

function speedEdge(b, me, op) {
  const a = me.pokemon.filter(live), c = op.pokemon.filter(live);
  if (!a.length || !c.length) return 0;
  let e = 0;
  for (const x of a) for (const y of c) e += Math.sign(x.storedStats.spe - y.storedStats.spe);
  return e / (a.length * c.length);
}

function evaluate(ctx, b) {
  const me = b.sides[ctx.me], op = b.sides[ctx.opp];
  if (b.ended) {
    if (!b.winner) return 0;
    return b.winner === me.name ? WIN + sideScore(b, me) : -WIN - sideScore(b, op);
  }
  let v = sideScore(b, me) - sideScore(b, op);
  const tr = b.field.pseudoWeather.trickroom;
  if (tr) v -= 0.12 * Math.max(0, tr.duration || 0) * speedEdge(b, me, op);
  return v;
}

// ------------------------------------------------------------------------------------------------ simulation
function pending(b) {
  return b.sides.filter(sd => sd.activeRequest && !sd.activeRequest.wait);
}

function settle(ctx, b) {
  for (let guard = 0; guard < 8 && !b.ended; guard++) {
    const sw = pending(b).filter(sd => sd.activeRequest.forceSwitch);
    if (!sw.length) return;
    for (const sd of sw) {
      const g = greedy(ctx, b, sd, sd.activeRequest);
      if (!b.choose(sd.id, g.str)) b.choose(sd.id, 'default');
    }
  }
}

function greedyTurn(ctx, b) {
  for (const sd of pending(b)) {
    const g = greedy(ctx, b, sd, sd.activeRequest);
    if (!b.choose(sd.id, g.str)) b.choose(sd.id, 'default');
  }
}

function sim(ctx, mine, theirs, seed, rollout) {
  ctx.sims++;
  try {
    const b = clone(ctx.base, seed);
    const me = b.sides[ctx.me], op = b.sides[ctx.opp];
    if (mine !== null && !b.choose(me.id, mine)) {
      if (!ctx.chooseErr) ctx.chooseErr = `ours "${mine}": ${me.choice.error}`;
      return null;
    }
    if (theirs !== null && !b.choose(op.id, theirs)) {
      if (!ctx.chooseErr) ctx.chooseErr = `theirs "${theirs}": ${op.choice.error}`;
      return null;
    }
    settle(ctx, b);
    for (let r = 0; r < rollout && !b.ended; r++) {
      greedyTurn(ctx, b);
      settle(ctx, b);
    }
    return evaluate(ctx, b);
  } catch (e) {
    ctx.bad++;
    if (!ctx.lastError) ctx.lastError = String(e && e.stack || e).slice(0, 400);
    return null;
  }
}

// ------------------------------------------------------------------------------------------------ matrix game
function solve(A, iters) {
  const R = A.length, C = A[0].length;
  const x = new Array(R).fill(1 / R), y = new Array(C).fill(1 / C);
  const rr = new Array(R).fill(0), rc = new Array(C).fill(0);
  const sx = new Array(R).fill(0), sy = new Array(C).fill(0);
  const norm = (r, out) => {
    let s = 0;
    for (const v of r) s += v;
    for (let i = 0; i < r.length; i++) out[i] = s > 0 ? r[i] / s : 1 / r.length;
  };
  for (let t = 1; t <= iters; t++) {
    const ur = new Array(R).fill(0);
    let vr = 0;
    for (let i = 0; i < R; i++) { for (let j = 0; j < C; j++) ur[i] += A[i][j] * y[j]; vr += ur[i] * x[i]; }
    for (let i = 0; i < R; i++) rr[i] = Math.max(0, rr[i] + ur[i] - vr);
    norm(rr, x);
    const uc = new Array(C).fill(0);
    let vc = 0;
    for (let j = 0; j < C; j++) { for (let i = 0; i < R; i++) uc[j] += A[i][j] * x[i]; vc += uc[j] * y[j]; }
    for (let j = 0; j < C; j++) rc[j] = Math.max(0, rc[j] + vc - uc[j]);
    norm(rc, y);
    for (let i = 0; i < R; i++) sx[i] += t * x[i];
    for (let j = 0; j < C; j++) sy[j] += t * y[j];
  }
  norm(sx, x);
  norm(sy, y);
  let v = 0;
  for (let i = 0; i < R; i++) for (let j = 0; j < C; j++) v += x[i] * A[i][j] * y[j];
  return {x, y, value: v};
}

function probeScore(ctx, str, probesOther, ours, seed) {
  const vals = [];
  for (const pr of probesOther) {
    const v = ours ? sim(ctx, str, pr ? pr.str : null, seed, 0) : sim(ctx, pr ? pr.str : null, str, seed, 0);
    if (v === null) return null;
    vals.push(ours ? v : -v);
  }
  if (!vals.length) return null;
  return 0.5 * vals.reduce((s, v) => s + v, 0) / vals.length + 0.5 * Math.min(...vals);
}

function prune(ctx, root, sd, req, slots, probesOther, ours, seed, g) {
  const kept = [];
  for (let i = 0; i < slots.length; i++) {
    const opts = slots[i];
    if (opts.length === 1) { kept.push([{o: opts[0], score: 0}]); continue; }
    const scored = [];
    for (const o of opts) {
      if (scored.length && ctx.elapsed() > ctx.soft() * 0.6) { ctx.pruneCut = true; break; }
      const parts = g.parts.slice();
      parts[i] = o;
      const k = 1 - i;
      if (slots.length === 2 && o.kind === 'switch' && parts[k].kind === 'switch' && parts[k].j === o.j) {
        parts[k] = bestAttack(ctx, root, sd, slots[k], null) || slots[k].find(x => x.kind !== 'switch' || x.j !== o.j);
      }
      const score = probeScore(ctx, parts.map(x => x.str).join(', '), probesOther, ours, seed);
      if (score !== null) scored.push({o, score});
    }
    scored.sort((p, q) => q.score - p.score);
    const keep = scored.slice(0, ctx.p.slot_keep);
    if (scored.length) {
      const top = scored[0].score;
      for (const cat of [s => s.o.kind === 'switch', s => s.o.kind === 'move' && PROTECT.has(s.o.move)]) {
        if (keep.some(cat)) continue;
        const s = scored.find(cat);
        if (s && s.score >= top - ctx.p.margin) keep.push(s);
      }
    }
    kept.push(keep.length ? keep : [{o: opts[0], score: 0}]);
  }
  let joint;
  if (kept.length === 1) joint = kept[0].map(s => ({str: s.o.str, parts: [s.o], score: s.score}));
  else {
    joint = [];
    for (const a of kept[0]) {
      for (const c of kept[1]) {
        if (a.o.kind === 'switch' && c.o.kind === 'switch' && a.o.j === c.o.j) continue;
        if (ours && !jointOk([a.o, c.o], ctx.tmplSlots)) continue;
        joint.push({str: `${a.o.str}, ${c.o.str}`, parts: [a.o, c.o], score: a.score + c.score});
      }
    }
  }
  joint.sort((p, q) => q.score - p.score);
  if (!joint.some(j => j.str === g.str) && (!ours || jointOk(g.parts, ctx.tmplSlots))) {
    joint.push(Object.assign({score: -Infinity}, g));
  }
  return joint.slice(0, Math.max(ctx.p.keep, 1)).concat(joint.slice(ctx.p.keep).filter(j => j.str === g.str));
}

// ------------------------------------------------------------------------------------------------ entry point
function search(battle, sideIdx, st, params, t0) {
  const ctx = new Ctx(battle, sideIdx, params, t0);
  const root = clone(ctx.base, ctx.seed());
  const me = root.sides[ctx.me], op = root.sides[ctx.opp];
  const req = me.activeRequest;
  if (!req || req.wait) throw new Error('no request for our side after reconstruction');
  const oppReq = op.activeRequest && !op.activeRequest.wait ? op.activeRequest : null;
  const mySlots = rootSlots(root, me, req, st.options || []);
  ctx.tmplSlots = st.options || [];
  const myAll = jointOptions(mySlots, ctx.tmplSlots);
  // no pair passes the joint check against the platform template (e.g. both slots only into the same Pokemon,
  // "switch 3, switch 3"): say so instead of searching an empty matrix; the classic pick plays
  if (!myAll.length) {
    throw new Error('no legal joint engine option: no jointly legal row for our side ('
      + mySlots.map(s => s.map(o => o.str).join('/')).join(' | ') + ')');
  }
  let classic = null;
  if (st.classic) {
    const parts = [];
    for (let i = 0; i < mySlots.length; i++) parts.push(mapChoice(root, me, req, i, st.classic[`slot_${i}`]));
    if (parts.every(Boolean)) classic = {str: parts.map(x => x.str).join(', '), parts, classic: true};
  }
  let oppSlots = null, oppAll = [null];
  if (oppReq) {
    oppSlots = slotOptions(root, op, oppReq);
    oppAll = jointOptions(oppSlots);
  }
  const info = {n_my: myAll.length, n_opp: oppReq ? oppAll.length : 0, forced: !!req.forceSwitch};
  if (myAll.length === 1) return finish(ctx, myAll[0], info);
  const seedA = ctx.seed();
  const myProbes = probes(ctx, root, me, req, mySlots);
  const oppProbes = oppReq ? probes(ctx, root, op, oppReq, oppSlots) : [null];
  const gMine = greedy(ctx, root, me, req, mySlots);
  let rows = myAll.length > ctx.p.keep ? prune(ctx, root, me, req, mySlots, oppProbes, true, seedA, gMine) : myAll;
  let cols = oppReq && oppAll.length > ctx.p.keep
    ? prune(ctx, root, op, oppReq, oppSlots, myProbes, false, seedA, greedy(ctx, root, op, oppReq, oppSlots)) : oppAll;
  if (!rows.length) rows = [myProbes[0]];
  if (!cols.length) cols = [oppProbes[0]];
  if (classic) {
    const k = rows.findIndex(r => r.str === classic.str);
    if (k < 0) rows = rows.concat([classic]);
    else rows[k] = Object.assign({}, rows[k], {classic: true});
    if (ctx.p.guard && !req.forceSwitch) {
      // guard rows: the classic pick with one slot replaced by a switch or Protect (the classic scorer's blind spot)
      for (let k2 = 0; k2 < mySlots.length; k2++) {
        for (const o of mySlots[k2]) {
          if (!(o.kind === 'switch' || (o.kind === 'move' && PROTECT.has(o.move)))) continue;
          const parts = classic.parts.slice();
          parts[k2] = o;
          if (parts.length === 2 && parts[0].kind === 'switch' && parts[1].kind === 'switch' && parts[0].j === parts[1].j) continue;
          const str = parts.map(x => x.str).join(', ');
          const at = rows.findIndex(r => r.str === str);
          if (at >= 0) rows[at] = Object.assign({}, rows[at], {guard: true});
          else rows.push({str, parts, guard: true});
        }
      }
    }
  }
  info.stageA_ms = ctx.elapsed();
  let samples = ctx.p.samples;
  if (req.forceSwitch) samples = Math.max(samples, 3);
  if (info.stageA_ms > ctx.soft() / 2) samples = 1;
  const seeds = [];
  for (let s = 0; s < samples; s++) seeds.push(ctx.seed());
  const rollout = req.forceSwitch ? Math.max(1, ctx.p.rollout) : ctx.p.rollout;
  const A = rows.map(() => cols.map(() => 0));
  const ok = rows.map(() => cols.map(() => 0));
  const tried = rows.map(() => 0);
  let done = 0, cut = false;
  const hard = ctx.hard();
  for (let s = 0; s < seeds.length && !cut; s++) {
    const ts = Date.now();
    for (let i = 0; i < rows.length && !cut; i++) {
      for (let j = 0; j < cols.length; j++) {
        const v = sim(ctx, rows[i].str, cols[j] ? cols[j].str : null, seeds[s], rollout);
        if (v !== null) { A[i][j] += v; ok[i][j]++; }
        // a slow CPU: stop mid-row too (a row is up to `cols` sims, seconds when the machine is overloaded), once
        // one row is complete; the unfinished row is not counted as tried (dropped if no sample completed)
        if (j < cols.length - 1 && (done > 0 || i > 0) && ctx.elapsed() > hard) { cut = true; break; }
      }
      if (cut) break;
      tried[i]++;
      if (ctx.elapsed() > hard) cut = true;          // overloaded machine: stop mid-sample
    }
    if (cut) break;
    done++;
    const per = Date.now() - ts;                     // the next sample would end past the budget: stop now
    if (ctx.elapsed() + per > ctx.soft()) break;
  }
  info.cut = cut;
  if (cut && done === 0) {                           // only rows played out against every column count
    for (let i = 0; i < rows.length; i++) if (!tried[i]) ok[i].fill(0);
  }
  const ri = rows.map((_, i) => i).filter(i => ok[i].some(n => n > 0));
  const cj = cols.map((_, j) => j).filter(j => ri.some(i => ok[i][j] > 0));
  if (!ri.length || !cj.length) {
    throw new Error(`empty matrix (${ctx.bad} sim errors: ${ctx.lastError || ''}; ${ctx.chooseErr || ''})`);
  }
  const M = ri.map(i => cj.map(j => (ok[i][j] ? A[i][j] / ok[i][j] : -WIN)));
  rows = ri.map(i => rows[i]);
  cols = cj.map(j => cols[j]);
  const sol = solve(M, ctx.p.iters);
  let pick = 0;
  let gcol = -1;
  if (oppReq) {
    const gOpp = greedy(ctx, root, op, oppReq, oppSlots);
    gcol = cols.findIndex(c => c && c.str === gOpp.str);
  }
  if (ctx.p.mode === 'blend') {
    // best reply to a mix of the equilibrium opponent and the opponent's greedy (max-damage) joint action: most
    // agents mostly attack; w = blend_w on the greedy column (the equilibrium alone when it was pruned away)
    const w = gcol >= 0 ? Math.max(0, Math.min(1, ctx.p.blend_w)) : 0;
    // sucker_q > 0 (ARENA_SEARCH_SUCKER): a row where one of our slots uses a move that needs the target to attack
    // (Sucker Punch) keeps only (1 - q) of the greedy weight; the rest goes to the equilibrium mix. Real opponents
    // made 25 of 35 of our Sucker Punches fail (10-15), the greedy column assumes none. 0 = unchanged.
    const sq = Math.max(0, Math.min(1, Number(ctx.p.sucker_q) || 0));
    const needsAttack = r => !!(r && r.parts && r.parts.some(o => o && o.kind === 'move' && NEEDS_FOE_ATTACK.has(o.move)));
    let nSucker = 0;
    let bv = -Infinity;
    M.forEach((r, i) => {
      let wi = w;
      if (sq > 0 && w > 0 && needsAttack(rows[i])) { wi = w * (1 - sq); nSucker++; }
      let v = 0;
      for (let j = 0; j < r.length; j++) v += r[j] * ((1 - wi) * sol.y[j] + (j === gcol ? wi : 0));
      if (v > bv) { bv = v; pick = i; }
    });
    if (sq > 0) info.sucker_rows = nSucker;
  } else if (ctx.p.mode === 'maximin') {
    let bv = -Infinity;
    M.forEach((r, i) => { const w = Math.min(...r); if (w > bv) { bv = w; pick = i; } });
  } else if (ctx.p.mode === 'argmax') {
    pick = sol.x.indexOf(Math.max(...sol.x));
  } else {
    const w = sol.x.map(p => (p >= ctx.p.min_prob ? p : 0));
    const tot = w.reduce((s, v) => s + v, 0);
    if (tot <= 0) pick = sol.x.indexOf(Math.max(...sol.x));
    else {
      let u = ctx.rng() * tot;
      pick = w.length - 1;
      for (let i = 0; i < w.length; i++) { u -= w[i]; if (u <= 0 && w[i] > 0) { pick = i; break; } }
    }
  }
  const rowVal = i => cols.reduce((s, _, j) => s + M[i][j] * sol.y[j], 0);
  const ci = rows.findIndex(r => r.classic);
  // delta > 0: keep the classic pick unless the searched pick is worth clearly more against the opponent's mix
  if (ci >= 0 && ctx.p.delta > 0 && pick !== ci && rowVal(pick) - rowVal(ci) < ctx.p.delta) {
    info.kept_classic = true;
    pick = ci;
  }
  // guard: classic unless (a) a guard row (classic + one switch / Protect) beats it by guard_delta, or (b) any row
  // beats it by free_delta; values against the opponent's equilibrium mix. Forced switches: the search decides.
  if (ci >= 0 && ctx.p.guard && !req.forceSwitch) {
    const vc = rowVal(ci);
    let bi = -1, bv = -Infinity, gi = -1, gv = -Infinity;
    for (let i = 0; i < rows.length; i++) {
      const v = rowVal(i);
      if (v > bv) { bv = v; bi = i; }
      if (rows[i].guard && v > gv) { gv = v; gi = i; }
    }
    if (bi >= 0 && bv - vc > ctx.p.free_delta) { pick = bi; info.guard = 'free'; }
    else if (gi >= 0 && gv - vc > ctx.p.guard_delta) { pick = gi; info.guard = 'guard'; }
    else { pick = ci; info.guard = 'classic'; }
  }
  const sup = (arr, p) => arr.map((o, i) => [o ? o.str : '-', Math.round(p[i] * 1000) / 1000]).filter(t => t[1] >= 0.02)
    .sort((a, c) => c[1] - a[1]).slice(0, 5);
  Object.assign(info, {rows: rows.length, cols: cols.length, samples: done, value: Math.round(sol.value * 1000) / 1000,
    pick_value: Math.round(rowVal(pick) * 1000) / 1000,
    classic_value: ci >= 0 ? Math.round(rowVal(ci) * 1000) / 1000 : null,
    classic_prob: ci >= 0 ? Math.round(sol.x[ci] * 1000) / 1000 : null, is_classic: !!rows[pick].classic,
    mix: sup(rows, sol.x), opp_mix: sup(cols, sol.y)});
  return finish(ctx, rows[pick], info);
}

function finish(ctx, opt, info) {
  if (ctx.pruneCut) info.prune_cut = true;
  info.sims = ctx.sims;
  info.errors = ctx.bad;
  if (ctx.lastError) info.last_error = ctx.lastError;
  info.ms = ctx.elapsed();
  info.choice = opt.str;
  const action = {};
  opt.parts.forEach((o, i) => {
    action[`slot_${i}`] = o.out || {type: 'pass'};
  });
  return {action, info};
}

// ------------------------------------------------------------------------------------------------ server
function runSearch(msg) {
  const t0 = Date.now();
  const st = msg.state || {};
  let battle;
  if (msg.battle_json) {
    const {Battle} = ps();
    battle = Battle.fromJSON(msg.battle_json);
    battle.send = () => {};
  } else {
    battle = build(st);
  }
  const buildMs = Date.now() - t0;
  const sideIdx = st.me === 'p2' ? 1 : 0;
  const res = search(battle, sideIdx, st, msg.params || {}, t0);
  res.info.build_ms = buildMs;
  return res;
}

function warm() {
  const t0 = Date.now();
  const {Battle} = ps();
  const set = (species, moves, item, ability) => ({name: species, species, moves, item, ability, nature: 'Hardy',
    level: 50, evs: {hp: 4}, ivs: {}});
  const team = [set('Incineroar', ['Fake Out', 'Knock Off', 'Parting Shot', 'Flare Blitz'], 'Safety Goggles', 'Intimidate'),
    set('Rillaboom', ['Fake Out', 'Wood Hammer', 'Grassy Glide', 'U-turn'], 'Assault Vest', 'Grassy Surge'),
    set('Flutter Mane', ['Moonblast', 'Shadow Ball', 'Icy Wind', 'Protect'], 'Booster Energy', 'Protosynthesis'),
    set('Tornadus', ['Bleakwind Storm', 'Taunt', 'Tailwind', 'Protect'], 'Mental Herb', 'Prankster')];
  const b = new Battle({formatid: FORMAT, seed: [1, 2, 3, 4]});
  b.send = () => {};
  b.setPlayer('p1', {name: 'p1', team});
  b.setPlayer('p2', {name: 'p2', team});
  b.choose('p1', 'team 1234');
  b.choose('p2', 'team 1234');
  const c = clone(serialize(b), [5, 6, 7, 8]);
  c.choose('p1', 'move 2 1, move 2 2');
  c.choose('p2', 'move 2 1, move 2 2');
  return {ok: true, ms: Date.now() - t0};
}

// A comparable summary of a battle (scripts/search_fidelity.py: reconstruction vs the local simulator's truth).
function summarize(b) {
  const vol = p => {
    const out = {};
    for (const [k, v] of Object.entries(p.volatiles || {})) {
      out[k] = {};
      for (const f of ['duration', 'counter', 'move', 'time', 'bestStat', 'fromBooster', 'hp', 'sourceSlot']) {
        if (f === 'sourceSlot' && k !== 'leechseed') continue;
        if (v[f] !== undefined && v[f] !== null) out[k][f] = v[f];
      }
    }
    return out;
  };
  const sides = {};
  for (const sd of b.sides) {
    sides[sd.id] = {
      pokemonLeft: sd.pokemonLeft,
      active: sd.active.map(p => (p ? toID(p.set.species) : null)),
      mons: sd.pokemon.map(p => ({species: toID(p.set.species), hp: p.hp, maxhp: p.maxhp, fainted: !!p.fainted,
        status: p.status, item: p.item, ability: p.ability, boosts: Object.assign({}, p.boosts), isActive: !!p.isActive,
        volatiles: p.isActive ? vol(p) : {}, activeMoveActions: p.isActive ? p.activeMoveActions : 0,
        slp: p.status === 'slp' ? p.statusState.time : undefined, tox: p.status === 'tox' ? p.statusState.stage : undefined,
        fallen: p.abilityState && p.abilityState.fallen || 0})),
      conditions: Object.fromEntries(Object.entries(sd.sideConditions).map(([k, v]) => [k, {duration: v.duration, layers: v.layers}])),
      request: sd.activeRequest ? (sd.activeRequest.wait ? 'wait' : sd.activeRequest.forceSwitch ? 'switch' : 'move') : null,
      moves: sd.activeRequest && sd.activeRequest.active
        ? sd.activeRequest.active.map(a => (a ? a.moves.filter(m => !m.disabled).map(m => m.id) : [])) : null,
    };
  }
  return {turn: b.turn, sides,
    field: {weather: b.field.weather, weather_d: b.field.weatherState.duration, terrain: b.field.terrain,
      terrain_d: b.field.terrainState.duration,
      pseudo: Object.fromEntries(Object.entries(b.field.pseudoWeather).map(([k, v]) => [k, v.duration]))}};
}

function handle(line) {
  let msg;
  try { msg = JSON.parse(line); } catch (e) { return {id: null, error: 'bad json'}; }
  const id = msg && msg.id !== undefined ? msg.id : null;
  try {
    if (msg.cmd === 'warm') return Object.assign({id}, warm());
    if (msg.cmd === 'search') return Object.assign({id}, runSearch(msg));
    if (msg.cmd === 'inspect') {
      const {Battle} = ps();
      const b = msg.battle_json ? Battle.fromJSON(msg.battle_json) : build(msg.state || {});
      return {id, summary: summarize(b)};
    }
    if (msg.cmd === 'quit') process.exit(0);
    return {id, error: 'unknown cmd'};
  } catch (e) {
    return {id, error: String(e && e.message || e).slice(0, 300), detail: String(e && e.stack || '').slice(0, 800)};
  }
}

if (require.main === module) {
  const rl = readline.createInterface({input: process.stdin});
  rl.on('line', line => { process.stdout.write(JSON.stringify(handle(line)) + '\n'); });
}

module.exports = {build, search, runSearch, warm, handle, solve, slotOptions, rootSlots, mapChoice, jointOptions,
  evaluate, DEFAULTS};
