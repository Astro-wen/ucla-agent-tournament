// Batch damage-calc server: one JSON object per line on stdin -> one per line on stdout.
//   {"id":1,"calcs":[{"attacker":{...},"defender":{...},"move":"Heat Wave","field":{...}}, ...]}
//   -> {"id":1,"results":[{"min":300,"max":354,"rolls":[...16 ints],"defender_hp":210,"desc":"..."} | {"error":"..."}]}
// Pokémon fields: species (display name), item, ability, nature, evs, ivs, level, boosts, curHP, status, teraType(ignored),
//   hpFraction (0..1): current HP as a fraction of max, scaled to the calculator's own max HP. Prefer it over curHP:
//   the platform reports FOE HP as a percentage (x/100), so a raw curHP would mis-state e.g. Multiscale at full HP.
//   alliesFainted (0..5): fainted teammates counted by Supreme Overlord when this Pokémon came in.
// Field: gameType ("Doubles"|"Singles"), weather, terrain, isGravity, attackerSide/defenderSide:
//   {isTailwind,isHelpingHand,isReflect,isLightScreen,isAuroraVeil,isFriendGuard,isProtected,isSR,spikes,isBattery,isPowerSpot}
// Per calc: hits (optional, fixes the hit count of a multi-hit move), isCrit.
// Result rolls are equally likely TOTAL damage outcomes, with corrections @smogon/calc leaves to the caller:
//   - a multi-hit move rolls every hit independently: `rolls` are 32 quantiles of the convolved total (min/max exact);
//   - a variable multi-hit move (2-5 hits) without an explicit `hits` returns the mixture over Showdown's hit-count
//     odds (2/3 hits 35% each, 4/5 hits 15% each; Loaded Dice 4 or 5; Skill Link 5);
//   - Focus Sash / Sturdy: a single hit on a full-HP holder cannot KO; rolls/min/max are capped at max HP - 1
//     (`survives` names the effect). Multi-hit moves break it on the first hit, so they are never capped.
'use strict';
const readline = require('readline');
const {calculate, Pokemon, Move, Field, Generations, toID} = require('@smogon/calc');
const gen = Generations.get(9);

const MOLD_BREAKERS = ['Mold Breaker', 'Teravolt', 'Turboblaze'];
const IGNORES_ABILITIES = new Set(['Sunsteel Strike', 'Moongeist Beam', 'Photon Geyser', 'Searing Sunraze Smash',
  'Menacing Moonraze Maelstrom', 'Light That Burns the Sky']);
const MIXTURE_POINTS = 32;

function mon(p) {
  const opts = {};
  for (const k of ['item', 'ability', 'nature', 'evs', 'ivs', 'level', 'boosts', 'curHP', 'status', 'abilityOn']) {
    if (p[k] !== undefined && p[k] !== null && p[k] !== '') opts[k] = p[k];
  }
  if (typeof p.alliesFainted === 'number' && p.alliesFainted > 0) opts.alliesFainted = Math.min(5, Math.floor(p.alliesFainted));
  if (typeof p.hpFraction === 'number') {
    const full = new Pokemon(gen, p.species, opts);
    const f = Math.min(1, Math.max(0, p.hpFraction));
    opts.curHP = f >= 1 ? full.maxHP() : Math.max(f > 0 ? 1 : 0, Math.round(f * full.maxHP()));
  }
  return new Pokemon(gen, p.species, opts);
}

// One roll array per hit (a multi-hit result is one 16-roll array per hit; a single hit is a flat array or a number).
function hitRolls(damage) {
  if (Array.isArray(damage) && Array.isArray(damage[0])) return damage.filter(h => Array.isArray(h) && h.length);
  return [Array.isArray(damage) ? damage : [damage]];
}

// Distribution of the TOTAL over hits, Map value -> probability. Showdown rolls the random factor once PER HIT, so the
// total is the convolution of the hits' (uniform) roll distributions. Summing roll i over the hits instead makes every
// hit roll low or high together: the range is right but the KO odds near its edges are far off (Surging Strikes into
// Kingambit at 61/100: 0.44 instead of 0.18).
function totalDist(hits) {
  let dist = new Map([[0, 1]]);
  for (const rolls of hits) {
    const next = new Map();
    const p = 1 / rolls.length;
    for (const [v, q] of dist) {
      for (const x of rolls) next.set(v + x, (next.get(v + x) || 0) + q * p);
    }
    dist = next;
  }
  return dist;
}

// n equally likely representative rolls of a distribution: its (k + 0.5) / n quantiles, ascending.
function quantiles(dist, n) {
  const entries = [...dist.entries()].sort((a, b) => a[0] - b[0]);
  const total = entries.reduce((a, e) => a + e[1], 0);
  const out = [];
  let i = 0, cum = entries[0][1];
  for (let k = 0; k < n; k++) {
    const target = (k + 0.5) / n * total * (1 - 1e-9);
    while (cum < target && i < entries.length - 1) cum += entries[++i][1];
    out.push(entries[i][0]);
  }
  return out;
}

// [[hits, weight], ...] for a 2-5 hit move without a fixed count; null when the calculator's own count is right.
function hitDistribution(c, atk) {
  if (c.hits) return null;
  const data = gen.moves.get(toID(c.move));
  const mh = data && data.multihit;
  if (!Array.isArray(mh) || data.multiaccuracy || mh[0] === mh[1]) return null;
  if (atk.hasAbility('Skill Link')) return [[mh[1], 1]];
  if (mh[0] === 2 && mh[1] === 5) {
    return atk.hasItem('Loaded Dice') ? [[4, 1], [5, 1]] : [[2, 7], [3, 7], [4, 3], [5, 3]];
  }
  const out = [];
  for (let h = mh[0]; h <= mh[1]; h++) out.push([h, 1]);
  return out;
}

function survivor(atk, def, move) {
  if (def.curHP() < def.maxHP()) return null;
  if (def.hasItem('Focus Sash')) return 'Focus Sash';
  if (def.hasAbility('Sturdy') && !atk.hasAbility(...MOLD_BREAKERS) && !IGNORES_ABILITIES.has(move.name)) return 'Sturdy';
  return null;
}

function one(c) {
  try {
    const atk = mon(c.attacker), def = mon(c.defender);
    const f = c.field || {};
    const field = new Field({
      gameType: f.gameType || 'Doubles', weather: f.weather || undefined, terrain: f.terrain || undefined,
      isGravity: !!f.isGravity, attackerSide: f.attackerSide || {}, defenderSide: f.defenderSide || {},
    });
    const move = new Move(gen, c.move, {isCrit: !!c.isCrit, hits: c.hits});
    const r = calculate(gen, atk, def, move, field);
    const perHit = hitRolls(r.damage);
    const multi = perHit.length > 1;
    let rolls = perHit[0].slice();
    let [min, max] = r.range();
    let hits = move.hits;
    if (multi) {
      // Independent per-hit rolls, mixed over the hit-count odds of a 2-5 hit move: MIXTURE_POINTS quantiles.
      const odds = hitDistribution(c, atk) || [[move.hits, 1]];
      const wsum = odds.reduce((a, [, w]) => a + w, 0);
      const mix = new Map();
      for (const [h, w] of odds) {
        const rh = h === move.hits ? r : calculate(gen, atk, def, new Move(gen, c.move, {isCrit: !!c.isCrit, hits: h}), field);
        for (const [v, p] of totalDist(hitRolls(rh.damage))) mix.set(v, (mix.get(v) || 0) + p * w / wsum);
      }
      rolls = quantiles(mix, MIXTURE_POINTS);
      min = Math.min(...mix.keys());
      max = Math.max(...mix.keys());
      hits = odds.length > 1 ? odds.map(([h]) => h).join('/') : odds[0][0];
    }
    let survives = null;
    if (!multi) {
      survives = survivor(atk, def, move);
      if (survives) {
        const cap = def.maxHP() - 1;
        rolls = rolls.map(x => Math.min(x, cap));
        min = Math.min(min, cap);
        max = Math.min(max, cap);
      }
    }
    let desc = '';
    try { desc = r.desc(); } catch (e) { desc = ''; }
    const res = {min, max, rolls, defender_hp: def.maxHP(), attacker_stats: atk.stats, defender_stats: def.stats, desc};
    if (multi) res.hits = hits;
    if (survives) res.survives = survives;
    return res;
  } catch (e) {
    return {error: String(e && e.message || e)};
  }
}

const rl = readline.createInterface({input: process.stdin});
rl.on('line', line => {
  let msg;
  try { msg = JSON.parse(line); } catch (e) { return out({error: 'bad json'}); }
  if (msg.cmd === 'quit') process.exit(0);
  out({id: msg.id, results: (msg.calcs || []).map(one)});
});
function out(o) { process.stdout.write(JSON.stringify(o) + '\n'); }
