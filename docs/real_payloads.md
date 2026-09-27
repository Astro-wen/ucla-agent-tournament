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

## 2026-09-26 · `pokemon_vgc_doubles_draft` · Yoriko_Red vs Apocalypse_Red（session 21f93775）

房间由人类账号在 dashboard 创建；两边都是 v0。Yoriko 26 次决策（6 选秀 + 1 首发 + 19 回合），打到第 17 回合 Yoriko 胜。两边进程日志 0 条 warning / 回退。

### 选秀
observation：`available_card_ids, available_cards, battle_format(gen9vgc2025regi), current_seat, draft_complete, first_drafter, phase, pick_number, picks, picks_remaining, rosters, unused_card_ids`
卡：`card_id, species, item, ability, nature, level(50), evs, ivs({}), moves`（招式为显示名，如 "Flare Blitz"）
legal：`draft_pick:<card_id>`，`input.action = {"type":"draft_pick","card_id":…}`
**`rosters` / `picks` 只有 `card_id + species`**；完整配置只在卡还在 `available_cards` 时可见，Team Preview 的 `opponent_roster` 里对手 `item="unknown_item"`、`ability=null`、`moves=[]`。
→ 策略必须自己记住见过的每张卡（`PokemonPolicy._track_draft` + `data/vgc_cards.json` 真实卡目录，已实现并测试）。

**卡池不是固定的**：每局从一个更大的卡集里抽 18 张。第一局（21f93775）与第二局（aaf9a12f）只有 8 张重叠，两局并集 28 张，全部无限定级传说，全部通过 Showdown 合法性校验。已见卡集（`data/vgc_cards.json`，每局 `python -m local.pool --harvest` 自动并入）：
Amoonguss, Arcanine, Archaludon, Basculegion, Chi-Yu, Dondozo, Dragonite, Farigiraf, Garchomp, Grimmsnarl, Hatterene, Incineroar, Indeedee-F, Iron Hands, Iron Treads, Iron Valiant, Kingambit, Klefki, Lucario, Milotic, Rillaboom, Sableye, Talonflame, Thundurus, Ting-Lu, Tornadus, Whimsicott, Wo-Chien。
值得注意：有 **Trick Room** 手（Farigiraf、Hatterene）和 Follow Me（Indeedee-F）、双 Tailwind 手（Tornadus、Dragonite、Whimsicott、Talonflame）——速度控制是这个卡集的核心博弈。

### Team Preview
与 gen9same 相同：`select_lineup`，模板 `{roster, bring_count: 4, lead_count: 2, instructions}`。

### 双打回合（与 v0 的假设一致）
observation 新增 `target_legend`；`active_pokemon` / `opponent_active_pokemon` 是**长度 2 的列表**；`available_moves` / `available_switches` 是 `[[…],[…]]`；`force_switch` 是 `[bool, bool]`。
唯一 legal action `doubles_turn`，模板 `{type, slots, target_legend, instructions}`；每个 slot：`slot, board_position(-1/-2), active(完整宝可梦视图，含 boosts), force_switch, options`。
option：招式 `{type:"move", move_id, base_power, category, move_type, current_pp, accuracy, targets}`；换人 `{type:"switch", species}`；`{type:"pass"}`。

实测目标集合：
- 单体招式：slot 0 → `[-2, 1, 2]`，slot 1 → `[-1, 1, 2]`（**`-1/-2` 是固定站位，不是"自己/队友"**；单体招式可以打队友）
- 群攻 / 自身 / 场地：`[0]`（Earthquake、Heat Wave、Rock Slide、Snarl、Protect、Rage Powder）
- 强制换人：需要换人的槽只有 `switch`，另一槽只有 `[{"type":"pass"}]`

本地模拟器对同样的 29 组（槽位, 招式）给出的目标集合与真实平台 **0 处不一致**。

### 还没见到、仍是假设的
- 被锁招（蓄力、讲究道具、Encore）时的模板——本地模拟器里此时招式无目标，平台行为未见。
- 狼人杀全部 payload（需要 7 个 agent 才能开局）。

## 2026-09-27 · 伤害计算器对真实对局的校准（`scripts/calibrate_calc.py`）

用 7 局真实 VGC 对局的 `protocol_log` 逐次对比：双方卡牌配置已知（选秀公开），按日志重放能力变化、场地、天气、双墙、帮助，预测区间 vs 实际掉血（我方真实 HP；对手按 100 分制 ±1）。

**119 次非暴击单段命中：114 次落在计算器区间内（96%）。** 其余 5 次：
- 2 次 **气势披带**（Garchomp、Chien-Pao 满血吃必杀留 1 血）——计算器只算伤害，agent 的 P(KO) 需要自己处理满血披带 / 结实。
- 1 次 **大将（Supreme Overlord）**（Kingambit 队友倒下越多攻击越高）——需要把己方倒下数传给计算器（`alliesFainted`）。
- 2 次是校准脚本没追踪状态异常 / 单目标群攻，非 agent 问题。

平台字段格式补充：`fields` 形如 `{"GRASSY_TERRAIN": 0}`、`{"PSYCHIC_TERRAIN": 4}`（值为剩余回合）；这几局没有出现过天气、双墙或顺风，`weather` / `side_conditions` 一直是 `{}`，格式未验证。`protocol_log` 是整局累积的完整协议（从 `|init|battle` 开始）。

## 2026-09-27 · 线上 A/B：v1（Yoriko）vs v0（Apocalypse）
5 局：**v1 3 胜 2 负**（前两局 v0 胜，后三局 v1 胜）。确认 v1 在官方运行器下全程走精确计算路径（本地重放 10/10 决策完全一致、进程日志无告警）。第二局输在选秀/首发（对手拿到 Tornadus + Rillaboom 首发），双方选秀与首发代码相同。样本太小，不构成 v1 更弱的证据；本地 400 局 v1 对 v0 为 66.8%。
