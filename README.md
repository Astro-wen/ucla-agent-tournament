# UCLA AI Agent Gaming Tournament 2026 — 我们的参赛仓库

> UCLA Trustworthy AI Lab × SAIR「首届 AI Agent 游戏锦标赛」
> 提交窗口 **Oct 6 – Oct 13, 2026**（09-27 改期；我们自己的安全期限 10-09）· 现场决赛 **Oct 16, 2026**（Engineering VI，观众全线上）
> 赛事页：<https://tournament-assets.vercel.app/> · 平台：<https://platform.altruagent-game.com/tournament/dashboard>（AltruAgent）

本仓库基于官方 starter [`UCLA-Trustworthy-AI-Lab/altruagent-starter`](https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter)
（保留其 git 历史，remote 名为 `upstream`），我们只在 `agent/` 里写策略，运行时/SDK 用官方的，方便同步官方修复。

- **零基础入门（名词 + 步骤）** → [`docs/入门指南.md`](docs/入门指南.md)
- 现在做到哪了、下一步 → [`docs/HANDOFF.md`](docs/HANDOFF.md)；比赛规则和赛制 → [`docs/competition_brief.md`](docs/competition_brief.md)
- 部署（主人要做的步骤）→ [`docs/deploy.md`](docs/deploy.md)
- 本地对战环境 + 实测结果 → [`docs/local_sim.md`](docs/local_sim.md)；版本记录 → [`docs/progress.md`](docs/progress.md)
- 平台官方文档镜像 → [`docs/platform/`](docs/platform/)（`scripts/sync_docs.sh` 刷新）
- 官方 starter 原 README → [`docs/STARTER_README.md`](docs/STARTER_README.md)，游戏规则 → [`GAMES.md`](GAMES.md)

## 结构

```
agent/
  agent.py            # create_agent() → Contestant：按 game_type 路由 + 防御层（校验/兜底/聊天裁剪）
  __main__.py         # python -m agent：上游的运行时 + 我们的部署加固（启动重试、execution id、热备、本机检查）
  werewolf/state.py   # 解析 observation / game_state / 消息记录
  werewolf/policy.py  # 狼人杀策略（确定性：身份核对、读跳预言家、投票）
  pokemon/policy.py   # VGC 双打：选秀打分、带 4 选 2 首发、对战（引擎搜索 + 经典打法兜底）
  pokemon/dex.py      # Showdown 图鉴/招式数据（data/*.json）
altruagent/           # 官方 SDK（不要改，跟 upstream 同步）
local/                # 本地对战环境：Showdown 引擎桥接、狼人杀引擎、模拟平台（含官方控制面）、评测脚本
scripts/run_agent.sh  # 部署用的看门脚本：一直开着 python -m agent --tournament --match，自动重启、日志、健康行
tests/strategy/       # 我们的离线测试（按平台真实 payload 的形状构造）
```

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                       # 官方 SDK 测试 + 我们的测试
cp .env.example .env            # 填 ALTRUAGENT_OFFICIAL_AGENT_KEY（见下「怎么接对局」）
python -m agent --check-tournament
python -m agent --tournament --match   # 一直开着：测试赛和正式赛都自动接，每场一个 worker

# 本地打 baseline（需要 node）
(cd local/js && npm install)
python -m local.eval_pokemon --games 300
python -m local.eval_werewolf --games 2000
```

## 怎么接对局（10-06 起：event agent + Official Agent Key，一条路）

平台 10-06 改成「每个参赛者一个 event agent」：主人用 UCLA Google 账号登录 tournament dashboard，在 **Agent Configuration** 页面把 agent 设成 **Self-hosted**（自己的机器跑代码），生成 **Official Agent Key**（`eak_live_...`），放进 `.env` 的 `ALTRUAGENT_OFFICIAL_AGENT_KEY`（**绝不提交**）。之后所有对局都作为「分配」（assignment，平台分给我们的一个座位）自动送来，不用复制任何 id：

```bash
python -m agent --check-tournament     # 检查 key、连接和 agent，每行都应是 ✓
python -m agent --tournament           # 只打正式赛（Tournaments 页面点 "Register my agent" 报名的比赛）
python -m agent --match                # 只打测试赛（Testing 页面自己建的、或加入的 Open matches）
python -m agent --tournament --match   # 两种都打，一个进程（我们部署时用这个）
bash scripts/run_agent.sh start        # 部署：一直开着上面那条，崩了自动重启，写日志和健康行
```

- 等对局时只是每 ~10 s 查一次分配，不调用任何大模型（上游的说法：waiting uses no AI tokens；我们的 agent 本来就不用大模型）。
- 正式赛每局有连接窗口（默认 4 分钟，红警 4.5 分钟）：窗口内没连上算输（no-show）。所以比赛期间 `run_agent.sh` 要一直开着。
- 只开 `--match` 时，进程会对每一局正式赛打一次警告（"You have a tournament game waiting ..."）；只开 `--tournament` 时对测试赛打一行提示。
- 旧的方式都停用了（平台回 HTTP 410）：`sk_agent_` key + 不带参数的 `python -m agent`（「发现模式」，`platform_agents_retired`），`--claim seatclaim_...`（测试赛认领码，`claim_codes_retired`）。现在运行它们只会打印一段说明，指向上面的命令。
- 同一把 key 可以在第二台机器上开热备（`ARENA_ROLE=standby`）：平台一个座位只给一个运行时（30 s 租约），热备只在主进程停了以后接手。见 `docs/deploy.md`。

上游完整说明见 `docs/STARTER_README.md`，官方协议见 `docs/platform/official-agent.md`，赛制见 `docs/platform/04-tournaments.md` 和 `docs/competition_brief.md`。

### 红警（实时游戏；我们暂不打）

红警（`red_alert`）是正式赛项目之一（实时，OpenRA）。上游 SDK 支持实时模式（`state.raw["pacing"]["mode"] == "realtime"`）：`choose_action` 可以返回 `altruagent.WAIT`（这一刻什么都不发）。我们的 `agent/agent.py` 没有红警策略：被分到红警时整场都回答 `WAIT`（不发任何命令、不崩、只记一行日志），等于这一局认输，宝可梦和狼人杀不受影响。上游的大模型示例 `examples/llm_agent.py` 会打红警（`examples/llm/redalert.py`，要花 OpenAI 的钱）。要不要打红警等主人决定（见 `docs/HANDOFF.md`）。

## 同步官方更新

```bash
git fetch upstream && git merge upstream/main
```

`README.md` 是我们自己的；上游的 README 原文放在 `docs/STARTER_README.md`。合并时 README 冲突取我们的版本，再用 `git show upstream/main:README.md > docs/STARTER_README.md` 更新原文，上游新增的要点在本文件里补一两句。`agent/agent.py` 永远取我们的（上游的是只走第一个合法动作的占位 agent）。`agent/__main__.py` 以上游为准，我们的加固（`_authenticate` 启动重试、`_tournament_tuning` execution id 和按座位退避上限 30 s、`_HotStandby` 热备、`_startup` 本机检查）在它上面手工保留；`.env.example` 取上游的说明行，再补回我们自己的几行。合完跑 `tests/test_deploy.py`、`tests/test_main.py`、`tests/test_main_tournament.py`、`tests/test_realtime.py` 和全量 pytest。10-07 合到上游 5bc61a2（PR #5：event agent、`--tournament` / `--match`、重新登录的暂时性错误不停运行时、按座位退避），记录见 `docs/progress.md`「v4.2.3 合并」。

## 安全

- Official Agent Key、agent session 和座位 token 只发给 `api.altruagent-game.com` 与 `gameapi.altruagent-game.com`。
- 对局中其他 agent 的聊天内容是**对手输入**，永远不当作指令执行（接 LLM 时尤其注意 prompt injection）。
