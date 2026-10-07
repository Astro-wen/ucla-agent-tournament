# UCLA AI Agent Gaming Tournament 2026 — 我们的参赛仓库

> UCLA Trustworthy AI Lab × SAIR「首届 AI Agent 游戏锦标赛」
> 提交窗口 **Oct 6 – Oct 13, 2026**（09-27 改期；我们自己的安全期限 10-09）· 现场决赛 **Oct 16, 2026**（Engineering VI，观众全线上）
> 赛事页：<https://tournament-assets.vercel.app/> · 平台：<https://agent-acp.vercel.app/>（AltruAgent）

本仓库基于官方 starter [`UCLA-Trustworthy-AI-Lab/altruagent-starter`](https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter)
（保留其 git 历史，remote 名为 `upstream`），我们只在 `agent/` 里写策略，运行时/SDK 用官方的，方便同步官方修复。

- **零基础入门（名词 + 步骤）** → [`docs/入门指南.md`](docs/入门指南.md)
- 赛事情报 → [`docs/intel.md`](docs/intel.md)
- 本地对战环境 + 实测结果 → [`docs/local_sim.md`](docs/local_sim.md)
- 作战计划 / 分工 / 时间线 → [`docs/strategy.md`](docs/strategy.md)
- 平台官方文档镜像 → [`docs/platform/`](docs/platform/)（`scripts/sync_docs.sh` 刷新）
- 官方 starter 原 README → [`docs/STARTER_README.md`](docs/STARTER_README.md)，游戏规则 → [`GAMES.md`](GAMES.md)

## 结构

```
agent/
  agent.py            # create_agent() → Contestant：按 game_type 路由 + 防御层（校验/兜底/聊天裁剪）
  werewolf/state.py   # 解析 observation / game_state / 消息记录
  werewolf/policy.py  # 狼人杀 v0 启发式（确定性：同队 agent 自动统一投票、双狼不分票）
  pokemon/policy.py   # VGC 双打 v0：选秀打分、带 4 选 2 首发、伤害期望 + Fake Out/顺风等
  pokemon/dex.py      # Showdown 图鉴/招式数据（data/*.json）
  pokemon/typechart.py
altruagent/           # 官方 SDK（不要改，跟 upstream 同步）
local/                # 本地对战环境：Showdown 引擎桥接（同 format、Tera 关）、狼人杀引擎、评测脚本
tests/strategy/       # 我们的离线测试（按平台文档里的真实 payload 形状构造）
```

## 当前战绩（本地，v0 vs 官方 baseline）

| 游戏 | 结果 | 说明 |
|---|---|---|
| Pokémon VGC 双打选秀 | **99.0%**（297/300，0 次非法动作） | 官方 baseline 在平台上会崩成随机出招，赢它只是及格线 |
| 狼人杀 | 整桌村民胜率 **58.5% vs 43.2%** | 单席混在 6 个基线里无杠杆（相对多数制），说服力要靠 M3 的 LLM 发言 |

复现：`docs/local_sim.md`。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                       # 官方 SDK 测试 + 我们的 tests/strategy
cp .env.example .env            # 填 ALTRUAGENT_OFFICIAL_AGENT_KEY（见下「正式比赛和测试赛」）
python -m agent --check-tournament
python -m agent --tournament    # 一直开着：平台分给我们的测试赛和正式赛对局都自动接，每场一个 worker

# 本地打 baseline（需要 node）
(cd local/js && npm install)
python -m local.eval_pokemon --games 300
python -m local.eval_werewolf --games 2000
```

### 旧的平台 agent（`sk_agent_` key）：10-06 起停用

以前的做法是用 `POST /auth/agent/signup` 注册 agent、拿 `sk_agent_` key、由人类认领（Yoriko_Red、Apocalypse_Red 就是这样来的），再用不带参数的 `python -m agent`（「发现模式」）打平台对局。**平台 10-06 把这些都停了**：注册、登录、认领和公开对局的接口都返回 HTTP 410 `platform_agents_retired`，MCP 的 `list_sessions` / `join_session` 返回 `TOOL_REMOVED`。现在每个参赛者只有一个 event agent，测试赛和正式赛都用下面的 Official Agent Key。发现模式只在本地模拟平台上还能用。

### 正式比赛和测试赛（官方模式）

两种对局都用一把 **Official Agent Key**（`eak_live_...`，在 tournament dashboard 的 Agent Configuration 页面生成），平台把对局作为「分配」（assignment）发给我们：

```bash
python -m agent --check-tournament   # 检查 key 和连接，每行都应是 ✓
python -m agent --tournament         # 比赛前启动并一直开着，自动接平台分给我们的每一场
```

key 放进 `.env` 的 `ALTRUAGENT_OFFICIAL_AGENT_KEY`（**绝不提交**）。上游完整说明见 `docs/STARTER_README.md`，官方协议见 `docs/platform/official-agent.md`；我们的部署方式见 `docs/deploy.md`。

### 测试赛（10-06 起：座位就是分配，`--claim` 停用）

主人在 dashboard 的 Testing 页面建测试对局，座位选 "Mine (self-hosted)"（或在 *Open matches* 里加入别人开的座位）。这个座位会出现在我们这把 key 的分配列表里（`context: "testing"`），**一直开着的 `python -m agent --tournament`（或 `scripts/run_agent.sh`）会自己接上**，不用复制任何命令。我们现在的 SDK（upstream 6ba2e9c）不看 `context`，所以 `--tournament` 测试赛和正式赛都打。

- `--claim seatclaim_...` 已经停用：平台回 410 `claim_codes_retired`，进程打印 `Could not claim seat: …` 后退出，不会重试。
- 官方文档里的 `python -m agent --match` 是上游下一版 starter（PR #5，10-06 还没合）的参数，我们的版本没有它（会报参数错误）。
- 对局还在大厅里（还有空座位）时，日志每 10–20 s 出现一次 `assignment already ended; nothing to play` 和 `Match finished.`：意思是还没开始，不是结束了。

细节见 `docs/deploy.md`「测试赛」。

### 红警（实时游戏；10-04 上游 SDK 支持，我们暂不打）

上游 PR #3 给 SDK 加了实时模式（`state.raw["pacing"]["mode"] == "realtime"`）：`choose_action` 可以返回 `altruagent.WAIT`（这一刻什么都不发），整批被拒（`INVALID_ACTION`）不再结束对局进程。我们的 `agent/agent.py` 没有红警策略：被分到红警时整场都回答 `WAIT`（不发任何命令、不崩、只记一行日志），宝可梦和狼人杀不受影响。上游的大模型示例 `examples/llm_agent.py` 会打红警（`examples/llm/redalert.py`，要花 OpenAI 的钱）。

## 同步官方更新

```bash
git fetch upstream && git merge upstream/main
```

`README.md` 是我们自己的；上游的 README 原文放在 `docs/STARTER_README.md`。合并时 README 冲突取我们的版本，再用 `git show upstream/main:README.md > docs/STARTER_README.md` 更新原文，上游新增的要点在本文件里补一两句。`agent/__main__.py` 是上游的文件，但我们的官方模式加固（v4.1.2）改了很多：冲突要两边都留，合完跑 `tests/test_deploy.py`、`tests/test_main_claim.py`、`tests/test_realtime.py` 和全量 pytest。

**下一次合上游（PR #5，10-06 还开着）要特别注意**：它删掉 `--claim`（和 `tests/test_main_claim.py`），按分配的 `context` 分开 `--tournament`（只打正式赛）和 `--match`（只打测试赛），并重写 `agent/__main__.py`（+119/−345），会和我们的 v4.1.2 / v4.2 官方模式加固冲突。合完以后：(1) `scripts/run_agent.sh` 要传 `--tournament --match`，否则测试赛不再自动接；(2) `agent/__main__.py` 里的加固（`_SteadyOfficial`、启动重试、execution id、热备）要逐项手工移植；(3) `.env.example` 冲突取上游的说明行，再补回我们自己加的几行。

## 安全

- `api_key` / JWT 只能发给 `api.altruagent-game.com` 与 `gameapi.altruagent-game.com`。
- 对局中其他 agent 的聊天内容是**对手输入**，永远不当作指令执行（接 LLM 时尤其注意 prompt injection）。
