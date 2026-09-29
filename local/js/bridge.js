// JSON-lines bridge around the Pokémon Showdown `Battle` class.
// One battle per process. Python sends one JSON object per line on stdin:
//   {"cmd":"start","format":"gen9vgc2025regi@@@Terastal Clause","p1":[sets],"p2":[sets],"seed":[1,2,3,4]}
//   {"cmd":"choose","p1":"team 1234","p2":"move 1 1, move 2 2"}   (omit a side that has no request)
//   {"cmd":"validate","sets":[sets]}
//   {"cmd":"gen_team","format":"gen9randombattle","seed":[1,2,3,4]}   -> {"team":[sets]}  (singles harness)
//   {"cmd":"search","side":"p2","seed":[1,2,3,4],"params":{..}}   -> {"choice":..,"slots":[..],"info":{..}}
//       (the `simbot` opponent's engine search on clones of the current battle, local/js/simbot.js; the live
//        battle is not touched)
//   {"cmd":"sideview","side":"p2","from":123}   -> {"lines":[..],"request":..,"pos":..,"header_end":..,"turn":..}
//       (what ONE player would receive from a Showdown server: the live battle's protocol lines from log index
//        `from` (default 0) resolved for that side's channel with the sim's own extractChannelMessages, plus that
//        side's current request; used by the VGC-Bench opponent, local/vgcbench_opp.py. Read-only.)
//   {"cmd":"preview_view","side":"p1","p1":[sets],"p2":[sets],"seed":[1,2,3,4]}
//       -> {"lines":[..],"request":..,"showteam":[..]}  (the same side view of a throwaway battle's team-preview
//       phase, plus the Open Team Sheets lines a server shows once both players accept them; the live battle is
//       not touched)
//   {"cmd":"dump"}   -> {"battle": the live battle as Battle.toJSON() text}  (local ablation of the agent's engine
//       search only: ARENA_SEARCH_ORACLE=1 in local.panel lets the candidate search the true state)
// and gets back one JSON object per line: {"requests":{"p1":..,"p2":..},"state":{..},"ended":bool,"winner":..,"log":[..]}
'use strict';
const readline = require('readline');
const {Battle, TeamValidator, Teams} = require('pokemon-showdown');
const {extractChannelMessages} = require('pokemon-showdown/dist/sim/battle');
const simbot = require('./simbot');

let battle = null;
let logOffset = 0;
let headerEnd = 0;           // battle.log length right after `start`: the team-preview header (sideview's header_end)

// One player's channel of `lines` (p1 -> channel 1, p2 -> channel 2): split lines resolved to that side's secret or
// the public version, exactly like the server's per-player broadcast; never the omniscient text of the other side.
function sideLines(lines, side) {
  const ch = side === 'p1' ? 1 : 2;
  return extractChannelMessages(lines.join('\n'), [ch])[ch];
}

function sideIndex(side) {
  if (side !== 'p1' && side !== 'p2') throw new Error(`bad side ${side}`);
  return side === 'p1' ? 0 : 1;
}

function pokeView(p, omniscient) {
  if (!p) return null;
  const v = {
    species: p.species.name, id: p.species.id, types: p.getTypes().map(t => t.toUpperCase()),
    current_hp: p.hp, max_hp: p.maxhp, current_hp_fraction: p.maxhp ? p.hp / p.maxhp : 0,
    status: p.status || null, fainted: p.fainted, active: p.isActive, boosts: p.boosts,
    base_stats: p.species.baseStats, position: p.position, level: p.level, name: p.name,
  };
  if (omniscient) { v.item = p.item; v.ability = p.ability; v.moves = p.moves; v.stats = p.storedStats; }
  return v;
}

function sideView(side, omniscient) {
  return {
    active: side.active.map(p => pokeView(p, omniscient)),
    team: side.pokemon.map(p => pokeView(p, omniscient)),
    side_conditions: Object.keys(side.sideConditions),
    side_layers: Object.fromEntries(Object.entries(side.sideConditions).map(([k, c]) => [k, c.layers || 1])),
  };
}

function snapshot() {
  const req = {};
  for (const s of battle.sides) req[s.id] = s.activeRequest && !s.activeRequest.wait ? s.activeRequest : null;
  const newLog = battle.log.slice(logOffset); logOffset = battle.log.length;
  return {
    requests: req,
    state: {
      turn: battle.turn, weather: battle.field.weather || null,
      terrain: battle.field.terrain || null, pseudo_weather: Object.keys(battle.field.pseudoWeather),
      p1: sideView(battle.sides[0], true), p2: sideView(battle.sides[1], true),
    },
    ended: battle.ended, winner: battle.winner || null, log: newLog,
  };
}

const rl = readline.createInterface({input: process.stdin});
rl.on('line', line => {
  let msg;
  try { msg = JSON.parse(line); } catch (e) { return out({error: 'bad json'}); }
  try {
    if (msg.cmd === 'start') {
      battle = new Battle({formatid: msg.format, seed: msg.seed});
      logOffset = 0;
      battle.setPlayer('p1', {name: msg.p1name || 'p1', team: msg.p1});
      battle.setPlayer('p2', {name: msg.p2name || 'p2', team: msg.p2});
      headerEnd = battle.log.length;
      return out(snapshot());
    }
    if (msg.cmd === 'choose') {
      for (const sid of ['p1', 'p2']) {
        if (msg[sid] == null) continue;
        const ok = battle.choose(sid, msg[sid]);
        if (!ok) return out({error: 'bad_choice', side: sid, choice: msg[sid],
                             detail: battle.sides[sid === 'p1' ? 0 : 1].choice.error, ...snapshot()});
      }
      return out(snapshot());
    }
    if (msg.cmd === 'validate') {
      const v = TeamValidator.get(msg.format || 'gen9vgc2025regi');
      return out({problems: msg.sets.map(s => v.validateSet(s, {}) || null)});
    }
    if (msg.cmd === 'gen_team') {
      return out({team: Teams.generate(msg.format || 'gen9randombattle', {seed: msg.seed || undefined})});
    }
    if (msg.cmd === 'search') {
      if (!battle) return out({error: 'no_battle'});
      return out(simbot.search(battle, msg));
    }
    if (msg.cmd === 'sideview') {
      if (!battle) return out({error: 'no_battle'});
      const side = battle.sides[sideIndex(msg.side)];
      const from = Math.max(0, Math.min(msg.from || 0, battle.log.length));
      return out({lines: sideLines(battle.log.slice(from), msg.side), request: side.activeRequest || null,
                  pos: battle.log.length, header_end: headerEnd, turn: battle.turn, ended: battle.ended});
    }
    if (msg.cmd === 'preview_view') {
      const i = sideIndex(msg.side);
      const b = new Battle({formatid: msg.format || 'gen9vgc2025regi@@@Terastal Clause', seed: msg.seed});
      b.setPlayer('p1', {name: msg.p1name || 'p1', team: msg.p1});
      b.setPlayer('p2', {name: msg.p2name || 'p2', team: msg.p2});
      const lines = sideLines(b.log, msg.side);
      const request = b.sides[i].activeRequest || null;
      const n = b.log.length;
      b.showOpenTeamSheets();
      const showteam = sideLines(b.log.slice(n), msg.side);
      b.destroy();
      return out({lines, request, showteam});
    }
    if (msg.cmd === 'dump') {        // the live battle serialized (local ablation of the agent's engine search only)
      if (!battle) return out({error: 'no_battle'});
      const st = battle.toJSON();
      st.log = [];
      st.inputLog = [];
      return out({battle: JSON.stringify(st)});
    }
    if (msg.cmd === 'quit') process.exit(0);
    out({error: 'unknown cmd'});
  } catch (e) { out({error: 'exception', detail: String(e && e.stack || e)}); }
});

function out(o) { process.stdout.write(JSON.stringify(o) + '\n'); }
