# 本地对战环境（`local/`）

目的：在拿到平台 key 之前 / 之外，用**和比赛相同的规则**把我们的 agent 和参照对手放在一起打成百上千局，用数字说话。

## 规则相似度：我实测过的部分

环境：`pokemon-showdown` npm 0.11.11、`@smogon/calc` 0.12.0（2026-09-26）。

| 检查项 | 结果 | 含义 |
|---|---|---|
| `Dex.formats.get('gen9vgc2025regi')` | 存在；`gameType: doubles`；ruleset = `Flat Rules, !! Adjust Level = 50, Min Source Gen = 9, VGC Timer, Open Team Sheets, Limit Two Restricted` | 和平台声明的 `battle_format: gen9vgc2025regi` 是同一个规则集；**Open Team Sheets** 与「选秀后双方配置全公开」一致 |
| 内置规则 `Terastal Clause`（"Prevents Pokémon from Terastallizing"） | 存在 | 平台「没有太晶化」用 `gen9vgc2025regi@@@Terastal Clause` 复现 |
| 用 `Battle` 类开局后读 `sides[0].activeRequest` | 普通 Reg I：两个 active 槽都有 `canTerastallize`；加 Terastal Clause：**没有任何 tera 字段** | Tera 被引擎层面关掉，不是"AI 碰巧没用" |
| Team Preview `team 1234` | `side.pokemon.length == 4` | 6 带 4 与平台 `select_lineup` 一致 |
| 等级 | `Adjust Level = 50` 在直接用 `Battle` 类时不生效 | 本地必须自己把 set 的 `level` 写成 50（平台卡牌本来就是 50，`local/pool.py` 也是） |
| `@smogon/calc` 双打修正 | Chi-Yu Heat Wave vs Amoonguss：单打 402–474，双打 300–354（≈0.75×）；Helping Hand + 晴天 672–792 | 精确伤害计算可用，含群攻 0.75×、场地/天气/Helping Hand |

结论：**对战阶段可以 100% 复现**（同引擎、同 format、Tera 关、6 带 4）。**选秀阶段是平台自定义的**，我按 `docs/platform/pokemon.md` 重写了一份（18 卡蛇形、道具条款）。

卡池：`local/pool.py` 优先读 `data/vgc_cards.json`——**从真实对局记录里采集的平台卡**（2026-09-26 第一局 VGC 后为 18 张，09-28 为 48 张，全部通过 Showdown TeamValidator）；没有时退回手写的 26 套。
与真实平台对照（`docs/real_payloads.md`）：同 29 组（槽位, 招式）的可选目标集合 **0 处不一致**。

**每局抽哪 18 张（09-28 起）**：平台**不是**均匀抽卡。39 局真实选秀（`data/vgc_pools.json`，`python -m local.pool --harvest` 自动记录）里 Rillaboom 出现 35 次、Wo-Chien 33 次，而 Sneasler、Toxapex、Ursaluna 各只出现 3 次（各卡出现次数的方差约是均匀抽样的 7 倍）。分布还在变：09-28 实战 A/B 的 20 局里新卡明显更多（Tyranitar 7/20，之前 19 局只有 1 次）。所以本地每张卡的「入池概率」= 真实出现频率（每张卡加 0.5 次平滑、上限 0.97），再按这些概率精确抽 18 张（`sample_pool`，`python -m local.pool --freq` 可看每张卡的真实次数与本地概率）。
**道具冲突的卡照样列出**：平台的 `available_cards` / `available_card_ids` / `unused_card_ids` 列出所有还没被选的卡，包括和你已有道具重复、你不能选的卡（39 局里出现 624 次，没有一次是合法动作）；只有 legal actions 把它们排除。本地环境现在也这样。
`ARENA_POOL=legacy` 恢复旧环境（只从 09-28 之前目录里的 43 张卡均匀抽、同样的随机数序列、隐藏道具冲突卡；擂台 7 / 11、基准 31 的 1000 个对局种子抽到的卡池与 aa48e95 逐个相同），用来复现 09-28 之前的数字；`ARENA_POOL=uniform` 只把抽卡改回均匀（从全部 48 张里抽）。`--harvest`（以及读真实记录的测试）只读 VGC 双打选秀的记录（按平台的 `game_type` = `pokemon_vgc_doubles_draft` 过滤；记录里没有 `game_type` 时看 `battle_format` 是否 gen9vgc 开头），单打 OU 选秀（`pokemon_gen9ou_draft`，同样是 draft 阶段、`ou-` 卡）不会混进 VGC 目录和卡池。换了卡池分布后，擂台 / 基准 / 面板的绝对胜率和旧记录不能直接比，要在同一环境下对比（下文 v3 / v2 的面板数字都是旧环境测的）。新环境（48 张卡、39 个真实卡池）下 v3.2 的基准线：擂台（冠军自己打自己）种子 7 / 11 = 191 / 211（旧环境 195 / 204，`ARENA_POOL=legacy` 下原样复现）；基准 official_baseline / random / v0 / v1 / v2 / v3 = 100 / 100 / 96.0 / 92.0 / 88.5 / 51.5；种子 43 面板均值 92.4%，最低 v3 镜像 48.5%。
**选秀死局**：真实卡池道具重复很多（39 个真实卡池里每个有 6–9 张「道具和池里别的卡重复」的卡），偶尔一方剩下的卡全部撞道具、一张都不能选。平台上没见过，不知道平台怎么处理；本地以前直接崩溃（约万分之一局），现在这一手放宽道具条款（所有没被选的卡都合法）并记在 `Match.clause_waived` / `MatchResult.clause_waived`；擂台、基准、面板（`local.panel`）和 `scripts/draft_panel.py` 的输出里都有「draft dead ends」一项（出现了多少局）。
**选秀模型没见过的卡**：目录里有、但 `agent/pokemon/draft_model.json` 没拟合过的卡，明确列在 `local/pool.py` 的 `NOT_IN_DRAFT_MODEL`（现在是 09-28 采集到的 5 张：pelipper、scream-tail、ursaluna、tyranitar、toxapex），选秀时用模型对「未见过的卡」的先验打分（平台上遇到新卡本来就是这样）。测试要求「目录 − 模型 = 这个列表」：以后 `--harvest` 采到新卡，要么加进这个列表，要么用**含这些卡的对局**重拟合（`scripts/fit_draft_model.py` 只把训练对局里出现过的卡写进模型，没有对局的卡不会以 0 分冒充「已拟合」）后再从列表里删掉。

狼人杀：平台引擎不公开，`local/werewolf_env.py` 严格按 `docs/platform/werewolf.md` 实现（2 狼 1 预 4 民、第一夜平安、分票取低座位、死亡亮身份、每天一次公开讨论窗口 5 条 × 50 词、顺序暗投相对多数、平票无人、三天僵局狼胜、±1）。**状态 payload 按平台文档的形状构造**，所以 `agent/agent.py` 里部署用的 `Contestant` 在这里一行不改地跑。

## 用法

```bash
cd local/js && npm install && cd ../..          # 一次
python -m local.pool                             # 校验卡池合法性
python -m local.eval_pokemon --games 300 --opponent official_baseline
python -m local.eval_pokemon --games 300 --opponent random
python -m local.eval_werewolf --games 1000 --seeds 7 --jobs 3     # 狼人杀混桌评测，候选 agent/ 对冠军 champion/（种子 1、2 调参用过）
python -m local.werewolf_platform --games 40      # 狼人杀：用官方 runner（run_game）跑 7 个座位，按平台文档检查
```

狼人杀混桌评测（`local/eval_werewolf.py`）：我们占 k = 1..7 个座位，其余座位是一种背景对手——`first`（永远第一个合法动作，= basic / smoke 示例）、`heuristic`（仿平台演示 `werewolf_agent.py`：只看死亡和投票记录）、`random`、`llm`（`local/werewolf_bots.py` 里的「大模型替身」：会读聊天、容易被说服、有噪声、偶尔弃票、被指控时可能对跳预言家）、`llmx`（同一个替身，决策不变，但说话换着说法：随口的写法、「seer here: p3 is a wolf」「All players must vote Player3」，还夹杂看着像跳预言家其实不是的闲聊，比如「Is the Seer here?」「I checked Player3's votes」「What if I am the seer?」；跟着聊天里被点名最多的座位投票的概率只有 0.6。`llm` 只会说 8 句固定的话，而且正好是我们读聊天的格式，对我们偏心）或 `mixed`（每个座位从 first / heuristic / random / llm 这四种里抽；不抽 `llmx`，这样以前的牌局不变）。同一局的身份、我们的座位和背景对手对所有被测 agent 完全相同（配对比较）。报告按身份（村民 / 预言家 / 狼）的胜率和 Wilson 95% 置信区间、每座位胜率、配对差值。因为一桌里我们的村民和我们的狼会互相影响，按身份的结论用「只换一边」的配对：`agent/champion` 对 `champion`（只换狼）判狼，`champion/agent` 对 `champion`（只换村民和预言家）判村民和预言家；整体用整桌 `agent` 对 `champion` 的每座位胜率。全部条件合在一起会把某一种桌子上的退步平均掉，所以结论里还列出「分片退步」：按背景（全部 k，以及 k ≤ 3，也就是一桌里我们座位不多的现实情况）分片，任何一个身份或每座位胜率的配对差值 95% 置信区间整个在 0 以下都算；有分片退步时 SHIP 为 False。种子 1 和 2 在调参时用过，确认结论要换别的种子（比如 7）。本地引擎另外模拟平台的失败规则：出错、非法动作、被拒的聊天或超过 120 s 都算进程死亡，之后由平台代打（白天弃票、夜里第一个目标）；讨论时每轮随机发言顺序。

`local/werewolf_platform.py` 是按 `docs/platform/werewolf.md` 写的多线程平台替身：messaging / moving 两种 phase、state_version、WRONG_PHASE / NOT_YOUR_TURN / STALE_STATE / INVALID_ACTION、每窗口 5 条 50 词、terminate 幂等、120 s 闲置代打，每个座位跑真正的 `altruagent.runner.run_game`。平台没写清楚的地方按最严处理：已经 terminate 的座位再发聊天会被拒（runner 会因此让进程退出）。

参照对手：
- `official_baseline` = 官方 starter 原样 `return state.legal_actions[0]`。在平台上它选秀拿第一张卡；Team Preview 和双打回合返回模板会被服务器拒绝 → 进程被 runner 杀掉 → 平台 300 s 后随机代打。所以这里它 = 首卡选秀 + 随机带人 + 随机出招；狼人杀里 = 白天投最小座位、夜里杀最小座位、不发言。（这是按平台文档的「300 s 随机代打」建模；按实测的 VGC 计时器建模的版本见下面的 `basic`。）
- `random` = 全随机。
- `basic` / `messaging` / `smoke` / `llm_fallback` / `llm_timeout` = 主办方 `examples/` 里的示例 agent，按官方 runner 的规则和平台计时器来跑（`local/field_opps.py`，见下文「官方示例 agent 当对手」）：`python -m local.eval_pokemon --opponent smoke`。

## v0 结果（2026-09-26）

**Pokémon VGC 双打选秀**（各 300 局，轮流先手/后手）

| 对手 | 手写卡池 | **真实卡池** | 我方非法动作 |
|---|---|---|---|
| official_baseline | 297/300 = 99.0% | **293/300 = 97.7%**，95% CI [95.3%, 98.9%] | 0 |
| random | 299/300 = 99.7% | **292/300 = 97.3%**，95% CI [94.8%, 98.6%] | 0 |

真实卡池没有限定级传说、辅助型更多（Prankster ×4、双墙），对局更长（平均 7.4 vs 5.1 回合），胜率略降。

**狼人杀**（各 2000 局，同种子配对比较）

| 视角 | vs official_baseline | vs random |
|---|---|---|
| A. 我方 1 席 + 对方 6 席，该席平均得分（民 / 预 / 狼） | −0.164 / −0.067 / +0.113，**与对方完全相同** | −0.801 / −0.644 / +0.879 vs −0.811 / −0.881 / +0.795 |
| B. 整桌村民方胜率（全我方 vs 全对方） | **58.5% vs 43.2%** | **58.5% vs 9.2%** |
| C. 狼方胜率（双狼我方 vs 双狼对方，村民都是对方） | 56.8% vs 56.8%（相同） | 98.8% vs 90.8% |

怎么读：
- Pokémon：v0 已经远超官方基线。但对手是随机出招，这个数字只证明"不崩 + 有基本判断"，**不代表能赢认真做的队伍**。
- 狼人杀 A 视角完全相同不是 bug（已验证我方 144 次决策里 14 次与基线不同、发了聊天）：**相对多数制下 1 席永远投不过 6 个一致的席位**，夜里杀人也和基线撞在同一目标。单席的杠杆只能来自说服别人——而基线不读聊天。这直接说明 M3（LLM 发言）在混合桌上的价值。
- 狼人杀 B：整桌我方比整桌基线的村民胜率高 15 个点（预言家跳身份 + 大家跟票），比整桌随机高 49 个点。

## 单打格式（VGC 以外的四种宝可梦玩法，2026-09-28）

平台另外四种宝可梦玩法都是**单打**（一次只上场一只）：`pokemon_gen9same`（双方同一队）、`pokemon_gen9random`（Showdown 随机队）、`pokemon_gen9ou_teambuild`（自己提交 6 只 OU 队伍）、`pokemon_gen9ou_draft`（18 卡蛇形选秀后 OU 单打；官方演示视频就是这个）。`local/singles_env.py` 按真实单打录像（`logs/*/c8efbea9-*.jsonl`）和 `docs/platform/pokemon.md` 的形状模拟这四种；`agent/pokemon/singles.py` 是我们的单打策略（每回合一次精确伤害计算批处理，考虑对手的回击、换人、KO 竞速）。

```bash
python -m local.singles_eval --format all --games 200 --seed 5         # 我方 vs 所有参照对手（含 champion）
python -m local.singles_eval --format draft --games 100 --opponents champion
python -m local.singles_eval --format same --no-calc                   # 没有 Node / 计算器时的兜底路径
python -m local.singles_eval --format teambuild --opp-team randbats    # 没交队的一方拿随机对战队（100 级），而非平台兜底队
python -m local.singles_replay -v                                      # 真实单打录像回放检查
```

结果（200 局，**种子 5**：开发和调参都没用过的种子；座位轮换；胜率 [95% CI]；我方非法动作 **0**）：

| 格式 | 官方基线 | 随机 | v0 简单启发式 | v1 精确伤害 | 冠军 v3 |
|---|---|---|---|---|---|
| same | 96.5 [93.0, 98.3] | 99.5 [97.2, 99.9] | 95.0 [91.0, 97.3] | 97.0 [93.6, 98.6] | 96.5 [93.0, 98.3] |
| random | 97.5 [94.3, 98.9] | 98.0 [95.0, 99.2] | 86.0 [80.5, 90.1] | 85.0 [79.4, 89.3] | 86.0 [80.5, 90.1] |
| teambuild | 98.0 [95.0, 99.2] | 100 [98.1, 100] | 82.5 [76.6, 87.1] | 83.5 [77.7, 88.0] | 83.5 [77.7, 88.0] |
| draft | 99.0 [96.4, 99.7] | 99.0 [96.4, 99.7] | 97.0 [93.6, 98.6] | 98.0 [95.0, 99.2] | 93.0 [88.6, 95.8] |

对照：旧的 v3 在单打里和 v0 简单启发式差不多（同一种子：same 56.0%、random 46.0%、teambuild 47.5%），因为它从不主动换人、被迫换人时总是换列表里第一只、而且用 50 级通用配置算伤害（单打是 100 级）。

注意：teambuild 对官方基线 / 随机这两行，本地环境让"交模板"的一方在 8 次被拒后拿到平台兜底队；但文档说格式错误的提交不算次数、teambuild 阶段也没有超时，所以平台上这种对手可能根本拿不到队伍，这两行未必是真实会出现的局面。有参考意义的是对 v0 / v1 / 冠军（它们会自己交队）的三列。`--opp-team factory`（Battle Factory 队）和我方猜对手配置用的是同一份数据，结果偏乐观；`--opp-team randbats` 用的是另一来源，种子 5 下两者差不多（对官方基线 96.5% / 98.0%）。

真实录像检查（`local.singles_replay`）：录到的那一局 gen9same（两个视角各 36 个决策）重放 0 非法；录像里 30 次非暴击、无能力变化的普通命中，**全部**落在「100 级、六项 252 努力值、中性性格」的计算区间里（换成 v3 的 50 级通用配置：0/30），说明 gen9same 的配置推断是对的。

不确定的地方：OU 选秀的真实卡池、teambuild 的真实目录（`get_game_config`）都没见过；本地卡池用 Showdown 的 Battle Factory 配置代替。

## 官方示例 agent 当对手（`local/field_opps.py`，09-28）

为什么：简报（`docs/competition_brief.md` 3.2 第 6 条）估计很多真实对手就是主办方的示例 agent，或官网说的「我们提供的通用 agent」= `examples/llm_agent.py`（gpt-4o-mini）。这里**直接跑 `examples/` 里的原代码**，外面套一层「平台会怎么处理它」的模型：

- **runner 规则**：决策先过 `altruagent.runner._validate_decision`（和官方 runner 同一个函数）。把 Team Preview / 双打回合的**模板原样交回去**（`legal_actions[0]`）= 平台回 `INVALID_ACTION`（文档：只给 `action_id="doubles_turn"` 不够）→ runner 抛 DecisionError、丢下这局（60 s 冷却后重试，确定性的 agent 会再错一次）→ 这个座位**本局一直掉线**。agent 自己抛异常、或交了平台会拒的动作，同样处理。
- **VGC 计时器**（`VGCClock`，按本机 pokemon-showdown 0.11.11 的 `room-battle` 代码 + 简报实测的 90 s 预览 / 55 s 每步 / 420 s 总时间 + 90 s 宽限）：没按时出手的请求由 Showdown 自己的 `default` 代出（预览 = 前 4 只、前 2 只首发；对战 = 每个位置第一个能用的招、随机目标；强制换人 = 第一只能上的），并扣掉整步时间；总时间扣光 = 「因不活动判负」。**一个掉线座位 = 预览 + 7 个对战请求被代打，第 8 个请求直接判负**。平台文档里的「300 s 随机代打」永远轮不到。`clock="none"` 可切回文档模型（此时 `basic` 和 `official_baseline` 逐局完全相同，测试里验证过）。
- 选秀阶段平台没有计时器：慢的回答照样生效。决策时间是模拟的（`SimClock`），不真的等。

| 名字 | 是什么 | 在 VGC 双打里实际会怎样 |
|---|---|---|
| `basic` | `examples/basic_agent.py`：永远 `legal_actions[0]` | 选秀拿第一张卡；Team Preview 交回模板 → INVALID_ACTION → 掉线 → Showdown 计时器代打，约第 8 个请求判负 |
| `messaging` | `examples/messaging_agent.py`（狼人杀聊天示例） | 宝可梦里 `choose_action` 就是 `legal_actions[0]`，`choose_message` 不会被调用 → 和 `basic` 完全一样 |
| `smoke` | `examples/smoke_agent.py` | 第一张卡；带前 4 只、前 2 只首发；每个位置用第一个招、打第一个对手（没有招就换第一个能换的） → 永远合法，**集火对面 A 位** |
| `llm_fallback` | **替身**：真的 `LLMAgent` 类 + 一个立刻失败的模型（key 有但无效 / 额度用完 / 服务挂了） | 每步重试 1 次后走 agent 自带的兜底 = 第一个合法动作 / smoke 的带人和出招 → 下法和 `smoke` 相同，但走的是 llm_agent 的代码。**不是「没 key」**：没有 `OPENAI_API_KEY` 时 `create_agent()` 在 `provider_from_env()` 里直接抛错，这个座位一步都不会下（选秀没有计时器，平台之后怎么处理这局没有建模） |
| `llm_timeout` | **替身**：同上，但每次模型调用都卡满 60 s 超时（`REQUEST_TIMEOUT_SECONDS`） | 每步 2 × 60 s = 120 s > 55 s（预览 120 s > 90 s）→ 每个对战请求都被计时器代打，和掉线一样判负；选秀没有计时器，兜底的第一张卡照样生效。**假设**：晚到 120 s 的回答被丢掉，所以从不崩溃。runner 提交时用的是决策前读到的 `state_version`，而文档第 8 节说计时器代打**不**增加对战的 `state_version`；如果 VGC 计时器也是这样，晚到的回答会落到**后面**的请求上（被当成过时的招执行，或被拒 INVALID_ACTION → DecisionError → 60 s 冷却）。复查时模拟过这种情况（种子 31，150 局对 v3）：每局都有 INVALID_ACTION，21 次过时的招被执行，44 局判负（丢弃模型是 46 局），v3 胜负 150/150 完全相同。平台上两种都没验证过，只影响对方的计数 |

替身**不能**代表「gpt-4o-mini 按时答出来」时的下法（那需要 key）；面板里的 `greedy` / `protect_spam` 是目前最接近「会挑看起来最强的招」的脚本对手。

有 key 时用 `llm_live`（只能按名字点，不在任何组里，不可复现，花 API 钱）：真的 `LLMAgent` + 环境变量里的模型（`OPENAI_API_KEY`，可选 `OPENAI_MODEL` / `OPENAI_BASE_URL`，任何兼容 OpenAI 的服务都行，包括本地模型；**不读 .env**），每次模型调用的真实耗时记到这个座位的计时器上，所以慢模型会被 VGC 计时器代打 / 判负，和平台一样。

```bash
OPENAI_API_KEY=... python -m local.panel --opps llm_live --games 50 --jobs 2 --name v3-llm   # 或 local.eval_pokemon --opponent llm_live
```

**v3（main 的 agent/）对官方示例，种子 31，每个 500 局，座位轮换**（`python -m local.panel --opps field --games 500 --seed 31`）：

| 对手 | v3 胜率 | 95% CI | 非法动作（我方/对方） | 平台模型：被拒 / 掉线局 | 计时器代打 | 对方判负局 |
|---|---|---|---|---|---|---|
| basic | 99.4% | [98.3, 99.8] | 0 / 0 | 500 次 / 500 局（全在 Team Preview） | 平均 7.2 步/局 | 167（33%） |
| messaging | 99.4% | [98.3, 99.8] | 0 / 0 | 500 / 500 | 7.2 步/局 | 167 |
| smoke | **97.8%** | [96.1, 98.8] | 0 / 0 | 0 / 0 | 0 | 0 |
| llm_fallback | **97.6%** | [95.9, 98.6] | 0 / 0 | 0 / 0（每步都走兜底，模型调用 2 次/步） | 0 | 0 |
| llm_timeout | 99.4% | [98.3, 99.8] | 0 / 0 | 0 / 0 | 7.2 步/局 | 167 |

同一批牌局 v2（`versions/v2_joint_lookahead`，前 200 局）：basic / llm_timeout 96.5%、smoke / llm_fallback **90.0%**。怎么读：
- 最「笨」的 smoke 反而比 random、basic 难打：它永远用第一招（通常是主力输出招，常是群攻）集火对面 A 位，从不浪费回合。v3 输的 11 局里有 3 局（种子 392 / 396 / 419）连 basic 也输——都是「首卡选秀」恰好拿到强力群攻 / 天气核心的卡池（例：地震双打 + 晴天 Torkoal 喷火，v3 自己开的戏法空间反而帮了对面的 Torkoal）。
- 替身和原型下法相同，所以 smoke≈llm_fallback、basic≈messaging≈llm_timeout 是预期的；500 对里只有 1 对（第 35 局）不同，原因是 v3 的首发矩阵在机器满载时碰到了 6 s 的墙钟保险（日志里 1 次 `preview over budget`，另一次是搜索中途被截断），换成了另一套首发。**这说明 v3 的首发在高负载下不是完全确定的**（比赛时 90 s 预览不会因此超时，但会降级）。面板现在按局记下这两种情况（previewWallCuts / previewFallbacks），出现差异时能直接看到原因。

## 多对手晋级面板（`local/panel.py`，09-28 从 `~/exp3/panelkit` 移入）

`local.ladder` 只和当前冠军打，`local.benchmark` 单进程、只有历史版本。面板（panel）= 一次让**任意一份代码目录**里的 `agent/` 打遍一组固定对手，用来发现「赢了上一版、却输给别的打法」的非传递性退步。

```bash
python -m local.panel --candidate-dir ../wt-foo --games 200                                  # 13 个对手各 200 局，6 个低优先级进程
python -m local.panel --candidate-dir ../wt-foo --env LOOKAHEAD_ALPHA=0.5 --name foo-a05       # 环境变量只给候选
python -m local.panel --candidate-dir ../wt-foo --compare logs/panel/runs/v3-s43.jsonl         # 配对比较 + 结论
python -m local.panel --opps field --games 500                                               # 只打官方示例
python -m local.panel --summary logs/panel/runs/foo-s43.jsonl                                 # 只汇总已有结果
```

- **对手**（默认 13 个，`--opps strength` / `field` 取一组，也可逗号列表）：official_baseline、random、v0、v1、v2（`versions/v2_joint_lookahead`）、cautious（09-27 的 la-fixed v2 把纳什 argmax 写死为开，放在 `local/sparring/`，不是冠军）、greedy（每个位置打当下期望伤害最高的招，只在被迫时换人）、protect_spam（被威胁的位置能守就守，不连续守，否则同 greedy）、v3（`versions/v3_draft_preview`），加上官方示例 basic、smoke、llm_fallback、llm_timeout（messaging 只在 `field` 组里或按名字；llm_live 只按名字，要 key）。两个脚本机器人在 `local/panel_bots.py`，选秀和带人跟 v1 一样，只比对战。
- **`--env` 只给候选**：worker 只在候选 import / 决策时设这些变量，对手出手前恢复；shell 里 export 的 `LOOKAHEAD_*` / `ARENA_DRAFT_*` / `PREVIEW_MATRIX_*` 会被丢掉并警告（v2、v3 读同样的名字）。
- **计算器检查**：每个对手包必须跑**自己的** calc.js（`versions/*`、`local/sparring/*` 的 `agent/js/node_modules` 要链到 `agent/js/node_modules`，`scripts/setup_machine.sh` 会建）；否则开跑前报错（`--allow-no-calc` 跳过）。
- **配对种子**：第 g 局对每个对手都用种子 `seed*100000+g`、座位 `g%2`，和 benchmark / ladder 完全一样。两个候选用同一个 `--seed` 就是在打**完全相同的牌局**，比较时一局对一局。**默认种子 43**，故意不用 benchmark 的 31（`--seed 31` 时对 official_baseline / random / v0 / v1 / v2 / v3 的 200 局就是 `local.benchmark` 那 200 局，每个候选都是看着这些局调出来的，13 行里有 6 行等于重复 benchmark）也不用 ladder 的 7 / 11。
- **候选按真实时间走 VGC 计时器**（`field_opps.WallClockSeat`）：候选每次 Team Preview / 对战决策的真实耗时记到和示例对手同一个 `VGCClock` 上（预览 90 s、每步 55 s、420 s + 90 s 总时间）；超时的决策改由 Showdown 代打，总时间扣光就判负——和平台一样，所以太慢的候选会在 `timer_cand` / 判负里显出来（以前候选没有计时器，这两项恒为 0，「干净」检查形同虚设）。每局记下候选最慢的一步（`tmax_cand`，选秀另记 `tdraft_cand`）和剩余总时间（`bank_cand`），汇总表最后一行 `candidate clock` 显示。对手不计时（历史版本几秒内出手；示例对手有自己的模拟时钟）。
- **可复现**：每局开始清空伤害计算缓存，计算器在第一局前启动，`PYTHONHASHSEED` 固定。只有碰到时间上限的决策会随机器负载变化，每局都计数：前瞻 2.5 s 上限（capHits）、纳什混合回退（mixFallbacks）、v3 首发矩阵的搜索被墙钟上限截断但仍给出答案（previewWallCuts，读 `preview.choose` 返回的 `info["wall_hit"]`，agent 本身不记日志）、v3 首发矩阵放弃改用启发式首发（previewFallbacks，日志 `preview matrix failed`，几乎都是 6 s 墙钟上限）。首发矩阵按「工作量」限时，只有墙钟上限会随负载变化。worker 里的 WARNING 日志不再打印，而是按「当时谁在决策」记到每局的 `warn_cand` / `warn_opp`（附第一条的文字），汇总表里非零时显示。实测（09-28）：v3 对 v3 的 200 局用 3 个 worker 重跑（机器上另有 3 个满载进程），和第一次逐局胜负相同（46.0%），首发回退 0 次。
- **输出**：默认 `logs/panel/runs/<name>-s<seed>.jsonl`（**不能**直接放在 `logs/<目录>/` 下：`scripts/replay_real.py`、`local.pool --harvest` 和测试会把 `logs/*/*.jsonl` 当真实对局记录读），每局一行（对手、种子、胜负、回合数、双方非法动作、计时器代打次数、判负方、示例对手的平台计数 `pf_*`、耗时），外加 `.meta.json` 和 `.summary.json`。汇总表：每个对手的胜率 + 95% Wilson 置信区间、**不加权面板均值**（稳健性分数，不是对真实对手的胜率估计）、**对手中的最低胜率**。几乎必胜的对手（基线、示例 agent）对均值差和它的标准误几乎没有贡献，不会冲淡结论。
- **面板均值的置信区间按局聚类**（`clustered_se`）：第 g 局对 13 个对手是同一个种子、同一个座位（同一个卡池），同一局的胜负在对手之间是相关的，独立的只有「局」。以前把 13 个对手当独立样本，v3 − v2 的区间窄了 1.6 倍（复查发现）：按局聚类后是 +20.1 [+17.4, +22.8]，不是 [+18.4, +21.8]。对「没有真实提升」的候选，旧公式约 11% 的概率给出 PROMOTE（应为 2.5%）。
- **结论**（`--compare`）：PROMOTE = 面板均值差的 95% 区间下限 > 0 且没有任何对手显著变差、候选 0 非法动作 / 0 计时器代打 / 0 判负、0 崩溃局、每局都有候选的决策耗时（旧版面板的结果没有这项，不能证明「干净」，一律 REJECT 并说明原因）；HOLD = 均值差 ≥ 0 且无显著变差、候选干净；否则 REJECT（退出码 1）。两次运行没有共同的局（种子或对手不同）时会直接提示。

**当晋级门槛用**（在 `local.ladder` / `local.benchmark` 之后多跑一步）：

```bash
# 一次性：在冠军代码上跑参照（logs/ 不进 git，换机器要重跑；默认种子 43、每个对手 200 局，3 个 worker 约 30 分钟）
python -m local.panel --name v3 --games 200 --jobs 3                       # -> logs/panel/runs/v3-s43.jsonl
# 每个候选：同种子、同局数，配对比较
python -m local.panel --candidate-dir ../wt-foo --name foo --games 200 --jobs 3 --compare logs/panel/runs/v3-s43.jsonl
```

结论不能是 REJECT：没有任何对手显著变差，候选自己 0 非法动作、0 计时器代打、0 判负、0 崩溃局，每局都量过决策耗时。面板均值是「各种打法下的稳健性」，不是对真实对手的胜率；和擂台（只对冠军）一起看。换了冠军就重跑参照（旧参照也没有候选计时，本来就要重跑）。

**v3 与 v2 的完整面板（`--seed 31`，每个对手 200 局，同一批牌局；在候选计时之前跑的，面板均值的区间已按局聚类重算）**：

| 对手 | v3 | v2 | 差（v3 − v2，配对 95% CI） |
|---|---|---|---|
| official_baseline | 100.0 | 99.5 | +0.5 [−0.5, +1.5] |
| random | 100.0 | 99.5 | +0.5 [−0.5, +1.5] |
| v0 | 94.5 | 72.0 | +22.5 [+15.4, +29.6] |
| v1 | 90.0 | 52.0 | +38.0 [+30.0, +46.0] |
| v2 | 88.0 | 47.5 | +40.5 [+32.5, +48.5] |
| cautious | 91.5 | 50.5 | +41.0 [+32.9, +49.1] |
| greedy | 94.0 | 58.5 | +35.5 [+27.8, +43.2] |
| protect_spam | 97.0 | 69.5 | +27.5 [+20.6, +34.4] |
| v3 | 46.0 | 13.5 | +32.5 [+23.7, +41.3] |
| basic | 100.0 | 96.5 | +3.5 [+0.9, +6.1] |
| smoke | 98.0 | 90.0 | +8.0 [+3.5, +12.5] |
| llm_fallback | 97.5 | 90.0 | +7.5 [+2.9, +12.1] |
| llm_timeout | 100.0 | 96.5 | +3.5 [+0.9, +6.1] |
| **面板均值** | **92.0** [90.8, 93.2] | **72.0** [69.7, 74.2] | **+20.1 [+17.4, +22.8]**，11 个对手显著更好，0 个更差 |

两边 0 非法动作、0 崩溃局、0 次前瞻超时；v3 对 v3 的 46.0% 是镜像局（座位和首发矩阵的随机性），不是退步。v2 当候选对 v3 参照的结论：REJECT（11 个对手显著更差；这两次运行没有候选计时，现在还会多一条「未计时」的原因）。

**v3 参照，默认种子 43，候选按真实时间走 VGC 计时器（09-28，13 × 200 局）**：official_baseline / random 99.5 / 100.0，v0 / v1 / v2 93.0 / 92.5 / 90.5，cautious / greedy / protect_spam 89.5 / 93.5 / 98.5，v3 46.0，四个官方示例全部 100.0；面板均值 92.5 [91.5, 93.6]（按局聚类）。v3 最慢的一步（预览或对战）1.49 s，2600 局里 0 次计时器代打、0 次判负、0 非法动作、0 崩溃局。

## 已知缺口（下一步）

1. 预言家夜里查验时会查"最可疑"= 平票取最小座位 = 明天就要被投出去的人，查验浪费。应排除最可能被处决的席位。
2. Pokémon 出招不看对手防御、不看速度、不预测对手。`@smogon/calc` 已验证可用，接进 bridge 即可做精确伤害。
3. ~~卡池是手写的~~ → 已换成真实卡池。
