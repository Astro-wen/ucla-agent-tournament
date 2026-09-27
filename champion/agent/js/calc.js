// Batch damage-calc server: one JSON object per line on stdin -> one per line on stdout.
//   {"id":1,"calcs":[{"attacker":{...},"defender":{...},"move":"Heat Wave","field":{...}}, ...]}
//   -> {"id":1,"results":[{"min":300,"max":354,"rolls":[...16 ints],"defender_hp":210,"desc":"..."} | {"error":"..."}]}
// Pokémon fields: species (display name), item, ability, nature, evs, ivs, level, boosts, curHP, status, teraType(ignored),
//   hpFraction (0..1): current HP as a fraction of max, scaled to the calculator's own max HP. Prefer it over curHP:
//   the platform reports FOE HP as a percentage (x/100), so a raw curHP would mis-state e.g. Multiscale at full HP.
// Field: gameType ("Doubles"|"Singles"), weather, terrain, isGravity, attackerSide/defenderSide:
//   {isTailwind,isHelpingHand,isReflect,isLightScreen,isAuroraVeil,isFriendGuard,isProtected,isSR,spikes,isBattery,isPowerSpot}
'use strict';
const readline = require('readline');
const {calculate, Pokemon, Move, Field, Generations} = require('@smogon/calc');
const gen = Generations.get(9);

function mon(p) {
  const opts = {};
  for (const k of ['item', 'ability', 'nature', 'evs', 'ivs', 'level', 'boosts', 'curHP', 'status', 'abilityOn']) {
    if (p[k] !== undefined && p[k] !== null && p[k] !== '') opts[k] = p[k];
  }
  if (typeof p.hpFraction === 'number') {
    const full = new Pokemon(gen, p.species, opts);
    const f = Math.min(1, Math.max(0, p.hpFraction));
    opts.curHP = f >= 1 ? full.maxHP() : Math.max(f > 0 ? 1 : 0, Math.round(f * full.maxHP()));
  }
  return new Pokemon(gen, p.species, opts);
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
    let rolls = r.damage;
    if (Array.isArray(rolls) && Array.isArray(rolls[0])) rolls = rolls.map(x => x.reduce((a, b) => a + b, 0)); // multi-hit
    if (!Array.isArray(rolls)) rolls = [rolls];
    const [min, max] = r.range();
    let desc = '';
    try { desc = r.desc(); } catch (e) { desc = ''; }
    return {min, max, rolls, defender_hp: def.maxHP(), attacker_stats: atk.stats, defender_stats: def.stats, desc};
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
