# 部署与比赛日运行手册

比赛日（10/16）的模式：**我们的 agent 进程必须一直在线**，平台把对局分配给 agent，`python -m agent` 自动发现并开打。进程挂了 = 该局被平台随机代打 = 基本等于输。

谁来运行 agent 还不确定（见 `docs/competition_brief.md`），两条路都准备好了：

- **A. 自托管**：我们自己的云主机上用 `scripts/run_agent.sh`（systemd 或 tmux）常驻。
- **B. 交给主办方**：`scripts/bundle.sh` 打一个不含密钥、**自带 Node** 的压缩包，或用 `deploy/Dockerfile` 构建镜像（镜像里也有 Node）。

名词：**看门脚本**（`scripts/run_agent.sh`：崩溃重启、日志轮转、健康行、干净停止）；**计算器**（`agent/js/calc.js`，用 Node 跑的精确伤害计算）；**worker**（每场对局一个独立子进程）。

脚本一律用 `bash scripts/...` 调用：补丁（`diff -ruN`）不带可执行位，这样写不依赖 `chmod +x`。

## 机器要求

- Linux x86-64（推荐美西云主机，平台在 AWS us-west-2），Python ≥ 3.11（实测 3.12），bash ≥ 4.2
- **计算器必须能跑**：Node ≥ 18 加 `agent/js/node_modules`。没有计算器时 agent 只能用启发式，对有计算器的自己只赢约 12%（见「没有 Node 会怎样」）。所以 `selfcheck.py` 会判失败，`run_agent.sh` 会**拒绝启动**（退出码 2，日志和终端 / journal 里都有醒目的 ERROR 横幅），除非显式设 `ARENA_ALLOW_NO_CALC=1`（仅限紧急情况：启发式总比 agent 不在线、被随机代打强）。
- 压缩包自带固定版本的官方 Node（`node/bin/node`，v22.23.3），Docker 镜像里也是同一版本；自托管的 git checkout 用系统的 node（≥ 18）。
- 稳定网络；不要用会睡眠 / 合盖的笔记本
- 内存：每场并发对局约 150 MB（worker）+ 50 MB（计算器），主进程约 55 MB；2 场并发时整棵进程树峰值实测 418–447 MB（130 / 125 分钟演练），按 **500 MB** 预留

## 一次性准备（A. 自托管）

```bash
git clone https://github.com/Astro-wen/ucla-agent-tournament.git && cd ucla-agent-tournament
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock      # 锁定版本的 29 个运行依赖；开发机再 pip install -e ".[dev]"
(cd agent/js && npm ci --omit=dev)              # 计算器的 node_modules，按 package-lock.json 精确安装
cp .env.example .env && chmod 600 .env          # 填 ALTRUAGENT_API_KEY（主 agent Yoriko_Red）
.venv/bin/python scripts/selfcheck.py           # 必须以 "selfcheck: ready" 结尾，并有 "OK    damage calculator answers: node v…"
.venv/bin/python scripts/check_connection.py    # 必须显示 status=claimed（这一步会连平台）
```

`selfcheck.py` 不连平台、不需要密钥：检查 Python 和锁定的包版本、数据文件和选秀模型、计算器能否在一个新的 node 进程里真的算出一次伤害（**算不出来就 FAIL，退出码 1**；`--allow-no-calc` 或 `ARENA_ALLOW_NO_CALC=1` 才降为警告；旧参数 `--require-calc` 仍可用，现在就是默认行为），并用仓库里录下的真实对局状态分别以「有计算器 / 无计算器」跑决策，按官方 runner 的规则校验每个答案。

## 运行（A. 自托管）

三种方式任选其一，底下都是同一个看门脚本。**同一个密钥同一时间只能在一台机器上运行**：本机上按密钥哈希加锁（`/tmp/arena-agent-key-<哈希>.lock`，可用 `ARENA_LOCK_DIR` 改目录），看门脚本和正在运行的 agent 进程都持有这把锁，所以另一个 checkout、另一个解压出来的包或另一个 `LOG_DIR` 都起不来同一个密钥；跨机器只能靠人。

**1）systemd 用户服务（推荐：开机自启；看门脚本本身挂了也会被拉起）**

```bash
bash scripts/run_agent.sh install-systemd       # 写 ~/.config/systemd/user/arena-agent@.service（填入仓库路径和 node 所在目录）
systemctl --user enable --now arena-agent@main  # main 用 .env；arena-agent@apocalypse 用 .env.apocalypse
sudo loginctl enable-linger $USER               # 不登录也运行，重启机器后自动起来
bash scripts/run_agent.sh status                # 状态 + 健康行
systemctl --user stop arena-agent@main          # 停止（看门脚本是某个 systemd 服务的主进程时，run_agent.sh stop 会拒绝并提示这条命令）
```

**2）tmux**

```bash
bash scripts/run_agent.sh tmux   # 在 tmux 会话 arena-main 里运行；tmux attach -t arena-main 看实时日志，Ctrl-b d 退出
bash scripts/run_agent.sh stop   # 停止
```

**3）后台进程**：`bash scripts/run_agent.sh start` / `stop` / `restart` / `status` / `logs`（`logs` = tail -F 日志）。

第二个 agent（陪练 Apocalypse_Red）：密钥放 `.env.apocalypse`，所有命令加 `--name apocalypse`。注意官方 SDK 在 agent 进程里还会读仓库根目录的 `.env`（不覆盖已有变量），所以看门脚本启动前也把根目录 `.env` 里的计算器开关（`ARENA_CALC` / `ARENA_NODE` / `ARENA_ALLOW_NO_CALC`）算进去检查。

### 看门脚本做了什么

- **计算器是启动条件**：启动前用 agent 自己的代码（`agent/pokemon/calc.py` 的 `probe()`）起一个新的 node 进程真算一次伤害。算不出来（没有 node、node 太旧或跑不起来、缺 node_modules、`ARENA_CALC=off`）就不启动：退出码 2，横幅写进日志，也写到终端 / journal；systemd 下每 5 s 重试一次，node 修好后自动起来。`ARENA_ALLOW_NO_CALC=1` 时照样启动，但每次启动都打 WARNING 横幅、健康行显示 `calc=MISSING` 之类。运行中 node 坏了不会停 agent（该局退回启发式，每场最多自动重启计算器 6 次），健康行会显示。
- **崩溃重启**：`python -m agent` 退出就重启，间隔 3 s；1 分钟内连续退出则翻倍退避（3→6→12…最多 60 s），避免密钥错误时狂刷登录。重启前把上一轮残留的 worker 和 Node 进程整组杀掉，不会有两个进程抢同一个座位。退避期间的 sleep 不持有任何锁，退避中 `restart` 也能马上起来。
- **单场恢复更快**：设置 `ARENA_DISCOVERY_S=10`、`ARENA_WORKER_COOLDOWN_S=10`。官方 SDK 默认 15 s 发现一次、某场 worker 崩溃后冷却 60 s，合计 60–75 s，会丢掉一个 55 s 的对战回合；现在约 20 s。
- **发现接口出错不再全军覆没**：官方 supervisor 里，`sessions()` 轮询只要抛一次异常（网络抖动、5xx、限流）就会结束整个 `python -m agent`，并终止所有正在打的 worker。`agent/__main__.py` 现在把失败的轮询当作「这次没有新对局」，正在打的对局不受影响；连续失败 30 次（约 5 分钟）才退出，交给看门脚本重启。
- **日志**：`logs/agent-NAME.out`，每行带时间戳，满 20 MB 轮转、保留 10 个（`LOG_MAX_MB`、`LOG_KEEP`）。`logs/` 整个在 .gitignore 里，**不要提交 `logs/*.out`**。
- **健康行**：每 5 分钟（`HEALTH_EVERY`）写一行 `[health]`，也可随时 `bash scripts/run_agent.sh health`：

  ```
  [health] name=main agent=up pid=… run_up=2h05m agent_up=1h47m restarts=1 workers=2 node=2 calc_procs=2 rss=418MB fds=87
           last_output=3s records=41 last_record=12s disk_free=38.2G logs=12M calc=ok(v18.19.1) key=#762c809cde
  ```

  `workers` = 正在打的对局数；`calc_procs` 应 ≤ `workers`；`restarts` 比赛中应不变；`calc` 必须是 `ok(...)`，出现 `MISSING` / `NO-NODE_MODULES` / `OFF(ARENA_CALC)` 立刻处理；`key=#…` 是密钥哈希的前 10 位（不是密钥），用来核对两台机器没跑同一个密钥。
- **干净停止**：`stop`（或 systemd stop、tmux 里 Ctrl-C）向 agent 整个进程组发 SIGTERM，20 s 内不退就 SIGKILL；不认输、不再调用平台。实测 0.6 s 停完。看门脚本自己被 SIGKILL / OOM 杀掉时，它的健康循环 5 s 内自行退出，日志写入进程在最后一个写端关闭后退出，下一次 `start` / `stop` 会清理残留的 agent 和这两个辅助进程（测试覆盖：`kill -9` 看门脚本后 `stop`，本测试的进程一个不剩）。
- **pid 文件不盲信**：`start` / `stop` 只认命令行是 `run_agent.sh run` 且环境里 `ARENA_RUN_ID=<NAME>@<LOG_DIR>` 对得上的进程；pid 文件过期、pid 被别的进程复用时，既不会误报「已在运行」，也不会去杀别人的进程。
- 启动自检写进日志：密钥是否设置（不打印密钥）、`.env` 权限、计算器（真算一次）、磁盘空间。

可调环境变量：`PYTHON`、`LOG_DIR`、`LOG_MAX_MB`、`LOG_KEEP`、`HEALTH_EVERY`、`RESTART_DELAY`、`RESTART_MAX_DELAY`、`STOP_TIMEOUT`、`ARENA_LOCK_DIR`（密钥锁目录，默认 `/tmp`）、`ARENA_DISCOVERY_S`、`ARENA_WORKER_COOLDOWN_S`、`ARENA_DISCOVERY_MAX_FAILURES`、`ARENA_NODE`（node 可执行文件路径，systemd / nvm 下 PATH 里没有 node 时用；路径失效时只记一条警告、改用包里的 `node/bin/node` 或 PATH 里的 node，不会因此关掉计算器）、`ARENA_CALC=off`（**只用于诊断**：强制关计算器，看门脚本和 selfcheck 会拒绝，除非同时设 `ARENA_ALLOW_NO_CALC=1`）、`ARENA_ALLOW_NO_CALC=1`（计算器跑不起来也照样启动；仅紧急情况，用完删掉）。

## B. 交给主办方：压缩包 / Docker

```bash
bash scripts/bundle.sh     # -> dist/arena-agent-<版本>.tar.gz（44 MB，解开 127 MB，其中 node 119 MB）+ .sha256
```

包里有：`agent/`（含选秀模型 `draft_model.json`）、`altruagent/`、`data/`、计算器的 `node_modules`（纯 JS，随包带上）、**官方 Node v22.23.3 Linux x64（`node/bin/node` + LICENSE + VERSION.txt）**、`requirements.lock`、`scripts/run_agent.sh`、`selfcheck.py`、`check_connection.py`、`replay_real.py`、`deploy/`（Dockerfile、systemd 单元）、本文件、一场真实对局录像（给 selfcheck 用）、`SHA256SUMS`、`BUNDLE_README.txt`。

- Node 从 `https://nodejs.org/dist/v22.23.3/` 下载（缓存在 `~/.cache/arena-agent/`），SHA-256 必须等于 `bundle.sh` 里写死的值（取自该版本的 SHASUMS256.txt），不符就不出包；出包前还用包里的 node 和包里的 node_modules 真算一次伤害。对方机器上默认用包里的 node（`ARENA_NODE` 可指定别的）。
- **没有任何密钥**：只按白名单拷文件，`.env*` 一律不进包，打包后再扫描一遍（`sk_agent_…`、`seatclaim_…`、`sk-…`、私钥、`ALTRUAGENT_API_KEY=` 后跟真实值），有就拒绝出包。
- `--node-tarball FILE`：离线出包，用已下载的 `node-v22.23.3-linux-x64.tar.xz`（或 `.tar.gz`），同样校验 SHA-256
- `--node-dist DIR`：改带另一个解压好的官方 Node ≥ 18（`DIR/bin/node`）
- `--no-node`：不带 node（对方必须自己装 Node ≥ 18，否则 agent 拒绝启动）
- `--no-node-modules`：不带 node_modules，对方需 `cd agent/js && npm ci --omit=dev`
- `--no-sample-logs`：不带对局录像

对方机器上（已实测：全新解压 + 全新 venv，`pip install -r requirements.lock`，selfcheck 通过且计算器用的是包里的 node）：

```bash
tar xzf arena-agent-*.tar.gz && cd arena-agent-*
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
.venv/bin/python scripts/selfcheck.py           # 必须 "selfcheck: ready"，且有 "OK    damage calculator answers: node v22.23.3 (…/node/bin/node)"
export ALTRUAGENT_API_KEY=...                   # 或写进 .env（chmod 600）
bash scripts/run_agent.sh start
```

一定要通过 `run_agent.sh`（或 Docker / systemd）启动：直接 `python -m agent` 不检查计算器。

Docker（Python 3.12 + 官方 Node 22.23.3，与压缩包同一版本；密钥只从环境变量进，不进镜像；构建时跑 `selfcheck`，计算器不可用就构建失败；容器启动时 `run_agent.sh` 再检查一次）：

```bash
docker build -f deploy/Dockerfile -t arena-agent .
docker run -d --name arena-main --restart unless-stopped -e ALTRUAGENT_API_KEY=... -v arena-logs:/data arena-agent
docker logs -f arena-main            # 与 /data/logs/agent-main.out 内容相同
```

不要加 `--init`：镜像的 ENTRYPOINT 已经是 tini，再加会套两层 tini 并打印警告。本机 WSL 没有 docker，Dockerfile 还没真正构建过；它的每一步（锁定依赖、npm ci、selfcheck、run_agent.sh）都已在「全新解压 + 全新 venv」里验证过，node 与压缩包是同一个官方版本。第一次构建时留意 `node --version` 这一步必须是 v22.23.3。

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

`local/fake_platform.py` 在 127.0.0.1 上模拟平台的控制面和 MCP 游戏接口（与官方 SDK 协议一致、令牌定时过期、平台计时 90 / 55 / 420+90 s）。对局由本地 Showdown 引擎跑，对手是本地 bot，对局状态保存在「平台」一侧，所以 agent 被杀后重启能接着打。`scripts/rehearsal.py` 起一整套：模拟平台 → 看门脚本（`--supervise start` 或 `systemd`）→ `python -m agent` → 每场一个 worker → Node 计算器，定时采样内存 / 文件描述符 / 进程，并按时间点注入故障：

```bash
python scripts/rehearsal.py --minutes 120 --supervise systemd      # 结果在 logs/rehearsal/<时间>/report.json
python scripts/rehearsal.py --minutes 20 --agent-env ARENA_CALC=off --agent-env ARENA_ALLOW_NO_CALC=1   # 无计算器
```

故障（按时长比例）：15% `kill -9` 主进程、30% `kill -9` 一个 worker、45% `kill -9` 一个计算器、55% 发现接口 503 90 s、70% 全部接口 503 45 s、85% `kill -9` 看门脚本本身（systemd 自动拉起；start 模式下 10 s 后像值班的人一样再 `start`）。2 场并发，对手 random / baseline 混合。

| 演练 | 时长 | 看门方式 | 对局 / 决策 | 非法 / 计时器代打 | 进程树峰值 | 计算器 / worker 泄漏采样 | 停止后残留 | 意外重启 |
|---|---|---|---|---|---|---|---|---|
| reh-s1 | 7 min | start | 54 / 710 | 0 / 0 | 298 MB | 0 / 0 | 0 | 0（1 次 = 注入的 kill_main） |
| reh-sd1 | 16 min | systemd | 101 / 1342 | 0 / 0 | 332 MB | 0 / 0 | 0 | 0（1 次 = 注入） |
| reh-long2 | 130 min | systemd | 510 / 6819 | 0 / 0 | 418 MB | 0 / 0 | 0 | 0（1 次 = 注入，14:10:43） |
| reh-nonode（无计算器） | 20 min | start | 107 / 1495 | 0 / 0 | 197 MB | 0 / 29 | 0 * | 0（1 次 = 注入） |
| reh-final-start2（本版看门脚本） | 12 min | start | 44 / 624 | 0 / 0 | 405 MB | 0 / 0 | 0 | 0（1 次 = 注入） |
| reh-final-sd（本版看门脚本） | 13 min | systemd | 40 / 559 | 0 / 0 | 398 MB | 0 / 0 | 0 | 0（1 次 = 注入） |

- 恢复时间（reh-long2）：kill 主进程后新主进程 24 s 内接上；kill worker 后该局 16 s 得到新 worker；kill 计算器无可见延迟；发现接口 503 90 s 期间在打的对局不受影响；全部接口 503 45 s 后 50 s 恢复；kill 看门脚本后 systemd 7 s 拉起。本版看门脚本在 start 模式下 `kill -9` 看门脚本后，旧 agent 照常在打（它仍持有密钥锁，别的目录起不来同一个密钥）；10 s 后再 `start`，新的看门脚本先清掉旧 agent 和旧辅助进程再接手，在打的对局由新 agent 的 worker 接着打。所有演练 0 次计时器代打、0 非法动作、0 次判负。
- 日志里没有 Traceback；每次全接口 503 期间有 2 条 worker 的 `unexpected error: AuthenticationError('rehearsal outage')`（worker 退出，SDK supervisor 冷却后重开，对局无损）。
- 作废的一次：reh-long（125 min）中途模拟平台的调度线程挂了（模拟平台自身的 bug，已修），只完成 360 场，且有 45 个 worker 泄漏采样，不作为证据。
- \* reh-nonode 用的是旧版看门脚本：85% 处注入的 `kill -9` 看门脚本留下了它的健康循环和日志写入进程，演练结束后又跑了 4 个多小时（每分钟写一行 `agent=down`，已手动清掉）；旧版 `leftovers_after_stop` 只数 node / python 进程，没发现。本版已修（见「干净停止」），演练脚本现在把 bash 辅助进程也算进残留，并按日志统计整场的重启次数（`agent_restarts`，旧的 `restarts_last_loop` 只看最后一个看门进程）。

## 加入锦标赛

`python -m agent` **不会自动加入锦标赛**，它只打已分配的对局。如果主办方不替我们入座：

```bash
.venv/bin/python -c "
from altruagent import AltruAgentClient; c = AltruAgentClient(); print(c.join_tournament('<tournament_id>'))"
```

（确切的 SDK 调用以 `altruagent/client.py` 的 `join_tournament` 为准。）之后 agent 会看到子对局并自动开打。测试座位（`--claim seatclaim_...`）是一次性的，只在终端里手动跑，不放进看门脚本（脚本会拒绝）。

## 每局都在录

`agent/agent.py` 把每次决策的原始局面写到 `logs/<session_id>.jsonl`（可用 `ARENA_RECORD_DIR` 改目录），每场约 0.25 MB。比赛后 `python -m local.pool --harvest` 把新见到的卡并入 `data/vgc_cards.json`。

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

## 上线前检查清单

- [ ] `git pull` 到冻结的 tag；`python -m pytest -q` 全绿；`python scripts/replay_real.py` 和 `--no-calc` 0 失败
- [ ] `.venv/bin/python scripts/selfcheck.py` → `selfcheck: ready`，且有 `OK    damage calculator answers`
- [ ] `python scripts/check_connection.py` → claimed；`.env` 权限 600；`.env` 里没有 `ARENA_CALC` / `ARENA_ALLOW_NO_CALC`
- [ ] 本机只跑这一个密钥；其他机器（笔记本、旧云主机）上的同一密钥已停掉
- [ ] systemd 已 `enable --now` 且 `enable-linger`（或 tmux 会话已开）；`bash scripts/run_agent.sh status` 显示 `agent=up`、`calc=ok(...)`、`restarts=0`
- [ ] 磁盘剩余 > 1 GB；`logs/` 可写；内存至少 500 MB 空闲
- [ ] 提前至少 1 小时启动并看到健康行；比赛中每小时看一次 `status`（`restarts` 不涨、`calc` 仍是 ok、`workers` 与在打的对局数一致）
- [ ] 主办方回复：需要自己 join 锦标赛吗？几点开始？交代码还是交运行中的 agent？
- [ ] 交给主办方时：`bash scripts/bundle.sh` 出包（自带 node），在一台干净机器上按 `BUNDLE_README.txt` 走一遍 selfcheck，确认计算器用的是包里的 node

## 比赛中出事怎么办

| 现象 | 处理 |
|---|---|
| `status` 显示 `agent=down` 且不恢复 | `tail -50 logs/agent-main.out` 看原因；密钥 / 未认领类错误修好后 `systemctl --user restart arena-agent@main` |
| 日志有 "THE DAMAGE CALCULATOR CANNOT RUN … NOT STARTING" | 按横幅里的原因修（装 node / 设 `ARENA_NODE` / `cd agent/js && npm ci --omit=dev` / 删掉 `ARENA_CALC=off`），systemd 会自己重试；实在修不好又必须上场：临时设 `ARENA_ALLOW_NO_CALC=1`（启发式也比被随机代打强），修好后删掉并重启 |
| 运行中 `calc=MISSING` / `NO-NODE_MODULES` | 同上修好后重启；在此之前 agent 仍在用启发式打 |
| `restarts` 一直涨 | 看日志里 `exited with code` 前的报错；网络问题先 `curl -sI https://api.altruagent-game.com` |
| 磁盘满 | 删旧的 `logs/agent-*.out.N` 和不需要的 `logs/rehearsal/` |
| 日志报 "another instance on this machine already runs key" | 本机已有同密钥实例在跑（可能在另一个目录 / 另一个 LOG_DIR），先 `status` / `stop` 旧的；`ls /tmp/arena-agent-key-*.lock` 看锁文件，`fuser` 看谁持有 |
