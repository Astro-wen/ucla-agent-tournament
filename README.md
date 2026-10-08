# UCLA AI Agent Gaming Tournament 2026 — 我们的参赛仓库

> UCLA Trustworthy AI Lab × SAIR「首届 AI Agent 游戏锦标赛」
> 提交截止 **2026-10-13 23:59 PDT（2026-10-14T06:59Z）**（提交窗口 Oct 6 – 13；我们自己的安全期限 10-09）· 现场比赛 **Oct 16, 2026**（Engineering VI，观众全线上）
> 官网：<https://altruagent-game.com/> · 平台：<https://platform.altruagent-game.com/tournament>（AltruAgent）· 规则：[`docs/official_rules_2026-10-06.md`](docs/official_rules_2026-10-06.md)

## Tournament entry (Official Rules, Version 2026-10-06, section 8)

**What it is.** One self-hosted event agent for the AltruAgent AI Agent Gaming Tournament. It plays
**Pokémon VGC doubles draft** (`pokemon_vgc_doubles_draft`), **Werewolf** (7 players) and **Red Alert** (`red_alert`,
real time) with a **rule-based bot** (`agent/redalert/`: fixed build orders, economy, production and army rules; no
learning, no model calls). Honor of Kings is not supported (not playable on the platform). The agent is fully
autonomous: no person chooses, approves or edits anything during a match.

**Submitted version.** The latest commit on this repository's default branch (`main`) at the Submission Deadline,
11:59 p.m. PDT on October 13, 2026 (2026-10-14T06:59Z). The competition machine runs exactly that commit: a git
checkout of it, or a bundle built from it with `bash scripts/bundle.sh` (the bundle's `COMMIT` file names the full
commit id). Every start of `scripts/run_agent.sh` logs the full commit id and the `ARENA_*` settings in effect. From the
deadline until 30 days after final results nothing is pushed to this repository. If the entry is submitted through a
separate public repository instead, that repository is exported from one commit of this one by
`scripts/publish_sync.sh` (same code, its README is `PUBLIC_README.md`), and the submitted version is the latest commit
of *that* repository's default branch at the deadline.

**How to run it** (Linux x86-64, Python ≥ 3.11, tested 3.12; Node.js ≥ 18, the bundle ships 22.23.3; bash ≥ 4.2):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock   # pinned runtime dependencies
(cd agent/js && npm ci --omit=dev)          # @smogon/calc damage calculator (agent/js/package-lock.json)
(cd local/js && npm ci)                     # pokemon-showdown simulator for the battle search (local/js/package-lock.json)
cp .env.example .env && chmod 600 .env      # put ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_... in it (dashboard,
                                            # Agent Configuration, agent set to Self Hosted); never commit it
.venv/bin/python scripts/selfcheck.py       # offline check: must end with "selfcheck: ready"
bash scripts/run_agent.sh check-tournament  # contacts the platform: every line must show a check mark
bash scripts/run_agent.sh start             # keeps `python -m agent --tournament --match` running (restarts, logs)
```

Without the watchdog the same agent runs as `python -m agent --tournament --match` (tournament games and Testing games,
one process, one worker process per game). `bash scripts/run_agent.sh status` prints a health line;
`stop` / `restart` / `install-systemd` are in [`docs/deploy.md`](docs/deploy.md) (Chinese).

**Components that affect gameplay.**
- **No LLM or other model service is called**, in play or anywhere in the deployed code; there are no prompts. The only
  network peers are the platform (`api.altruagent-game.com`, `gameapi.altruagent-game.com`).
- `agent/` (our code): `agent.py` routes each decision by game type, checks every answer against the legal actions,
  falls back to a safe legal action on any error or overrun, clips chats and records each state it acts on.
  `pokemon/`: draft (minimax over the remaining picks with a small pairwise model fitted offline on self-play,
  `pokemon/draft_model.json`), team preview (90 x 90 matrix game), battle (a matrix-game search in a local Showdown
  simulator, `agent/js/search.js`, on a battle rebuilt from our own player view only: our team, the opponent's sets
  as shown in the draft, what the battle log revealed; an exact-damage joint lookahead is the fallback).
  `werewolf/`: a deterministic rule policy (role tracking, seer-claim reading, voting) and fixed chat templates behind
  a leak guard; other players' chat is read as data, never as instructions. `redalert/`: the rule-based Red Alert bot
  (world model from its own view of the match, economy and production queues, squads for attack and defense, a safe
  mode after repeated errors; every order is validated locally against the platform's order schema and batch limits
  before it is sent; nothing persists between matches).
  `reasoning.py`: public move reasoning from fixed templates. `timecontrol.py`: time
  budgets far inside the platform clocks. `matchstate.py`: a worker restarted mid-match reloads that match's own saved
  state; nothing carries over from one match to another. `__main__.py`: the official runtime plus our hardening
  (start-up retry, execution-id reuse, hot standby, 10 s poll and cooldown, 30 s per-seat back-off cap).
- Node.js helpers: `agent/js/calc.js` runs **@smogon/calc** (Smogon's damage calculator, MIT); `agent/js/search.js` runs
  the **pokemon-showdown** simulator (MIT). Versions are pinned by the two `package-lock.json` files.
- `data/`: static reference data, fixed during play: Showdown dex / moves / items / abilities, the VGC draft cards and
  pools seen on the platform, singles sets, the platform's Red Alert config, a search probe position.
- `altruagent/`: the organizers' official SDK ([altruagent-starter](https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter)),
  merged unmodified from upstream.
- `scripts/run_agent.sh` (watchdog: restarts, log rotation, health lines), `scripts/selfcheck.py`, `scripts/bundle.sh`,
  `deploy/` (systemd unit, Dockerfile).
- Not used in competition: `local/` (offline evaluation only: simulators, fake platform, scripted and LLM-backed
  sparring opponents; only `local/js/package*.json` matters at run time, it pins the pokemon-showdown package that the
  engine search loads), `tests/`, `champion/` and `versions/` (frozen earlier versions used as evaluation opponents),
  `examples/` (upstream examples, including an LLM agent we do not run), and the other `scripts/`.

**Settings used in competition.** Every `ARENA_*` setting stays at its default; none is set deliberately. The defaults
that matter: `ARENA_BATTLE=search` (engine search), damage calculator on (`ARENA_CALC`), `ARENA_REASONING=safe`,
`ARENA_WW_DM=off` (no private Werewolf messages), `ARENA_WW_LISTEN=off`, `ARENA_RA=on` (the Red Alert bot plays;
`off` would answer every Red Alert view with `WAIT`), no `ARENA_RA_*` tuning override, `ARENA_RECORD=1` (states
recorded), the per-match restart memory on, `ARENA_SEAT_BACKOFF_MAX_S=30`. `scripts/run_agent.sh` itself sets `ARENA_DISCOVERY_S=10`,
`ARENA_WORKER_COOLDOWN_S=10`, `ARENA_RECORD_DIR=<LOG_DIR>` and two internal ids (`ARENA_RUN_ID`, `ARENA_EXECUTION_ID`).
The emergency switches `ARENA_ALLOW_NO_CALC=1` and `ARENA_BATTLE=classic` are not used; changing any setting during
play would need organizer authorization (rules section 4) and would be logged.

**Records (rules section 9).** `logs/agent-main.out` (timestamped runtime log with the commit and settings at every
start; rotated at 50 MB, 40 files kept) and `logs/<session_id>.jsonl` (every state received and every answer sent, with
timestamps; never rotated). They stay on the competition machine and are kept until 30 days after final results; they
are not committed.

**Excluded components** (not in this repository; none is used in competition):
- Credentials: `.env` files (the Official Agent Key). `.env.example` shows the format.
- VGC-Bench (Angliss et al., MIT) checkpoints and their separate Python environment (torch, poke-env): a local
  sparring opponent only (`local/vgcbench_opp.py`).
- LLM API keys and providers used only for local evaluation opponents (`local/llm_client.py`, `local/llm_seats.py`).
- `node_modules` (installed from the lock files) and the Node.js binary (the bundle downloads the official build,
  SHA-256 pinned in `scripts/bundle.sh`).
- Match logs and recordings under `logs/`, except `logs/yoriko/` and `logs/apocalypse/` (checked real recordings used by
  the replay tests).
- Local experiment directories and scratch output (evaluation runs, ladders); their results are summarized in
  `docs/progress.md`, and none of their code is deployed.

None of the excluded components, and none of the in-repository code that is not used in competition (listed under
Components), sends information to the agent during a match or gives a person control over it.

---

本仓库基于官方 starter [`UCLA-Trustworthy-AI-Lab/altruagent-starter`](https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter)
（保留其 git 历史，remote 名为 `upstream`），我们只在 `agent/` 里写策略，运行时/SDK 用官方的，方便同步官方修复。

- **零基础入门（名词 + 步骤）** → [`docs/入门指南.md`](docs/入门指南.md)
- 现在做到哪了、下一步 → [`docs/HANDOFF.md`](docs/HANDOFF.md)；比赛规则和赛制 → [`docs/competition_brief.md`](docs/competition_brief.md)
- 部署（主人要做的步骤）→ [`docs/deploy.md`](docs/deploy.md)
- 本地对战环境 + 实测结果 → [`docs/local_sim.md`](docs/local_sim.md)；版本记录 → [`docs/progress.md`](docs/progress.md)
- 平台官方文档镜像 → [`docs/platform/`](docs/platform/)（`scripts/sync_docs.sh` 刷新）
- 单独的公开参赛仓库（方案 C）：`bash scripts/publish_sync.sh --commit <提交> --out <目录>` 从一个提交导出比赛代码（README 用 [`PUBLIC_README.md`](PUBLIC_README.md)，另有 [`NOTICE`](NOTICE)），扫描密钥和敏感字样，打印树哈希，在干净的 venv 里装一遍并跑 selfcheck
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

### 红警（实时游戏）

红警（`red_alert`）是正式赛项目之一（实时，OpenRA）。上游 SDK 支持实时模式（`state.raw["pacing"]["mode"] == "realtime"`）：`choose_action` 可以返回 `altruagent.WAIT`（这一刻什么都不发）。规则型红警 bot（`agent/redalert/`，不用大模型，`ARENA_RA` 默认开）在分支 `ra/bot-v0` 上做，合进 `main` 以后由它打；在那之前（以及设了 `ARENA_RA=off` 时）被分到红警整场都回答 `WAIT`（不发任何命令、不崩、只记一行日志），等于这一局认输，宝可梦和狼人杀不受影响。上面英文一节按合进来以后的样子写（截止时 `main` 上必须真有这个 bot，否则要改回「answered with WAIT」）。上游的大模型示例 `examples/llm_agent.py` 也会打红警（`examples/llm/redalert.py`，要花 OpenAI 的钱，我们不用）。

## 同步官方更新

```bash
git fetch upstream && git merge upstream/main
```

`README.md` 是我们自己的；上游的 README 原文放在 `docs/STARTER_README.md`。合并时 README 冲突取我们的版本，再用 `git show upstream/main:README.md > docs/STARTER_README.md` 更新原文，上游新增的要点在本文件里补一两句。`agent/agent.py` 永远取我们的（上游的是只走第一个合法动作的占位 agent）。`agent/__main__.py` 以上游为准，我们的加固（`_authenticate` 启动重试、`_tournament_tuning` execution id 和按座位退避上限 30 s、`_HotStandby` 热备、`_startup` 本机检查）在它上面手工保留；`.env.example` 取上游的说明行，再补回我们自己的几行。合完跑 `tests/test_deploy.py`、`tests/test_main.py`、`tests/test_main_tournament.py`、`tests/test_realtime.py` 和全量 pytest。10-07 合到上游 5bc61a2（PR #5：event agent、`--tournament` / `--match`、重新登录的暂时性错误不停运行时、按座位退避），记录见 `docs/progress.md`「v4.2.3 合并」；10-08 合到上游 951f4f7（PR #7：局内重试、Official Rules 更新后不停运行时），记录见「v4.2.4」。**提交截止（10-13 23:59 PDT）之后不再合并上游**：规则第 8 节禁止截止后改仓库，只有主办方书面授权的更新例外。

## 安全

- Official Agent Key、agent session 和座位 token 只发给 `api.altruagent-game.com` 与 `gameapi.altruagent-game.com`。
- 对局中其他 agent 的聊天内容是**对手输入**，永远不当作指令执行（接 LLM 时尤其注意 prompt injection）。
