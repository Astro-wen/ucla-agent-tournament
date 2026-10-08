# 红警（Red Alert）规则型 bot 设计（ra/bot-v0）

状态：设计稿，2026-10-08，分支 `ra/bot-v0`（基于 main 111d14b，v4.2.3）。**v0 已实现**（`agent/redalert/`，v4.3），和本稿不同的地方见第 15 节，测量结果见 `docs/progress.md` 的 v4.3 节。
范围：`red_alert`（OpenRA 引擎，实时 1 对 1）。不调用任何 LLM。主人已同意做规则型 bot；要不要报名红警比赛仍是主人的决定。

资料来源（全部只读）：
- `docs/platform/redalert.md`（平台规则和协议；10-08 和线上 `https://api.altruagent-game.com/skill/redalert` 对比过，差异见第 2.10 节）
- `data/red_alert_config.json`（`get_game_config("red_alert")` 的存档：命令格式、每个单位和建筑的造价、建造时间、电力、前置条件、地图）
- `altruagent/runner.py` 的实时路径，上游 `examples/llm/redalert.py`（平台团队自己的红警玩家）和 `tests/test_realtime.py`
- 我们的 `agent/agent.py`（现在红警整场回答 `WAIT`），`local/fake_redalert.py`
- OpenRA 源码，提交 `9b271c1a5562a07deeb1c3f81d9a556cd563a676`（配置里 `rules.engine_commit` 写的就是它）：`mods/ra/rules/ai.yaml`、`OpenRA.Mods.Common/Traits/BotModules/*`、`mods/ra/rules/{player,vehicles,structures}.yaml`、`mods/ra/weapons/*`

---

## 0. 一页总结（给主人）

**红警怎么比**：两个 AI 各有一辆基地车（MCV）。展开成建造厂以后造电厂、矿厂（自带一辆采矿车，采矿车把矿运回来变成钱）、兵营、战车工厂，然后造兵去拆对方的家。游戏是实时的，每秒 25 个 tick（引擎的时间单位），谁也不等谁。拆光对方所有建筑和 MCV 就赢；20 分钟到了还没分出胜负，就比 `kills_cost - deaths_cost`（我方打掉的敌方价值减去我方损失的价值），再相等比 `assets_value`（我方现存单位和建筑的总价值）。

**我们的策略一句话**：稳定的经济开局（双矿厂或快速战车工厂，按开局资金选）+ 不停产 + 永远不缺电 + 坦克集结成团再按路径推进 + 集火拆防御塔和工厂 + 打不过就撤 + 最后两分钟按平局规则保分或拼分。

**为什么能赢 LLM 和简单脚本**：
1. **快**：我们每 0.3 到 0.5 秒看一次局面、发一批命令，每次决策不到 30 ms。LLM 每次要 2 到 30 秒，建好的建筑常常不放、队列空着、电不够（缺电时生产慢 3 倍）。`docs/competition_brief.md` 引用过的 OpenRA-RL 实验显示，LLM 在 OpenRA 里经常连仗都不打。
2. **运营不出错**：建筑做好的那一刻就放下；队列永远不空；采矿车死了马上补；矿厂放在矿边上。
3. **打法成团**：简单脚本通常把 `attack_now` 一个一个往前送（添油），我们在集结点攒够一团才推，路上按航点等慢车，见到防御塔先集火，打不过就撤回来再攒。
4. **懂计分**：最后两分钟根据 `kills_cost - deaths_cost` 决定是收兵保分还是全军压上。
5. **不会崩**：任何异常都回答 `WAIT`（等下一个画面），平台拒绝的命令有记账、有拉黑，不会刷屏。

**开关**：环境变量 `ARENA_RA=off` 时回到今天的行为（整场 `WAIT`）。默认值在主人决定报名后再定。

**时间表**（第 13 节）：10-09 v0 核心 + 本地模拟器；10-10 到 10-11 主人在平台开 Testing 自对弈、修 bug、加集火/撤退/终局；10-12 冻结候选；10-13 23:59 PDT 截止。

---

## 1. 术语表（第一次接触这个领域时先看）

| 术语 | 意思 |
|---|---|
| tick | 引擎的时间单位，每秒 25 个。`state_version` 就是我们这次看到的画面的 tick |
| MCV / `mcv` | 基地车。`deploy`（展开）后变成建造厂 `fact`，**id 会变** |
| 建造厂 `fact` | 造建筑必须有它。它死了就不能再造建筑（除非再造一辆 MCV） |
| 电厂 `powr` / `apwr` | 发电。`power_provided` 是总发电，`power_drained` 是总耗电。耗电超过发电叫缺电（low power） |
| 矿厂 `proc` | 精炼厂，自带一辆采矿车 `harv`。采矿车把矿石运回矿厂变成钱 |
| 兵营 `tent`（盟军）/ `barr`（苏军） | 造步兵 |
| 战车工厂 `weap` | 造车辆（坦克、采矿车、MCV） |
| 维修厂 `fix` | 盟军 `2tnk` 中坦、苏军 `3tnk` 重坦和 `mcv` 都要它 |
| 生产队列（queue） | `Building`（建筑）、`Defense`（防御塔）、`Infantry`（步兵）、`Vehicle`（车辆）等。**每种队列同一时间只造一个东西** |
| 一批命令（batch） | 一次 `play_action` 发出的 1 到 20 条命令 |
| 判决（verdict） | 平台对每条命令的答复：`pending`（已发出，等引擎）、`ok`、`partial`（部分单位执行）、`rejected`（被拒）、`lost`（没送到引擎） |
| 战争迷雾（fog） | 只能看到自己单位和建筑附近的敌人；看不到的敌人不能用 `attack` 指定 |
| 残骸（wreck / husk） | 车辆被打爆后留下的燃烧外壳，不能下命令，也不值得打 |
| 集结点（rally point） | 工厂造出的单位自动走去的位置 |
| 攻击移动（`attack_move`） | 向目的地移动，路上遇到敌人就打 |
| 集火（focus fire） | 一群单位同时打同一个目标，让它最快死掉 |
| 小队（squad） | 我们自己记账的一组单位（守家、主力、增援） |
| 平局规则（tiebreak） | 时间到时先比 `kills_cost - deaths_cost`，再比 `assets_value` |

---

## 2. 必须遵守的平台协议事实

以下每条都会变成代码里的约束或测试。出处：`redalert.md` 第几节（§），或配置、runner。

### 2.1 一局怎么开始
- 分到红警座位后马上 `get_game_state` 就是签到。两边都签到才开始。锦标赛连接窗口最多 4.5 分钟（270 s），超时算缺席判负（§2、§3）。runner 已经负责这些，我们的策略只管 `phase == "playing"` 之后。
- 开始前 `observation` 是 `null`，`is_current_actor` 是 false；这时发命令得到 `NOT_YOUR_TURN`（§3）。我们看到 `observation` 不是字典就回答 `WAIT`。
- 阵营每局随机：`observation.faction` / `enemy_faction`（例如 `england`、`russia`）。盟军 = `allies`、`england`、`france`、`germany`；苏军 = `soviet`、`russia`、`ukraine`。**能造什么只看 `available_production`**，阵营名只用来选偏好列表的顺序，不用来硬编码能不能造。

### 2.2 我们用到的状态字段（§4）
- 顶层：`state_version`（tick，发命令时原样送回）、`is_current_actor`、`pacing`（`mode`、`min_batch_interval_s` 0.2、`stale_seconds` 120、`next_batch_allowed_in_ms`）、`limits`（`max_orders_per_batch` 20、`max_units_per_order` 50、`max_train_count` 10）、`time`（`max_minutes`、`game_seconds`、`remaining_seconds`）、`match_rules`（`starting_cash`、`starting_units`、`explored_map`、`kill_bounties`、`crates`）、`map`（`id`、`width`、`height`、`bounds [x, y, w, h]`）、`last_orders`、`recent_order_problems`、`legal_actions`、`result`。
- `observation`（大约每 0.2 秒一个新的）：`tick`、`faction`、`enemy_faction`、`economy`（`cash`、`ore`、`power_provided`、`power_drained`、`resource_capacity`、`harvesters`）、`military`（`kills_cost`、`deaths_cost`、`assets_value`、`army_value`、`units_killed`、`units_lost`、`buildings_killed`、`buildings_lost`）、`units`（`id`、`type`、`x`、`y`、`hp` 百分比、`act`、`idle`、`stance`、残骸带 `wreck: true`）、`buildings`（`id`、`type`、`x`、`y`、`hp`、`powered`、`powered_down`、`repairing`、`producing`、`rally`）、`production`（`queue`、`item`、`progress` 0 到 1、`remaining_s`、`paused`、`ready`）、`available_production`、`visible_enemies`、`visible_enemy_buildings`、`base_center`、`explored_percent`、`events`。
- **注意**：敌方 HP 是百分比；敌方建筑不显示电力状态；看不到的敌人不在列表里。

### 2.3 命令格式和上限（§5、`order_schema`）
- 外壳：`play_action(session_id, state_version, action={"type": "orders", "orders": [...]}, reasoning_summary?)`。
- 每批 1 到 20 条；每条 `units` 1 到 50 个**不重复**的整数 id；`train.count` 1 到 10；**一批展开后最多 200 条引擎命令**（单位命令按单位数算，`train` 按 count 算）。超过 20 条或 200 条：整批 `INVALID_ACTION`，什么都不发。
- 未知字段让那条命令 `invalid`。`attack` 必须且只能给 `target` 或 `to` 之一。
- 格子 `[x, y]` 必须在 `map.bounds` 内（可玩区是 `x..x+w-1`、`y..y+h-1`）。在 bounds 外但在 width×height 内：runner 先答 `pending`，引擎之后判 `invalid`（§9）。
- `item` 必须在 `available_production` 里，大小写无所谓。
- 各命令吃什么 id：`move`/`attack_move`/`stop`/`guard`/`set_stance` 吃我方任何单位；`attack`、`stop`、`set_stance`、`guard` 也能点我方建筑（防御塔）；`sell`/`repair`/`power_toggle` 吃我方建筑；`set_primary`/`rally` 只吃 `production_buildings`。

### 2.4 实时节奏（§5、runner）
- **同一时间只能有一批在路上**：上一批还没答复时再发 → `NOT_YOUR_TURN`。runner 是同步的，天然满足。
- **两批之间至少 0.2 s**：太早发的批次会被服务端扣住到满 0.2 s，调用只是变慢。
- **过期**：`state_version` 必须在最近 3000 tick（2 分钟）内且不在未来，否则 `STALE_STATE`。
- runner 行为（`altruagent/runner.py`）：`choose_action` 返回 `WAIT` → runner 调 `wait_for_update(since_version=…)`，新画面一出（约 0.2 s）就再问我们；发了一批 → 立刻 `get_game_state` 再问。`INVALID_ACTION` 和 `STALE_STATE`/`NOT_YOUR_TURN` 在实时模式下被当作竞态：runner 重新读状态继续；`RUNTIME_TEMPORARILY_UNAVAILABLE` 等 1 秒重试。**`choose_action` 抛异常 → `DecisionError` → 这一局的进程被杀**；`on_action_result` 抛异常同样被杀。
- `context.game_config` 在第一次决策前由 runner 取一次（取不到就是 `None`，我们要能退回本地 `data/red_alert_config.json` 的副本或纯 `legal_actions` 逻辑）。
- 上游 PR #7（正在合并到 `integrate/v424`）增加了 `TOO_MANY_WAITS` 处理：平台每个 agent 每局只允许少数几个 `wait_for_update` 同时等待。**我们自己永远不调用 `wait_for_update`**，只交给 runner。

### 2.5 判决（§5）
- `play_action` 立即答复每条命令 `{index, cmd, status, reason, detail, commands, failed}`；`status` 是 `rejected`（附原因）、`pending`，或者 `ok`（`repair`/`power_toggle` 没有改变任何东西时，`detail` 是 `already_repairing`/`not_repairing`/`already_on`/`already_off`）。
- 引擎大约 0.2 到 0.4 s 内执行 `pending` 命令，最终判决出现在之后某个状态的 `last_orders`（最新一批：`{state_version, batch, carried, pending, applied_at_tick, results, accepted_orders, rejected_orders}`，`pending` 变 false 时行变成最终结果）。
- 如果最终判决到达前我们又发了新批次：看 `recent_order_problems`（最近 5 个已结算批次里 `rejected`/`partial`/`lost` 的行，最多 10 行，最新的在最后）。
- 拒绝原因：runner 的 `invalid`、`not_owned`、`wreck`、`is_building`、`target_not_visible`、`not_available`、`not_ready`；引擎自己的（例如 `unknown_actor`），以及队列满时 `build`/`train` 的 `not_ready`。`lost`（`reason: not_applied`）= 没送到引擎，要重发；`repair`/`power_toggle` 在 lost 后 2 秒内重发可能被当成无变化，要 2 秒后再发。
- 单位命令里个别 id 失效只跳过那几个（`partial`），全部失效才 `rejected`。
- **整批没有一条有效** → `INVALID_ACTION`，不算一批，不占 0.2 s 间隔；`detail` 形如 `[i] cmd: reason (detail)`。只含"无变化但有效"命令的批次会被接受并计数。

### 2.6 建造与放置（§4、§5、§8、§9）
- `build item` 加到队列末尾；做好后在队列里等，直到我们 `place`。**做好的建筑不放下，Building 队列就一直被堵着**（OpenRA 的经典队列每种类型同时只造一个）。
- `place item at?`：`at` 是左上角格子的建议。缺省、被挡、离基地太远、在 bounds 外（但在 width×height 内）→ 引擎自动放在建造厂附近，仍然 `ok`。在 width×height 外 → `invalid`；**基地附近已经没有空地** → `invalid`。做好之前 `place` → `not_ready`。
- `deploy` 把 MCV 变成 `fact`，**新 id**，只有一个 `building_created` 事件；没地方展开 → `invalid`。
- `production_ready` 事件告诉我们建筑做好了，但我们以 `production[].ready` 为准。

### 2.7 电力（§4、§8、OpenRA `player.yaml`）
- `low_power` 事件 = 耗电超过发电；`power_restored` = 恢复。缺电时建筑 `powered: false` 不工作。
- **OpenRA RA 的所有生产队列 `LowPowerModifier: 300`：缺电时生产时间变成 3 倍。** 这是最致命的运营错误，我们的电力管理必须保证几乎从不缺电。
- 生产不是预付费（`PayUpFront: false`）：钱随进度扣，钱不够时进度停住。所以多排几个单位不会锁钱，但会决定谁先拿到钱。

### 2.8 战争迷雾、残骸、id
- 只能 `attack` 看得见的敌人 id，否则 `target_not_visible`；之前见过但现在看不见的 id 不能用（§9）。对看不见的目标用 `attack_move` 到格子。
- 残骸（`wreck: true`，类型以 `husk` 结尾）会在我方 `units` 和 `visible_enemies` 里停留一段时间：一律过滤掉。
- 单位上运输车后从 `units` 消失并产生 `unit_lost`。死 id 在命令里只是被跳过。每次都从当前 `units` 重新取 id。

### 2.9 事件（§4）
- 只保留最近 20 条，每条 `{seq, tick, kind, …}`，`seq` 每个座位每事件 +1；两次读之间 `seq` 有缺口说明有事件被挤掉了。
- 类型：`unit_created`、`building_created`、`unit_lost`（可能带 `wreck_id`）、`building_lost`、`building_sold`、`production_ready`、`enemy_spotted`、`enemy_killed`（带 `count`）、`under_attack`（每个单位每 10 s 最多一次）、`low_power`、`power_restored`。
- **决策以状态差分为主**（这一画面和上一画面的 `units`/`buildings`/HP 对比），事件只当提示。`under_attack` 的具体字段平台文档没写全，等第一份真实录像确认。

### 2.10 规则数值以运行时为准（文档之间有出入）
- **开局资金**：`redalert.md` 说"写这份文档时每局 $5,000"；`get_game_config` 的 `match_rules` 文字说 `starting_cash (default 10000)`。**以 `state.match_rules.starting_cash` 为准**，开局按资金选两套建造顺序之一（第 5 节）。
- **空闲规则（idle rule）**：本地 `redalert.md` 和配置 `timeouts.idle_seconds` 写 600 s（10 分钟）；10-08 线上版写 20 分钟。不管哪个，只要我们有被引擎接受的命令就不会触发；我们一直在发命令，不受影响。
- **时长**：Testing 由主人选 10 到 20 分钟（默认 20），UCLA 锦标赛 20 分钟。用 `time.remaining_seconds`，不写死。
- 其他线上差异：`players[].agent_id` 对手是 `null`；服务器自己出问题导致无结果的局在锦标赛里重赛，最多两次。
- 地图池（配置里）：`sidestep`、`trapped`、`shadowfiend-ii`（第 4.4 节）。比赛地图由组织方定，可能不在池里：要能在没有地图图片时退回直线航点。

### 2.11 胜负与平局（§7、OpenRA `PlayerStatistics`）
- 胜利：拆光对方**所有建筑和 MCV**，剩下的单位不算。
- 时间到或空闲规则：比 `(kills_cost - deaths_cost, assets_value)`，取各自最新观察里的 `military`。
- OpenRA 源码确认：`kills_cost` = 我们击杀的敌方单位/建筑造价之和；`deaths_cost` = 我们被击杀的造价之和（**卖掉不算死亡**）；`assets_value` = 我们现存单位和建筑的造价之和（**现金不算**）。
- 只有两个玩家时，对方的 `kills - deaths` 约等于我们的 `deaths - kills`（除非有中立伤害）。所以**我方 `kills_cost > deaths_cost` 基本就赢平局**。
- 平局是 0.5/0.5；锦标赛瑞士轮里平局就是平局。

### 2.12 `legal_actions`（§4）
每个列表都在（没有时为空）：`build`、`train`、`place`、`deploy`（我们的 MCV）、`repair`（受损且没在修的建筑，HP 低的在前）、`attack`（看得见的敌方单位，然后建筑）、`attack_now`（现成的一条 `attack_move`：空闲战斗单位，或者没有空闲时全部战斗单位，最多 50 个，去最近的可见敌人，看不到敌人就去 `enemy_base`；没有战斗单位时为 `null`）、`enemy_base`（离我们基地最远的出生点）、`combat_units`（空闲的战斗单位，不含 MCV、采矿车、医疗兵、工程师、船）、`idle_units`（**包括非战斗单位**）、`harvesters`、`production_buildings`（`{id, type}`）、`how_to_send`。
我们用它们做交叉校验和兜底（安全模式用 `attack_now`），主逻辑读 `observation`。

### 2.13 Windows 模式（§5）
运营方可以把服务器切成 5 秒窗口：`pacing.mode == "windows"`，`state_version` 变成窗口编号，每个窗口一批。**这时 runner 的 `_is_realtime` 为假，走回合制路径：返回 `WAIT` 会 `DecisionError`，`INVALID_ACTION` 也会 `DecisionError`，进程都会被杀。** 所以：
- 路由不能只看 `pacing.mode == "realtime"`，还要看 `game_type == "red_alert"`；
- windows 模式下每次被问都必须交出一批**至少一条有效**的命令；没事可做时发无害命令 `{"cmd": "power_toggle", "building": <fact id>, "on": true}`（已经开着 → `ok already_on`，被接受）；连建筑都没有时 `{"cmd": "stop", "units": [<任一我方非残骸单位>]}`；
- 每批的命令都必须通过本地校验，确保不会整批无效。

### 2.14 `reasoning_summary`
可选，最多 500 字符，存档后公开，比赛进行中观众看不到，只在结束后看到；但我们的**命令摘要**可能被实时展示。观众能看到双方，所以我们的打法本来就不是秘密。我们只发很短的阶段标签（第 11.8 节）。

### 2.15 箱子（crates）
`match_rules.crates` 当前是开。OpenRA RA：地图上 1 到 3 个，第一个在 1500 tick（60 s）后，之后每 3000 tick（2 分钟），存在 4500 tick（3 分钟）。**观察里没有箱子字段**，所以我们没法主动去捡，只能路过时碰到。v0 不处理箱子。

---

## 3. OpenRA 自带 AI 参考（`mods/ra/rules/ai.yaml` @ 9b271c1a）

RA 模组带 7 个内置 bot：beginner、easy、medium、rush、normal、turtle、naval。它们由几个模块组成。下面是对我们有用的部分。

### 3.1 建造（`BaseBuilderBotModule` + `BaseBuilderQueueManager`）
每次挑下一个建筑的优先级（`ChooseBuildingToBuild`）：
1. **先解决缺电**：剩余电力 < 目标余量时造发电量最大的电厂。目标余量是动态的：`MinimumExcessPower + ExcessPowerIncrement × floor(建筑数 / ExcessPowerIncreaseThreshold)`，上限 `MaximumExcessPower`。normal：0 起，每 4 座建筑 +40，最多 200；rush：每 4 座 +40，最多 160；turtle：20 起，每 2 座 +20，最多 250。
2. **矿厂数量够不够**：`InititalMinimumRefineryCount`，有了生产建筑后再加 `AdditionalMinimumRefineryCount`。normal/rush/medium：0 + 2；easy：1 + 1；beginner：1 + 0。造矿厂会导致缺电就先造电厂。
3. **钱太多就加生产建筑**：现金 > `NewProductionCashThreshold`（normal 8000、rush 7000、medium 9000、easy 10000）时 50% 概率造兵营或战车工厂。
4. 储量 > 容量 80% 时造储油罐 `silo`。
5. 其余按 `BuildingFractions`（每种建筑占总建筑数的百分比）随机挑一个还没达标的，受 `BuildingDelays`（多少 tick 之后才允许）和 `BuildingLimits`（上限）限制；会导致缺电就改造电厂。

normal 的比例：powr 1、proc 1、tent/barr 3、kenn 1、dome 1、weap 4、hpad/afld 1、pbox 9、gun 9、ftur 10、tsla 5、gap 2、fix 1、agun 5、sam 1、atek/stek 1、mslo 1。延迟：dome 6000 tick（4:00）、fix 3000（2:00）、pbox 1500（1:00）、gun 2000、ftur 1500、tsla 2800、kenn 7000、atek/stek 9000（6:00）。上限：tent/barr 7、weap 4、dome 1、fix 1、hpad/afld 4。
rush：weap 上限 4、比例 weap 4、tent/barr 3，dome 比例 10（很高），fix 延迟 3000。turtle：weap 和兵营各 1 座，防御塔比例 12 到 13。
防御塔放在朝向最近敌方建筑的方向（`PlaceDefenseTowardsEnemyChance` 100%），离基地中心 5 到 20 格；矿厂放在矿附近。放置连续失败 3 次就取消并等一段时间。

### 3.2 采矿车（`HarvesterBotModule`）
`InitialHarvesters`：normal/turtle 4、medium 3、rush/easy 2、beginner 1。采矿车少于这个数或少于矿厂数就补造。空闲采矿车每 50 tick 被派去采矿，选矿时避开 10 格内的敌人。

### 3.3 造兵（`UnitBuilderBotModule`）
每 30 tick 轮流看一个单位队列；队列空闲时，在可造单位里挑"实际占比 - 目标占比"最负的那个（`count×100/total - share < 0` 就造）。现金低于 `ProductionMinCashRequirement`（normal 默认 500）不造。
normal 的目标占比（相对权重）：e1 65、e3 30、e2 15、e4 15、dog 15、shok 15、harv 15、apc 30、jeep 20、arty 15、v2rl 40、ftrk 30、1tnk 40、2tnk 50、3tnk 50、4tnk 25、ttnk 25、stnk 5，加上空军和海军各 10 到 30。上限：dog 4、harv 8、jeep 4、ftrk 4。rush 差不多，没有空海军，1tnk 50。

### 3.4 小队和进攻（`SquadManagerBotModule`、`Squads/*`）
- 新单位先"在基地附近闲逛"。闲逛数 ≥ `SquadSize + random(SquadSizeRandomBonus)` 时整体组成进攻小队（Assault）。normal：40 + random(30，默认值) = **40 到 69 个单位才进攻**；rush：20 + random(30)；medium：20 + random(15)；easy：8 + random(4)；beginner：3 + random(2)；turtle：10 + random(30) 但 `MinimumAttackForceDelay` 100000 tick（实际上不进攻）。medium 两次组队至少隔 5000 tick（3:20）。
- 突袭（Rush）：每 `RushInterval`（默认 600 tick = 24 s；medium 800、easy 2500）检查一次，地面部队总数 ≥ `SquadSize` 并且模糊逻辑判断打得过敌方建造厂 15 格内的敌军，就去打。
- 小队状态机：Idle → AttackMove（选队伍中心的慢车当"队长"，单位离队长超过 `单位数/3` 格时队长停下、其他人向队长攻击移动，等齐了再走）→ Attack（12 格内有敌人：没在开火的单位攻击移动到目标）→ Flee（10 格内敌我模糊比较打不过就撤到随机一座己方建筑并解散；10 格内有己方建筑时从不撤）。卡住检测：队长 63 tick（2.5 s）没动就回 Idle。
- 防守（Protection）：`ProtectionTypes`（采矿车、MCV、各种建筑）被可见敌人攻击时（冷却 30 tick），基地附近能打到攻击者的闲置单位组成防守小队；附近 15 格内的小队也改打攻击者。

### 3.5 其他模块
`BuildingRepairBotModule`（建筑受损就修）、`PowerDownBotModule`（缺电时关掉 dome/tsla/mslo/agun/sam）、`McvExpansionManagerBotModule`（normal/rush/turtle 要 2 个建造厂，会造第二辆 MCV 扩张）、`SupportPowerBotModule`（超级武器）。

### 3.6 我们借鉴什么、不借鉴什么
借鉴：
- 优先级顺序：**缺电 > 矿厂数量 > 钱多加工厂 > 其他**；动态电力余量（建筑越多留得越多）。
- 采矿车数量 ≥ 矿厂数，死了立刻补。
- 防守响应：建筑或采矿车被打 → 附近闲置单位去打攻击者，有冷却。
- 小队按队长等齐再走；卡住检测；打不过就撤回基地。
- 防御塔朝敌方方向放；建筑受损就修；放置失败 3 次就取消。

不借鉴：
- **40 到 69 个单位才进攻**：20 分钟的比赛太慢，对 LLM 也没必要；我们 5 到 8 辆坦克就推。
- 随机挑建筑和单位：我们用确定的顺序（方便测试和复现）。
- 大量防御塔（turtle 风格）、空军、海军、科技中心、超级武器、扩张第二基地：v0 都不做。

---

## 4. 游戏数据与经济估算

### 4.1 核心建筑（`rules.buildings`，满电时的建造时间）

| 建筑 | 造价 | 时间 s | 电力 | 前置 | 队列 | 说明 |
|---|---|---|---|---|---|---|
| `powr` | 300 | 7.2 | +100 | 无 | Building | 2×3 |
| `apwr` | 500 | 12 | +200 | dome | Building | v0 不用 |
| `proc` | 1400 | 33.6 | -30 | 任一电厂 | Building | 3×4，自带 harv，存 2000 |
| `tent`（盟）/`barr`（苏） | 500 | 12 | -20 | 任一电厂 | Building | |
| `weap` | 2000 | 48 | -30 | proc | Building | 3×3 |
| `fix` | 1200 | 28.8 | -30 | weap | Building | 2tnk/3tnk/mcv 的前置 |
| `dome` | 1500 | 36 | -40 | proc | Building | arty/v2rl/apwr 的前置，v0 不用 |
| `silo` | 150 | 3.6 | -10 | proc | Defense | 储量快满时 |
| `pbox`（盟） | 600 | 14.4 | -20 | tent | Defense | 机枪碉堡，打步兵 |
| `gun`（盟） | 800 | 19.2 | -40 | tent | Defense | 炮塔 6000 伤害、射程 6.5，打车辆 |
| `ftur`（苏） | 600 | 14.4 | -20 | barr | Defense | 火焰塔 15000×2，射程 5，打步兵 |
| `tsla`（苏） | 1200 | 28.8 | -80 | weap | Defense | 特斯拉线圈 10000，射程 7，耗电大 |

### 4.2 核心单位（`rules.units`）

| 单位 | 造价 | 时间 s | HP | 速度 格/s | 武器 | 前置 | 用途 |
|---|---|---|---|---|---|---|---|
| `e1` | 100 | 2.4 | 5000 | 1.32 | 步枪，射程 5 | 兵营 | 早期守家、哨兵 |
| `e2`（苏） | 150 | 3.6 | 5000 | 1.66 | 手雷，打步兵和建筑 | barr | 苏军反步兵 |
| `e3` | 300 | 7.2 | 4500 | 1.32 | 火箭，对重甲 100%，也能防空 | 兵营 | 反坦克步兵 |
| `1tnk`（盟） | 700 | 16.8 | 23000 | 2.76 | 25mm，对轻甲 116%、重甲 48% | weap | 盟军早期、追采矿车 |
| `2tnk`（盟） | 850 | 20.4 | 46000 | 1.76 | 90mm，对重甲 115% | fix | **盟军主力** |
| `3tnk`（苏） | 1150 | 27.6 | 60000 | 1.56 | 105mm 双发，对重甲 115% | fix | **苏军主力** |
| `4tnk`（苏） | 2000 | 48 | 90000 | 1.05 | 120mm | fix + stek | v0 不用 |
| `apc`（苏） | 850 | 20.4 | 35000 | 3.12 | 机枪，对重甲 10% | weap | 不当主力 |
| `ftrk`（苏） | 600 | 14.4 | 15000 | 2.76 | 防空炮，对重甲 10% | weap | 只在敌方有飞机时 |
| `jeep`（盟） | 500 | 12 | 15000 | 4.0 | 机枪 | weap | v1 骚扰 |
| `harv` | 1100 | 26.4 | 60000 | 1.76 | 无 | proc | 一车 20 包，矿 25/包 = 500，宝石 50/包 = 1000 |
| `mcv` | 2000 | 40 | 60000 | 1.46 | 无 | fix | 建造厂死后重建 |

要点：**苏军在维修厂之前没有像样的坦克**（apc、ftrk 打重甲只有 10%），所以苏军要尽早 `fix`，之前靠 `e1`/`e3`/`e2` 守家。坦克是履带，能碾死步兵。三种主战坦克的花钱速度都是约 41.7 元/秒（700/16.8 = 850/20.4 = 1150/27.6），步兵队列也是 41.7 元/秒（100/2.4、300/7.2）。所以"一个战车工厂 + 一个兵营"满负荷每分钟花约 5000。

多个工厂的加速（OpenRA `ClassicProductionQueue`）：Vehicle 队列 1/2/3/4 座 weap 时建造时间 100%/75%/60%/50%；其他队列默认 100/86/75/67/60/55/50%。所以第二座 weap（2000）让车辆产能 +33%。

### 4.3 收入估算（要在平台上实测）
采矿车装满 20 包：装载 4 tick/包（80 tick = 3.2 s，再加在矿格之间挪动），卸货 1 tick/包加对接动画约 2 到 3 s，满载速度 85%，矿地速度 88%。
- 矿离矿厂约 5 格：一趟约 14 s → 约 36 元/s（约 2100/分钟）。
- 矿离矿厂约 10 格：一趟约 20 s → 约 25 元/s（约 1500/分钟）。
结论：**矿厂一定要放在矿边上**（用 `place` 的 `at` 指向离基地最近的矿区，第 6.3 节）。估算时按每车 1200 到 2000/分钟。

### 4.4 地图（`rules.maps`，从 `layout` 图片算出）

| 地图 | 可玩区 | 出生点 | 直线距离 | 地面路径（8 方向步数） | 出生点 12 格内矿格 |
|---|---|---|---|---|---|
| `sidestep`（雪地） | 96×96 | [23,44]、[74,53] | 52 | 51 | 各 58 |
| `trapped` | 97×97 | [66,72]、[26,32] | 57 | 40 | 各 28，有宝石 |
| `shadowfiend-ii` | 83×90 | [15,80]、[68,10] | 88 | 70 | 69 / 56 |

坦克（1.56 到 1.76 格/s）从家走到对方家：sidestep、trapped 约 30 到 40 s，shadowfiend-ii 约 55 到 65 s。

### 4.5 开局时间线估算（粗模型，设计时用一个几十行的脚本算的，没有进仓库）
模型：满电、按进度扣钱、每队列同时一个、展开 3 s、每车每趟 500 元、一趟 25 s（约 1200/分钟）。结果（建成时刻，秒）：

| 方案 | 开局资金 | 第一个 proc | 第二个 proc | weap | fix | 4:00 / 6:00 / 8:00 军队价值 |
|---|---|---|---|---|---|---|
| 盟军 快速工厂（weap 先） | 5000 | 44 | 159 | 104 | 199 | 1700 / 6800 / 11900 |
| 盟军 双矿厂（proc 先） | 5000 | 44 | 90 | 145 | 174 | **3400 / 8500 / 12750** |
| 苏军 快速工厂 | 5000 | 44 | 194 | 104 | 140 | 500 / 6250 / 10850 |
| 苏军 双矿厂 | 5000 | 44 | 90 | 145 | 174 | **2800 / 7400 / 13150** |
| 盟军 快速工厂 | 10000 | 44 | 145 | 104 | 174 | **5500 / 10600 / 15700** |
| 苏军 快速工厂 | 10000 | 44 | 174 | 104 | 140 | **3950 / 8550 / 15450** |

一趟 18 s（约 1700/分钟）时排序不变。结论：**开局资金 < 8000 用"双矿厂"，≥ 8000 用"快速工厂"**（10000 资金时双矿厂到 8:00 略多，但 4:00 到 6:00 少，早期更怕被压）。模型很粗（没算多余采矿车、单位损失、放置延迟），只用来定顺序，具体数值在平台上测。

---

## 5. 开局建造顺序（两个阵营）

总原则：Building 队列一直有活；建筑做好的那一帧就 `place`；每一步开工前先过电力检查（第 6.1 节）；能造什么以 `available_production` 为准。下表的"电力余量"是这一步建成后的 `power_provided - power_drained`。

### 5.1 开局 A：双矿厂（`starting_cash < 8000`，当前平台文档的 5000 走这个）

盟军（`tent`；苏军把 `tent` 换成 `barr`，`fix` 提前一步，见 5.3）：

| 步 | 命令 | 造价 | 时间 s | 电力余量 | 前置 | 预计建成 |
|---|---|---|---|---|---|---|
| 0 | `deploy` MCV（id 来自 `legal_actions.deploy`） | 0 | 约 3 | 0 | 无 | 3 s |
| 1 | `build powr` → `place` | 300 | 7.2 | +100 | fact | 10 s |
| 2 | `build proc` → `place at=矿边` | 1400 | 33.6 | +70 | 电厂 | 44 s（自带第 1 辆采矿车） |
| 3 | `build tent` → `place` | 500 | 12 | +50 | 电厂 | 56 s |
| 4 | `build proc` → `place at=矿边` | 1400 | 33.6 | +20 | 电厂 | 约 90 s（第 2 辆采矿车） |
| 5 | `build powr` | 300 | 7.2 | +120 | | 约 97 s |
| 6 | `build weap` | 2000 | 48 | +90 | proc | 约 145 s |
| 7 | `build fix` | 1200 | 28.8 | +60 | weap | 约 175 s |
| 8 | `build powr` | 300 | 7.2 | +160 | | 约 182 s |
| 9 起 | 进入中期规则（第 6 节）：多余采矿车、第二座 weap、第三个矿厂、按需电厂 | | | | | |

步兵队列：兵营一建好，造 3 个 `e1`（守家和前哨）。车辆队列：weap 建好后盟军先造 `1tnk`（最多 2 辆，用来守家和打早期骚扰），fix 一好就转 `2tnk`。

### 5.2 开局 B：快速工厂（`starting_cash >= 8000`）

| 步 | 盟军 | 苏军 | 预计建成（10000 资金） |
|---|---|---|---|
| 1 | powr | powr | 10 s |
| 2 | proc（矿边） | proc（矿边） | 44 s |
| 3 | tent | barr | 56 s |
| 4 | weap | weap | 104 s |
| 5 | powr | powr | 111 s |
| 6 | proc | **fix** | 盟 145 s / 苏 140 s |
| 7 | fix | proc | 盟 174 s / 苏 174 s |
| 8 | powr | powr | 约 181 s |
| 9 | weap #2 | weap #2 | 约 229 s（钱多时） |
| 10 | proc #3 | proc #3 | 约 262 s |

钱充足时 Building 队列之外的队列同时满负荷：步兵队列 3 到 5 个步兵，车辆队列从 weap 建好起不停。

### 5.3 苏军的差别
- 苏军在 fix 之前没有能打坦克的车，所以开局 A 的苏军顺序是：powr → proc → barr → proc → powr → weap → **fix → powr**（和盟军同序，但 weap 建好后车辆队列先空着，除非要补采矿车）；开局 B 的苏军把 fix 放在第二个 proc 之前（上表）。
- 苏军兵营早期造 3 个 `e1` + 2 个 `e3`（e3 火箭打车辆 100%）。敌方有早期坦克压力时继续补 `e3`。
- 第一辆 `3tnk` 大约在 fix 建好后 28 s 出来（开局 B 约 168 s，开局 A 约 200 s）。

### 5.4 被早期压制时（两个开局通用）
"早期"指 5:00 之前、我方军队价值 < 2000。看到敌方战斗单位进入基地 15 格内，或建筑被打：
- 步兵队列立刻连续造兵（敌方以步兵为主 → `e1`；有车辆 → `e3`；苏军对步兵可以 `e2`）。
- Defense 队列造 1 座防御塔：敌方步兵为主 → 盟 `pbox`/苏 `ftur`；敌方车辆为主 → 盟 `gun`/苏 `tsla`（要 weap，没有就 `ftur`）。`place at=` 基地中心朝威胁方向 5 到 6 格。早期最多 2 座，之后每次新的进攻潮最多再加 1 座，总数不超过 4。
- 建造顺序不倒退：矿厂和 weap 照常造（经济是赢的根本），只是 Defense 队列并行出塔。

---

## 6. 中期运营规则

每个画面都跑一遍（便宜），由"已发未见"账本保证不会重复下单（第 11.4 节）。

### 6.1 电力
- `surplus = power_provided - power_drained`。
- 目标余量 `margin = clamp(10 + 10 × floor(建筑数 / 4), 10, 60)`（借鉴 OpenRA 的动态余量，数值调小，因为我们不造高耗电建筑）。
- 开工任何耗电 `d` 的建筑之前：如果 `surplus - d < margin` 且没有电厂在造或待放，先造 `powr`（钱 ≥ 1000 并且 `apwr` 在 `available_production` 里时可以造 `apwr`）。
- 缺电（`surplus < 0` 或 `low_power` 事件）：下一个开工的建筑必须是电厂。正在造的建筑不取消（取消会浪费已付的钱和进度），它一做完就放下，然后马上造电厂；Building 队列空闲时立刻造。电厂被打掉造成缺电时同样处理。钱不够造电厂时，用 `power_toggle on=false` 关掉 `dome`、`tsla`（不在被攻击时）、`agun`、`sam`、`gap`（借鉴 `PowerDownBotModule`），恢复后再打开。
- 不变量（测试会检查）：除了电厂刚被打掉，缺电累计不超过 5 s。

### 6.2 矿厂和采矿车
- 目标：2 个矿厂（开局里就有）；3 个采矿车（第 3 个用车辆队列造 `harv`，在 fix 之后或 4:00 之前）；钱多（第 6.4 节的条件）时第 3 个矿厂。矿厂上限 4，采矿车上限 `2 × 矿厂数` 且不超过 8。
- **采矿车数 < 矿厂数** → 车辆队列下一个必须是 `harv`（最高优先级，除非基地正在被打）。
- 空闲的采矿车（`idle` 且类型是 `harv`）→ `harvest`（不给 `to`，引擎自己找矿）。连续 3 次空闲（附近没矿了）→ `harvest to=` 地图图片里离矿厂最近的另一片矿（`$`、`*`、`M`）。
- 采矿车被打（HP 下降且 6 格内有敌人）→ 守家小队去打；没人能去时让它回矿厂（`move` 到最近的矿厂），10 s 后再 `harvest`。
- `ore > 0.8 × resource_capacity` 时造 `silo`（钱快满了会浪费）。我们一直在花钱，这条很少触发。

### 6.3 放置位置
- 默认不给 `at`，让引擎放在建造厂附近（平台说这样总是 `ok`）。
- 矿厂：在地图图片里找离 `base_center` 最近的矿格簇（15 格内），`at` = 从基地中心往矿的方向、离矿约 3 格的点，再按 3×4 占地换成左上角。被挡就由引擎自动放。
- 防御塔：`at` = 基地中心朝威胁方向（没有威胁时朝 `enemy_base`）5 到 6 格。
- 放置 `invalid`（没地方）：在基地中心 6 到 10 格的圆环上按螺旋试 `at`（选地图图片上是 `.` 的格子并避开已知建筑占地），3 次都失败 → `cancel` 这个建筑（退钱），60 s 内不再计划它。

### 6.4 第二座战车工厂和更多生产
满足任一条件就加第二座 `weap`（上限 2，v0 不造第三座）：
- 车辆队列连续 30 s 都在忙，且现金 > 2000 持续 20 s；
- 开局 B（资金 ≥ 8000）在第一轮建造顺序里直接安排；
- 采矿车 ≥ 3 且游戏时间 ≥ 4:00 且现金 > 1500。
现金 > 3000 持续 30 s 而且两座 weap 都在忙 → 第 3 个矿厂（再多一车收入）或步兵队列连续出 `e3`。
第二座 weap 建成后用 `set_primary` 指定离集结点近的那座。

### 6.5 修理
- `legal_actions.repair` 里的建筑，HP < 70% 且现金 > 500 → `repair`（幂等，重复发也不会关掉）。
- 建造厂 HP < 50% 时不看现金，立刻修，并让守家小队回防。
- 现金 < 300 时停修（`repair on=false`），把钱留给生产。

### 6.6 集结点
- 每座生产建筑（`production_buildings` 里的兵营、weap）出现后发一次 `rally to=集结点`；它的 `rally` 字段和我们的集结点不同时（5 s 冷却）重发。
- 集结点 = 从我方出生点到敌方出生点的地面路径上、离基地中心约 10 步的格子（第 8.2 节）。新单位自动走过去，不用每个都下命令。
- 兵营的第一个 `e1` 派去更远的"前哨点"（路径上约 25 步处）站着，提前看到来犯的敌人。

### 6.7 不卖建筑（只有一个例外）
v0 从不 `sell`：卖掉会减少 `assets_value`，也减少"敌方必须拆光的建筑"。放置失败用取消解决。
**10-08 复审后的例外**：采矿车全灭（有矿厂、0 辆采矿车，持续 5 s，剩余时间 > 90 s）时进入「采矿车优先」：先排一辆采矿车（没有 weap 就造一座矿厂，矿厂自带一辆），取消排在它前面的单位和其它队列里在造的东西（退款是全额的已付款），不开新建筑、不造步兵（基地被威胁时除外）、停修理；钱还是不够、采矿车停在半路时才卖一座用不着的建筑（剩余时间 > 300 s，一次一座，每次采矿车危机最多 3 座；顺序：没存矿的 silo、雷达、多余的兵营、基地安静时的防御塔、多余的 weap / 矿厂 / 电厂；从不卖建造厂、最后一座矿厂 / 电厂 / 兵营 / weap、维修厂）。复审时模拟器里采矿车全灭且现金清零的局面，旧版整局 0 收入。
缺电而 Building 队列在造别的（10-08 复审后）：那一项在三分之一速度下还要超过 20 s 才完，就取消它（全额退款），先造电厂，并给电厂留最多 300。

---

## 7. 兵种组合

规则：每个单位队列保持"1 个在造 + 最多 1 个排队"（钱随进度扣，排太多会让优先级失灵）；只在 `available_production` 里有该单位时才下单。"经济优先"：第二个矿厂在造而现金不够它剩下的钱时，车辆和步兵队列暂停排新单位，**除非**基地正在被威胁。

### 7.1 盟军（`allies`、`england`、`france`、`germany`）
- 步兵：开局 3 个 `e1`；之后只有在（现金 > 1500）或（基地被威胁）时继续：敌方车辆多 → `e3`，敌方步兵多 → `e1`。活着的步兵最多 10 个。
- 车辆：`harv` 补足 > `mcv`（建造厂死了）> 主战坦克。fix 之前 `1tnk`（最多 2 辆，被压制时最多 4 辆）；fix 之后 `2tnk` 为主，每 5 辆 `2tnk` 配 1 辆 `1tnk`（快，用来追采矿车和收尾）。
- v1 可选：`arty`（要 dome，射程 12，拆防御塔）、`jeep` 骚扰采矿车。

### 7.2 苏军（`soviet`、`russia`、`ukraine`）
- 步兵：开局 3 个 `e1` + 2 个 `e3`；fix 之前被坦克压就连续出 `e3`，被步兵压出 `e2`/`e1`；之后同盟军的规则。最多 10 个。
- 车辆：`harv` 补足 > `mcv` > `3tnk`（fix 之后 100%）。fix 之前不造 `apc`/`ftrk`，除非看到敌方飞机（`heli`、`mh60`、`yak`、`mig` 出现在 `visible_enemies`），那时造 2 辆 `ftrk`。
- v1 可选：钱很多时 `stek` + `4tnk`；`v2rl`（要 dome）。

### 7.3 偏好列表（不靠阵营名硬编码）
主战坦克按顺序取第一个可造的：`["3tnk", "2tnk", "1tnk"]`（盟军在 fix 之前只有 `1tnk` 可造，苏军在 fix 之前三者都不可造 → 车辆队列只造 `harv`）。反坦克步兵 `e3`，反步兵 `e1`（苏军有 `e2` 时用 `e2`）。防空：盟 `e3`，苏 `ftrk`。防御塔：反步兵 `["ftur", "pbox"]`，反车辆 `["tsla", "gun", "ftur", "pbox"]`。全部按 `available_production` 过滤。

---

## 8. 军队控制

### 8.1 小队
- **守家（HOME）**：所有步兵 + 集结点上刚出厂的单位。负责基地中心 15 格内和采矿车附近。
- **主力（MAIN）**：进攻的坦克团。
- **增援（REINF）**：主力在外时，集结点攒够的一组，去追主力。
小队只是我们自己的账（单位 id 集合），每个画面用 `units` 清理死掉的和残骸。

### 8.2 路径和航点
- 第一次决策时（每局一次，几毫秒）：在地图图片上做 BFS，可走字符是 `.`、`,`、`=`、`$`、`*`、`M`、`S`、`_`，从我方出生点到敌方出生点（敌方出生点 = 离 `base_center` 最远的出生点，和 `legal_actions.enemy_base` 交叉核对）。
- 航点：路径上每 12 步取一个。集结点 = 第 10 步；前哨点 = 第 25 步；攻城集结点 = 离敌方出生点 18 步处。
- 地图不在池里（没有图片）：直线插值航点，夹在 `bounds` 内。

### 8.3 主力状态机
1. **集结 GATHER**（在集结点等）：满足进攻条件（8.4）→ 推进。
2. **推进 ADVANCE**：对全队发 `attack_move` 到当前航点；全队 80% 的单位到了航点 5 格内，或已过 20 s，就换下一个航点（这样快车会等慢车，借鉴 OpenRA 的"等队长"）。
3. **交战 ENGAGE**：小队中心 10 格内有可见的敌方战斗单位或防御塔 → `attack_move` 到敌人中心（单位自己选目标），再加集火命令（8.5）。每 1 s 重新评估。
4. **攻城 SIEGE**：看到敌方建筑 → 按 8.5 的优先级集火。
5. **搜索 HUNT**：到了敌方出生点 15 s 内看不到任何敌方建筑但游戏没结束 → 依次去这些点：敌方出生点周围 10 格、地图上离我方远的矿区（`M` 矿井附近，敌方可能扩张）、地图四个象限中心；军队 ≥ 8 时分成两组。**10-08 复审后**：先追最近 90 s 内在我方基地 20 格外见过的敌方单位（采矿车优先，它会开回矿厂）；然后去还没有我方单位到过 4 格内的点（2:00 起算，建筑算 7 格），由近到远：先是上面这些点，再是覆盖全图的网格（每 10 格一个点，只取从我方出生点走得到的格子，边和角都有）；一组朝一个点走 45 s 还没到就算看过；全看过还没找到就从头再来。≥ 8 个单位分 2 组，≥ 18 个分 3 组。复审时对手把基地展开在远离两个出生点的角落，旧版 15 分钟都没找到，按平局判负。
6. **撤退 RETREAT**（8.6）→ 回集结点 → 集结。

卡住检测：小队中心 3 s 没动而且不在交战 → 重新发 `attack_move` 到当前航点；连续 3 次 → 跳到下一个航点。

### 8.4 进攻条件（参数，A/B 时调）
- 第一波：主战车辆 ≥ 5 **且** 主力 `army_value` ≥ 4000；或者游戏时间 ≥ 7:00 且主战车辆 ≥ 3。
- 对比敌方：如果最近 60 s 见过的敌方军队价值（见过的敌方战斗单位按造价记，30 s 不见就衰减）`E` > 0，还要求我方 ≥ 1.3 × E。
- 机会进攻：4:00 之后，如果 60 s 内没见过任何敌方战斗单位且我方 ≥ 3 辆坦克，提前推（对手可能根本没造兵）。
- 一波失败（撤退回来）后，下一波门槛 × 1.25（最多 × 2）；成功拆掉敌方建筑后门槛回到原值。
- 增援：主力在外时，集结点攒到 ≥ 3 个单位或 ≥ 2000 价值 → 作为增援沿航点 `attack_move` 去主力中心，到 8 格内并入主力。

### 8.5 目标选择和集火
`attack_move` 时引擎自己挑最近的敌人打，这对单位对单位够用。我们额外对**建筑和关键目标**集火（`attack target=<可见 id>`）：
1. 小队 9 格内的敌方防御塔（`tsla`、`gun`、`ftur`、`pbox`、`hbox`、`agun`、`sam`），HP 低的先打；有 `tsla` 时如果能看到敌方电厂，也优先打电厂（缺电会让特斯拉线圈停火）。
2. 8 格内的敌方采矿车 `harv`（1100 价值，打掉它对手收入大减，平局分也大涨）。
3. 生产建筑：`weap`，`tent`/`barr`，（`hpad`、`afld`、`spen`、`syrd`）。
4. `proc`。5. `powr`/`apwr`。6. `fact`。7. 其余（`dome`、`fix`、`silo`、`kenn`、科技建筑、围墙最后）。
- 分配：小队 ≤ 12 个单位全打第一个目标；> 12 时按 HP 估算分两组打前两个目标。目标消失或死亡就重选；每 2 s 最多重发一次。
- 被拒 `target_not_visible` 很正常（目标刚进雾里），不计入拉黑。

### 8.6 撤退和重新集结
- 交战中算 `我方强度 = Σ(造价 × hp%)`（小队单位），`敌方强度 = Σ(造价 × hp%)`（小队 10 格内可见的敌方战斗单位）+ 防御塔造价 × 1.5（塔性价比高）。
- `我方 < 0.7 × 敌方` → 撤退：全队 `move` 回集结点（`move` 而不是 `attack_move`，免得边撤边被拖住）。恢复交战要 > 1.2 倍（滞回，防止来回抖）。
- 本次交战已经损失 > 50% 的价值而击杀很少 → 也撤。
- 例外：攻城时如果敌方建筑只剩 ≤ 2 座可见且我方 ≥ 0.5 倍，不撤（快赢了）；最后 90 s 的"全力进攻"模式下不撤（第 9 节）。
- 回到集结点后合并守家里的坦克，重新按 8.4 判断。

### 8.7 守家和应对敌方突袭
- 威胁值 `T` = 基地中心 15 格内或任一我方建筑 8 格内可见敌方战斗单位的造价和。
- `T > 0`：守家小队 `attack_move` 到威胁中心。
- `T > 守家价值 + 防御塔价值`：集结中的主力也去。
- 主力在外：`T ≥ 0.5 × 主力价值` 且主力不在攻城 → 召回；主力正在拆对方基地而且对方建筑在快速减少 → 不召回（换家）。
- 建造厂被打且 HP < 50%：所有守家单位回防，修理。
- 早期防御塔见 5.4。敌方工程师（`e6`）靠近 → 守家单位集火它（它能占领我们的建筑）。
- 防守响应有 3 s 冷却（借鉴 OpenRA 的 30 tick），避免每帧改命令。

### 8.8 单位命令去重
- 每个单位记录"上一条命令（cmd, 目标）和时间"。同一单位 0.8 s 内不再改命令；同样的命令 3 s 内不重发，除非单位 `idle`。
- 一批里的单位命令要满足 200 条引擎命令上限：我们自己留余量，**每批单位总数 + train count 之和 ≤ 180**；50 个单位以上的小队拆成多条命令，放不下的下一批再发。

---

## 9. 终局与平局规则

`remaining_seconds` 从 `state.time` 读。设 `margin = kills_cost - deaths_cost`（我方 `military`）。按第 2.11 节，`margin > 0` 基本就赢平局。

| 剩余时间 | 条件 | 行为 |
|---|---|---|
| ≤ 150 s | `margin > 1500` | **保分**：主力回家（除非正在打一场 ≥ 2 倍优势的仗，或敌方可见建筑 ≤ 2 座快赢了），不开新仗；修建筑；钱全部换成单位 |
| ≤ 150 s | `0 < margin ≤ 1500` | **谨慎**：不发起新进攻，只在基地区域或 ≥ 1.5 倍优势时交战 |
| ≤ 150 s | `margin ≤ 0` | **全力进攻**：所有战斗单位去打最值钱、最近能打到的目标（采矿车、矿厂、战车工厂）；最后 90 s 取消撤退规则 |
| ≤ 60 s | 任何 | 只开工能在剩余时间 - 5 s 内完成的东西（`build_time_s` 看配置）；现金多时多排便宜快的单位（`e1` 2.4 s），因为 `assets_value` 只算已经存在的单位和建筑，不算现金 |

特别情况：双方都没交过手（`kills_cost` 和 `deaths_cost` 都是 0）时第二项 `assets_value` 决定胜负，所以整局都不要囤钱、不要卖建筑。

---

## 10. 特殊情况

- **建造厂被毁**：`mcv` 在 `available_production` 里（要 fix 和 weap 还活着）→ 车辆队列最高优先级造 `mcv`（2000，40 s），造好后开到原 `base_center` 展开；`deploy` 被拒 `invalid`（没地方）→ 朝地图中心挪 3 格再试，换 3 个方向。没有 fix：Building 队列不可用（`available_production` 里没有建筑），转入"纯军队"模式：所有钱造兵，按第 9 节的平局逻辑打。（v0 实际没做到：步兵到上限后钱堆在存储上限、军队不动；10-08 复审后补上：步兵上限 60、一次排 5 个，主力 ≥ 15 个单位、价值 ≥ 1500 且过了强度比较就进攻。）
- **MCV 开局展开失败**：同上，挪 2 到 3 格再试。
- **没有采矿车也没有钱**：卖不卖？v0 不卖；继续用剩下的钱造采矿车（如果有矿厂和 weap）。10-08 复审后：见 6.7 的「采矿车优先」，必要时卖一座用不着的建筑。
- **放置失败**：第 6.3 节。
- **缺电**：第 6.1 节。
- **windows 模式**：第 2.13 节。
- **箱子**：观察里没有，不处理。
- **未知单位或建筑类型**：用 `rules` 里的造价；配置里也没有时按 500 估算，不崩。
- **`game_config` 为 `None`**：用 `data/red_alert_config.json` 的本地副本（规则和地图）；连本地副本也读不到时，只用 `available_production`、`legal_actions.enemy_base` 和直线航点。

---

## 11. 决策循环与安全

### 11.1 代码结构（全部在 `agent/redalert/` 下，`agent/agent.py` 只加最小的路由）
| 文件 | 内容 |
|---|---|
| `agent/redalert/__init__.py` | `RedAlertPolicy`：`decide(raw, game_config) -> list[order] | None`，`on_result(result)`，`summary()` |
| `world.py` | 把 `observation` 解析成快照：我方单位按角色分组（MCV、采矿车、步兵、坦克、残骸）、建筑按类型、各队列的生产项、可见敌人（去掉残骸）、电力、时间；每局缓存一次的静态数据：规则表、地图通行网格、出生点、BFS 路径和航点 |
| `economy.py` | Building/Defense 队列：建造顺序（开局 A/B）、电力、矿厂、第二座 weap、silo、放置位置、修理、集结点 |
| `production.py` | Infantry/Vehicle 队列：采矿车补足、MCV、主战坦克、步兵、经济优先暂停 |
| `army.py` | 小队、主力状态机、进攻条件、航点、集火、撤退、守家、搜索、终局模式 |
| `ledger.py` | "已发未见"账本、判决跟踪、重复拒绝拉黑 |
| `orders.py` | 命令构造和本地校验（字段、上限、bounds、`available_production`、200 条引擎命令） |
| `params.py` | 所有可调参数（进攻门槛、撤退比例等），可用 `ARENA_RA_*` 环境变量覆盖，A/B 用 |
| `safe.py` | 安全模式的极简策略（11.6） |

### 11.2 每个画面的流程
1. `observation` 不是字典 → `WAIT`（windows 模式见 2.13）。
2. 解析快照（目标 ≤ 5 ms），第一次决策时做 BFS（每局一次，≤ 50 ms）。
3. 用 `last_orders`、`recent_order_problems` 更新账本。
4. 生成候选命令，按优先级：`place` 做好的建筑 > `deploy` > 缺电处理 > 防守响应 > `build`/`train` > 主力和增援的单位命令（每个小队最多每 1 s 一次）> `rally`/`repair`/`harvest`。
5. 本地校验和裁剪：≤ 20 条；单位数 + train count ≤ 180；每条 `units` ≤ 50 且不重复、全是当前我方非残骸单位；格子在 `bounds` 内；`item` 在 `available_production` 里；去掉被拉黑的；去掉账本里已发未见的重复项。
6. 没有命令 → `WAIT`。有 → 记入账本，返回 `{"type": "orders", "orders": [...]}`（带短的 `reasoning_summary`）。

### 11.3 节奏和预算
- runner 每个新画面问一次（约 0.2 s 一个）。只有非紧急命令时，距上一批不到 0.35 s 就回答 `WAIT`；紧急命令（放建筑、防守、撤退）不受限。预计每秒 2 到 4 批，每分钟 100 到 200 批（LLM 每分钟 2 到 30 批）。
- 每次决策 p50 ≤ 10 ms、p99 ≤ 30 ms（Python，单位几百个以内）。超过 50 ms 的那一帧跳过军队部分只做经济，并记一次警告。策略**在 runner 线程里直接跑**，不用 Pokémon 的看门狗线程（它是确定性的、很快）。
- 单位命令每个小队最多每 1 s 一次，集火重选每 2 s 一次，避免每帧重发导致单位来回重算路径。

### 11.4 去重账本（"已发未见"）
- `build`/`train X` 发出后记 `pending[X] = (发出 tick, 当时队列里 X 的数量, 数量)`。下面任一情况清掉：状态里 X 的生产项数量增加了；判决 `rejected`/`lost`；`play_action` 整批报错（`NOT_YOUR_TURN`、`STALE_STATE`、`INVALID_ACTION`、`RUNTIME_TEMPORARILY_UNAVAILABLE`：都表示没执行）；超过 75 tick（3 s）还没看到（然后按当前状态重新决定）。**有 pending 时绝不再发同一个 build**（平台 §9 明说重复发会排两个）。
- `place X`：清除条件是生产项不再 `ready`（已放下）、拒绝、或 50 tick（2 s）。
- `rally`：建筑的 `rally` 字段等于目标就不发；发后 5 s 冷却。
- 单位命令：第 8.8 节的每单位记录。
- `repair`/`power_toggle` 是幂等的，但 `lost` 之后 2 s 内不重发（平台 §5 说可能被当作无变化）。

### 11.5 判决跟踪和拉黑
- `on_action_result(result)`（runner 每次 `play_action` 后调用）：
  - `{"error": "INVALID_ACTION"}`：解析 `detail` 里的 `[i] cmd: reason (detail)`，每行记一次拒绝，清掉这批的账本项；
  - 其他 `error`：清掉这批的账本项，什么都不学；
  - 接受：每行 `rejected` 记一次拒绝并清账本项；`pending`/`ok`/`partial` 保留。
- 每个状态里的 `last_orders`（`pending` 为 false 时的最终结果）和 `recent_order_problems`（按 `(batch, index)` 去重）：`rejected`/`lost` 清账本项并记录；`partial` 只记日志。
- 拉黑（移植上游 `examples/llm/redalert.py` 的规则）：同一条命令（内容去掉 `queued`、单位 id 排序后作为键）因为**同一原因**被拒 3 次，在 30 s 内并且原因还成立时不再发送（`place` + `not_ready`：还没做好；`build`/`train` + `not_available`：还不在 `available_production`；`not_owned`/`wreck`/`is_building`：id 还不全是我方单位）；120 s 没再被拒就忘掉。`target_not_visible` 不计入。
- 物品级拉黑：同一建筑放置 `invalid` 3 次 → 取消并 60 s 不计划（6.3）。

### 11.6 永不抛异常
- `decide` 和 `on_action_result` 外面都包 `try/except Exception`：异常 → 记日志（每种异常只打一次完整堆栈）、计数、返回 `WAIT`（windows 模式下返回无害命令）。`on_action_result` 异常直接吞掉（runner 遇到它抛异常会杀进程）。
- **安全模式**：30 s 内异常 ≥ 5 次 → 60 s 内改用 `safe.py` 的极简策略：展开 MCV；放下做好的建筑；缺电造电厂；没有矿厂造矿厂；车辆队列空就造主战坦克；每 10 s 如果 `army_value > 3000` 就发 `legal_actions.attack_now`。60 s 后再试主策略。安全模式自己也包 `try`，再出错就 `WAIT`。
- 绝不 `resign`。

### 11.7 路由改动（`agent/agent.py`，尽量小）
- `Contestant.choose_action` 开头：`game_type == "red_alert"`（来自 `context.game_type` 或 `raw.game_type`）或者（实时且看起来像红警：观察里有 `available_production`/`economy`）→ `self._red_alert(state, context)`；其他未知实时游戏仍走现在的 `_realtime_wait`。
- `ARENA_RA=off` → 走现在的 `_realtime_wait`（整场 `WAIT`）：一键回退。
- 新增 `Contestant.on_action_result(result, context)`：红警局转给 `RedAlertPolicy`，其他游戏什么都不做；整个方法 `try/except`，永不抛。
- `[match-summary]` 行加红警指标（11.9）。
- 每局一个 `RedAlertPolicy` 实例（每个对局一个 worker 进程，Contestant 也是每局一个）。worker 重启时不恢复记忆：策略几乎完全从当前状态推出来（小队、集结点都能重建），丢记忆只损失几秒。
- 测试 `tests/strategy/test_realtime_safety.py` 现在断言"红警什么都不发"：改成在 `ARENA_RA=off` 下断言，另加 `ARENA_RA` 打开时的新断言。

### 11.8 `reasoning_summary`
只发阶段标签，≤ 40 字符，例如 `opening`、`economy`、`gathering`、`attacking`、`defending`、`endgame-hold`、`endgame-push`。不写计划、不写数字。比赛中观众看不到，赛后可见，这些标签不泄露任何有用信息。`ARENA_REASONING=off` 时不发。

### 11.9 日志和录像
- 每 30 游戏秒一行：`[ra] t=…s cash=… ore=… pow=+… harv=… bld=… army=… K=… D=… assets=… phase=… batches=… rej=…`。
- 每种拒绝原因第一次出现打一行警告；之后只计数。
- `[match-summary] game=red_alert` 行：`first_powr_s first_proc_s first_weap_s first_fix_s max_army_value final K/D/assets batches orders rejected_by_reason invalid_action waits decision_ms_p50/p99 exceptions safe_mode low_power_s result`。
- 录像（`ARENA_RECORD`，生产默认开）：**不能每个画面都录**（每秒 5 个画面 × 20 分钟 = 6000 个，几百 MB）。只录第一个画面、之后每 10 游戏秒一个、每次异常或新类型拒绝的那一帧、终局一帧：每局约 150 帧、几 MB。本地模拟器和回放照旧 `ARENA_RECORD=0`。

---

## 12. 测试计划

### 12.1 单元测试（`tests/redalert/`，纯函数，用手写的观察）
- 解析：残骸过滤、阵营分组、生产项计数、`base_center` 缺失时的退路。
- 建造顺序：开局 A/B 的每一步；缺电插队；`available_production` 没有某物时跳过而不是卡住；建造厂死后不再计划建筑。
- 电力：每一步的余量计算、关停顺序。
- 账本：发出后同一 `build` 不重复；看到生产项后清除；被拒和 lost 后清除；超时清除。
- 判决：`INVALID_ACTION` 的 detail 解析；`last_orders`/`recent_order_problems` 去重；3 次拉黑和 30 s 解除。
- 命令校验：每种命令的字段（和 `order_schema.orders` 对照）、20 条上限、50 单位上限、180 引擎命令裁剪、bounds、重复 id、残骸 id。
- 军队：进攻门槛、航点推进、集火优先级、撤退滞回、守家召回、终局三种模式、windows 模式的无害命令。
- 路由：`ARENA_RA=off` 回到 `WAIT`；`on_action_result` 吞异常；Pokémon/Werewolf 的状态不受影响。

### 12.2 本地模拟器（`local/ra_sim.py`，抓逻辑错误，不追求逼真）
必须模拟：
- **协议**：和平台同形状的状态（`pacing`、`limits`、`legal_actions` 字典、观察字段）；阶段 `queued → starting → waiting_for_agents → playing → finished`；判决生命周期（答复里 `pending`，0.2 到 0.4 s 后在 `last_orders` 给最终结果，1% 概率 `lost`）；`recent_order_problems`；`INVALID_ACTION`（全无效、超过 20 条、超过 200 条引擎命令）；重叠发送 `NOT_YOUR_TURN`；`STALE_STATE`；0.2 s 间隔扣留；事件窗口 20 条和 `seq`；可切换的 windows 模式。
- **游戏**：MCV 展开成新 id 的 `fact`；按 `rules` 的前置条件和阵营算 `available_production`；每种队列同时一个、按 `build_time_s` 推进、按进度扣钱、缺电 × 3；做好的建筑等 `place`，"没地方"开关（第 N 座之后 `invalid`）；矿厂送采矿车；采矿车按固定速率产钱（参数）；单位在工厂出生走向集结点；直线移动（按速度）；简单战斗（伤害 × 护甲系数 / 装填，射程，打最近）；车辆死后留 10 s 残骸；视野半径（迷雾）；胜负条件（建筑和 MCV 全灭）；时间到按 `(K-D, assets)` 判。
- **对手**：（a）什么都不做；（b）我们自己（自对弈）；（c）脚本突袭者（90 s 时 10 个 e1，之后 1tnk 添油）；（d）"像 LLM 的"对手（每 3 到 10 s 发一批，常用 `attack_now`，偶尔不放建筑、重复下单）；（e）乌龟（狂造防御塔）。
- **每局断言**：0 个 `INVALID_ACTION` 批次（我们自己造成的）；0 次异常；没有意外的重复建造；做好的建筑 1 s 内放下；缺电累计 ≤ 5 s（电厂被打掉除外）；每批 ≤ 20 条、≤ 200 引擎命令；决策 p99 < 30 ms；对 (a)(c)(d) 在模拟器里胜率 ≥ 90%。
- 跑法：`python -m local.ra_sim --games 50 --seed 7`（3 张地图 × 两个阵营 × 5 种对手），放在 `flock /tmp/arena-heavy.lock` 下，`ARENA_RECORD=0`。

### 12.3 只能在平台上测的
- 真实的放置、寻路、战斗平衡、采矿收入、网络延迟和实际节奏、真实事件字段（`under_attack` 带什么）、`act` 字符串、`at` 提示是否被采纳、箱子效果、对手的真实行为。
- **主人操作**：在 Testing 页面建红警对局，选地图，两个座位都选 "Mine (self-hosted)" = 自对弈（我们的 runtime 用 `--match` 两个座位都会接）；或者留一个座位给别的参赛者。三张地图都要测；阵营是随机的，多打几局才能覆盖盟军对苏军的四种组合。
- 可选 A/B：一个环境变量让 `Multi0` 和 `Multi1` 用不同参数（例如进攻门槛），自对弈时直接比较；锦标赛前必须关掉。
- 拿到真实录像后：用 `scripts/replay_real.py` 风格的回放把录像里的每一帧喂给策略，确认不抛异常、命令全部通过本地校验。

### 12.4 每局要记录的指标
地图、我方阵营对敌方阵营、开局资金；第一个 powr/proc/兵营/weap/fix 的建成时刻；采矿车数量随时间变化；平均和最大囤钱；缺电秒数；每 15 s 采样的 `army_value`；终局的 `kills_cost`/`deaths_cost`/`assets_value`、单位和建筑的击杀与损失；发出的批次数、命令数、按原因分的拒绝数、`INVALID_ACTION` 数；决策耗时 p50/p99；异常和安全模式次数；结果和结束原因。

### 12.5 晋级门槛（红警自己的，和宝可梦的天梯分开）
1. `python -m pytest -q` 全绿（包括 `tests/redalert/` 和改过的 `test_realtime_safety.py`），`scripts/replay_real.py` 两种模式 0 失败（宝可梦部分不能退化）。
2. 本地模拟器 50 局：上面的断言全部成立。
3. 平台：至少 6 局自对弈（三张地图都有）没有协议错误，指标看过；能找到别的参赛者时至少 3 局对外。
4. `ARENA_RA=off` 回退测过。
5. 合入 main 时只动 `agent/redalert/`、`agent/agent.py` 的路由、`local/` 的模拟器和测试；和 `integrate/v424` 的冲突在合并时解决。

---

## 13. 实施计划

| 日期 | 内容 |
|---|---|
| 10-08 | 本设计（本文件） |
| 10-09 | v0 核心：`world`、`ledger`、`orders`、开局 A/B + 电力 + 矿厂 + 放置、步兵和车辆生产、集结点、简单进攻（门槛 + 航点 + `attack_move`）、守家、永不抛异常和安全模式；路由和开关；单元测试；模拟器 v0（协议 + 生产 + 收入 + 简单战斗） |
| 10-10 | 模拟器对各种对手跑，修 bug；主人开第一批 Testing 自对弈（2 到 3 局）拿到真实录像；回放；按真实数据修正 |
| 10-11 | v0.1：集火、撤退、增援、终局三模式、MCV 重建、搜索；更多平台对局，争取对外 |
| 10-12 | 冻结候选，最后一轮平台检查；主人决定是否报名红警比赛、`ARENA_RA` 默认值 |
| 10-13 | 截止（23:59 PDT），之后代码冻结、Testing 关闭；当天只修 bug |
| v1（时间够才做） | 盟军 `jeep`/`1tnk` 骚扰采矿车、`arty` 拆塔、受伤坦克回维修厂、苏军 `4tnk`、按地图调参 |

---

## 14. 开放问题

1. 比赛用哪个开局资金：5000（文档）还是 10000（配置说明）？我们运行时读，但两套时间线的强弱只能在平台上验证。
2. `under_attack`、`enemy_spotted`、`unit_lost` 等事件的确切字段（有没有坐标和攻击者 id）。
3. `units[].act` 的实际取值和默认 `stance`。
4. 这三张地图上每辆采矿车每分钟的真实收入；`place` 的 `at` 提示（矿厂放矿边）是否被引擎采纳。
5. 一个 runtime 同时接同一个 Official Agent Key 的两个 Testing 座位做自对弈，是否真的可行（文档说可以）。
6. 空闲规则到底是 10 分钟（本地文档和配置）还是 20 分钟（10-08 线上文档）。对我们没有影响，但要确认文档。
7. 比赛地图是否就是配置里的三张；不是的话我们只能用直线航点。
8. 比赛当天运营方会不会切到 windows 模式。
9. `rally`、`place`、`build` 各算几条引擎命令（我们按 1 条估算，留了 20 条余量）。
10. 是否报名红警比赛、`ARENA_RA` 默认开还是关：**主人决定**。
11. 防御塔和围墙是否都算"必须拆光的建筑"（决定搜索阶段要不要专门找围墙）。

---

## 15. v0 实现和本稿的差异（10-08，按本地模拟器的结果改的）

代码在 `agent/redalert/`（模块划分同第 11.1 节）。下面这些和前面各节写的不一样，原因都是本地模拟器（`local/ra_platform.py`）里测出来的问题；模拟器不是真引擎，这些改动在平台上还要再看。

1. **开局顺序**：兵营提前到第一个矿厂之前（`powr → tent/barr → proc → proc → powr → weap → fix → powr`；开局 B 同样把兵营提前）。第一个步兵当侦察兵：走到敌方出生点，再往远侧走几秒，然后回到路径中点当哨兵。矿厂先造时，侦察兵 85 秒左右才到，在 shadowfiend-ii 上看不到 2 分钟出发的步兵海。
2. **"先防后攻"**：侦察兵在 150 秒前被打死，或者在我方这半边地图看到大批敌军（速攻），4:00 之前先把步兵补到 36 个，再造战车工厂和维修厂（只放行电厂、兵营和矿厂）。只看敌方在自己家里的部队不算速攻。
3. **看不见的威胁**：模拟器里建筑视野只有 4–5 格（从建筑中心算），步兵站在射程 5 打建筑时我们看不见它。现在 `under_attack` / `unit_lost` / `building_lost` 事件（用事件里的 x/y，没有就用我方那个单位或建筑的位置）在基地 25 格内也算威胁，守家部队去那里，到了就能看见。
4. **经济保留**：第 7 节的"经济优先"原来保留关键建筑剩下的全部造价。生产是边造边扣钱，这样会让钱闲置（模拟器里 100 秒闲着约 2000）。现在只留 400 的缓冲，多出来的钱让步兵吸收；能造坦克（4:00 以后）步兵最多占军队价值的 40%，对付步兵为主的敌人放宽到 65%。没看到敌人时假设敌方是步兵为主（早期），所以早期不造火箭兵。
5. **进攻门槛**：第 8.4 节的"最近 60 秒见过的敌军"会很快忘掉龟缩在家的对手，导致一波一波送死。现在用"敌方强度" = max(最近 3 分钟见过的敌军, 一次见过的最大一支敌军按 6 分钟半衰期慢慢衰减) + 1.5 × 已知防御塔，要求我方 ≥ 1.3 倍；一波撤退回来后，下一波价值至少是那一波的 1.3 倍；拆掉敌方建筑后清零。
6. **守家**：集结点从路径第 10 步改到第 8 步、守家点第 5 步，两组人离得近；在家等待的主力（GATHER）在家被打时总是和守家小队一起打（以前只在威胁大于守家价值时才去，结果被各个击破）。
7. **重建基地车**：建造厂被毁后造出的新 MCV，先在旧基地附近找一块 3×4 的空地（按地图图片、我方建筑占地、矿格），开过去再展开；展开被拒就换一块地。
8. **整批不会被拒**：只含"可能被世界变化作废"的命令的批次（看不见的目标、刚被造好又被取消的建筑、建筑命令、一两个单位的命令），以及 windows 模式下的每一批，都附加一条"锚"命令：给一座没关电的建筑发 `power_toggle on=true`（平台答 `ok already_on`，不占引擎命令），保证这一批至少有一条有效。取消建筑后，在取消生效前不再放置它。3 分钟内两种建筑都因为没地方放被取消 → 2 分钟内不再造新建筑。
9. **终局**：全力进攻（K−D 落后）只在主力回到集结点后再出发，不会在"撤退 / 进攻"之间每帧来回切换。
10. **节奏**：非紧急命令每 9 tick（0.36 s）最多一批；模拟器里实际每秒约 0.35 批（大多数画面没有新命令，回答 WAIT），每次决策 p50 约 0.2–0.5 ms、p99 约 1–3 ms。
11. **合并 v4.2.4（SDK PR #7）后**：runner 遇到暂时故障（断线、5xx、429、工具故障）不再结束这局，而是停一下、重读状态、再问我们，**从不重发**（那一批可能已经送达）。带错误码的这类故障会通过 `on_action_result` 以 `{"error": 码}` 告诉我们。以前账本对任何整批错误都当作「整批没执行」，马上允许重发；现在只有明确「什么都没执行」的码（`INVALID_ACTION`、`STALE_STATE`、`NOT_YOUR_TURN`、`WRONG_PHASE`、`GAME_ALREADY_COMPLETE`、`RUNTIME_TEMPORARILY_UNAVAILABLE`、`PLAYER_ELIMINATED`）才这样；别的码这一批的「已发未见」记录保留（状态里出现就清掉，没出现就按原来的时限过期后再发），不会把同一个建造 / 训练排两次。模拟器加了 `--fault-rate`（一部分调用像网络不稳一样失败：读状态 502；出招在送达前 502、送达后 502、送达后 429 `rate_limited` 三种）来测这条路径。
