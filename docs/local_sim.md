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
python -m local.eval_werewolf --games 1000 --seeds 7 --jobs 3     # 狼人杀混桌评测，候选 agent/ 对冠军 champion/（种子 1、2 调参用过；1003、1004 用于 10-03 身份交叉检查 + 读聊天的测量；1005、1006 用于 10-05 审查修正后 llmq 的测量；1007、1008 用于 10-06 措辞变化的测量）
python -m local.werewolf_platform --games 40      # 狼人杀：用官方 runner（run_game）跑 7 个座位，按平台文档检查
```

狼人杀混桌评测（`local/eval_werewolf.py`）：我们占 k = 1..7 个座位，其余座位是一种背景对手——`first`（永远第一个合法动作，= basic / smoke 示例）、`heuristic`（仿平台演示 `werewolf_agent.py`：只看死亡和投票记录）、`random`、`llm`（`local/werewolf_bots.py` 里的「大模型替身」：会读聊天、容易被说服、有噪声、偶尔弃票、被指控时可能对跳预言家）、`llmx`（同一个替身，决策不变，但说话换着说法：随口的写法、「seer here: p3 is a wolf」「All players must vote Player3」，还夹杂看着像跳预言家其实不是的闲聊，比如「Is the Seer here?」「I checked Player3's votes」「What if I am the seer?」；跟着聊天里被点名最多的座位投票的概率只有 0.6。`llm` 只会说 8 句固定的话，而且正好是我们读聊天的格式，对我们偏心）、`llmq`（还是同一个替身，但照真实 Qwen3 大模型座位的写法说话：跳预言家用回归集 `tests/strategy/data/ww_llm_chat.json` 里的句式，比如「I investigated Player3 and confirmed they are a wolf」「Player3 is the real wolf」「Player3 is lying — they're a wolf」；闲聊里夹着真实大模型常见的怀疑口吻，这些话其实没说任何人是狼，比如「Player3 voted against me, but that doesn't mean they're a wolf」「I'm not convinced they're a wolf」「Seer claim: none yet」「Player3 was killed last night. It was the wolves」。它同时衡量读懂真实大模型的收益和读错的代价；默认的 `--bg` 里包含它）或 `mixed`（每个座位从 first / heuristic / random / llm 这四种里抽；不抽 `llmx` / `llmq`，这样以前的牌局不变）。同一局的身份、我们的座位和背景对手对所有被测 agent 完全相同（配对比较）。报告按身份（村民 / 预言家 / 狼）的胜率和 Wilson 95% 置信区间、每座位胜率、配对差值。因为一桌里我们的村民和我们的狼会互相影响，按身份的结论用「只换一边」的配对：`agent/champion` 对 `champion`（只换狼）判狼，`champion/agent` 对 `champion`（只换村民和预言家）判村民和预言家；整体用整桌 `agent` 对 `champion` 的每座位胜率。全部条件合在一起会把某一种桌子上的退步平均掉，所以结论里还列出「分片退步」：按背景（全部 k，以及 k ≤ 3，也就是一桌里我们座位不多的现实情况）分片，任何一个身份或每座位胜率的配对差值 95% 置信区间整个在 0 以下都算；有分片退步时 SHIP 为 False。种子 1 和 2 在调参时用过，确认结论要换别的种子（比如 7）。本地引擎另外模拟平台的失败规则：出错、非法动作、被拒的聊天或超过 120 s 都算进程死亡，之后由平台代打（白天弃票、夜里第一个目标）；讨论时每轮随机发言顺序。

`local/werewolf_platform.py` 是按 `docs/platform/werewolf.md` 写的多线程平台替身：messaging / moving 两种 phase、state_version、WRONG_PHASE / NOT_YOUR_TURN / STALE_STATE / INVALID_ACTION、每窗口 5 条 50 词、terminate 幂等、120 s 闲置代打，每个座位跑真正的 `altruagent.runner.run_game`。平台没写清楚的地方按最严处理：已经 terminate 的座位再发聊天会被拒（runner 会因此让进程退出）。10-06 起按 10-05 的文档：讨论窗口以外 `new_messages` 是空的（投票时也是），窗口开 120 s 强制结束（状态里带 `messaging_seconds_left` / `messaging_deadline`），超过 1024 个字符的聊天回 `MESSAGE_TOO_LONG`；`--no-messaging`（或 `ARENA_WW_MESSAGING=off`）是没有讨论窗口的局（`messaging_enabled: false`，发消息回 `MESSAGING_DISABLED`）。

狼人杀措辞（10-06 起）：两个本地引擎默认用 10-05 文档的新说法（`Vote to eliminate PlayerN`、`cause` 是 `vote`、`(eliminated by vote)` / `(killed at night)`、`— PlayerN eliminated by vote`、预言家查验前 `You have not investigated anyone yet.`、结束时 `Game over — WOLVES win`）；`ARENA_WW_WORDING=old` 换回旧说法（lynch）。`ARENA_WW_SAME_NAMES=on`（只用于评测）让我方座位看到所有座位同一个名字，这时 agent 只能读 observation 里按座位写的文字记录（测文字解析那条路）。

狼人杀讨论的节奏（10-06 起，`ARENA_WW_TIMING`，只影响 `local/werewolf_env.py`）：默认 `rounds`，每轮把活着的座位（包括我方）随机排一次、各问一次，最多 6 轮，每轮在窗口时钟上算 20 s。`rt` 照官方 runner 的真实节奏：我方几毫秒就回答，所以每天都是我方在 t = 0 第一个开口；之后别的座位每发一条聊天或 terminate，我方都会被叫醒再问一次（runner 的 `wait_for_update` 就是这样），其余座位每轮算 15 s；轮数用完后我方还没结束发言，就每 5 s 再问一次，直到 120 s 窗口关闭（这记作 `window_close`）。两种节奏下状态里都带 `messaging_seconds_left`。`local/werewolf_platform.py` 新增 `--window`（讨论窗口秒数，平台是 120，调小可以压缩时间跑）、`--think`（替身 bot 每次发言前平均想多少秒，像大模型调用；0 = 立刻回答）和 `--llm llmq`（大模型替身换成 Qwen 的说法）；输出多一行：我方收到的错误码（`WRONG_PHASE_NOOP` 是 runner 在窗口关闭的那一刻给已经结束发言的座位又发了一次 terminate，无害）和「多听一会儿」（`ARENA_WW_LISTEN`，见 `docs/deploy.md`「狼人杀聊天」）的统计。

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

同一个 agent 接 Nebius 的真实模型、带花费上限：`llm_real`，见下文「真实大模型对手」。

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

## 真实大模型对手（Nebius Token Factory，`llm_real`，09-28）

为什么：主办方的通用 LLM agent（`examples/llm_agent.py`）很可能是真实对手之一，以前本地只有它的替身（`llm_fallback` / `llm_timeout`，模型从不回答）。现在让**同一份** `LLMAgent` 代码（同样的提示词、同样从合法动作生成的严格 JSON、同样「1 次重试 + 规则兜底」）去调 Nebius 上的真实模型，只把 OpenAI 换成 Nebius（OpenAI 兼容接口 `https://api.tokenfactory.nebius.com/v1/`）。替身保持不变。

- `local/llm_client.py`：和 agent 无关的客户端（只用 httpx + python-dotenv + 标准库），以后可以原样拷成 `agent/llm.py` 给发言层用。每次请求 60 s 超时；429 / 5xx / 连接错误退避重试（超时不重试，免得一步拖 120 s）；相同请求走 sqlite 缓存（不花钱、答案复用，仅本地评测；缓存坏了就当没命中）；**花费账本** `~/.cache/altruagent/llm_usage.jsonl`（仓库外；每次请求先写一行「预留」，结束再写一行结果；只有 run、模型、tag、请求 id、token、估算花费、耗时、状态，不含 key、提示词或回答）。
- `local/llm_seats.py`：`NebiusProvider`（给 `LLMAgent` 用）；VGC 的 `llm_real`（`local/field_opps.py` 注册，经官方 runner 规则 + VGC 计时器，模型真实耗时记到这个座位的计时器上，和 `llm_live` 一样）；狼人杀的 `LLMWerewolfSeat`（`werewolf_env` 的座位：读聊天、投票 / 夜间行动、每个讨论窗口最多 2 条 × 40 词、可私聊 1 人）。座位自己**不过滤**任何东西：本地引擎对它和对其他座位一样套用平台的失败规则（出错、非法动作、平台会拒的聊天、单步超过 120 s → 进程死亡，之后平台代打），都记在 `Game.stats` 里。
- 狼人杀混桌评测（`local/eval_werewolf.py`，新策略）里它是背景对手的一种：`--bg llm_real`（所有背景座位都是真实 LLM；牌局和 `--bg llm` 完全相同，可以一局对一局比），或者 `--llm-real N`：每个条件里 N 个背景座位（每局单独抽，对所有被测 agent 相同）换成真实 LLM，其余照 `--bg`；同一局再用 `--llm-control`（默认 `llm` 替身）坐这 N 个座位打一遍当对照。背景可以写 `mixed:first+heuristic`（每个背景座位从列出的几种里抽）。报告多一节：每个 k 我方按身份的胜率和 Wilson 区间（对真实 LLM / 对替身）、配对差值、LLM 座位自己按身份的胜率、兜底 / 不合规回答 / 平台失败 / 发言、请求数、缓存命中、超时、花费、延迟，以及账本里这个 run 的花费。带真实座位时默认 20 局、只测 `agent`（不配对）；计划的真实局数超过 `--llm-max-games`（默认 400）直接拒绝开跑。

**Key**：放在 checkout 的 `.env`（已 gitignore）里一行 `NEBIUS_API_KEY=...`；在别的目录跑时用 `NEBIUS_DOTENV=/path/to/.env` 指过去。客户端只读 `.env` 里 `NEBIUS_*` 的行，key 只进 HTTP 头，不打印、不写日志 / 账本 / 缓存 / 报错。

| 变量 | 默认 | 作用 |
|---|---|---|
| `NEBIUS_MODEL` | `Qwen/Qwen3-30B-A3B-Instruct-2507` | 模型 |
| `NEBIUS_BUDGET_USD` | 5.0 | **全局预算** = 账本里所有花费之和（所有进程、所有 run，含还没结束的预留）；下一次请求按最坏情况（估计的提示 token + max_tokens）会超就拒绝（`BudgetExhausted`），不发请求。**最高 20**（`HARD_CEILING_USD`，填 1000 也只算 20）；填了但不是数字（`$1.84`、`1,84`、`1.84 USD`、nan、inf）→ 0（全拒，拒绝理由里写明），**不会悄悄变回 5**；负数 → 0 |
| `NEBIUS_RUN_BUDGET_USD` | 不设 = 没有 | **每个 run 的美元小上限**（按 `NEBIUS_RUN_ID` 算这个 run 在账本里的花费，含预留），和全局预算同时生效；乱写 / nan / 负数 → 0（全拒，不会悄悄失效） |
| `NEBIUS_MAX_CALLS` / `NEBIUS_MAX_TOKENS` | 2000 / 3,000,000 | **每个 run** 的请求数（**每次重试都算**）/ token 上限（面板的多个 worker 共用一个 `NEBIUS_RUN_ID`）；`100.0`、`1e6`、`1_000_000` 都能读，乱写 → 0（全拒） |
| `NEBIUS_LEDGER` / `NEBIUS_CACHE` | 见上 | 账本 / 缓存位置；`NEBIUS_CACHE=off` 关缓存。**真跑时别改账本路径、别删账本**：全局预算只管同一个账本文件（`NEBIUS_LEDGER` 写错、或用 root 跑（HOME 不同）= 另一本空账 = 另一份预算）。账本必须是普通文件（`/dev/null` 之类直接拒绝） |
| `NEBIUS_BASE_URL` / `NEBIUS_TIMEOUT` | Nebius / 60 | 任何 OpenAI 兼容服务都行 |

上面四个上限（预算、run 小上限、请求数、token 数）以**环境变量 / `.env` 里的值为天花板**：代码自己 `LLMClient(...)`，或者给 `from_env` / `shared_client` 传参数，只能调低、不能调高。`ledger_path=None`（只在本进程里记账）的客户端不发真实请求（它的「全局」预算只管它自己）。

**怎么读账本（09-29）**：`reserved`（预留）行只是占位，不是花费：最坏情况写在 `reserve_usd` / `reserve_tokens`，它的 `cost_usd` 和 token 字段是 0，所以直接把 `cost_usd` 一列加起来 = 已结算的花费（不含还在飞的预留）。09-29 之前的旧预留行把最坏情况写在 `cost_usd` 里，直接相加会把每个请求算两遍——09-28 的「$3.25」就是这么来的，真实账本总额是 $1.04（用 main 上旧代码 + 假服务器复现过：2 个 worker、上限按 0.1 倍缩放成 $0.184，真实总额 $0.104 时直接相加是 $0.258，看起来「超了上限」；一直跑到被拒，真实总额最高 $0.18399 ≤ $0.184）。要看预算检查用的数，跑 `python -m local.llm_client ledger`。

**没 key、超预算、超上限、请求失败**：客户端立刻拒绝（不发请求），`LLMAgent` 走自己的规则兜底（第一个合法动作 / smoke 的带人和出招 / 结束发言）= 和 `llm_fallback` 下法相同，**不会崩**；面板和评测开跑前打印 WARNING。`eval_pokemon` / `eval_werewolf`（真实 LLM 座位）跑到一半被上限拒绝时会**提前停**并说明（不再把后面几百局兜底局混进「对 LLM」的胜率里；狼人杀被打断的那一局丢掉，对照也截到同样的局）。

**审查加固（09-28）**，每条都有测试（`tests/strategy/test_llm_opponents.py`，全用假 HTTP，不联网、不读真 `.env`）：
- **并发**：检查上限和写「预留」在同一把文件锁里完成，多个 worker 不会同时通过同一次检查（8 个并发、只够 3 次 → 恰好发 3 次）；进程中途被杀，它的预留按最坏情况一直计着。
- **结果不明的请求照最坏情况计费**：超时、发出后断线、502 / 504、200 但读不出内容、200 但没报 usage —— 服务器可能已经算钱；连不上、4xx、429、500 / 503 记 0。
- **多进程预算（09-29，`tests/strategy/test_llm_budget.py`）**：12 个进程各自一个客户端、同时猛打一个只够 7 次的上限 → 恰好发 7 次，逐行重放账本，任何时刻都没超过上限；把文件锁换成空锁，同样的测试会发 12 次（超 60%）。没有 `fcntl.flock` 的平台直接拒绝。表里没有的模型按表里最贵的输入 / 输出价预留和计费；缓存命中不花钱、不进账本、`cached=True`。
- **账本读不了 / 写不了就拒绝**（以前会退回进程内计数、而那份计数不参与预算检查 → 等于没上限）；账本里坏掉的行不会让检查崩。
- **对抗测试（09-29，`tests/strategy/test_llm_budget_attacks.py`）**，每条先写成会失败的测试再修：
  - **发出之后才出错的请求照最坏情况计费**：比如回答的 gzip 压缩坏了（httpx 抛 `DecodingError`，它不算 `TransportError`），以前记 0 美元 → 调用方一直重试就能无限花钱（测试里只够 3.5 次的上限发出了 20 次）。现在只有「确定没发出去」的错误（连不上、请求体编码失败）才记 0；请求体编码失败在预留之前就报错，不发也不记账。
  - **最坏情况的提示 token 改成「1 字节 = 1 token」**：字节级分词器一个 token 至少占 1 字节，这才是真上限。旧的「3 字节 = 1 token」对数字多的提示不成立（Qwen 等把数字拆成单个数字，差不多 1 字节 1 token），实际花费会超过预留、账本能越过上限。代价：预留大约变成原来的 3 倍（只影响还在飞的请求占的额度，结算仍按服务器报的实际 token）。
  - **账本最后一行没写完（进程写到一半被杀、磁盘满）**：以前下一条预留会粘到这半行后面，整行解析失败被丢掉 → 别的 worker 看不到这条预留（测试里只够 1.5 次的上限，2 个 worker 都发了；8 个进程抢只够 5 次的上限时发了 6 次）。现在写之前先补一个换行。
  - **账本在运行中被删 / 截短 / 换成另一个文件**：以前这个进程会从 0 重新算（等于预算重置）。现在这个进程从此拒绝（要重启进程）。注意：删账本之后**新启动**的进程只能看到新文件——所以不要删账本。
  - **预留写完必须能读回来**，否则拒绝（`/dev/null` 这类「账本」会吞掉写入，每次检查都看到 $0）；拿到文件锁之后再确认锁住的还是账本那个文件。
  - **设置写错不再变回默认值**；代码传参不能调高环境变量里的上限（见上面表格下面那段）；`max_tokens` / `temperature` 不是有限数字 → `LLMError`，不发请求。
  - **守住了、测试证明的**：同一个客户端被 10 个线程共用、只够 3 次 → 恰好 3 次；`max_tokens=10**12` 直接被预算拒；重试每次重新预留；在预留和结算之间抛出的非 `Exception` 异常（类似 KeyboardInterrupt）让预留一直计着；结算时账本变只读 → 预留保留、下一次拒绝；垃圾行、时间戳错乱（未来 / 负数 / 没有）不影响总额；VGC `llm_real` 和狼人杀 `LLMWerewolfSeat` 走共享客户端，预算用完后不再发请求、座位走规则兜底不崩。
  - **残余风险（代码挡不住，只能记账）**：服务器无视 `max_tokens` 多算钱 → 按服务器报的数记上，下一次请求会看到并拒绝，超出量最多是当时还在飞的那几个请求；`llm_live`（`OPENAI_*` 环境变量，官方 `OpenAIProvider`）完全不走这个账本，别把 Nebius 的 key 填进 `OPENAI_API_KEY`；账本只是按价目表的估算，以 Nebius 控制台为准。
- **绝不因为我们自己的代码让座位崩**：`NebiusProvider` 把任何异常都变成 `ProviderError`（只带异常类型名），`LLMAgent` 照常重试一次再兜底；usage 字段格式怪、正文不是 JSON、缓存出错、key 不是合法 HTTP 头（例如含非 ASCII 字符）都不再抛出别的异常。
- **不合 schema 的回答算无效**：端点不支持严格 `json_schema` 时会退到 `json_object`，那时模型什么都能答（比如 `bring` 里放对象，官方 `pokemon.py` 的 `build()` 会抛 TypeError → runner 判座位掉线）；现在按 schema 检查类型 / 枚举 / 必填 / 多余字段，不合就是无效回答（OpenAI 严格模式本来就保证这一点）。JSON 模式按 schema 分别记忆，一个 schema 被拒不会让别的也降级。
- **对局里的文字只是数据**：聊天里的恶意文本以转义字符串进请求；回答外面包着说明文字、代码块或 `<think>` 都能解析，没写完的 `<think>`、极深的嵌套也不会抛异常；照着注入内容选的非法动作会被官方 agent 自己的校验挡下 → 重试 → 兜底。

```bash
python -m local.llm_client status          # key 在不在（只打印 present/absent）、模型、已花 / 预算
python -m local.llm_client ping            # 一次很短的调用（约 40 token）
python -m local.llm_client ledger          # 按 run 汇总账本；总额 = 已结算 + 未结束的预留（预算检查用的就是这个数）
python -m local.eval_pokemon --games 10 --opponent llm_real --seed 43 --verbose
python -m local.panel --opps llm_real --games 20 --jobs 2 --name v3-llmreal   # 只能按名字点，不在任何组里
python -m local.eval_werewolf --llm-real 2 --bg mixed:first+heuristic --ks 1,5 --games 40 --seeds 43 --jobs 2   # 对照 = llm 替身
```

**模型和价格**：默认 Qwen3-30B-A3B-Instruct-2507，每百万 token 输入 $0.10 / 输出 $0.30，最便宜的一档；非思考模型，支持严格 JSON schema，短回答约 1–3 s；和示例默认的 gpt-4o-mini 同档，代表「主办方的通用 LLM agent」。`google/gemma-3-27b-it` 同价也能用；DeepSeek-V4-Flash、Nemotron-3.5-Lightning 在 30 token 的 ping 里没给出 JSON（思考型，要加大 max_tokens）。价格表在 `llm_client.PRICES`：Nebius 自己的价格页是前端渲染、抓不到，数字取自 Mastra 的 Nebius 模型文档（和 `GET /models` 的模型列表一致，OpenRouter 上 Qwen3-30B / gpt-oss-120b 的价格相同）；表里没有的模型按最贵的 $3 / $15 计。花费是**估算**（服务器报的 token × 表价），以 Nebius 账单为准。

**冒烟测试（09-28，真 key；两轮合计账本 $0.138 / $5：第一轮 $0.077 含面板，审查加固后第二轮 $0.061）**。注意：这一节是在 aa48e95 上测的——旧的 VGC 卡池（43 张均匀抽）、旧的狼人杀引擎（没有平台失败规则）和旧的狼人杀策略；狼人杀那几行是当时的「视角 L」（2 个 LLM 座位 + 5 个我方座位，对照 = 官方基线）。rebase 到 main（242d78d）之后的数字见下一节。

| 项目 | 结果 |
|---|---|
| `GET /models` / ping | 25 个模型；Qwen3-30B 严格 JSON schema 可用，ping 1–2 s |
| VGC：v3.2（main 的 agent/）对 `llm_real`，10 局，种子 43 | **10/10 胜**（95% CI [72%, 100%]），平均 4.9 回合；我方 0 非法 |
| VGC `llm_real` 自己 | 134 个决策，规则兜底 4 次（3.0%），不合规回答 11 / 141 次调用（7.8%，含失败的请求；4 个决策两次都不合规 → 兜底），平台拒绝 0；1 次 60 s 超时 → 1 步被 VGC 计时器代打；141 次请求 34.5 万 token，**$0.037**（约 $0.004 / 局），延迟 p50 2.7 s / p95 10 s / 最大 37.5 s |
| 狼人杀：2 个 LLM 座位 + 5 个我方座位，20 局（对照 = 同种子，那 2 席换成官方基线） | 我方座位胜率 **43.0%**（对照 51.0%）；村民方赢 8/20（对照 12/20）；LLM 座位胜率 52.5%（狼 69%、预言家 71%、村民 35%） |
| 狼人杀 LLM 座位自己 | 116 个决策，兜底 0，不合规回答 2 / 260（0.8%），非法 0；发言 142 条（33 条私聊），0 条超平台限制；245 次请求（另有 14 次缓存命中）29 万 token，**$0.032**（约 $0.0016 / 局），延迟 p50 1.1 s |
| 加固后重跑种子 43（缓存重放） | VGC 139 / 140、狼人杀 258 / 259 次命中缓存，对局结果和第一轮完全相同；**schema 检查拒掉 0 个**历史回答（新检查不改变严格模式下的真实行为）；唯一的新请求是第一轮超时的那一次，这次 3–5 s 答出 |
| 加固后 VGC 种子 44，10 局 | **10/10 胜**；`llm_real` 144 个决策兜底 2 次（1.4%），不合规回答 6 / 148（4.1%），schema 不合 0，平台拒绝 0，计时器代打 0；116 次请求 **$0.030**，p50 2.8 s / p95 4.2 s / 最大 6.0 s |
| 加固后狼人杀种子 44，20 局 | 我方座位 **49.0%**（对照 53.0%）；村民方 8/20（对照 10/20）；LLM 座位 37.5%；兜底 0，不合规 3 / 260（1.2%），发言 146 条（29 条私聊），0 超限；223 次请求 **$0.030**，p50 2.1 s |
| 狼人杀两个种子合计 40 局 | 我方座位对 LLM **46.0%**（92/200 座位局）vs 对基线 52.0%（104/200）；村民方 16/40 vs 22/40 —— 方向两次一致，但样本仍不够显著 |

怎么读：
- Pokémon：10 局全胜只说明 Qwen3-30B 这一档的通用 LLM 对 v3.2 不构成威胁（样本小，区间下限 72%）；但它**会按时答出来**、自己挑招，下法和替身不同，所以是比 `llm_fallback` 更真实的对手。模型偶尔卡 30–60 s，真实平台上同样会被计时器代打。
- 狼人杀：两个种子共 40 局，区间仍很宽，但方向一致：LLM 村民不跟我方的投票逻辑、LLM 狼会说话带节奏，村民方胜率比对基线时低。这正是下一步「狼人杀发言层」要解决的：我方要能读懂、反驳真实 LLM 的发言。注意缓存：同一个提示词永远得到同一个回答（包括不同局里完全相同的第一夜提示），所以 LLM 座位比真实采样更「确定」；要独立样本就 `NEBIUS_CACHE=off`。
- 一局 VGC 约 $0.004、一局狼人杀（2 个 LLM 座位）约 $0.0016，默认 $5 预算够上千局；想更省就开缓存重跑同样的种子（相同提示直接复用答案）。

**rebase 到 main（242d78d）之后（09-28，`NEBIUS_CACHE=off` 全部新采样，两项合计 $0.210；账本 $0.138 → $0.348）**：

| 项目 | 结果 |
|---|---|
| 狼人杀（新策略、新引擎）：我方 1 席 + 2 个真实 LLM 座位 + 4 个 first / heuristic，40 局，种子 43；对照 = 同样的牌局，那 2 席换成 `llm` 替身 | 我方每座位胜率 **52.5%** [36.8, 68.2] vs 对照 67.5% [52.8, 82.2]，配对差 **−15.0 [−28.2, −1.8]**；村民 31.6%（n=19）vs 52.6%，差 −21.1 [−39.9, −2.2]；预言家 60.0% vs 60.0%（n=5）；狼 75.0% vs 87.5%（n=16），差 −12.5 [−29.2, +4.2] |
| 狼人杀：我方 5 席 + 2 个真实 LLM 座位，40 局，种子 43 | 每座位 58.0% [51.0, 65.0] vs 对照 58.5% [51.6, 65.4]，差 −0.5 [−6.4, +5.4]；村民 60.0 vs 67.5、预言家 62.5 vs 68.8、狼 38.5 vs 30.8（配对差都不显著） |
| 狼人杀 LLM 座位自己（80 局） | 442 个决策兜底 0，不合规回答 2 / 1048 次调用（0.2%）；平台失败规则 0 次（崩溃 / 非法 / 被拒聊天 / 超时 / 结束后再发言）；发言 522 条 + 82 条私聊；LLM 座位胜率 k=1 33.8%（替身 36.2%）、k=5 45.0%（替身 55.0%）；1048 次请求 **$0.134**（约 $0.0017 / 局），p50 0.8 s / p95 1.2 s / 最大 1.9 s |
| VGC：v3.2（main 的 agent/，48 张卡、真实卡池频率的新环境）对 `llm_real`，20 局，种子 45 | **19/20 胜**（95.0%，95% CI [76.4%, 99.1%]），平均 4.7 回合；我方 0 非法、0 计时器代打 |
| VGC `llm_real` 自己 | 270 个决策（含选秀）兜底 6 次（2.2%），不合规回答 20 / 284 次调用（7.0%），0 超时、0 平台拒绝、0 计时器代打；284 次请求 **$0.076**（约 $0.0038 / 局），p50 0.9 s / p95 1.2 s / 最大 1.8 s |

怎么读：
- 狼人杀：我方只有 1 席时，真实 LLM 比 `llm` 替身难打得多（每座位 −15 个点，95% 区间整个在 0 以下），主要在我方当村民时（−21 个点）：替身读我们格式的聊天、跟着被点名最多的座位投票（上文也说它「对我们偏心」），真实模型不这样。5 席时差别消失（我方自己的票就够多）。所以对 `llm` 替身的数字高估了我方在少数席位桌上的表现；样本仍小（k=1 的预言家只有 5 局），要确认就换种子再跑。
- VGC：新环境下仍是 19/20，第一次输给 `llm_real` 一局（种子 45 第 18 局）；这次模型没有卡住（最慢 1.8 s）。
- **平台替身和示例 agent 的一处不一致**：`local/werewolf_platform.py` 每条聊天和每次 terminate 都让 `state_version` +1，而 `examples/llm_agent.py` 按「state_version 只在走子时变」来数每轮 2 次发言；在替身上，一个一直想说话的模型每条消息后都拿到新的 2 次，第 6 条被平台拒掉（MESSAGES_QUOTA_EXCEEDED）→ runner 报错、进程退出（假模型复现）。本地引擎 `werewolf_env` 讨论期间版本号不变，所以 `eval_werewolf` 里不会发生。平台到底怎样文档没写（`wait_for_update` 单独有 `since_message_seq`，示例作者的注释也说只在走子时变），测试里只用不说话的模型跑替身。

## 外部风格对手 simbot（09-28，`local/ext_opp.py` + `local/js/simbot.js`）

为什么：面板里的对手要么是我们自己的历史版本，要么是脚本机器人。公开的强 VGC bot（VGC-Bench 等）要下载 torch 和模型权重，没有获批，所以没下载。simbot 是自己写的、**原理和我们完全不同**的对手：不用伤害计算器，也没有手写战术。每回合它在**真 Showdown 引擎的克隆**上把双方的双槽组合都打一遍（打 1 回合，再让双方按最大伤害贪心多打 1 回合），按存活数、HP、异常、能力变化、顺风、墙、戏法空间估值，把结果排成零和矩阵博弈，用 regret matching 求均衡，再按均衡的混合策略抽一个动作。守住、击掌奇袭、看我嘛、帮助、控速、换人，只要引擎算出来划算，它就会用。

- **信息**：克隆的是真实对局，所以它知道对手的精确 HP 和带了哪 4 只（配置本来就公开，这是一点小优势）。它看不到对手本回合的指令；随机数种子也换掉了，不会预知伤害浮动或会心一击。回合中途的换人请求（U-turn 等）会用已经排队的招式模拟本回合剩下的部分，这是一点小泄漏，很少见。
- **选秀 / 带人**：simbot 没有自己的选秀。`simbot` 用**冠军自己的**选秀和 90×90 首发（champion/agent），所以正常选秀下比的就是对战；`simbot_v1` 用 v1 的固定规则（card_score 选秀 + v1 带人）。
- **速度**：每步约 1 s（p95 约 2 s，最慢 3.2 s），每局约 15 s；2 个 worker 打 200 局约 25 分钟。唯一受机器负载影响的地方是预算：前半段搜索超过 1.5 s 时后半段只用 1 个样本，超过 3 s 就提前停。
- **协议**：答案走正常模板检查，不合法算 invalid（实测 0 次）；搜索出错时随机出一个合法动作并计数（实测 0 次）。对随机对手 19/20。

```bash
python -m local.panel --opps simbot --games 200 --seed 71 --jobs 2 --name v32-vs-simbot --save-logs
python -m local.panel --opps simbot --games 200 --seed 71 --jobs 2 --name v32-vs-simbot-same --same-team --save-logs
python -m local.panel --opps simbot --seed 71 --only 12,18 --name replay --save-logs     # 只重放指定的局
python scripts/extopp_analyze.py logs/panel/runs/v32-vs-simbot-s71.jsonl --species Ting-Lu,Incineroar
```

- `--same-team`：双方拿**同一套 6 张**卡（从本局 18 张里随机抽，物种不同，尽量道具也不同）。选秀流程照走，每一手只有一个合法选项，所有配置照常展示给双方，所以只比首发和对战。它和普通运行不是同一批局：要用不同的 `--name`，不能拿来 `--compare`。
- `--save-logs`：每局写一个 `<out>.logs/<对手>-g<g>.json`，里面有 Showdown 全日志、双方阵容、我方每一步的答案，以及 simbot 每一步的矩阵值和混合策略。`scripts/extopp_analyze.py` 读这些文件，列出双方行为对比、按局面特征拆开的胜率、每局输掉的那一回合发生了什么、按卡的胜率；同队运行还会列出同一只宝可梦在两边的招式使用率。
- `SIMBOT_*` 环境变量用来调 simbot（样本数、剪枝宽度、预算等，见 `local/ext_opp.py`），会写进 meta 文件。
- simbot 默认不在任何组里（`all` / `strength` / `field` 都不含它）。已有的面板、擂台和基准局一局都不变（同种子 A/B 逐局相同）。

**v3.2（main 的 agent/）对 simbot**，种子 71（simbot 只在种子 70 上调过）；0 非法动作，0 计时器代打，simbot 0 回退：

| 条件 | 冠军胜率 | 95% CI | 平均回合 |
|---|---|---|---|
| 正常选秀（simbot 用冠军的选秀和首发） | 95/200 = 47.5% | [40.7, 54.4] | 9.1 |
| 同队（双方同 6 张） | 95/200 = 47.5% | [40.7, 54.4] | 11.8 |
| 正常选秀，simbot_v1（v1 的固定选秀规则 + v1 首发） | 84/100 = 84.0% | [75.6, 89.9] | 8.9 |

对照：冠军对面板里其他所有对手都在 88% 以上（v3 镜像除外）。simbot 是目前唯一能和冠军打平的非自家对手。它一换成 v1 的选秀和首发，冠军就赢 84%，所以冠军的领先主要来自选秀和首发，对战本身并不比这个一回合搜索强。输局分析在 `~/exp8/extopp/NOTES.md`。主要问题：从不主动换人（simbot 每局约 2 次，靠抗性换入吃伤害、反复威吓）；被更快的对手在出手前击倒（输局里最亏的那一回合 44–60% 是这种情况）；几张卡的招式估值有错（Ting-Lu 的 Ruination 被计算器算成 0 伤害，改去连用撒菱）；控速（冰冻之风）和击掌奇袭用得少；另外还有群攻和集火造成的击倒。

## 开源学习型对手 VGC-Bench（09-28，`local/vgcbench_opp.py` + `local/vgcbench_worker.py`）

为什么：simbot 是我们自己写的搜索。VGC-Bench（Angliss 等，AAMAS，MIT 许可）是公开的**学习型** VGC 双打 bot：用 Showdown 天梯的公开队伍表（OTS）对局记录做行为克隆（BC，模仿人类），再用自我对弈（self-play）微调。别的参赛者最可能直接下载它来用，所以拿它当对手最接近"外面的人会带什么来"。

- **两个对手**：`vgcbench_bc`（只做行为克隆，`saves_bc/seed1/100.zip`）和 `vgcbench_sp`（行为克隆 + 自我对弈，`saves_bc_sp/reg_all/seed1/98304000.zip`）。
- **怎么接的**：他们的代码跑在他们自己的虚拟环境里（torch、poke-env），由 `local/vgcbench_worker.py` 这个子进程负责，每个面板 worker 进程一个，模型只加载一次，局与局之间复用。我们这边（`local/vgcbench_opp.py`）只做翻译：
  - **选秀**：用冠军的代码（和 simbot 一样），所以正常选秀下比的是首发和对战。
  - **首发（team preview）**：用他们的策略网络。我们的环境在真正的 Showdown 对局开始之前就要首发，所以 bridge 用同样的两队和同一个随机种子临时建一个对局，只取这一方在首发阶段能看到的消息、请求（request）和公开队伍表（OTS），喂给 poke-env；它答的 `/team 3142` 换成平台的 `select_lineup`。
  - **对战**：每一步把真实对局里**这一方能看到的**协议消息（bridge 的 `sideview` 命令，用 Showdown 自己的 `extractChannelMessages` 拆分，看不到对方的精确 HP、看不到对方的 request）和这一方的 request 发给 worker；worker 走 poke-env 自己的消息处理和 VGC-Bench 的 `PolicyPlayer`（embed_battle + 动作掩码 + 策略网络 + action_to_order），返回 Showdown 指令串（如 `/choose move closecombat 1, switch Incineroar`），我们再换成平台的 `doubles_turn`（招式 id、目标编号本来就一样、换人名字换成 species），用 `template_ok` 检查。
  - **出错时**：worker 启动失败或崩溃先重启一次，再不行就随机出一个合法动作；答案不在模板里也随机出。**全部计数**（面板里 `vgcbench:` 那一栏：fallbacks、worker errors、restarts）。VGC-Bench 词表里没有的招式 / 道具 / 特性映射到它的 null（编号 0）并计数（`unknown-vocab`）；Showdown 本来就会自动改的目标（打向已倒下的对手位置 → 打剩下那只；锁招不看目标）照样改掉并计数（`retargeted`）。
  - **随机性**：策略按概率抽样（和他们的 `play.py` 一样；`VGCBENCH_DETERMINISTIC=1` 改成取最大概率），每步用由本局种子导出的种子，所以同种子重跑结果相同。
- **可选安装**：没有安装时面板拒绝这两个名字并说明原因，pytest 跳过需要它的那一个测试（Mac / CI 上照常全绿）。默认不在任何组里，已有面板逐局不变（同种子 A/B 相同）。
- **忠实度检查**：`scripts/vgcbench_check.py` 边打边核对：worker 看到的 request 与环境完全一致；它的 DoubleBattle（回合、自己每只的 HP/状态/道具/特性/招式/能力变化、双方场上、对方 HP（百分比 ±1）/状态/能力变化/道具/特性、天气/场地/双方场地状态）与引擎真值一致；动作掩码与平台模板允许的动作一致；动作 → 指令串 → 平台字典 → 环境的 Showdown 指令与直接解读指令串一致。

```bash
python -m local.panel --opps vgcbench_sp --games 200 --seed 71 --jobs 3 --name champ-vs-vgcb-sp --save-logs
python -m local.panel --opps vgcbench_bc --games 200 --seed 71 --jobs 3 --name champ-vs-vgcb-bc --save-logs
python -m local.panel --opps vgcbench_sp --games 100 --seed 71 --jobs 3 --name champ-vs-vgcb-sp-same --same-team --save-logs
python scripts/vgcbench_check.py --opp random --games 30           # 忠实度检查 + 对随机（也可 --opp v0 / simbot）
python scripts/extopp_analyze.py logs/panel/runs/champ-vs-vgcb-sp-s71.jsonl
```

- 速度：每个面板 worker 第一次用时启动 VGC-Bench worker 约 1–1.5 分钟（从 D: 冷读 torch），之后每步约 0.06 s（p95 约 0.1 s）。
- 环境变量：`VGCBENCH_HOME`（默认 `/mnt/d/altruagent`，下面有 `vgc-bench-sv/`、`venv/`、`models-sv/`），或分别设 `VGCBENCH_CODE` / `VGCBENCH_PYTHON` / `VGCBENCH_MODELS`；`VGCBENCH_THREADS`（每个 worker 的 torch 线程数，默认 1）；`VGCBENCH_DEBUG=1`（trace 里带上 worker 的完整视图）；`VGCBENCH_WORKER_STDERR=<文件>`（worker 的 stderr，默认丢弃）。这些会写进 meta 文件。

**安装方法**（全部放 D:，不占 C:；只需要一次）：

```bash
D=/mnt/d/altruagent; mkdir -p $D/{pip-cache,tmp,hf-home}
export PIP_CACHE_DIR=$D/pip-cache TMPDIR=$D/tmp HF_HOME=$D/hf-home
git clone https://github.com/cameronangliss/vgc-bench $D/vgc-bench-sv
git -C $D/vgc-bench-sv checkout Scarlet/Violet          # = 72057ce，朱紫时代的版本，对应我们的 gen9 Reg I
python3.12 -m venv $D/venv
$D/venv/bin/pip install torch==2.12.0 --index-url https://download.pytorch.org/whl/cpu      # 只要 CPU 版
$D/venv/bin/pip install "poke-env @ git+https://github.com/cameronangliss/poke-env.git@sv" stable_baselines3==2.8.0 huggingface_hub==0.36.2
$D/venv/bin/pip install --no-deps -e $D/vgc-bench-sv    # 不装 open-spiel（只有他们的 eval 模块要用）
$D/venv/bin/python -c "from huggingface_hub import hf_hub_download as d
for f in ('results/saves_bc/seed1/100.zip', 'results/saves_bc_sp/reg_all/seed1/98304000.zip'):
    print(d('cameronangliss/vgc-bench-models', f, revision='c15e5bc', local_dir='$D/models-sv'))"
```

本机实际装的是 poke-env sv 分支的 379a046、torch 2.12.0+cpu、stable_baselines3 2.8.0、numpy 2.5.3，Python 3.12.3。不要用 VGC-Bench 的 main 分支（现在只支持 Pokémon Champions）。

**忠实度**（`scripts/vgcbench_check.py`，对 random / v0 / simbot 共 160 局、1527 个对战决策）：request 0 处不一致；6108 次己方宝可梦核对、2728 次对方在场宝可梦核对、1527 次天气/场地/场地状态核对全部一致；动作掩码和模板只有一种差别：poke-env 允许打向**已经倒下的对方位置**（Showdown 会自动改打剩下那只，适配层照此换算并计入 `retargeted`），其他 0 处；解码往返 1527/1527 一致。对随机 30/30、20/20（SP / BC），对 v0 29/30、15/20；SP 对 simbot（双方都用冠军的选秀）34/60 = 56.7% [44.1, 68.4]。

**冠军（main 的 agent/）对 VGC-Bench**（冠军用真实 VGC 计时，双方都 0 非法动作、0 回退、0 次 worker 出错；每步平均 45–64 ms，p95 ≤ 108 ms）：

| 条件 | 冠军胜率 | 95% CI | 平均回合 |
|---|---|---|---|
| vgcbench_sp，正常选秀，种子 71 + 72 | 216/400 = 54.0% | [49.1, 58.8] | 8.4 |
| vgcbench_sp，同队，种子 71 + 72 | 98/200 = 49.0% | [42.2, 55.9] | 10.5 |
| vgcbench_bc，正常选秀，种子 71 | 155/200 = 77.5% | [71.2, 82.7] | 8.8 |
| vgcbench_sp 取最大概率（`VGCBENCH_DETERMINISTIC=1`），种子 71 前 100 局 | 48/100 = 48.0% | [38.5, 57.7] | 8.9 |

VGC-Bench 的自我对弈版和 simbot 一样，是能在对战上和冠军打平的外部对手。输局里和 simbot 研究相同的问题全部重现（不主动换人、在更快对手的击杀线上被先手击倒、Ruination 估值错误、被群攻 / 集火击倒），另外它还靠长局消耗（寄生种子、吸取拳、祈愿、再来一次）和 Parting Shot / 威吓循环赢冠军。明细在 `~/exp11/NOTES.md`。

## 引擎搜索对战策略的本地评测（`ARENA_BATTLE=search`，09-29）

agent 的对战默认用引擎搜索（`agent/js/search.js`，在 agent 自己的 Node 进程里跑 Showdown 引擎；`ARENA_BATTLE=classic` = 冠军的打法，回退开关）。本地评测它时要注意三件事：

- **一定要开 `ARENA_PLOG=1`**：平台的对战状态里总带 `protocol_log`（协议日志，docs/real_payloads.md），搜索从里面读挥发状态和场地剩余回合；本地环境默认不带（为了让旧结果逐局重放）。不开的话搜索只能用粗略的备用信息，候选会被低估。冠军不受影响：开着它时冠军镜像在种子 7 / 11 仍是 191/400、211/400，逐个数字相同。
- **用面板代替 `local.ladder`**：`local.ladder` 只有一个进程，搜索每局约 25 秒，400 局要近三小时。`python -m local.panel --opps champion --games 400 --seed 7 --jobs 6` 打的是和 ladder 完全相同的局（同样的种子和座位），可以并行（6 进程约 30 分钟），结果可以直接和 191 / 211 比。
- 搜索的工作量是固定的（`ARENA_SEARCH_FIXED=1`，默认）：不被截断时，同一局面、同一种子给出同一个答案，和机器快慢无关。**但有安全网**：每步搜索最多用「这一步可用时间 × 60% − 1 秒」的 80%。对战每步可用时间最多 10 秒（`timecontrol.BATTLE_S`），**平台上也一样**（录下的 793 个真实对战状态全是 10 秒；平台的计时行只会让它更少），所以安全网约 **3.9 秒**，`ARENA_SEARCH_CAP_MS`（默认 6 秒）实际上不起作用。另外剪枝阶段超过安全网的一半（约 2 秒）时只抽 1 遍样而不是 2 遍。机器很忙时两者都会发生（负载 ~14 时约 30% 的搜索只抽 1 遍、约 5% 碰到安全网），所以高负载下同一批局两次运行会有少数局不同，门槛测到的也是这个「略减量」的版本；要逐局复现就在低负载下跑。`ARENA_SEARCH_ORACLE=1`（只给候选）让搜索直接读模拟器里的真实对局，用来量「信息缺口」，不是参赛配置。

## 已知缺口（下一步）

1. 预言家夜里查验时会查"最可疑"= 平票取最小座位 = 明天就要被投出去的人，查验浪费。应排除最可能被处决的席位。
2. Pokémon 出招不看对手防御、不看速度、不预测对手。`@smogon/calc` 已验证可用，接进 bridge 即可做精确伤害。
3. ~~卡池是手写的~~ → 已换成真实卡池。
