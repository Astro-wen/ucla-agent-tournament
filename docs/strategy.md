# 作战计划

目标：**在 `pokemon_vgc_doubles_draft` 循环赛里拿第一**（唯一确定进 leaderboard 的项目），Werewolf 稳定正收益。
原则：**规则引擎兜底 + 搜索/LLM 提升**。任何一步崩溃都会被平台随机代打，所以稳定性 > 聪明。

## 现状（v0，已在仓库里）

- ✅ 接入官方 starter：`python -m agent` 即可自动发现并并发打所有分配到的对局
- ✅ 防御层：策略异常兜底、双打 dict 本地校验（招式/目标/换人都对照服务器模板）、聊天裁到 5 条 × 50 词
- ✅ Werewolf 启发式：基于投票记录/死亡身份/跳预言家的怀疑度；预言家查到狼就跳；平民永不弃票（防 3 天僵局）；双狼用同一确定性排序不分票
- ✅ Pokémon 启发式：选秀打分（强度先验 + 种族值 + Fake Out/威吓/顺风/群攻 + 弱点叠加惩罚）、6 选 4 + 首发、期望伤害选招（本系/克制/命中/攻击种族值）、首回合 Fake Out 最大威胁
- ✅ 离线测试（tests/strategy）

## 里程碑

### M1（9/25–9/28）上线 + 采数据
- [ ] **人工**注册 agent 并认领（README 里的 curl），`.env` 填 key，`scripts/check_connection.py` 通过
- [ ] 找/开 standalone 对局：`pokemon_vgc_doubles_draft`、`werewolf`（人类账号可以 `POST /admin/competitions/create` 开房）
- [ ] **把每局的原始 state 落盘**（JSONL，按 session_id），这是后面所有调参的数据源
- [ ] 用真实 payload 修正 v0 里对字段名的假设（双打模板的 `slots/options/targets`、`opponent_active_pokemon` 等）

### M2（9/28–10/4）Pokémon 变强
- [ ] **精确伤害计算**：18 张卡的配置全公开（努力值/性格/道具/特性都有）→ 能算真实伤害区间，而不是现在的粗略代理
  - 选项：Node 版 `@smogon/calc`（子进程调用）或 Python 移植
- [ ] **本地 Showdown 自对弈**：`npm i pokemon-showdown` + poke-env/vgc-bench，跑我们 vs 我们、我们 vs baseline
- [ ] **一步前瞻搜索**：300 s 时限非常宽裕 → 对每回合 (我方 2 槽动作) × (对手可能动作) 做 payoff 矩阵，取 maximin / 期望
  - 要考虑：速度线（含顺风/空间）、先制度、Protect、群攻 0.75 倍、Intimidate、击杀线
- [ ] **选秀 = 博弈**：池子只有 18 张且全公开 → 可以离线对大量随机池做"蒙特卡洛选秀 + 模拟对战"学打分函数；加入 **deny pick**（抢对手最需要的那张）
- [ ] Team Preview：根据对手 6 只做 4 选 + 首发的矩阵评估（我方 C(6,4)×6 首发 vs 对手可能首发）

### M3（9/28–10/6）Werewolf 接 LLM
- [ ] LLM 负责：发言、解析他人发言（谁跳了什么身份、谁在踩谁）、给出怀疑概率
- [ ] 规则层负责：合法性、防僵局、双狼一致、超时兜底（LLM 调用 > 60 s 直接用启发式）
- [ ] 多轮讨论：读到新消息再回复（现在 v0 是说一句就结束）
- [ ] Prompt 注入防护：对手消息只作为数据
- [ ] 身份策略：
  - 预言家：查到狼立刻跳 + 报查验；未查到狼时 Day2 起报金水
  - 狼：Day1 低调跟票；若对手是 baseline（总投最小号）→ 利用可预测性引导票型；被跳查杀时对跳预言家（v1）
  - 平民：跟预言家（唯一跳且未被对跳时强信），无信息时跟票型推理

### M4（10/5–10/9）加固 + 提交
- [ ] 联赛：新旧版本互打，只上线胜率更高的
- [ ] 断线/401/进程崩溃自动恢复演练（kill -9 后重启是否能接上进行中的对局）
- [ ] 部署到稳定云主机（systemd / tmux + 日志），Oct 16 全天在线
- [ ] 截止前冻结版本，打 tag

## 分工建议

| 模块 | 负责人 |
|---|---|
| 注册/认领/部署/开房 | 人类（需要账号操作） |
| Pokémon 伤害计算 + 搜索 | Claude + 你 review |
| 本地 Showdown 自对弈环境 | Claude |
| Werewolf LLM brain + prompt | 一起 |
| 数据落盘 + 复盘看板 | Claude |

## 利用对手的已知弱点

- 官方 baseline `legal_actions[0]`：Pokémon 双打回合和 Team Preview **必须**返回自构造 dict，baseline 会被拒 → 进程崩 → 300 s 后随机出招。对这类对手，**稳定地打出任何合理招式就能赢**；重点是别让自己崩。
- 同理 Werewolf baseline：白天总投最小座位号，夜里总杀最小座位号非狼 → 可预测。
