// simbot: an engine-search VGC doubles opponent for the local simulator (local/ext_opp.py, the `simbot` opponent).
//
// Every battle decision is a zero-sum matrix game over joint (two-slot) actions, where each cell is played out on a
// CLONE of the real Showdown battle (Battle.toJSON / fromJSON) with a fresh random seed, then scored by a static
// evaluation. Nothing is modelled by hand: speed order, priority, Protect, Fake Out flinches, redirection,
// Intimidate, spread damage, weather, items and abilities all come from the engine itself.
//
//   1. options   every legal choice per slot from the side's live request (damaging single-target moves at live
//                foes only, status moves at their natural target, switches); joint = product of the two slots.
//   2. stage A   (both sides, when a side has more than `keep` joint options) every option of a slot is played with
//                the partner slot on its greedy choice against `probes` replies of the other side (greedy
//                max-damage; greedy with its most threatened slot Protecting, else greedy avoiding its main
//                target), one turn, scored 0.5 mean + 0.5 worst; each slot keeps its best `slot_keep` options plus its
//                best switch and best Protect when within `margin` of the top (a one-turn score undervalues them);
//                joint options = product of the kept ones, best `keep` by summed slot scores (+ the greedy one).
//   3. stage B   rows x cols x `samples` seeds (common random numbers), each: the chosen turn, forced switches
//                auto-filled (best matchup), then `rollout` extra turn(s) where both sides play greedy.
//   4. solve     regret-matching+ gives both sides' equilibrium mixes; mode nash samples ours (probabilities under
//                min_prob dropped), maximin plays the best worst case, argmax the most likely action.
// Time: about 150-250 engine turns per decision, ~1 s (p95 ~2 s). Only load-dependent part: stage A slower than
// budget_ms / 2 drops stage B to one sample, and stage B stops after the sample that crosses budget_ms.
//
// Evaluation (our view): sum over each side's brought Pokemon of 0.35 + 0.65 * HP fraction when alive, status and
// active stat-stage adjustments, Tailwind / screens by turns left, Trick Room by turns left x who is slower; a
// finished battle is +-8.
// Information: the clone is the real battle, so the bot knows exact HP, which four the opponent brought and their
// sets (sets are public under Open Team Sheets / the draft; the four and exact HP are a small edge). The opponent's
// already-sent choice is cleared in every clone and the random seed is replaced, so it never sees the other side's
// move or the real future rolls. Mid-turn switch requests (U-turn, Eject Button) simulate the rest of the turn with
// the moves already queued (a small leak, rare).
'use strict';
const {Battle, PRNG, toID} = require('pokemon-showdown');

const NEEDS_TARGET = new Set(['normal', 'any', 'adjacentFoe', 'adjacentAlly', 'adjacentAllyOrSelf']);
const SPREAD = new Set(['allAdjacentFoes', 'allAdjacent']);
const PROTECT = new Set(['protect', 'detect', 'spikyshield', 'banefulbunker', 'silktrap', 'burningbulwark',
  'kingsshield', 'obstruct']);
const FIRST_TURN = new Set(['fakeout', 'firstimpression', 'matblock']);
const WIN = 8;
const DEFAULTS = {samples: 2, rollout: 1, keep: 8, slot_keep: 3, probes: 2, budget_ms: 3000, mode: 'nash',
  min_prob: 0.05, iters: 1500, margin: 0.6};

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

function serialize(battle) {
  const st = battle.toJSON();
  st.log = [];
  st.inputLog = [];
  return JSON.stringify(st);
}

function clone(base, seed) {
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

// ------------------------------------------------------------------------------------------------ search context
class Ctx {
  constructor(battle, sideIdx, params) {
    this.p = Object.assign({}, DEFAULTS, params || {});
    this.t0 = Date.now();
    this.base = serialize(battle);
    this.me = sideIdx;
    this.opp = 1 - sideIdx;
    this.turn0 = battle.turn;
    this.rng = mulberry(((params && params.rng) || 1) >>> 0);
    this.sims = 0;
    this.bad = 0;
    this.est = Battle.fromJSON(this.base);           // damage estimates: fixed 92.5% roll, no crits
    this.est.send = () => {};
    this.est.randomizer = d => Math.floor(Math.floor(d * 925 / 10) / 100);
    this.est.randomChance = () => false;
    this.est.random = (m, n) => (m === undefined ? 0.5 : n === undefined ? Math.floor((m - 1) / 2) : Math.floor((m + n - 1) / 2));
    this.dmgCache = new Map();
    this.prof = this.p.prof ? {} : null;
  }

  seed() {
    return [1 + Math.floor(this.rng() * 65534), 1 + Math.floor(this.rng() * 65534),
      1 + Math.floor(this.rng() * 65534), 1 + Math.floor(this.rng() * 65534)];
  }

  // Expected damage (HP points, accuracy and hit count included) of `att` using `moveId` on `def`, from the root
  // position (boosts / field as they are now). Cached by (side, species, move, side, species).
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

  // Value of dealing that expected damage now: fraction of max HP actually removed, + 0.4 for a (likely) KO.
  hitValue(att, moveId, def) {
    if (!live(def)) return 0;
    const d = this.dmg(att, moveId, def);
    if (d <= 0) return 0;
    return Math.min(d, def.hp) / def.maxhp + (d >= def.hp ? 0.4 : 0);
  }

  elapsed() {
    return Date.now() - this.t0;
  }
}

// ------------------------------------------------------------------------------------------------ options
// Per-slot options of side `sd` in battle `b` for its live request `req`.
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

function jointOptions(slots) {
  if (slots.length === 1) return slots[0].map(a => ({str: a.str, parts: [a]}));
  const out = [];
  for (const a of slots[0]) {
    for (const c of slots[1]) {
      if (a.kind === 'switch' && c.kind === 'switch' && a.j === c.j) continue;
      out.push({str: `${a.str}, ${c.str}`, parts: [a, c]});
    }
  }
  return out;
}

// Immediate damage value of one slot option (null = not a damaging move).
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
  if (o.target === 0 && foes.length) {           // locked / random-target move
    let v = 0;
    for (const x of foes) v += ctx.hitValue(att, o.move, x);
    return v / foes.length;
  }
  return 0;
}

// Greedy joint choice for `sd`: per slot the most damaging option; `avoid` = foe target (1/2) not to aim at;
// `protectSlot` = slot that Protects instead (when it can).
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

// The most damaging move option (a zero-damage attack still beats nothing); `avoid` = a foe target to skip.
function bestAttack(ctx, b, sd, opts, avoid) {
  let best = null, bv = -1;
  for (const o of opts) {
    if (o.kind !== 'move' || (avoid && o.target === avoid)) continue;
    const v = optValue(ctx, b, sd, o);
    if (v !== null && v > bv) { bv = v; best = o; }
  }
  return best;
}

// A status move a greedy player may repeat harmlessly (no Protect, no Trick Room / Tailwind already up).
function neutralStatus(b, sd, o) {
  if (PROTECT.has(o.move)) return false;
  if (o.move === 'trickroom' && b.field.pseudoWeather.trickroom) return false;
  if (o.move === 'tailwind' && sd.sideConditions.tailwind) return false;
  return true;
}

// Forced switch-in: the bench Pokemon with the best (offence - 0.7 x defence) against the foes on the field.
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
  let ti = -1, tv = 0.45;                          // the most threatened slot that can Protect
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
  // +1 = all our Pokemon are faster than all theirs, -1 = all slower (alive ones)
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
  // fill forced switches (faints, U-turn...) until both sides face a move request or the battle ends
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
  const pend = pending(b);
  for (const sd of pend) {
    const g = greedy(ctx, b, sd, sd.activeRequest);
    if (!b.choose(sd.id, g.str)) b.choose(sd.id, 'default');
  }
}

// One cell: our joint choice vs theirs from the root, seed `seed`; returns our evaluation or null if illegal.
function sim(ctx, mine, theirs, seed, rollout) {
  ctx.sims++;
  let b;
  const P = ctx.prof;
  let t = P ? process.hrtime.bigint() : 0n;
  const lap = k => { if (P) { const n = process.hrtime.bigint(); P[k] = (P[k] || 0) + Number(n - t) / 1e6; t = n; } };
  try {
    b = clone(ctx.base, seed);
    lap('clone');
    const me = b.sides[ctx.me], op = b.sides[ctx.opp];
    if (mine !== null && !b.choose(me.id, mine)) return null;
    if (theirs !== null && !b.choose(op.id, theirs)) return null;
    lap('turn');
    settle(ctx, b);
    lap('settle');
    for (let r = 0; r < rollout && !b.ended; r++) {
      greedyTurn(ctx, b);
      lap('rollout');
      settle(ctx, b);
      lap('settle');
    }
    const v = evaluate(ctx, b);
    lap('eval');
    return v;
  } catch (e) {
    ctx.bad++;
    if (ctx.p.debug) ctx.lastError = String(e && e.stack || e).slice(0, 500);
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

// ------------------------------------------------------------------------------------------------ stage A
// Score one joint choice of a side against the other side's probes: 0.5 mean + 0.5 worst (null = illegal).
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

// Slot-wise pruning: every option of a slot is scored with the partner slot on its greedy choice, against the
// other side's probes; each slot keeps its best `slot_keep` options (+ its best switch and best Protect when within
// `margin` of the top, so a one-turn score cannot prune them). Joint options = product of the kept options, ranked
// by the sum of their slot scores, best `keep`.
function prune(ctx, root, sd, req, slots, probesOther, ours, seed) {
  const g = greedy(ctx, root, sd, req, slots);
  const kept = [];
  for (let i = 0; i < slots.length; i++) {
    const opts = slots[i];
    if (opts.length === 1) { kept.push([{o: opts[0], score: 0}]); continue; }
    const scored = [];
    for (const o of opts) {
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
        joint.push({str: `${a.o.str}, ${c.o.str}`, parts: [a.o, c.o], score: a.score + c.score});
      }
    }
  }
  joint.sort((p, q) => q.score - p.score);
  if (!joint.some(j => j.str === g.str)) joint.push(Object.assign({score: -Infinity}, g));   // greedy stays in
  return joint.slice(0, Math.max(ctx.p.keep, 1)).concat(joint.slice(ctx.p.keep).filter(j => j.str === g.str));
}

// ------------------------------------------------------------------------------------------------ entry point
function search(battle, msg) {
  const sid = msg.side;
  const sideIdx = sid === 'p1' ? 0 : 1;
  const live_ = battle.sides[sideIdx];
  const req = live_.activeRequest;
  if (!req || req.wait) return {error: 'no_request'};
  const ctx = new Ctx(battle, sideIdx, Object.assign({rng: (msg.seed || [1])[0] * 65536 + ((msg.seed || [1, 1])[1] || 1)}, msg.params || {}));
  const root = clone(ctx.base, ctx.seed());
  const me = root.sides[ctx.me], op = root.sides[ctx.opp];
  const oppReq = op.activeRequest && !op.activeRequest.wait ? op.activeRequest : null;
  const mySlots = slotOptions(root, me, me.activeRequest);
  const myAll = jointOptions(mySlots);
  let oppSlots = null, oppAll = [null];
  if (oppReq) {
    oppSlots = slotOptions(root, op, oppReq);
    oppAll = jointOptions(oppSlots);
  }
  const info = {n_my: myAll.length, n_opp: oppReq ? oppAll.length : 0, forced: !!req.forceSwitch};
  if (myAll.length === 1) {
    return finish(ctx, myAll[0], info, null, null);
  }
  // stage A: prune both sides' options against the other side's probes
  const seedA = ctx.seed();
  const myProbes = probes(ctx, root, me, me.activeRequest, mySlots);
  const oppProbes = oppReq ? probes(ctx, root, op, oppReq, oppSlots) : [null];
  let rows = myAll.length > ctx.p.keep ? prune(ctx, root, me, me.activeRequest, mySlots, oppProbes, true, seedA) : myAll;
  let cols = oppReq && oppAll.length > ctx.p.keep ? prune(ctx, root, op, oppReq, oppSlots, myProbes, false, seedA) : oppAll;
  if (!rows.length) rows = [myProbes[0]];
  if (!cols.length) cols = [oppProbes[0]];
  info.stageA_ms = ctx.elapsed();
  // stage B: full matrix, common random numbers, rollout
  let samples = ctx.p.samples;
  if (req.forceSwitch) samples = Math.max(samples, 3);
  if (info.stageA_ms > ctx.p.budget_ms / 2) samples = 1;
  const seeds = [];
  for (let s = 0; s < samples; s++) seeds.push(ctx.seed());
  const rollout = req.forceSwitch ? Math.max(1, ctx.p.rollout) : ctx.p.rollout;
  let A = rows.map(() => cols.map(() => 0));
  const ok = rows.map(() => cols.map(() => 0));
  for (let s = 0; s < seeds.length; s++) {
    for (let i = 0; i < rows.length; i++) {
      for (let j = 0; j < cols.length; j++) {
        const v = sim(ctx, rows[i].str, cols[j] ? cols[j].str : null, seeds[s], rollout);
        if (v !== null) { A[i][j] += v; ok[i][j]++; }
      }
    }
    if (ctx.elapsed() > ctx.p.budget_ms) break;
  }
  // drop rows / columns that never simulated (illegal)
  const ri = rows.map((_, i) => i).filter(i => ok[i].some(n => n > 0));
  const cj = cols.map((_, j) => j).filter(j => ri.some(i => ok[i][j] > 0));
  if (!ri.length || !cj.length) return finish(ctx, myProbes[0], Object.assign(info, {empty: true}), null, null);
  const M = ri.map(i => cj.map(j => (ok[i][j] ? A[i][j] / ok[i][j] : -WIN)));
  rows = ri.map(i => rows[i]);
  cols = cj.map(j => cols[j]);
  const sol = solve(M, ctx.p.iters);
  let pick = 0;
  if (ctx.p.mode === 'maximin') {
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
  const sup = (arr, p) => arr.map((o, i) => [o ? o.str : '-', Math.round(p[i] * 1000) / 1000]).filter(t => t[1] >= 0.02)
    .sort((a, c) => c[1] - a[1]).slice(0, 5);
  Object.assign(info, {rows: rows.length, cols: cols.length, samples: seeds.length, value: Math.round(sol.value * 1000) / 1000,
    mix: sup(rows, sol.x), opp_mix: sup(cols, sol.y)});
  return finish(ctx, rows[pick], info);
}

function finish(ctx, opt, info) {
  info.sims = ctx.sims;
  info.errors = ctx.bad;
  if (ctx.prof) info.prof = Object.fromEntries(Object.entries(ctx.prof).map(([k, v]) => [k, Math.round(v)]));
  if (ctx.lastError) info.last_error = ctx.lastError;
  info.ms = ctx.elapsed();
  info.choice = opt.str;
  const slots = opt.parts.map(o => (o.kind === 'move' ? {type: 'move', move_id: o.move, target: o.target}
    : o.kind === 'switch' ? {type: 'switch', species: o.species} : {type: 'pass'}));
  return {choice: opt.str, slots, info};
}

module.exports = {search, solve, slotOptions, jointOptions, evaluate, DEFAULTS};
