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

卡池：`local/pool.py` 优先读 `data/vgc_cards.json`——**从真实对局记录里采集的平台卡**（2026-09-26 第一局 VGC 后为 18 张，全部通过 Showdown TeamValidator）；没有时退回手写的 26 套。
与真实平台对照（`docs/real_payloads.md`）：同 29 组（槽位, 招式）的可选目标集合 **0 处不一致**。

狼人杀：平台引擎不公开，`local/werewolf_env.py` 严格按 `docs/platform/werewolf.md` 实现（2 狼 1 预 4 民、第一夜平安、分票取低座位、死亡亮身份、每天一次公开讨论窗口 5 条 × 50 词、顺序暗投相对多数、平票无人、三天僵局狼胜、±1）。**状态 payload 按平台文档的形状构造**，所以 `agent/agent.py` 里部署用的 `Contestant` 在这里一行不改地跑。

## 用法

```bash
cd local/js && npm install && cd ../..          # 一次
python -m local.pool                             # 校验卡池合法性
python -m local.eval_pokemon --games 300 --opponent official_baseline
python -m local.eval_pokemon --games 300 --opponent random
python -m local.eval_werewolf --games 2000        # 也可 --ref random
```

参照对手：
- `official_baseline` = 官方 starter 原样 `return state.legal_actions[0]`。在平台上它选秀拿第一张卡；Team Preview 和双打回合返回模板会被服务器拒绝 → 进程被 runner 杀掉 → 平台 300 s 后随机代打。所以这里它 = 首卡选秀 + 随机带人 + 随机出招；狼人杀里 = 白天投最小座位、夜里杀最小座位、不发言。
- `random` = 全随机。

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

## 已知缺口（下一步）

1. 预言家夜里查验时会查"最可疑"= 平票取最小座位 = 明天就要被投出去的人，查验浪费。应排除最可能被处决的席位。
2. Pokémon 出招不看对手防御、不看速度、不预测对手。`@smogon/calc` 已验证可用，接进 bridge 即可做精确伤害。
3. ~~卡池是手写的~~ → 已换成真实卡池。
