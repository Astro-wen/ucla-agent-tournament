# 部署与比赛日运行手册

比赛日（10/16）的模式（10-06 平台改版后，10-07 合上游 PR #5）：每个参赛者一个 **event agent**，设成 **Self-hosted**（代码跑在我们自己的机器上），用 dashboard 的 Agent Configuration 页面生成的 **Official Agent Key**（`eak_live_` 开头）。参赛者**自己一直开着** `python -m agent --tournament --match`（我们用 `scripts/run_agent.sh` 开着它）：平台把测试赛和正式赛的每一局都作为「分配」发给这把 key，进程自动接手、每局一个 worker。**进程必须一直在线**：正式赛每局有连接窗口（默认 4 分钟，红警 4.5 分钟），窗口内没连上就判负（no-show）；对局中掉线则被平台代打（宝可梦对战 300 s、狼人杀 120 s 后默认出招），基本等于输。

名词（一行一个）：
- **看门脚本**：`scripts/run_agent.sh`，负责崩溃重启、日志轮转、健康行、干净停止。
- **计算器**：`agent/js/calc.js`，用 Node 跑的精确伤害计算。**引擎搜索**：`agent/js/search.js`，在 Showdown 引擎副本上试打每一步（默认对战策略）。
- **worker**：每场对局（每个座位）一个独立子进程。
- **event agent / Self-hosted / Oracle-hosted**：每人一个参赛 agent。Self-hosted = 自己跑代码（我们）；Oracle-hosted = 交一份 Markdown 提示词，由平台在 Oracle 上代跑（我们不用）。agent 设成 Oracle-hosted 时 key 会被拒。
- **两种对局**：分配里的 `context` 写着 `testing`（Testing 页面建的练习赛，不计分、没有连接期限）或 `tournament`（正式赛）。`--match` 只打测试赛、`--tournament` 只打正式赛，两个一起写就都打；看门脚本固定两个都写。
- **已停用（10-06，平台回 410）**：`sk_agent_` key 和不带参数的 `python -m agent`（「发现模式」，`platform_agents_retired`）、测试赛认领码 `--claim seatclaim_…`（`claim_codes_retired`）。现在运行它们只打印一段说明；看门脚本遇到 `ARENA_MODE=discovery`、只有 sk key、`--claim` 都拒绝启动并写明原因。
- **分配（assignment）**：平台告诉我们「你在哪场比赛的哪个座位」。**座位租约（lease）**：平台记录「此刻哪个进程在控制这个座位」，每次续约管 30 s。**execution id**：运行时的随机身份号，租约记在它名下（同一台机器上同一个 `--name` 和 `LOG_DIR` 的看门脚本，重启多少次都用同一个）。
- **热备（hot standby）**：另一台机器上用同一把 key 跑的第二个进程；主进程在时它只等待，主进程停止续约后约 30–45 s 自动接管座位。

两条路都准备好了：
- **A. 自托管（正式赛按这条准备）**：自己的云主机上用 `scripts/run_agent.sh`（systemd 或 tmux）常驻。
- **B. 压缩包 / Docker**：`scripts/bundle.sh` 打一个不含密钥、**自带 Node** 的压缩包（可以直接在云主机上用它自托管；如果最后要交给主办方或放到 Replit 上跑，交的也是它），或用 `deploy/Dockerfile` 构建镜像。

脚本一律用 `bash scripts/...` 调用：补丁（`diff -ruN`）不带可执行位，这样写不依赖 `chmod +x`。

## 机器要求

- Linux x86-64（推荐美西云主机，平台在 AWS us-west-2），Python ≥ 3.11（实测 3.12，要有 venv 模块：Ubuntu/Debian 先 `apt install python3-venv`），bash ≥ 4.2
- **压缩包自带的 Node 只能在 glibc ≥ 2.28 的 x86-64 上跑**：Ubuntu ≥ 18.10、Debian ≥ 10、RHEL/Rocky/Alma ≥ 8、Amazon Linux 2023 可以；Amazon Linux 2（glibc 2.26）、CentOS 7、Alpine/musl、任何 arm64（如 AWS Graviton）不行，那种机器要自己装 Node ≥ 18 并用 `ARENA_NODE` 指过去。**选云主机时直接选 x86-64 的 Ubuntu 22.04/24.04**，最省事。
- **计算器必须能跑**：Node ≥ 18 加 `agent/js/node_modules`。没有计算器时 agent 只能用启发式，对有计算器的自己只赢约 12%（见「没有 Node 会怎样」）。所以 `selfcheck.py` 会判失败，`run_agent.sh` 会**拒绝启动**（退出码 2，日志和终端 / journal 里都有醒目的 ERROR 横幅），除非显式设 `ARENA_ALLOW_NO_CALC=1`（仅限紧急情况：启发式总比 agent 不在线、被随机代打强）。
- **引擎搜索的模拟器也必须能跑**（默认对战策略 `ARENA_BATTLE=search`，见 `docs/local_sim.md`「引擎搜索对战策略」）：它在 agent 自己的 Node 进程里跑 Showdown 引擎（`agent/js/search.js` + `pokemon-showdown`）。**`pokemon-showdown` 不在 `agent/js/package.json` 里**，`cd agent/js && npm ci` 不会装它：压缩包把裁剪过的一份（8 MB）放在 `agent/js/node_modules`；git checkout 要另外 `(cd local/js && npm ci)`（本地模拟器用的同一份）。缺了它 agent 不会出错，但**每一步都悄悄退回冠军 v3.2 的打法**，所以 `selfcheck.py` 判失败、`run_agent.sh` 拒绝启动（退出码 2，ERROR 横幅），除非显式设 `ARENA_BATTLE=classic`（有意用冠军打法）。
- **`ARENA_ALLOW_NO_CALC=1` 单独设就够**（09-30 修好，以前还得同时设 `ARENA_BATTLE=classic`）：node 本身跑不起来时，计算器和搜索一起失败，看门脚本打 WARNING 横幅后照样启动，并自动给 agent 设 `ARENA_BATTLE=classic`（健康行显示 `search=classic(ARENA_BATTLE)`）。node 能跑、只是模拟器缺了，这个开关不管用（仍拒绝启动），那种情况用 `ARENA_BATTLE=classic`。`ARENA_CALC=off` 现在只关计算器，不再顺带关掉引擎搜索。
- 压缩包自带固定版本的官方 Node（`node/bin/node`，v22.23.3），Docker 镜像里也是同一版本；自托管的 git checkout 用系统的 node（≥ 18）。
- 装依赖要能访问 PyPI（`requirements.lock` 里 4 个包需要平台相关的 wheel）。离线机器：在一台能联网、Python 版本相同的机器上 `pip download -r requirements.lock -d wheels --only-binary=:all: --platform manylinux2014_x86_64 --python-version 3.12`，出包时 `bash scripts/bundle.sh --wheels wheels`，目标机器上 `.venv/bin/pip install --no-index --find-links wheels -r requirements.lock`。
- **绝不要 `pip install .`**（非可编辑安装）：它不带 `data/`、选秀模型和 JS，agent 会带着「没有数据」的状态上场。永远在仓库 / 压缩包目录里运行。直接跑 `python -m agent` 时如果数据文件缺了，会打醒目的 DEGRADED 横幅。
- 稳定网络；不要用会睡眠 / 合盖的笔记本
- 内存：每场并发对局约 150 MB（worker）+ 50–120 MB（计算器）+ **230–320 MB（引擎搜索的 Node 进程，每场一个）**，主进程约 55 MB。冠军版 2 场并发时整棵进程树峰值实测 418–447 MB（130 / 125 分钟演练）；加上搜索进程（门槛对局中实测每个 232–320 MB），2 场并发约 **1.1 GB**，按 **1.2 GB** 预留（旧的 500 MB 只够冠军版）。官方赛可能同时分到宝可梦和狼人杀（狼人杀 worker 约 80 MB，不起 Node），同时几场由主办方决定（未知），**云主机建议 ≥ 8 GB 内存、≥ 4 核**。

## 比赛日手册（event agent：`python -m agent --tournament --match`）

**谁做什么**：登录 dashboard、生成 key、跑 `check-tournament`（会连真实平台）、报名比赛、建测试赛，只由主人本人做；其余步骤在本地都演练过（见「官方模式演练」）。

### 主人的一次性准备（按顺序）

1. **登录**：用 UCLA Google 账号登录 <https://platform.altruagent-game.com/tournament>，把报名做完（问卷、接受 Official Rules、agent 名字、公开的代码仓库）。报名没做完时 key 会被拒（403 `registration_incomplete`）。
2. **Agent Configuration 页面**（`/tournament/dashboard/agent-setup`）：选 **Self Hosted**（不要选 Oracle Hosted：那是交 Markdown 提示词让平台代跑，选了它我们的 key 会被拒），然后生成 Official Agent Key（`eak_live_` + 64 位十六进制）。泄露了就在同一页面重新生成（新 key 替换旧 key，旧 key 不能再用），再换进 `.env`、`systemctl --user restart arena-agent@main`。
3. **放进主机的 `.env`**（只在要跑比赛的那台机器上；不要贴到聊天、GitHub、截图里）：

```bash
echo 'ALTRUAGENT_OFFICIAL_AGENT_KEY=<粘贴 key>' >> .env && chmod 600 .env    # .env 里如果有 .env.example 的占位行，删掉它
.venv/bin/python scripts/selfcheck.py        # 本机检查（不连平台）：最后一行 selfcheck: ready，且有 "OK    Official Agent Key configured (#…)"
bash scripts/run_agent.sh check-tournament   # 连平台检查（会连真实平台）：数据 / 计算器 / 引擎搜索 + 平台连接，每一行都应是 ✓
bash scripts/run_agent.sh install-systemd && systemctl --user enable --now arena-agent@main && sudo loginctl enable-linger $USER
bash scripts/run_agent.sh status             # 健康行必须有 mode=tournament(primary) agent=up calc=ok(...)；日志第一行写着
                                             # "mode tournament (python -m agent --tournament --match: …)"
```

4. **测试赛**（随时可以做，推荐比赛前做几次）：dashboard 的 **Testing** 页面建一局，座位选 "Mine (self-hosted)"（或在 *Open matches* 里加入别人开的座位）。开着的看门脚本 10 s 内自己接上，不用复制任何东西（见下面「测试赛」）。
5. **报名正式赛**：**Tournaments** 页面对每个要参加的比赛（宝可梦、狼人杀；红警见 HANDOFF）点 **Register my agent**，开赛前都可以撤回。报名后什么都不用做：比赛开始后每一局自动出现在分配里，看门脚本接上。

**只有一种模式**（每次启动的日志和每条健康行都写着 `mode=tournament(primary|standby)`）：看门脚本固定跑 `python -m agent --tournament --match`（同一个进程打测试赛和正式赛）。只想打一种时在命令后面写 `-- --tournament` 或 `-- --match`（`start --name main -- --tournament`），平时不要这样做。key 不对（没设、还是 `.env.example` 的占位、格式不对、只有停用的 sk key）或写了 `ARENA_MODE=discovery`：拒绝启动（退出码 2），日志只写原因、不写 key。

Docker 同理：`--env-file` 指向放 eak key 的文件（权限 600）；一次性检查 `docker run --rm --env-file arena.keys arena-agent check-tournament`（见 `deploy/Dockerfile` 顶部）。

### 比赛日时间表

| 时间 | 做什么 | 看什么 |
|---|---|---|
| T−1 天 | `git pull` 到冻结的 tag；`python -m pytest -q`；`selfcheck.py`；`check-tournament` | 全绿；每行 ✓ |
| T−2 小时 | 确认只有计划中的机器在跑这把 key（主机 + 经主办方同意的热备）；`free -m`、`df -h` | 内存 ≥ 1.2 GB × 预计并发场数，磁盘 > 1 GB |
| T−1 小时 | `systemctl --user restart arena-agent@main`（或 `start`），看 `status` | `mode=tournament(primary) agent=up restarts=0 calc=ok(...)`，`search=idle(probe=…s)`（还没对局）或 `search=ok(used=…)` |
| 比赛中每小时 | `bash scripts/run_agent.sh status`；`grep -c 'Match assigned' logs/agent-main.out` | `restarts` 不涨；`workers` = 在打的座位数；日志没有 Traceback |
| 比赛后 | `systemctl --user stop arena-agent@main`；`python -m local.pool --harvest`；提交 `logs/yoriko` 之外的新录像按惯例处理 | — |

### 看门脚本和 agent 在上游运行时之上额外做了什么（都不改 `altruagent/`）

- **重新登录失败不再全军覆没（现在由上游 SDK 自己做）**：agent session 大约一小时过期，运行时要用 key 重新登录。以前上游 SDK 只要重新登录回一次非 200（限流 429、网关 502/503、平台签发会话失败的 401 `Failed to mint agent session …`）就结束运行时、杀掉所有在打的 worker，我们从 v4.1.2 起用 `_SteadyOfficial` 包住它（v4.2.1 补丁又加上签发会话失败和「新会话仍 401」）。上游 10-06 的 1372ccc（随 PR #5 合进来）把这件事做进了 SDK 的 supervisor：`is_fatal_auth_error` 只把「key 被拒（401 `invalid_official_agent_key`，签发失败除外）」和「报名没完成（403 `registration_incomplete`）」当致命，其余都当暂时性：在打的 worker 照常，日志一行 `Could not renew your agent session (…). Games already running keep playing; retrying automatically.`，暂停查分配 10 / 20 / 40 / 60 s（429 后 60 s），恢复后一行 `Assignment discovery recovered.`；签发会话失败的提示也改成了「平台的暂时问题，不是你的 key」。所以 10-07 合并时 `_SteadyOfficial` 的这一半删掉了（热备那一半保留，改名 `_HotStandby`，见下）。代价：暂停最长 60 s（我们以前是 30 s），这期间看不到新座位。正式赛的连接窗口是 4 分钟，但只对**短的**登录故障来得及：查分配的重试大约在故障开始后 10、30、70、130、190、250 s，所以在故障开始时出现的新座位，故障持续约 190 s 以上时要到 240 s 之后才被看到（缺席判负）；我们以前的 30 s 上限会在恢复后 30 s 内看到。10-07 复审实测：90 s 的登录故障，恢复后 42 s 才 `Assignment discovery recovered.`，故障中出现的正式赛座位 121 s 接上（窗口 240 s）。这个节奏是官方 `official-agent.md` 对 503 写明的做法（10、20、40 s，之后每 60 s），所以我们没改（要改就是在 `_tournament_tuning` 里把 `AUTH_RETRY_MAX_SECONDS` 设成 30，由主人决定）。
- **启动时平台暂时连不上不退出**：第一次认证遇到网络错误 / 429 / 5xx / 签发会话失败，按 5、10、20、30、30… s 退避一直重试（日志 `Could not connect with your Official Agent Key yet (…); retrying in Ns`）；key 被拒才退出（`Could not connect with your Official Agent Key: …`）。上游的启动是试一次就退出（看门脚本会重启它，但日志里会一直是失败）。
- **worker 崩溃后 10 s 就重开，连续失败再翻倍，但最多 30 s**（intel-3 + 上游 4cb083a + 10-07 复审）：上游默认冷却 60 s，会丢一个 55 s 的对战回合；`ARENA_WORKER_COOLDOWN_S=10`、`ARENA_DISCOVERY_S=10`（查分配间隔）由看门脚本设置，不经看门脚本直接跑 `python -m agent` 也默认 10 / 10。上游 PR #5 加了按座位退避：同一个座位的 worker 连续失败时，冷却从这 10 s 起每次翻倍，打完一局、遇到 `seat_busy` 或座位从分配里消失才清零（worker 先打了一阵再失败也不清零）。上游的上限是 600 s，10-07 复审发现这对比赛不安全：正式赛座位要在 4 分钟内接上，如果开头几个 worker 都在拿座位时登录失败（平台回 503 `agent_session_unavailable` 或 429，worker 以退出码 2 结束），第 6 个 worker 要到 360 s 才起（缺席判负）；一局里 worker 反复崩溃时，间隔会变成 160 s、320 s，超过狼人杀 120 s、宝可梦 300 s 的超时代打。所以 agent 把上限改成 30 s（`_tournament_tuning` 改 SDK 的 `SEAT_FAILURE_BACKOFF_MAX_SECONDS`，不改 `altruagent/`；`ARENA_SEAT_BACKOFF_MAX_S` 可调，一般不用动）：worker 在 0、20、50、90、130、170、210 s 起，连续失败 6 次仍在窗口内；一局被游戏服务器弄丢（`SESSION_NOT_FOUND`，直到平台关掉它）时，也最多每 30 s 起一个 worker、登录一次（平台允许每个地址每分钟 30 次登录）。实测（v4.2 及以前）：对局中途 `kill -9` 宝可梦 worker，13.5–16 s 后新 worker 接上，约 16 s 后该局下一步，0 次计时器代打（官方模式演练 dry-r2…r7）。
- **重启后直接续上自己的租约**（intel-3）：看门脚本每次 `run` 生成一个随机 execution id，只通过环境变量交给 agent（不写日志、不写状态文件，python 读到后立刻从环境里删掉，并核对父进程确实是这个看门脚本），同一个看门脚本重启 agent 时沿用它。所以 agent 崩溃 / OOM 被杀后，新进程拿到的还是自己的租约，不用再等 30 s 租约过期 + 35 s 重试（以前约 45–55 s 不能出手）。实测（官方模式演练 dry-r4 / dry-r7）：宝可梦对战中 `kill -9` 运行时本身，看门脚本 3.4–3.5 s 后起新运行时，7.4–8.5 s 后该局下一步，0 次 seat_busy、0 次接管、0 次计时器代打。**不要自己设 `ARENA_EXECUTION_ID`，更不要把它抄到另一台机器**：两个进程共用一个 id 会同时控制一个座位；从 .env 或别处来的值会被忽略。
- **看门脚本本身重启也沿用 execution id**（10-06 复审修复）：以前每个 run 循环都换新 id，所以 systemd 重启看门脚本（`systemctl --user restart`、看门脚本被 kill / OOM 后 `Restart=always` 拉起）、`run_agent.sh restart`、看门脚本崩溃后重新 `start` 时，新进程发现自己的座位还在旧 id 名下：`seat is held by another runtime` → 等租约过期（30 s）→ SDK 再等 35 s 重试。复审实测 63–65 s 不能出手、宝可梦被计时器代打 1 回合；本机重现 `restart`：65.8 s、1 次代打、2 次 seat_busy。现在 id 存在 `LOG_DIR/agent-NAME.execid`（0600，和本次开机的 boot id、key 哈希、NAME@LOG_DIR 写在一起；日志只写 `execution id: the last run loop's …` 或 `execution id: new …`，不写 id 本身），新的 run 循环只在**拿到本机这把 key 的锁**（本机没有别的进程在跑这把 key）、并且已经杀掉上一个循环留下的 agent 和 worker 之后才沿用；开机换了、key 换了、文件是从别的机器拷来的（boot id 对不上）都换新 id。实测（本地假平台）：对战中 `run_agent.sh restart` → 8.4 / 9.1 s 后该局下一步；systemd 单元里 `kill -9` 看门脚本 → 5.6 s 后新循环、13.1 / 14.8 s 后下一步；都是 0 次 seat_busy、0 次接管、0 次计时器代打。即便如此，**比赛进行中不要无故重启**：重启那几秒里狼人杀桌偶尔会有 1 次 `WRONG_PHASE`（换阶段的竞态，runner 不退出，修复前也有）。
- **热备**：同一把 key 的第二个进程**不会被拒绝**，而是当热备。同一台机器上第二个看门脚本会打一行 `NOTE: … starting as a HOT STANDBY`，健康行显示 `mode=tournament(standby)`。热备应该放在**另一台机器**（同一台机器上只多占 CPU），那台机器的 key 文件里加一行 `ARENA_ROLE=standby`（它自己的机器锁拿得到，看门脚本只能靠这一行知道自己是热备）。官方文档的规则是「一把 key 跑一个运行时」（第二个拿到 `seat_busy`、什么都不打）：我们的热备正是这样（主进程在时它不起 worker、不下一步），只在主进程停止续约后才接手。**先问主办方允不允许热备**（问题清单第 3 条），同意之前只跑一台。
- **热备怎么等、怎么接管**（09-30 三次演练后定下来）：平台对**新座位**是「谁先申请谁拿」，没有主次之分（dry-r4 里热备在主进程好好的时候抢到过一张新开的狼人杀桌）。所以热备（`role=standby`）这样做：
  1. 新座位先让给主进程 20 s（`ARENA_NEW_SEAT_DELAY_S`；主进程每 10 s 查一次分配、马上申请），日志 `standby: new seat …; leaving it to the primary for 20s …`；
  2. 之后每次查分配（10 s 一次）都用自己的 execution id 对这个座位请求一次 `grant`（申请座位）。主进程的租约还在，平台就回 `seat_busy`，热备什么都不做：**不起 worker、不新建 agent session**，日志只在第一次打 `standby: seat … is held by another runtime (the primary); taking it over once its lease lapses`；
  3. 主进程停止续约，租约最多 30 s 后过期，下一次 `grant` 就成功（日志 `standby: seat … is free (no other runtime holds its lease): playing it`），同一轮就起 worker 接着打。接管 ≈ 租约剩余时间（≤ 30 s）+ 最多 10 s。

  以前的做法（dry-r5 实测）是 20 s 后直接把座位交给 SDK：SDK 的 worker 撞到 `seat_busy` 就退出、35 s 后才重试，每次重试都起一个新 worker、用同一把 key 新认证一次（主进程也用这把 key，认证多了可能触发限流）；20 s 让座再叠加 35 s 重试，接管用了 68.8 s，那一回合被计时器代打了 1 次。已经交给 SDK 的座位不会再被藏起来（否则分配列表偶尔漏一次，SDK 就会把在打的 worker 当成「不再分配」停掉）。`ARENA_SEAT_BUSY_RETRY_S`（35 s）现在只影响主进程这一侧（例如主进程换了新 execution id、座位还被热备占着），一般不用动。
- **OOM 不再停掉整个服务**（deploy-3）：systemd 单元加了 `OOMPolicy=continue`；引擎搜索进程（`oom_score_adj` 800）和计算器进程（500）是内核优先杀的对象，它们被杀后由 worker 自己重启（期间退回经典打法 / 启发式），不会连累 worker 和主进程。
- **Node 子进程拿不到任何密钥**（robust-8）：计算器和搜索进程只继承 PATH、HOME、LANG、TZ、TMPDIR、NODE_OPTIONS、ARENA_SHOWDOWN 和 NODE_PATH。
- **被分到红警（`red_alert`）也不出事**（v4.2.1，10-06）：红警是实时游戏（没有回合，`pacing.mode == "realtime"`），上游 10-04 起 SDK 支持（`altruagent.WAIT`、整批命令被拒不再结束 worker）。我们没有红警策略：worker 整场回答 `WAIT`（这一刻什么都不发），runner 等下一个画面再问，对局结束后 worker 正常退出。日志只有一行 `real-time game 'red_alert': no policy of ours plays it; answering every view with WAIT …`，第一个画面录一次（以后写红警策略时有真实 payload 可看），退出时 `[match-summary] … game=red_alert … waits=<次数>`。同时在打的宝可梦 / 狼人杀不受影响，`selfcheck`、`replay_real.py`、健康行碰到红警录像也照常。代价：这一局等于认输（被拆家，或 10 分钟双方都没下过命令时按击杀 / 资产判）。以前（合上游之前）会对每个画面发一个不合法的 `{"type": "pass"}`：旧 runner 下 worker 崩溃、冷却 10 s 后再起、再崩，新 runner 下变成每秒约 5 个被拒的命令并逐个录像。**这条只在实时模式下成立**：官方文档写着运营方可以把红警服务器切到「5 秒窗口」模式（`pacing.mode == "windows"`，每个窗口发一批命令）。那种状态不算实时，runner 不接受 `WAIT`（会当成决策错误），我们的兜底又会发上面那个不合法的 `{"type": "pass"}`，于是又回到旧的样子：worker 以 `match failed` 退出、冷却 10 s 后重起、再退出，整场如此（10-06 复审用脚本构造的窗口模式状态复现过）。这一局本来就认输，代价只是日志里多些 `match failed` 和 worker 启动，同时在打的宝可梦 / 狼人杀不受影响；官方文档说目前是实时模式（窗口模式是运营方的备用开关），遇到了也不用处理。

### 上游缺陷（请主人报告给主办方；我们这边已经绕开）

1. **重新认证一次失败就杀掉所有对局**：`altruagent/official.py` 的 `OfficialAgentAuth.login` 对 `/tournament/agent/authenticate` 的任何非 200 都抛 `OfficialAgentError`（`AuthenticationError` 的子类），而 `supervisor.run_tournament_once` 只捕获 `PlatformError`。一次 429 / 502 / 503 就让 `run_tournament_forever` 退出并终止所有 worker。**上游已在 10-06 修复**（1372ccc，随 PR #5 合进上游 main 5bc61a2：`is_fatal_auth_error`，只有 key 被拒和报名没完成才退出，其余暂停 10–60 s 再查），10-07 合进本仓库，我们自己的绕法（`_SteadyOfficial`）已删掉；这条不用再报。当初准备的英文：
   > In the official tournament runtime, a single transient failure of `POST /tournament/agent/authenticate` during re-authentication (429 rate_limited, 502, 503) ends `run_tournament_forever` and its `finally` terminates every running match worker: `OfficialAgentAuth.login` raises `OfficialAgentError` (an `AuthenticationError`) for any non-200 answer, and `run_tournament_once` only catches `PlatformError`. Suggested fix: raise `PlatformError` for 429/5xx/network errors in `login` (or treat them like `PlatformError` in `run_tournament_once`), and keep only a rejected key fatal.
2. **每个 worker 各自认证一次**：一个运行时 = 1 + 座位数个 agent session；开赛时同时分到多场会集中打认证接口（限流阈值未知）。worker 第一次拿座位时认证失败 = 该座位 worker 退出，等冷却后重来。
3. **重启后空转**：`python -m agent --tournament` 不传 `run_tournament_forever` 的参数（冷却 60 s、查询 10 s），每个进程新生成 execution id 且不保存，重启后要等租约过期（30 s）+ `SEAT_BUSY_RETRY_SECONDS`（35 s）。建议暴露这几个参数，或允许运行时在重启后沿用 execution id。
4. **worker 先建 agent 再申请座位**：每次 `seat_busy` 重试都要起一个新 worker、完整加载一遍 agent（我们的约 1–2 s CPU、100 MB 内存）、再认证一次。我们的热备已经绕开（运行时自己先用 `grant` 检查座位，拿到了才交给 SDK 起 worker）；主进程换了 execution id 时仍会这样重试。
5. **`ALTRUAGENT_CONTROL_URL` 没有默认值**：只给 key 的 harness 会直接启动失败；我们的 `agent/__main__.py` 和 `check_connection.py` 已默认用 `https://api.altruagent-game.com`。
6. （观察，不算缺陷）狼人杀讨论阶段，runner 在我们 terminate 之后每来一条新聊天就再发一次 terminate（服务端幂等）；真实平台上如果每次都算一次调用，量不小。
7. **GameAPI 令牌过期时偶尔不重新登录**：MCP 调用碰到 401 时，mcp 库有时把它包成 `BrokenResourceError` 而不是 `HTTPStatusError`，`mcp_transport.call_tool` 认不出是 401，就不做「重新登录再试一次」，直接抛 `MCPToolError`，worker 退出（本地模拟平台上令牌 2 s 过期的测试约 1/7 次复现，09-30 修复前的 40309a0 上同样）。影响：该座位 worker 重启，约 10–15 s 不能出手（冷却已调到 10 s）。
8. （观察，和热备有关）同一把 key 的两个运行时没有主次：新座位给最先 `grant` 的那个，SDK 自己的接管要等租约过期（30 s）再加 `SEAT_BUSY_RETRY_SECONDS`（35 s）。我们让热备先把新座位让给主进程（`ARENA_NEW_SEAT_DELAY_S`），再每 10 s 用 `grant` 检查一次（接管 ≈ 租约过期 + ≤ 10 s）；可以顺便问主办方是否允许热备、有没有官方的交接方式。
9. **`GAME_NOT_STARTED` 不在 runner 的「重读再试」列表里**（10-06 复审，没改）：官方 `07-errors.md` 把它列为「重读状态后重试」，但 `altruagent/runner.py` 的 `_RACE_ERROR_CODES` 里没有它。服务器要是在开局门期间（等所有座位的 agent 连上）对我们的出手或聊天回了它，worker 就以 `match failed … GAME_NOT_STARTED` 退出（退出码 1），10 s 冷却后重新拿座位；运行时和别的对局不受影响。按文档写的开局门（开局前我们永远不是当前行动者，狼人杀从夜里开始），runner 根本不会出手，所以这种情况不会发生，我们没去改 SDK。可以直接贴的英文：
   > `altruagent/runner.py`'s `_RACE_ERROR_CODES` does not include `GAME_NOT_STARTED`, although 07-errors.md lists it under "Retry after re-reading". If the server ever answers a move or a chat with it (for example a gated state that still says `is_current_actor`, or a gated Werewolf state in phase `messaging`), the worker ends with "match failed" and the seat is re-granted after the cooldown. Suggested fix: treat it like the other race codes, with a short sleep before re-reading so a contradictory state cannot cause a tight `get_game_state` loop.

## 一次性准备（A. 自托管，git checkout）

```bash
git clone https://github.com/Astro-wen/ucla-agent-tournament.git && cd ucla-agent-tournament
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock      # 锁定版本的 29 个运行依赖；开发机再 pip install -e ".[dev]"（-e 可编辑安装，不要去掉 -e）
(cd agent/js && npm ci --omit=dev)              # 计算器的 node_modules，按 package-lock.json 精确安装
(cd local/js && npm ci)                         # 引擎搜索用的 pokemon-showdown（按 local/js/package-lock.json，约 200 MB）
cp .env.example .env && chmod 600 .env          # 填 ALTRUAGENT_OFFICIAL_AGENT_KEY（正式赛和测试赛都用它；sk key 10-06 起停用）
.venv/bin/python scripts/selfcheck.py           # 必须以 "selfcheck: ready" 结尾，并有 "OK    damage calculator answers: node v…"
                                                # 和 "OK    battle policy: engine search (…), simulator loads in …"
bash scripts/run_agent.sh check-tournament      # 会连真实平台（主人执行），每行都应是 ✓
```

（云主机上也可以不 clone，直接用 B 的压缩包自托管：它自带 node、计算器和裁剪过的模拟器，步骤见 B。）

`selfcheck.py` 不连平台、不需要密钥：检查 Python 和锁定的包版本、数据文件和选秀模型、计算器能否在一个新的 node 进程里真的算出一次伤害（**算不出来就 FAIL，退出码 1**；`--allow-no-calc` 或 `ARENA_ALLOW_NO_CALC=1` 才降为警告）、引擎搜索能否在一个新的 node 进程里加载模拟器（`ARENA_BATTLE=search` 时**加载不了就 FAIL**；`ARENA_BATTLE=classic` 时只提示；设了 `ARENA_ALLOW_NO_CALC=1` 且计算器也跑不起来时降为警告），用仓库里录下的真实对局状态分别以「有计算器 / 无计算器」跑决策并按官方 runner 的规则校验每个答案，最后报告配了哪把 key、看门脚本会用哪种模式（只打印哈希前 10 位，不打印 key；eak key 格式不对判 FAIL）。

## 运行（A. 自托管）

三种方式任选其一，底下都是同一个看门脚本，跑的都是 `python -m agent --tournament --match`。本机上按 key 的哈希加锁（`/tmp/arena-agent-key-<哈希>.lock`，可用 `ARENA_LOCK_DIR` 改目录），看门脚本和正在运行的 agent 进程都持有这把锁：另一个 checkout、另一个解压出来的包或另一个 `LOG_DIR` 再用同一把 key 启动时，锁被占，它不拒绝，只打 NOTE 并以热备（standby）身份启动（平台的座位租约保证每个座位只有一个进程在下）。同一个 `--name` + `LOG_DIR` 只能有一个看门脚本（第二个退出码 3）。

**1）systemd 用户服务（推荐：开机自启；看门脚本本身挂了也会被拉起）**

```bash
bash scripts/run_agent.sh install-systemd       # 写 ~/.config/systemd/user/arena-agent@.service（填入仓库路径和 node 所在目录）
systemctl --user enable --now arena-agent@main  # main 用 .env；arena-agent@apocalypse 用 .env.apocalypse
sudo loginctl enable-linger $USER               # 不登录也运行，重启机器后自动起来
bash scripts/run_agent.sh status                # 状态 + 健康行（退出码：看门脚本在跑 0，没在跑 3）
systemctl --user stop arena-agent@main          # 停止（看门脚本是某个 systemd 服务的主进程时，run_agent.sh stop / restart 会拒绝并提示这条命令；
                                                # 从 su、sudo -u、cron 这类没有 XDG_RUNTIME_DIR 的 shell 里查不到 systemd 时也拒绝，不会变成「停了又被拉起」）
```

单元文件里有 `OOMPolicy=continue`：一个进程被 OOM 杀掉只影响它自己，不会让 systemd 停掉整个服务（以前默认 `OOMPolicy=stop`，实测 230 MB 上限下一次 OOM 就整服务重启，8 分钟重启 39 次）。**改了单元文件要重新 `install-systemd` 再 `systemctl --user daemon-reload`**。

**2）tmux**

```bash
bash scripts/run_agent.sh tmux   # 在 tmux 会话 arena-main 里运行；tmux attach -t arena-main 看实时日志，Ctrl-b d 退出
bash scripts/run_agent.sh stop   # 停止
```

**3）后台进程**：`bash scripts/run_agent.sh start` / `stop` / `restart` / `status` / `logs`（`logs` = tail -F 日志）。（以前的 `start --tournament` 仍然认，效果一样。）

`--name X` 用 `.env.X` 当 key 文件（以前给陪练 Apocalypse_Red 的 sk key 用；10-06 起 sk key 停用，现在每人只有一个 event agent、一把 key，一般用不上）。注意官方 SDK 在 agent 进程里还会读仓库根目录的 `.env`（不覆盖已有变量），所以看门脚本启动前也把根目录 `.env` 里的计算器开关（`ARENA_CALC` / `ARENA_NODE` / `ARENA_ALLOW_NO_CALC` / `ARENA_BATTLE`）算进去检查。

### 看门脚本做了什么

- **引擎搜索的模拟器也是启动条件**（`ARENA_BATTLE=search`，默认）：计算器通过后，再用 `agent/pokemon/engine.py` 的 `probe()` 起一个新的搜索进程加载模拟器（约 1 秒）。加载不了就不启动（退出码 2，横幅 "ARENA_BATTLE=search BUT THE ENGINE SEARCH CANNOT RUN … NOT STARTING"，systemd 下每 5 s 重试）；设 `ARENA_BATTLE=classic` 才会以冠军打法启动。仓库根目录 `.env` 里的 `ARENA_BATTLE` 也算（agent 进程也会读到它）。运行中搜索进程坏了不会停 agent：那一步退回冠军的走法，每场最多重启搜索进程 6 次。
- **计算器是启动条件**：启动前用 agent 自己的代码（`agent/pokemon/calc.py` 的 `probe()`）起一个新的 node 进程真算一次伤害。算不出来（没有 node、node 太旧或跑不起来、缺 node_modules、`ARENA_CALC=off`）就不启动：退出码 2，横幅写进日志，也写到终端 / journal；systemd 下每 5 s 重试一次，node 修好后自动起来。`ARENA_ALLOW_NO_CALC=1` 时照样启动，但每次启动都打 WARNING 横幅、健康行显示 `calc=MISSING` 之类；如果搜索也因此跑不起来（node 本身不行），自动改用 classic 打法，横幅 "THE ENGINE SEARCH CANNOT RUN EITHER"。运行中 node 坏了不会停 agent（该局退回启发式，每场最多自动重启计算器 6 次），健康行会显示。
- **key 检查**：只要 `ALTRUAGENT_OFFICIAL_AGENT_KEY`（格式 `eak_live_` + 64 位十六进制）；没设、还是 `.env.example` 的占位 `eak_live_your_key_here`、格式不对、只有停用的 `ALTRUAGENT_API_KEY`（sk key）、或写了 `ARENA_MODE=discovery`，都退出码 2，并写清原因（不写 key）。
- **LOG_DIR 写不进去是错误，不是「已在运行」**（deploy-6）：`LOG_DIR`（默认 `logs/`）不可写（只读挂载、别的用户的目录）时直接报 `ERROR: LOG_DIR … is not writable`、退出码 2（以前误报「已在运行」并以 3 退出，systemd 不会重试）。agent 的对局录像默认也写到 `LOG_DIR`（`ARENA_RECORD_DIR` 可改），写失败时每场打一条 WARNING，不再悄悄丢掉。
- **崩溃重启**：`python -m agent` 退出就重启，间隔 3 s；1 分钟内连续退出则翻倍退避（3→6→12…最多 60 s），避免密钥错误时狂刷登录。重启前把上一轮残留的 worker 和 Node 进程整组杀掉，不会有两个进程抢同一个座位。退避期间的 sleep 不持有任何锁，退避中 `restart` 也能马上起来。
- **单场恢复更快**：设置 `ARENA_DISCOVERY_S=10`（查分配间隔）、`ARENA_WORKER_COOLDOWN_S=10`（座位的 worker 失败后第一次重试；上游再按连续失败次数翻倍，agent 把上限从上游的 600 s 改成 30 s，`ARENA_SEAT_BACKOFF_MAX_S`）。上游默认冷却 60 s，会丢掉一个 55 s 的对战回合；现在约 20 s。
- **日志**：`logs/agent-NAME.out`，每行带时间戳，满 20 MB 轮转、保留 10 个（`LOG_MAX_MB`、`LOG_KEEP`）。`logs/` 整个在 .gitignore 里，**不要提交 `logs/*.out`**。
- **健康行**：每 5 分钟（`HEALTH_EVERY`）写一行 `[health]`，也可随时 `bash scripts/run_agent.sh health`：

  ```
  [health] name=main mode=tournament(primary) agent=up pid=… run_up=2h05m agent_up=1h47m restarts=1 workers=2 node=4 calc_procs=2 rss=1010MB fds=87
           last_output=3s records=41 last_record=12s disk_free=38.2G logs=12M calc=ok(v18.19.1) search=ok(used=38/40) key=#762c809cde
  ```

  `mode` = `tournament(primary)` / `tournament(standby)`（只有这一种模式，括号里是主进程还是热备），比赛日主机必须是 `tournament(primary)`；`workers` = 正在打的对局数；`calc_procs` 应 ≤ `workers`；`node` = 计算器 + 搜索进程（每场各一个）；`restarts` 比赛中应不变；`calc` 必须是 `ok(...)`，出现 `MISSING` / `NO-NODE_MODULES` / `OFF(ARENA_CALC)` 立刻处理；`search`（v4.2 起看真实效果，`agent/health.py`，细节见 `docs/restart_load.md` 第 4 节）：`ok(used=38/40)` = 最近 30 分钟的对战回合 ≥ 80% 用上了搜索的答案；`DEGRADED(used=3/20,no_answer=10,…)` = 低于 80%，后面是机器原因（CPU 被抢、内存不够、槽位满），要查；`idle(probe=1.9s)` = 最近 30 分钟没有对战，刚真跑过一次搜索（最多 15 分钟一次），`SLOW(probe=…)` 是超过 2.5 秒，`FAILED(…)` 是没答出来；`NO-SIMULATOR` / `MISSING` = 模拟器不见了，每步都在用冠军打法；`classic(ARENA_BATTLE)` = 有意关掉了搜索，或 `ARENA_ALLOW_NO_CALC` 的紧急退回。`key=#…` 是 Official Agent Key 哈希的前 10 位（不是密钥），用来核对两台机器跑的是哪把 key；看门脚本在跑时，`status` / `health` 显示的是它状态文件里记的角色和 key，不是问话的 shell 自己算的。
- **干净停止**：`stop`（或 systemd stop、tmux 里 Ctrl-C）向 agent 整个进程组发 SIGTERM，20 s 内不退就 SIGKILL；不认输、不再调用平台。实测 0.6 s 停完。看门脚本自己被 SIGKILL / OOM 杀掉时，它的健康循环 5 s 内自行退出，日志写入进程在最后一个写端关闭后退出，下一次 `start` / `stop` 会清理残留的 agent 和这两个辅助进程（测试覆盖：`kill -9` 看门脚本后 `stop`，本测试的进程一个不剩）。
- **pid 文件不盲信**：`start` / `stop` 只认命令行是 `run_agent.sh run` 且环境里 `ARENA_RUN_ID=<NAME>@<LOG_DIR>` 对得上的进程；pid 文件过期、pid 被别的进程复用时，既不会误报「已在运行」，也不会去杀别人的进程。
- 启动自检写进日志：模式和它跑的命令、密钥是否设置（不打印密钥）、`.env` 权限、计算器（真算一次）、磁盘空间。

不经看门脚本直接跑 `python -m agent --tournament --match`（例如在终端里按上游 README 跑）：没设 `ALTRUAGENT_CONTROL_URL` 时默认用 `https://api.altruagent-game.com`（上游直接报错退出）；启动时检查数据文件、计算器和引擎搜索，**有问题就在 stdout 和 stderr 各打一个 `WARNING: THIS MACHINE PLAYS DEGRADED` 横幅**（不拒绝启动：能打总比不在线强），一切正常时打一行 `Local checks: …`；`--check-tournament` 把这几项作为最前面的 ✓/✗ 行。不带 `--tournament` / `--match` / `--check-tournament`（以前的发现模式）或带 `--claim` 只打印上游的停用说明，不做任何检查。

可调环境变量：`PYTHON`、`LOG_DIR`、`LOG_MAX_MB`、`LOG_KEEP`、`HEALTH_EVERY`、`RESTART_DELAY`、`RESTART_MAX_DELAY`、`STOP_TIMEOUT`、`ARENA_LOCK_DIR`（密钥锁目录，默认 `/tmp`）、`ARENA_ROLE=standby`（只写在热备机器的 key 文件里）、`ARENA_DISCOVERY_S`、`ARENA_WORKER_COOLDOWN_S`、`ARENA_SEAT_BACKOFF_MAX_S`（同一座位连续失败时冷却翻倍的上限，默认 30 s；上游 600 s）、`ARENA_SEAT_BUSY_RETRY_S`（`seat_busy` 后的重试间隔，默认 35 s）、`ARENA_RECORD_DIR`（录像目录，默认 `LOG_DIR`）、`ARENA_NODE`（node 可执行文件路径，systemd / nvm 下 PATH 里没有 node 时用；路径失效时只记一条警告、改用包里的 `node/bin/node` 或 PATH 里的 node，不会因此关掉计算器）、`ARENA_CALC=off`（**只用于诊断**：强制关计算器，看门脚本和 selfcheck 会拒绝，除非同时设 `ARENA_ALLOW_NO_CALC=1`；不影响引擎搜索）、`ARENA_ALLOW_NO_CALC=1`（计算器跑不起来也照样启动；仅紧急情况，用完删掉）、`ARENA_BATTLE`（对战策略：不设或 `search` = 引擎搜索，默认；`classic` = 冠军 v3.2 的打法，回退开关）。v4.2 加的（含义和默认值见 `docs/restart_load.md`，一般不用设）：`ARENA_STATE` / `ARENA_STATE_DIR`（worker 重启后接上记忆的状态目录，默认 `LOG_DIR/state`）、`ARENA_SEARCH_MAX_PROCS` / `ARENA_SEARCH_MIN_FREE_MB`（整台机器的搜索进程上限和所需可用内存）、`ARENA_SEARCH_GRACE_S`、`ARENA_SUMMARY=0`（关掉每局的 `[match-summary]` 行）、`ARENA_HEALTH_PROBE=0`（健康行只看对局计数，从不另起探测搜索；Docker 的 HEALTHCHECK 用它）。`ARENA_EXECUTION_ID` 只由看门脚本设置，不要手动设。

## B. 压缩包 / Docker

```bash
bash scripts/bundle.sh     # -> dist/arena-agent-<版本>.tar.gz（46 MB，解开约 130 MB，其中 node 119 MB）+ .sha256
```

包里有：`agent/`（含选秀模型 `draft_model.json`）、`altruagent/`、`data/`、计算器的 `node_modules`（纯 JS，随包带上）、引擎搜索用的裁剪版 `pokemon-showdown`（MIT，8 MB，放在 `agent/js/node_modules`；出包前用包里的 node 真加载一次）、**官方 Node v22.23.3 Linux x64（`node/bin/node` + LICENSE + VERSION.txt）**、`requirements.lock`、`scripts/run_agent.sh`、`selfcheck.py`、`check_connection.py`、`replay_real.py`、`deploy/`（Dockerfile、systemd 单元）、本文件、**一整场真实对局的两个座位**（两个录像目录里都有、带选秀的最大那场，给 selfcheck 和 replay_real 重放：60 个决策，其中 46 个对战决策；以前只带两个 12 行的小录像）、包自己的 `.dockerignore`、`SHA256SUMS`、`BUNDLE_README.txt`（写了平台要求、官方模式三步、离线安装、不要 `pip install .`）。

- Node 从 `https://nodejs.org/dist/v22.23.3/` 下载（缓存在 `~/.cache/arena-agent/`），SHA-256 必须等于 `bundle.sh` 里写死的值（取自该版本的 SHASUMS256.txt），不符就不出包；出包前还用包里的 node 和包里的 node_modules 真算一次伤害。对方机器上默认用包里的 node（`ARENA_NODE` 可指定别的）。
- **没有任何密钥**：只按白名单拷文件，`.env*` 一律不进包，打包后再扫描一遍（`sk_agent_…`、`seatclaim_…`、**`eak_live_…`（Official Agent Key）**、`sk-…`、私钥、`ALTRUAGENT_API_KEY=` / `ALTRUAGENT_OFFICIAL_AGENT_KEY=` 后跟真实值），有就拒绝出包。
- `--node-tarball FILE`：离线出包，用已下载的 `node-v22.23.3-linux-x64.tar.xz`（或 `.tar.gz`），同样校验 SHA-256
- `--node-dist DIR`：改带另一个解压好的官方 Node ≥ 18（`DIR/bin/node`）
- `--no-node`：不带 node（对方必须自己装 Node ≥ 18，否则 agent 拒绝启动）
- `--no-node-modules`：不带 node_modules，对方需 `cd agent/js && npm ci --omit=dev`
- `--no-sample-logs`：不带对局录像
- `--wheels DIR`：带上一个事先下载好的 wheel 目录（`pip download …`，见「机器要求」），目标机器离线安装

对方机器上（09-30 实测：全新解压 + 离线复制的 venv，干净环境、PATH 里没有 node：selfcheck ready，103 s；`replay_real.py` 60 个决策 0 失败，搜索用在 46/46 个对战决策上）：

```bash
tar xzf arena-agent-*.tar.gz && cd arena-agent-*
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
.venv/bin/python scripts/selfcheck.py           # 必须 "selfcheck: ready"，且有 "OK    damage calculator answers: node v22.23.3 (…/node/bin/node)"
                                                # 和 "OK    battle policy: engine search (…), simulator loads in …"
.venv/bin/python scripts/replay_real.py         # 包里那场真实对局重放：0 failures
# 官方赛：ALTRUAGENT_OFFICIAL_AGENT_KEY 写进 .env（chmod 600），然后
bash scripts/run_agent.sh check-tournament      # 会连平台，每行 ✓
bash scripts/run_agent.sh start
```

一定要通过 `run_agent.sh`（或 Docker / systemd）启动：直接 `python -m agent` 没有重启和日志轮转（但会检查计算器和搜索，有问题就大声警告）。

Docker（Python 3.12 + 官方 Node 22.23.3，与压缩包同一版本；密钥只从环境变量进，不进镜像；构建时跑 `selfcheck`，计算器或引擎搜索的模拟器不可用就构建失败；容器启动时 `run_agent.sh` 再检查一次）。**要在解开的压缩包目录里构建**：模拟器在包的 `agent/js/node_modules` 里；仓库根目录的构建上下文没有它（根目录的 `.dockerignore` 排除了 `node_modules` 和 `local/`），在根目录构建会停在 selfcheck 这一步。包目录里有自己的 `.dockerignore`（排除 `.env*`、`.venv`、`logs`、`node/`、`wheels`），构建上下文不会带上密钥和 119 MB 的 node：

```bash
bash scripts/bundle.sh && tar xzf dist/arena-agent-*.tar.gz -C /tmp && cd /tmp/arena-agent-*
docker build -f deploy/Dockerfile -t arena-agent .
docker run --rm --env-file arena.keys arena-agent check-tournament     # arena.keys: ALTRUAGENT_OFFICIAL_AGENT_KEY=…（权限 600）；会连平台
docker run -d --name arena-main --restart unless-stopped --env-file arena.keys \
    --log-opt max-size=20m --log-opt max-file=3 -v arena-logs:/data arena-agent
docker logs -f arena-main            # 与 /data/logs/agent-main.out 内容相同（--log-opt 限制了大小）
docker ps                            # HEALTHCHECK：agent 进程在时显示 (healthy)，退避等待中显示 unhealthy
                                     # （它带 ARENA_HEALTH_PROBE=0：20 s 超时内只读对局计数，不另起探测搜索）
```

- 用 `--env-file`，不要 `-e KEY=值`（会留在 shell 历史和 `docker inspect` 里）。
- 镜像的 ENTRYPOINT 是 `tini -- bash run_agent.sh`，默认 CMD `run --name main`，所以 `docker run … arena-agent check-tournament` / `health` 都能直接用。
- 不要加 `--init`：ENTRYPOINT 已经是 tini，再加会套两层 tini 并打印警告。
- **镜像从来没有真正构建过**：本机 WSL 没有 docker（09-30 仍然没有）。它的每一步（锁定依赖、npm ci、selfcheck、run_agent.sh）都已在「全新解压 + 全新 venv」里验证过，node 与压缩包是同一个官方版本。第一次在有 docker 的机器上构建时留意 `node --version` 这一步必须是 v22.23.3，以及 `docker ps` 里出现 `(healthy)`。

依赖变了要重新生成锁文件：新建一个 venv，`pip install -c <工作 venv 的 pip freeze> httpx python-dotenv "mcp>=1.12,<2"`，再 `pip freeze --exclude-editable`（去掉 pip）写回 `requirements.lock`。升级 Node：改 `scripts/bundle.sh` 的 `NODE_VERSION` 和两个 SHA-256（从新版本的 SHASUMS256.txt 抄），同时改 `deploy/Dockerfile` 的 `node:<版本>-bookworm-slim` 和版本检查；`tests/test_deploy.py` 会核对两处一致。

## 没有 Node 会怎样（实测）

- 不会崩：真实对局重放 1341 个决策 0 失败（`--no-calc` 和 `ARENA_CALC=off` 都测了）；下面所有对局 0 非法动作；对局中途杀掉计算器进程，worker 会自动重启计算器（每场最多 6 次），期间用启发式出招。
- **但实力大跌**：无计算器的 agent 对有计算器的冠军，种子 7 为 45/400（11.2%），种子 11 为 51/400（12.8%），合计 12.0%（镜像对局是 49.9%）。对官方示例 bot 和旧版本的面板结果见下表。
- 所以现在是硬性要求：`selfcheck` 不通过、`run_agent.sh` 不启动，除非 `ARENA_ALLOW_NO_CALC=1`；健康行必须是 `calc=ok(...)`；systemd 下 PATH 里找不到 node 时设 `ARENA_NODE`（`install-systemd` 会把安装时 node 所在目录写进 PATH）。

| 对手（`local.panel` 种子 43，每个 100 局） | 有计算器 | 无计算器 |
|---|---|---|
| official_baseline / random | 100% / 100% | 100% / 100% |
| basic（官方 basic_agent） | 100% | 89% |
| smoke（官方 smoke_agent） | 100% | 85% |
| llm_timeout（官方 llm_agent 超时时的替身） | 100% | 89% |
| v1 | 94% | 55% |
| v3（与冠军对局水平相同） | 50% | 14% |
| 平均 | 92.0% | 76.0% |

无计算器的 700 局：0 非法动作、0 次计时器代打、0 次判负。决策反而更快（真实对局重放平均 14 ms，有计算器时 71 ms）。

## 无人值守演练（本地模拟平台，绝不连真实平台）

`local/fake_platform.py` 在 127.0.0.1 上模拟平台的控制面和 MCP 游戏接口（与官方 SDK 协议一致、令牌定时过期、平台计时 90 / 55 / 420+90 s）。对局由本地 Showdown 引擎跑，对手是本地 bot，对局状态保存在「平台」一侧，所以 agent 被杀后重启能接着打。**payload 默认按真实平台录像的形状**（09-30 起，deploy-7）：`team` 列出全部 6 只并带道具 / 特性 / 招式 / revealed / is_terastallized，`protocol_log` 总是有、最后一行是 `|inactive|Time left: …` 计时行，`fields` / `weather` 是 dict，observation 带 battle_tag、can_tera、target_legend 等键；每场第 25 个对战请求（`--blind-every`）是一次「盲状态」（只有 `No pending decision is currently available.` 的 observation 加上双打模板，和 4b85cc32 第 8 回合一样）。`--plain` 回到以前的本地形状。每个决策的 (阶段, 延迟) 记在 `platform.json` 的 `lat` 里，报告能单独算对战决策的 p99。`scripts/rehearsal.py` 起一整套：模拟平台（10-07 起是官方控制面 `--official`，测试赛 / 正式赛两种分配交替）→ 看门脚本（`--supervise start` 或 `systemd`）→ `python -m agent --tournament --match` → 每场一个 worker → Node 计算器，定时采样内存 / 文件描述符 / 进程，并按时间点注入故障：

```bash
python scripts/rehearsal.py --minutes 120 --supervise systemd      # 结果在 logs/rehearsal/<时间>/report.json
python scripts/rehearsal.py --minutes 20 --agent-env ARENA_CALC=off --agent-env ARENA_ALLOW_NO_CALC=1   # 无计算器（搜索照常）
```

故障（按时长比例）：15% `kill -9` 主进程、30% `kill -9` 一个 worker、45% `kill -9` 一个计算器、55% 分配接口 503 90 s（10-07 以前是发现接口）、70% 全部接口 503 45 s、85% `kill -9` 看门脚本本身（systemd 自动拉起；start 模式下 10 s 后像值班的人一样再 `start`）。2 场并发，对手 random / baseline 混合。下表各组都是发现模式时代（10-06 以前）跑的，换成官方控制面后还没重跑过长时间的这一种。

| 演练 | 时长 | 看门方式 | 对局 / 决策 | 非法 / 计时器代打 | 进程树峰值 | 计算器 / worker 泄漏采样 | 停止后残留 | 意外重启 |
|---|---|---|---|---|---|---|---|---|
| reh-s1 | 7 min | start | 54 / 710 | 0 / 0 | 298 MB | 0 / 0 | 0 | 0（1 次 = 注入的 kill_main） |
| reh-sd1 | 16 min | systemd | 101 / 1342 | 0 / 0 | 332 MB | 0 / 0 | 0 | 0（1 次 = 注入） |
| reh-long2 | 130 min | systemd | 510 / 6819 | 0 / 0 | 418 MB | 0 / 0 | 0 | 0（1 次 = 注入，14:10:43） |
| reh-nonode（无计算器） | 20 min | start | 107 / 1495 | 0 / 0 | 197 MB | 0 / 29 | 0 * | 0（1 次 = 注入） |
| reh-final-start2（本版看门脚本） | 12 min | start | 44 / 624 | 0 / 0 | 405 MB | 0 / 0 | 0 | 0（1 次 = 注入） |
| reh-final-sd（本版看门脚本） | 13 min | systemd | 40 / 559 | 0 / 0 | 398 MB | 0 / 0 | 0 | 0（1 次 = 注入） |

- 以上各组是 09-28 之前在旧形状 payload（4 只队伍、没有计时行）上跑的；v4.1 修掉的那类问题（6 只队伍、计时行）只有新形状才测得出来。
- 恢复时间（reh-long2）：kill 主进程后新主进程 24 s 内接上；kill worker 后该局 16 s 得到新 worker；kill 计算器无可见延迟；发现接口 503 90 s 期间在打的对局不受影响；全部接口 503 45 s 后 50 s 恢复；kill 看门脚本后 systemd 7 s 拉起。本版看门脚本在 start 模式下 `kill -9` 看门脚本后，旧 agent 照常在打（它仍持有密钥锁，别的目录起不来同一个密钥）；10 s 后再 `start`，新的看门脚本先清掉旧 agent 和旧辅助进程再接手，在打的对局由新 agent 的 worker 接着打。所有演练 0 次计时器代打、0 非法动作、0 次判负。
- 日志里没有 Traceback；每次全接口 503 期间有 2 条 worker 的 `unexpected error: AuthenticationError('rehearsal outage')`（worker 退出，SDK supervisor 冷却后重开，对局无损）。
- 作废的一次：reh-long（125 min）中途模拟平台的调度线程挂了（模拟平台自身的 bug，已修），只完成 360 场，且有 45 个 worker 泄漏采样，不作为证据。
- \* reh-nonode 用的是旧版看门脚本：85% 处注入的 `kill -9` 看门脚本留下了它的健康循环和日志写入进程，演练结束后又跑了 4 个多小时（每分钟写一行 `agent=down`，已手动清掉）；旧版 `leftovers_after_stop` 只数 node / python 进程，没发现。本版已修（见「干净停止」），演练脚本现在把 bash 辅助进程也算进残留，并按日志统计整场的重启次数（`agent_restarts`，旧的 `restarts_last_loop` 只看最后一个看门进程）。

### 官方模式演练（`scripts/rehearsal_official.py`，从解开的压缩包跑）

模拟平台加 `--official`（`local/fake_official.py`）：`/tournament/agent/authenticate`、`/tournament/agent/assignments`、座位 `grant` 和 `lease/renew`，和 `altruagent/official.py` 的协议一致，座位租约 30 s、`seat_busy`、`lease_not_held`、新 execution id 拿到座位时收回旧进程的 GameAPI 令牌；`--werewolf N` 同时开 7 人狼人杀桌（我们一个座位走 MCP，另外 6 个是本地 bot，`--ww-think` 让 bot 像大模型一样每步想几秒）。平台还统计每一步是不是「持有租约的那个进程」下的（`moves_by_non_holder` 必须是 0）。key 是本地假 key（`eak_live_` + `fa4e`×16，只有这个模拟平台认）。10-07 起分配带 `context`（`--contexts tournament,testing`，新座位轮流）、正式赛的 `tournament_name` / `round_label` / `opponents` 和 `connect_deadline_at`：正式赛座位在连接窗口（`--connect-window`，默认 240 s，红警 270 s）内没有被 grant 就算 no-show（从分配里消失、`no_shows` +1、宝可梦这局判我们负）；测试赛没有期限，和真实平台一样。

```bash
bash scripts/bundle.sh && mkdir -p /tmp/x && tar xzf dist/arena-agent-*.tar.gz -C /tmp/x && (cd /tmp/x/arena-agent-* && python3 -m venv .venv && .venv/bin/pip install -r requirements.lock)
python scripts/rehearsal_official.py --bundle /tmp/x/arena-agent-* --out /tmp/official-dry-run --path /usr/bin:/bin
```

流程：用只含 eak key 的 key 文件 `run_agent.sh start`（`python -m agent --tournament --match`）→ 宝可梦对局和狼人杀桌同时分到（2 个并发分配，测试赛和正式赛交替）→ 对战阶段 `kill -9` 宝可梦 worker → 对战阶段 `kill -9` 运行时本身（`python -m agent`）→ 注入 15 s「认证接口 503 + 所有 agent session 过期」→ 另一个 LOG_DIR / NAME 用同一把 key 起第二个看门脚本（热备）→ 等到两者都在时至少分配出一个新座位（必须归主进程，热备不能起任何 worker）→ 等宝可梦进入对战后停掉主进程 → 热备接管（接管那一回合不能被计时器代打）→ 热备再完整打一场宝可梦（`--after-takeover`）→ 模拟平台不再开新局（drain 文件），在打的打完 → 全部停止、查残留、查日志里有没有 key，并检查两种对局都打了、每个正式赛座位都在连接窗口内连上（`no_shows` 必须是 0，报告的 `connect` 一栏给出最慢的连接时间）。结果写在 `--out/report.json`。对局一直有新的（`--vgc-matches` / `--werewolf-tables` 只是上限），所以每一步都有正在打的对局可用，不管对局结束得多快。

09-30 实测（压缩包 `40309a0-dirty` + 本修复；干净环境，PATH 只有 `/usr/bin:/bin`，node 用包里的；r2 / r3 时同机负载 6–7，r5–r7 开始时本机没有别的对局在跑）。**最终代码是 dry-r7**（13 项检查全过）；r2–r6 是中间版本，留着是为了看每次改了什么。10-03 合入 main 376048d 后，用新出的包按同样条件复跑了一次（dry-i1），13 项也全过；10-05 合入 v4.2 后又跑了三次（dry-v42 开头的三行），10-06 复审修复后两次（dry-v42r、v42rs），合上游 + v4.2.1 后一次（dry-up），10-07 v4.2.1 补丁（分支上叫 v4.2.2）后一次（dry-up2：直接用工作区，不是压缩包，PATH 没限制），也都全过。演练默认 `ARENA_RECORD=0`，状态文件是关的，kill worker 后的新 worker 从零开始接着打；dry-v42s 用演练脚本的临时副本（不进仓库）打开了状态文件（`ARENA_STATE=1`）和整机槽位（`ARENA_SEARCH_MAX_PROCS=8`），新 worker 从状态文件接上：

| 演练 | 宝可梦 / 狼人杀 | 决策 | 非法 / 计时器代打 | 盲状态 | kill worker → 新 worker / 下一步 | kill 运行时 → 新运行时 / 下一步 | 认证 503 15 s | 非持有者下的步 | 残留 / key 进日志 |
|---|---|---|---|---|---|---|---|---|---|
| dry-r2 | 6 场 / 2 桌 | 84 | 0 / 0 | 2 | 16.0 s / 19.1 s | （还没有这一步） | 主进程 pid 不变，0 个 worker 被停，5 次 503 | 0 | 0 / 0 |
| dry-r3 | 10 场 / 5 桌 | 163 | 0 / 0 | 5 | 16.0 s / 16.0 s | （还没有这一步） | 同上，4 次 503 | 0 | 0 / 0 |
| dry-r4 | 10 场 / 5 桌 | 149 | 0 / 0 | 2 | 16.0 s / 18.0 s | 3.5 s / 8.5 s，0 次 seat_busy、0 次新 grant | 同上，6 次 503 | 0 | 0 / 0 |
| dry-r5 | 7 场 / 4 桌 | 113 | 0 / **1**（接管那一回合） | 3 | 13.0 s / 16.0 s | 3.5 s / —（测量不准，已改） | 同上，5 次 503 | 0 | 0 / 0 |
| dry-r6 | 8 场 / 3 桌 | 115 | 0 / 0 | 2 | 13.5 s / 15.5 s | 3.5 s / —（那场在 kill 的同一秒打完了，演练脚本已改成换一场再杀） | 同上，2 次 503 | 0 | 0 / 0 |
| **dry-r7** | 7 场 / 3 桌 | 102 | 0 / 0 | 2 | 13.5 s / 16.5 s | **3.4 s / 7.4 s**，0 次 seat_busy、0 次新 grant、0 次代打 | 同上，2 次 503 | 0 | 0 / 0 |
| dry-i1（10-03，合入 main 376048d 后，新出的包） | 7 场 / 3 桌 | 110 | 0 / 0 | 2 | 13.5 s / 15.5 s | 3.4 s / 8.4 s，0 次 seat_busy、0 次新 grant、0 次代打 | 同上，5 次 503 | 0 | 0 / 0 |
| dry-v42（10-05，v4.2 提交前的包） | 6 场 / 3 桌 | 96 | 0 / 0 | 3 | 13.5 s / 16.5 s | 3.4 s / 6.5 s，0 次 seat_busy、0 次新 grant、0 次代打 | 同上，5 次 503 | 0 | 0 / 0 |
| dry-v42c（10-05，提交 2ef08e4 出的包） | 7 场 / 3 桌 | 111 | 0 / 0 | 3 | 14.7 s / 17.7 s | 3.4 s / 8.4 s，0 次 seat_busy、0 次新 grant、0 次代打 | 同上，6 次 503 | 0 | 0 / 0 |
| dry-v42s（10-05，同一个包，状态文件打开） | 6 场 / 3 桌 | 98 | 0 / 0 | 3 | 16.0 s / 18.0 s，新 worker 从状态文件接上（「resumed match … 11 decisions before」） | 3.4 s / 7.4 s，0 次 seat_busy；宝可梦和狼人杀各从状态文件接上一次 | 同上，6 次 503 | 0 | 0 / 0 |
| dry-v42r（10-06，复审修复后 a3a1271 的包） | 7 场 / 3 桌 | 119 | 0 / 0 | 5 | 14.5 s / 17.5 s | 3.4 s / 11.1 s，0 次 seat_busy、0 次新 grant、0 次代打 | 同上，5 次 503 | 0 | 0 / 0 |
| dry-v42rs（10-06，同一个包，状态文件打开） | 6 场 / 3 桌 | 102 | 0 / 0 | 3 | 16.2 s / 18.2 s，新 worker 从状态文件接上 | 3.4 s / 6.4 s，0 次 seat_busy；宝可梦 2 次、狼人杀 1 次从状态文件接上 | 同上，5 次 503 | 0 | 0 / 0 |
| dry-up（10-06，合上游 + v4.2.1，bfa28d3 的包） | 7 场 / 3 桌 | 109 | 0 / 0 | 3 | 14.3 s / 17.3 s | 4.2 s / 8.2 s，0 次 seat_busy、0 次新 grant、0 次代打 | 同上，2 次 503 | 0 | 0 / 0 |
| dry-up2（10-07，v4.2.1 补丁，b0ac3a6 的工作区） | 6 场 / 3 桌 | 104 | 0 / 0 | 3 | 14.5 s / 20.6 s | 3.4 s / 7.4 s，0 次 seat_busy、0 次新 grant、0 次代打 | 同上，5 次 503 | 0 | 0 / 0 |
| **dry-c1**（10-07，v4.2.3 合并 + 上游事件流程，ec49587 的包；16 项检查：多了「两种对局都打了」「每个正式赛座位都在 240 s 窗口内连上」，测试赛 / 正式赛各 5 个座位，连上用时中位 4.7 s、最慢 29.6 s，0 次 no-show） | 7 场 / 3 桌 | 118 | 0 / 0 | 5 | 13.5 s / 16.5 s | 3.5 s / 7.6 s，0 次 seat_busy、0 次新 grant、0 次代打 | 同上（现在是上游 SDK 自己挡住），2 次 503 | 0 | 0 / 0 |
| dry-f1（10-07，复审修复：按座位退避上限 30 s，74defe4 的包；16 项全过，测试赛 / 正式赛各 5 个座位，连上用时中位 5.2 s、最慢 33.3 s，0 次 no-show） | 7 场 / 3 桌 | 118 | 0 / 0 | 3 | 13.5 s / 16.5 s（日志 `retrying that seat in 10s`） | 3.4 s / 6.4 s，0 次 seat_busy、0 次新 grant、0 次代打 | 同上，2 次 503 | 0 | 0 / 0 |

热备（同一把 key、另一个 LOG_DIR 的第二个看门脚本）：

| 演练 | 热备做法 | 主进程在时 | 主进程停 → 热备接管 | 接管那一回合 |
|---|---|---|---|---|
| dry-r2 / r3 | 座位直接交给 SDK：worker 撞 `seat_busy` 退出，35 s 后再起一个 | 每个座位约 35–45 s 起一次 worker、认证一次 | 45.8 s / 44.8 s | r3：那一步等了 43.7 s（< 55 s），0 次代打 |
| dry-r4 | 同上 | **抢到一张新开的狼人杀桌**（主进程好好的） | 没测成（接管时已经没有在打的对局；演练脚本已改成一直开新局） | — |
| dry-r5 | 新座位让 20 s，之后交给 SDK | 3 个新座位都归主进程；仍然起 worker 撞 `seat_busy` | **68.8 s** | **被计时器代打 1 次**（20 s 让座 + 35 s 重试叠加） |
| dry-r6 | 新座位让 20 s，之后每 10 s 用 `grant` 检查，拿到才交给 SDK（现行） | 3 个新座位都归主进程；热备 0 个 worker | 28.7 s | 那一步等了 30.4 s，0 次代打 |
| **dry-r7** | 同上（最终代码） | 1 个新座位归主进程；热备 0 个 worker，2 个座位检查拿到 `seat_busy` | **30.7 s** | 那一步等了 30.7 s，0 次代打，热备把这场打完 |
| dry-i1（10-03） | 同上 | 3 个新座位都归主进程；热备 0 个 worker，2 个座位检查拿到 `seat_busy` | 32.7 s | 那一步等了 34.2 s，0 次代打；接管的那桌狼人杀有 1 次 `WRONG_PHASE`（换阶段时的竞态，runner 没退出，这桌照常打完） |
| dry-v42 / v42c / v42s（10-05） | 同上 | 新座位都归主进程；热备 0 个 worker，2 个座位检查拿到 `seat_busy` | 28.9 s / 34.3 s / 28.9 s | 那一步等了 31.3 s / 31.5 s / 32.1 s，0 次代打，0 次竞争错误 |
| dry-v42r / v42rs（10-06） | 同上 | 新座位都归主进程；热备 0 个 worker | 32.7 s / 29.9 s | 那一步等了 29.8 s / 31.7 s，0 次代打，0 次竞争错误 |
| dry-up（10-06，合上游后） | 同上 | 2 个新座位都归主进程；热备 0 个 worker，2 个座位检查拿到 `seat_busy` | 29.7 s | 那一步等了 32.0 s，0 次代打，0 次竞争错误 |
| dry-up2（10-07，v4.2.1 补丁） | 同上 | 2 个新座位都归主进程；热备 0 个 worker，2 个座位检查拿到 `seat_busy` | 31.1 s | 那一步等了 31.0 s，0 次代打，0 次竞争错误 |
| dry-c1（10-07，v4.2.3 合并，`_HotStandby`） | 同上 | 2 个新座位都归主进程；热备 0 个 worker，2 个座位检查拿到 `seat_busy` | 30.8 s | 那一步等了 32.6 s，0 次代打，0 次竞争错误 |
| dry-f1（10-07，复审修复） | 同上 | 2 个新座位都归主进程；热备 0 个 worker，2 个座位检查拿到 `seat_busy` | 32.6 s | 那一步等了 33.8 s，0 次代打，0 次竞争错误 |

- 对战决策延迟（dry-r7，44 个）：p50 1.3 s；最大 30.7 s 就是热备接管那一步；整棵进程树峰值约 290 MB。宝可梦全胜（对手是官方 baseline bot，本来就弱，只说明流程通）。
- 第一次演练（dry-r1）发现模拟平台的一个保真问题：我们在狼人杀讨论窗口里重复发的 terminate 每次都让状态版本号 +1，runner 立刻又发一次，一桌 137 次。真实平台文档说 terminate 幂等，已改成模拟平台对重复 terminate 不改状态（dry-r2 起每桌 2–3 次）。这是模拟平台的问题，不是 agent 的问题，但上游缺陷第 6 条要问清楚。
- 热备接管 = 主进程最后一次续约后租约剩下的时间（≤ 30 s）+ 热备下一次检查（≤ 10 s）+ 起 worker（约 3 s），最坏约 45 s，宝可梦一回合 55 s 来得及。主进程如果只是 agent 崩溃，看门脚本沿用 execution id 重启，约 7–9 s 就续上（dry-r4 / r7）；看门脚本本身被 systemd 重启也沿用（10-06 起，约 13–15 s），比热备快得多，所以**主进程这台机器的稳定性仍是第一位**。
- 狼人杀：我们的座位每桌 2–4 个投票决策、0 次空闲代打、0 个工具错误。
- dry-v42s（状态文件打开）：对局中健康行是 `search=ok(used=12/12)`、`ok(used=32/35)` 这样的真实计数，没对局时 `idle(probe=1.8s)`；整机搜索槽位用了 1/8；18 个状态文件都是 0600、目录 0700。三次 v4.2 演练里 key 只出现在演练自己写的两个 key 文件里（0600），日志里没有。有几场宝可梦的汇总行是 `classic=1`：盲状态那一步（模板在、对战画面没有）用的经典打法，`degraded=0`，不是机器原因，状态文件关着的 dry-v42c 也一样。

## 加入锦标赛

**正式比赛不用 join**：主人在 dashboard 的 **Tournaments** 页面对要参加的比赛按 **Register my agent**（开赛前可以撤回；只有报名做完、Self Hosted 且有有效 key 的 agent 才能报名，按钮不能按时 dashboard 会写原因，代码表见 `docs/platform/04-tournaments.md` 第 6 节）。开赛后每一局都作为 `context: "tournament"` 的分配出现（带比赛名、轮次、对手和 `connect_deadline_at`），开着的 `python -m agent --tournament --match` 自己接上；轮与轮之间自动衔接（通常几秒，最多约 1 分钟），所以**从开赛前到比赛结束一直开着**。连平台的检查用 `bash scripts/run_agent.sh check-tournament`（就是 `python -m agent --check-tournament`，每行都要 ✓）。完整步骤见上面「比赛日手册」。不经看门脚本直接跑 `python -m agent --tournament --match` 也能打，但没有崩溃重启和日志轮转。

赛制（瑞士轮、淘汰赛 / 狼人杀决赛、计分、no-show）见 `docs/competition_brief.md` 最上面的「2026-10-07 更新」和 `docs/platform/04-tournaments.md`。旧的入座方式（sk key、不带参数的 `python -m agent`、SDK 的 `join_tournament`、MCP 的 `list_sessions` / `join_session` / `get_tournament`）**10-06 起停用**：接口返回 410 `platform_agents_retired` / `TOOL_REMOVED`（`docs/platform/07-errors.md`）。

## 测试赛（`--match`；10-06 起座位就是分配，`--claim` 停用）

测试座位和正式赛一样，是 Official Agent Key 的**分配**，只是带 `context: "testing"`（官方协议见 `docs/platform/official-agent.md`）。

- **怎么打**：主人在 dashboard 的 **Testing** 页面（`/tournament/dashboard/testing`）建测试对局，座位选 "Mine (self-hosted)"，或在 *Open matches* 里加入别人开的座位。开着的看门脚本（`python -m agent --tournament --match`）每 10 s 查一次分配，自己接上，不用复制任何命令。自对弈（两个座位都选自己的）也是同一个进程，每个座位一个 worker。先跑一次 `selfcheck.py` 看到 `selfcheck: ready`、`check-tournament` 每行 ✓。
- **测试赛和正式赛同一个进程打**：比赛日（10-16）如果还有没打完的测试对局挂在分配里，也会和正式赛一起打（多占一个 worker 和对应的内存）；测试对局打完就离开分配列表，比赛前别留着没打完的（或在 Testing 页面结束它）。
- **只开 `--match` 的进程不会接正式赛**：每一局正式赛它只打一次警告（`You have a tournament game waiting …: run with --tournament to play it`），那一局会因为 no-show 判负。看门脚本默认两个都开，不会出这个问题。
- **`--claim` 现在会怎样**：只打印上游的停用说明（`Testing claim codes were retired; run with --match …`）后退出（退出码 1），不连平台。看门脚本拒绝 `--claim`。
- **还没开打的座位每 10–20 s 起一个短命 worker，是正常的**：对局还在大厅里时（分配的 `match_status` 是 `lobby` / `pending`，比如开放座位还没人加入），平台对 `grant` 回 409 `assignment_not_grantable`，「还没准备好」和「已经结束」是同一个代码。SDK 的 worker 把它当成已结束：日志 `[match … seat=…] assignment already ended; nothing to play`，接着运行时打 `Match finished.`，冷却 10 s、再等下一次查询后重来。所以**同一个座位反复出现 `Match assigned` / `Starting match...` / `assignment already ended; nothing to play` / `Match finished.`，意思是对局还在大厅里，不是结束了**。每一轮起一个新进程、加载一遍 agent（约 1–2 s CPU）、用 key 认证一次；对局一准备好，下一个 worker 在约 20–30 s 内连上（正式赛的连接窗口默认 4 分钟，来得及）。这是 SDK 原有的行为，main 上一样（10-06 复审用真 worker、假时钟测过：60 s 里起 4 次 worker，每次退出码 0）。
- 测试对局不计分、没有连接期限：漏掉不算输，只是别的座位在等你（红警测试对局两边 15 分钟内没到齐就无结果结束）。
- 红警测试对局：我们的 agent 整场只回答 `WAIT`（见上面「被分到红警」），不要用它测红警。

## 每局都在录

`agent/agent.py` 把每次决策的原始局面写到 `logs/<session_id>.jsonl`（经看门脚本运行时默认写到 `LOG_DIR`，可用 `ARENA_RECORD_DIR` 改目录），每场约 0.25 MB。写失败（目录只读）时每场打一条 WARNING。比赛后 `python -m local.pool --harvest` 把新见到的卡并入 `data/vgc_cards.json`。

## 公开理由（reasoning_summary）

每步出手会附一句公开理由（`agent/reasoning.py`），观众能看到，我们按「对手也能实时看到」来写，所以只用公开信息、固定模板：

- 狼人杀：夜里一律不写（夜里只有狼和预言家行动，写一句就暴露身份）；白天投票对所有身份、所有目标都是同一句「Day N vote cast. Ballots stay hidden until everyone has voted.」。
- 宝可梦选秀：选秀是轮流且立刻公开的，写刚选的那张卡和一条属性相克事实，不写道具、特性、招式，也不提卡池里还没被选的卡。
- 宝可梦队伍预览和对战（双方同时出手）：默认 `safe` 模式只写回合数和公开的击倒数，内容与我们选了什么无关。

开关是环境变量 `ARENA_REASONING`（启动前设置；用看门脚本时写进 `.env`）：
- 不设或 `safe`：上面的默认行为。
- `detail`：预览写两只首发，对战写本回合选的招式、目标、换人（从不写别的选项）。只有在主办方确认观战要等双方都出手后才显示时再用，否则对手可能先看到我们的守住或换人。
- `off`（或 `0`）：完全不附理由，发给平台的内容与加这个功能之前一模一样。

## 狼人杀聊天

- 说的话全部来自固定模板（`agent/werewolf/policy.py`），发出前再过一遍 `chat_guard`：狼从不在公开聊天里提队友的座位、不说自己是狼、不提夜里的计划；村民从不假跳预言家；只有预言家决定跳的时候才公开查验结果；私信只可能是狼发给活着的队友。
- 别人的聊天一律当作不可信的文字：只从里面读「谁跳了预言家、说谁是狼 / 好人、说谁可疑 / 要投谁」这几类事实，权重有上限；像「ignore previous instructions」「[SYSTEM]」「moderator:」这种想给大家下指令的消息整条不读，发送者可疑度略加。能读懂随意的写法：p3、player 3、seat #3、显示名、「3 is wolf」「im seer」「sus」，全角字符也行；引号里转述别人的话不算。
- 同一个讨论窗口里，我们一旦发了 terminate（或平台把我们列进 `terminated_messaging`），之后只会再发 terminate，不会再发聊天；同一句话一天只发一次。
- 私信默认**关闭**。简报确认不需要登录的观战接口 `/games/{id}/spectate` 会显示每条聊天的收件人，SSE 消息事件还可能带私信内容：狼给队友发一条私信，观众（包括其他参赛者）就能看出两匹狼是谁、当晚刀谁。两匹狼按同一份公开记录算出同一个夜刀目标，不靠私信也不会分票；本地评测里单座位时私信没有可测出的收益。
- 只有设了环境变量 `ARENA_WW_DM=on`（启动 `python -m agent` 前设置；`1` / `true` / `yes` 也行）才会打开：狼白天第一条消息私信队友夜刀目标（「Tonight: PlayerN.」，两匹狼座位小的那个说了算）。只有主办方确认观战看不到私信的收件人和内容时再打开。无论开不开，队友（座位更小的那匹狼）发来的夜刀私信我们都会照着刀。
- 收件人不合法的私信直接丢掉，绝不会改成公开发送。
- 「多听一会儿」（环境变量 `ARENA_WW_LISTEN`，默认**关闭**，`on` 打开）：打开后，我方开场发言之后不马上结束发言，而是继续读讨论窗口（用 runner 自己的连接做只读的 `wait_for_update`，读不算「活动」），出现值得回应的话（比如有人跳预言家说我们是狼）就补一条回应，每天最多 1 条；其他座位都结束了、24 s 没人说话、或离窗口关闭只剩 15 s 时结束发言（120 s 窗口下的数，按窗口长度的 1/5 和 1/8 算）。为此它要在调用栈里找到 SDK `run_game` 的 `game` 变量：SDK 改了形状就找不到，这时自动退回「不听」（已测，不会有异常传给 runner）。关闭时和不带这个功能的版本逐个决策相同。本地只在一种模拟节奏下看到每座约 +0.6 个百分点的小收益，真 runner 替身上胜负没变，所以默认关（数字见 `docs/progress.md`）。

## 上线前检查清单

- [ ] `git pull` 到冻结的 tag；`python -m pytest -q` 全绿；`python scripts/replay_real.py` 和 `--no-calc` 0 失败
- [ ] `.venv/bin/python scripts/selfcheck.py` → `selfcheck: ready`，且有 `OK    damage calculator answers`、`OK    battle policy: engine search` 和 `OK    Official Agent Key configured … (python -m agent --tournament --match …)`
- [ ] dashboard：报名做完、Agent Configuration 是 **Self Hosted**、key 有效；要打的每个比赛都在 Tournaments 页面按过 **Register my agent**
- [ ] `bash scripts/run_agent.sh check-tournament` → 每行 ✓（会连平台，主人执行）；`.env` 权限 600；`.env` 里没有 `ARENA_CALC` / `ARENA_ALLOW_NO_CALC` / `ARENA_BATTLE=classic` / `ARENA_EXECUTION_ID` / `ARENA_MODE` / `ARENA_NEW_SEAT_DELAY_S` / `.env.example` 的占位 key，主机的 `.env` 里也没有 `ARENA_ROLE=standby`（只有热备机器才写）
- [ ] 至少打过一局 Testing（宝可梦和狼人杀各一局），日志里有 `Match assigned: … (Testing)` 和 `Match finished.`
- [ ] 这把 eak key 只在计划中的机器上跑（主机；热备只在主办方同意后才开）；笔记本、旧云主机上的都已停掉
- [ ] systemd 已重新 `install-systemd`（单元里有 `OOMPolicy=continue`）、`enable --now` 且 `enable-linger`；`bash scripts/run_agent.sh status` 显示 `mode=tournament(primary)`、`agent=up`、`calc=ok(...)`、`search=idle(probe=…s)`（还没对局）或 `search=ok(used=…)`、`restarts=0`
- [ ] 磁盘剩余 > 1 GB；`logs/` 可写；内存至少 1.2 GB × 预计并发场数空闲
- [ ] 提前至少 1 小时启动并看到健康行；比赛中每小时看一次 `status`（`restarts` 不涨、`calc` 仍是 ok、`workers` 与在打的对局数一致）
- [ ] 已加入主办方 Discord 公告频道；主办方回复了：几点开始、要在线到几点、允不允许热备、同时最多几场
- [ ] 交给主办方或换新机器时：`bash scripts/bundle.sh` 出包（自带 node），在一台干净机器上按 `BUNDLE_README.txt` 走一遍 selfcheck 和 replay_real，确认计算器用的是包里的 node

## 比赛中出事怎么办

| 现象 | 处理 |
|---|---|
| `status` 显示 `agent=down` 且不恢复 | `tail -50 logs/agent-main.out` 看原因；key / 报名类错误修好后 `systemctl --user restart arena-agent@main` |
| 主机的健康行是 `mode=tournament(standby)` | 本机已有同一把 key 的另一个看门脚本（`ps -ef \| grep run_agent`），或主机的 key 文件里误写了 `ARENA_ROLE=standby`：改好后 restart。否则这台机器每个新座位都晚 20 s 才接 |
| 日志 "ERROR: only ALTRUAGENT_API_KEY (an sk_agent_ key) is set" / "ARENA_MODE=discovery … was retired" / "still the .env.example placeholder" | key 文件里没有真正的 eak key，或留着停用的设置：放进 Official Agent Key、删掉 `ARENA_MODE`，restart；这时一场正式对局都接不到，立刻处理 |
| 日志 "ALTRUAGENT_OFFICIAL_AGENT_KEY doesn't look like an Official Agent Key" / "The Official Agent Key was not accepted. Check that your agent is set to Self-hosted …" | Agent Configuration 是不是被改成了 Oracle Hosted（改回 Self Hosted），否则从 dashboard 重新复制（或重新生成）key，写进 `.env`，restart；不要把 key 贴到任何聊天里 |
| 日志 "Your event registration isn't complete yet" | 主人在 dashboard 把报名的缺项补上（问卷、Official Rules、代码仓库），然后 restart |
| 日志 `Could not renew your agent session (…). Games already running keep playing; retrying automatically.` | 平台认证接口暂时出错 / 限流 / 签发会话失败，在打的对局不受影响，SDK 按 10–60 s 退避自己恢复（`Assignment discovery recovered.`）；持续几分钟以上就 `curl -sI https://api.altruagent-game.com` 并看 Discord 公告 |
| 日志 `You have a tournament game waiting …: run with --tournament to play it` | 这个进程只开了 `--match`（有人手动 `-- --match` 启动了）：去掉，按默认（两个都开）restart，否则这局 no-show 判负 |
| 日志 `Another runtime is playing this match with your Official Agent Key` | 另一个进程（忘了停的旧机器，或已经接管了座位的热备）占着座位。不是计划中的就去停掉它。看门脚本重启后正常不会出现（它沿用 execution id）；只有换了新 id（拿不到本机的 key 锁、换了 `LOG_DIR` / `--name`、机器重启过）而旧租约还没过期时出现一次属正常（约 65 s 后接上） |
| 热备日志 `standby: seat … is free (no other runtime holds its lease): playing it`，而主机明明在跑 | 主机至少 30 s 没续上这个座位的租约（网络断了、机器卡死）：看主机日志和 `status`；热备会把这场打完，主机恢复后只接新座位 |
| 日志 `Match ended with an error (exit code …); retrying that seat in 10s`（之后 20s、40s … 翻倍）反复出现 | 同一座位的 worker 一直崩：看该座位 `[match … seat=…]` 前后的报错（`SESSION_NOT_FOUND` = 游戏服务器把这局弄丢了，平台几分钟内会关掉它，不用管）；必要时 `ARENA_BATTLE=classic` 临时退回冠军打法 |
| 日志有 "THE DAMAGE CALCULATOR CANNOT RUN … NOT STARTING" | 按横幅里的原因修（装 node / 设 `ARENA_NODE` / `cd agent/js && npm ci --omit=dev` / 删掉 `ARENA_CALC=off`），systemd 会自己重试；实在修不好又必须上场：临时设 `ARENA_ALLOW_NO_CALC=1`（node 本身坏了时搜索也会自动改 classic；启发式也比被随机代打强），修好后删掉并重启 |
| 运行中 `calc=MISSING` / `NO-NODE_MODULES` | 同上修好后重启；在此之前 agent 仍在用启发式打 |
| 日志有 "ARENA_BATTLE=search BUT THE ENGINE SEARCH CANNOT RUN … NOT STARTING"，或健康行 `search=NO-SIMULATOR` | 模拟器没装或坏了：git checkout 里 `(cd local/js && npm ci)`，或换成压缩包；systemd 会自己重试。实在修不好又必须上场：临时设 `ARENA_BATTLE=classic`（冠军 v3.2 的打法，照样能打），修好后删掉并重启 |
| 健康行 `search=DEGRADED(...)` / `SLOW(probe=…)`，或日志有 `ENGINE SEARCH DEGRADED` / `ENGINE SEARCH NOT STARTED` | 机器太忙或内存不够，部分回合退回了经典打法（合法，只是弱一些）：`free -m`、`uptime`，停掉同机的其他重活；`NOT STARTED` 是整机搜索槽位满或可用内存 < 500 MB，别的对局结束后每 5 秒自动重试（`docs/restart_load.md` 第 2、3 节） |
| 日志 `resumed match … after a worker restart` | 正常：worker 重启后从状态文件接上了这局的记忆（v4.2）。频繁出现说明 worker 在反复崩，按上面「Match ended with an error」处理 |
| "ERROR: LOG_DIR … is not writable" | 把 `LOG_DIR` 指到可写目录（systemd：单元里加 `Environment=LOG_DIR=…`，或修目录权限），systemd 会自己重试 |
| `restarts` 一直涨 | 看日志里 `exited with code` 前的报错；网络问题先 `curl -sI https://api.altruagent-game.com` |
| 磁盘满 | 删旧的 `logs/agent-*.out.N` 和不需要的 `logs/rehearsal/` |
