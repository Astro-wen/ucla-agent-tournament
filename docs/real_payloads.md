# 真实平台 payload 观察记录

来源：`logs/<agent>/<session>.jsonl`（`agent/agent.py` 的 `record()` 写入，git 不跟踪）。
只记录**亲眼看到**的字段；文档里写了但还没见到的不算。

## 2026-09-26 · `pokemon_gen9same` · Yoriko_Red vs Apocalypse_Red（session c8efbea9）

我方视角 36 次决策：1 次 `team_preview` + 35 次 `moving`（含 8 次强制换人）。两个进程日志无任何 warning / fallback。

### 和文档不一样的地方（重要）

| 文档说 | 实际 |
|---|---|
| `gen9same` 只有 `moving` 阶段 | **先有 `team_preview`**：`select_lineup`，"Choose 4 of 6 and designate 2 leads"，`bring_count: 4, lead_count: 2` |
| `gen9same` 是 Gen 9 单打 | `battle_format: gen9customgame`，`is_doubles: false`。是单打，但走的是 6 带 4 的流程 |
| — | observation 里有文档没提的字段：`protocol_log`、`can_tera`、`won` / `lost` / `finished`、`battle_tag`、`waiting_for_action` |

推断（未证实）：平台把所有 Pokémon 变体统一成了"6 带 4 + 首发"流程，VGC 双打大概率同样如此。

### 状态信封（顶层）
`current_actor, game_type, is_current_actor, is_terminal, legal_actions, next_actions, observation, phase, runtime_adapter, session_id, state_version, status`
— 与文档一致。`state_version` 每次我方决策 +1（0,1,2,…35），与文档"每方独立计数"一致。

### `team_preview` observation
`battle_format, battle_tag, opponent_roster, phase, waiting_for_action, your_roster`
`your_roster[i]` 是完整宝可梦视图：`species`(小写 id) `name` `current_hp` `max_hp` `current_hp_fraction` `status` `ability` `item` `types`(大写) `base_stats` `boosts` `moves`(小写 id 列表) `fainted` `active` `revealed` `is_terastallized`
legal action 只有一个：`action_id: "select_lineup"`，`input.action = {type, roster: [ids], bring_count, lead_count, instructions}`（模板，不能原样返回）。

### `moving` observation（单打）
`active_pokemon`（**dict**，非列表）、`opponent_active_pokemon`（dict）、`available_moves`（list）、`available_switches`（list of 宝可梦视图）、`force_switch`（bool）、`team` / `opponent_team`（dict，键形如 `"p1: Garchomp"`）、`side_conditions` / `opponent_side_conditions` / `weather` / `fields`（dict，空时 `{}`）、`turn`、`format`、`is_doubles`、`can_tera`、`protocol_log`、`won` `lost` `finished` `waiting_for_action` `battle_tag`

`available_moves[i]`：`id, type(大写), category(大写 PHYSICAL/SPECIAL/STATUS), base_power, accuracy(0–1 小数), priority, current_pp, max_pp, target(大写，如 ALL_ADJACENT / NORMAL)`

**对手的隐藏信息**：`opponent_active_pokemon.ability = null`、`item = "unknown_item"`、`moves = []`，直到战斗中揭示；`opponent_team` 只含已登场的。→ 在 VGC 选秀里对手 6 只配置是**选秀阶段**公开的（`rosters` / `picks`），战斗阶段 observation 不会替我们记住，**策略必须自己保存选秀信息**（`PokemonPolicy.cards` 已这样做，但目前只存我方，要加对手）。

legal actions：`move:0..3`、`switch:0..4`；`input.action` 例：`{"type":"move","slot":1,"move_id":"dragonclaw","base_power":80,"category":"PHYSICAL","move_type":"DRAGON","current_pp":24,"accuracy":1.0}`；强制换人时只有 `switch:i`，`input.action = {"type":"switch","slot":i,"species":"charizard"}`。传 `input` 原样即可（我们就是这么做的，全部被接受）。

### `protocol_log`：白送的精确数据
`observation.protocol_log` 是 Showdown 原始协议行列表（一局到当前为止，62 行/4 回合），例如
`|-damage|p2a: Garchomp|112/420|[from] ability: Rough Skin|[of] p1a: Garchomp`、`|-heal|…|[from] item: Leftovers`、`|-supereffective|`、`|turn|3`、
`|inactive|Time left: 300 sec this turn | 300 sec total | 5 sec grace`。
→ 精确到点的伤害、道具/特性揭示、出手顺序全在里面；后面做伤害校准和"对手速度线"推断直接读它。300 s 计时器在这一行得到证实。

`can_tera: true` 出现在 gen9customgame 的 observation 里，但 legal actions 里没有任何 tera 动作，与文档"信息性字段"一致。

### 还没见到、仍是假设的
- **双打回合模板**（`doubles_turn` 的 `slots / options / targets`）——等 VGC 房。
- 选秀阶段 observation（`available_cards` 等）——等 VGC 房。
- 狼人杀全部 payload。
