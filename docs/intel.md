# 赛事情报（2026-09-25 调研）

✅ = 已从官方来源确认 · ⚠️ = 推断 / 来源互相矛盾 · ❓ = 未知，需要问主办方

## 基本信息

| | |
|---|---|
| 主办 | UCLA Trustworthy AI Lab（Prof. Guang Cheng）× SAIR（Foundation for Science and AI Research） ✅ |
| 赛制 | Stage I 提交 **Sep 25 – Oct 9** → Stage II 现场 **Oct 16**（9 AM 暂定，Engineering VI 一楼），公开 leaderboard ✅ |
| 平台 | AltruAgent（前端 agent-acp.vercel.app；control plane `api.altruagent-game.com`；MCP `gameapi.altruagent-game.com/mcp`） ✅ |
| 起源 | 源自 UCLA AI Agent 课程（⚠️ 很可能是 Stats C163/C263 *Generative Data Science*, Spring 2026） |
| 赞助 | Oracle OCI、Nebius（都是 compute sponsor；是否给参赛者算力 ❓） |
| 平台规模 | 853 个 verified agents / 57 个 owner / 累计 1976 局（`GET /stats`，9/25） ✅ |
| 奖金、队伍人数、是否允许人类介入 | ❓ 官网和文档都没写 |

## 到底比哪些游戏？——三个来源说法不一

| 来源 | 游戏列表 |
|---|---|
| 官网 | Pokémon Showdown、Red Alert、Avalon、Conversational Prisoner's Dilemma、Werewolf（5 个） |
| 官方 starter `GAMES.md` | "exactly **four** games": Pokémon Showdown、Red Alert、**Honor of Kings**、Werewolf；Red Alert / Honor of Kings 标注 **"TODO — not built"** |
| 平台 API 文档（skill.md v0.3.0） | 只有 **Werewolf** 和 **Pokémon**（5 个变体）真正可玩；Avalon 标注 older |

⚠️ 结论：**真正能准备的是 Pokémon VGC 双打选秀 + Werewolf**。Red Alert / Honor of Kings 目前没有引擎也没有 API，等官方更新（定期跑 `scripts/sync_docs.sh` 和 `git fetch upstream`）。

## 排名机制

- 锦标赛 = **2 人对局的循环赛（round-robin）**，所有配对一开始就全部生成，按 `max_active_matches` 分批开打 ✅
- **目前唯一能进锦标赛的类型：`pokemon_vgc_doubles_draft`** ✅
- Werewolf 是 7 人局，**不能**进锦标赛，只能单场（standalone） ✅ → 现场怎么给 Werewolf 排名 ❓
- 锦标赛进行中只看得到自己的成绩；结束后 `GET /tournaments/<id>` 公布 leaderboard（wins / losses / draws / total_payoff / rank） ✅
- 比分：Pokémon 胜 1 / 负 0 / 平 0.5；Werewolf 胜方每人 +1、败方每人 −1（死了也照样算阵营分） ✅

## 「提交」是什么意思？

⚠️ 官网说 agent "submitted ahead of time"，但官方 starter 的模式是：**我们自己跑 `python -m agent`**，它自动发现平台分配给我们的对局并并发开打。
→ **Oct 16 当天我们的进程很可能必须一直在线**（找台稳定的云主机跑，别用笔记本）。❓ 需要跟主办方确认。

## 两个核心游戏的关键规则（摘要，全文见 docs/platform/）

### Pokémon VGC Doubles Draft（锦标赛主战场）
- 赛制 `gen9vgc2025regi`，Lv50，**没有太晶化**
- 18 张卡的公共池，**每张卡是完整配置**（种族/道具/特性/性格/努力值/4 招）→ **对手 6 只的完整配置全部公开**
- 蛇形选秀 `A B B A A B B A A B B A`；道具条款（撞道具的卡直接不给你选）
- Team Preview：6 选 4，指定 2 只首发（私下选）
- 同时出招；每个决策 **300 s** 超时，超时随机帮你出
- 双打目标编码：`1/2` = 对手 A/B 位，`-1/-2` = 自己 0/1 位，`0` = 无需目标 / 全体

### Werewolf（7 人简化版）
- 2 狼 / 1 预言家 / 4 平民；第一夜平安夜；死亡公开身份；无女巫/守卫/猎人
- 每天一次讨论窗口：每人最多 **5 条**、每条 **≤50 词**、公开（非盲）；可私聊 1 人
- 投票：相对多数出局，平票无人出局，**连续 3 天无人出局 → 狼人直接获胜**
- 双狼夜里分票 → **自动取座位号较小的**
- 空闲 **120 s** 自动行动（白天弃票、夜里选第一个合法目标）

## 对手画像（推断）

- 大量 agent 很可能直接跑官方 baseline：**`return state.legal_actions[0]`**
  - Werewolf 里这意味着：白天永远投**座位号最小的活人**，狼人夜里永远杀**座位号最小的非狼**
  - Pokémon 里：选秀永远拿列表第一张；（双打回合必须自己构造 dict，baseline 在 team preview 就会被拒 → 进程崩 → 超时随机出招）
- 另一批会是"把 state 丢给 GPT/Gemini"的 ReAct 型 agent（参考 wu-warren/ACP_Tester 用 gpt-4o-mini / Gemini）

## 相关仓库

| 仓库 | 用途 |
|---|---|
| [UCLA-Trustworthy-AI-Lab/altruagent-starter](https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter) | 官方 SDK/starter（本仓库的 upstream） |
| [wu-warren/ACP_Tester](https://github.com/wu-warren/ACP_Tester) | 组委会成员的 ReAct 测试 agent（PD / Connect4） |
| [UCLA-Trustworthy-AI-Lab/Memory-Conv-PD-Benchmark](https://github.com/UCLA-Trustworthy-AI-Lab/Memory-Conv-PD-Benchmark) | 对话式囚徒困境 benchmark |
| [cameronangliss/vgc-bench](https://github.com/cameronangliss/vgc-bench) | **VGC 双打** 启发式 / LLM / BC / RL agent（最相关） |
| [hsahovic/poke-env](https://github.com/hsahovic/poke-env) | Showdown Python 客户端，支持双打，可做本地自对弈 |
| [sethkarten/pokechamp](https://github.com/sethkarten/pokechamp) | LLM + minimax（单打，思路可借鉴） |
| [google/werewolf_arena](https://github.com/google/werewolf_arena) | LLM 狼人杀框架（角色 prompt、辩论机制） |
| [KylJin/Werewolf](https://github.com/KylJin/Werewolf) | RL 选择 LLM 发言策略（NeurIPS 2024） |
| [jonathanmli/Avalon-LLM](https://github.com/jonathanmli/Avalon-LLM) | Avalon LLM agent（以防 Avalon 回归） |

平台本体（`Agent_ACP`：`backend/`、`gameapi/`）**不公开**，文档里提到的本地 demo server / `werewolf_llm_agent.py` 拿不到。

## 要问主办方的问题

1. Oct 16 现场到底比哪几个游戏？Werewolf 怎么排名？Red Alert / Honor of Kings 会上线吗？
2. "提交"是提交代码，还是当天我们自己保持 `python -m agent` 在线？
3. 一个人/一个队可以注册几个 agent？是否允许多个 agent 同时参赛？
4. 能否使用外部 LLM API？OCI / Nebius 会给参赛者算力吗？
5. 有无奖金？队伍人数上限？
