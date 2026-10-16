# 10-15 夜间两项升级：交接说明（给接手的 agent）

写于 2026-10-15 21:30 PDT。先读 `CLAUDE.md`、`docs/HANDOFF.md`，再读这份。

## 0. 最要紧的时间点

- **提交截止：2026-10-16 09:30 PDT**。平台 Testing 页 10-15 晚写的是 "closes when submissions close (9:30 a.m. Pacific Time, October 16)"，Agent Configuration 的 "Save repository" 按钮当时还能点。官网写过 10-15 7 PM，正式规则页写的还是 10-13，**以平台实际为准，但越早越好**。
- **比赛 10-16 10:00 开始**：宝可梦 + 狼人杀在 10:00–12:00，红警在 14:00–17:00。比赛中不许改代码 / 配置（规则第 4 节）。
- **截止前必须完成（主人的操作）**：
  1. GitHub 上 `Astro-wen/yuriko-red-agent`（提交用仓库，现在是 **PRIVATE**）改成 Public。主人说一句「改公开」，agent 可以用 `gh repo edit Astro-wen/yuriko-red-agent --visibility public --accept-visibility-change-consequences` 代办。
  2. 主人在平台 Agent Configuration 把仓库地址从 `https://github.com/Astro-wen/ucla-agent-tournament`（私有开发仓库）改成 `https://github.com/Astro-wen/yuriko-red-agent`，点 "Save repository"。
- 提交仓库现在的内容 = 开发仓库 main b43a363（v4.7 + anchor2），提交号 6bd7e0799ea132d724025fa0e6b1a939082b170c。下面两项若合并，要重新同步（见第 4 节）。

## 1. 背景：为什么有这两项

10-15 下午的测试赛里：
- 宝可梦被 **LeCharmander（升级版）** 3 连败（942389a7、236ca484、63419b59）：快龙顺风 + 鳞射横扫；我方仆刀将军突袭对着用变化招 / 守住的对手连续失败；一局首选的铝钢桥龙没被带上场。
- 红警被 **Tom_2026（升级版）** 连败 5 局（6d0c7844、f1cd3ba4、edf99e95、335d2a62、21bf0e2a）：他约 4:45 就有 5–6 辆坦克，我们 5 分钟时军队只有 7–10k；另有一局对 friendly fighter（c6bc2d69）在没看到敌方基地的情况下三次进攻，按时间到比分输。

## 2. 宝可梦：分支 `pk/lech-1015`（worktree `~/wt/pk1015`）

- 截至 21:20：提交 21eaae9 —— 开关 `ARENA_SEARCH_SUCKER`（默认关）：搜索里「突袭」那一行只保留混合对手模型中贪心部分的 (1 − q)，q = 对手这一回合不攻击的概率，用来减少突袭落空。
- 证据：37 局真实 / 本地对局里突袭 82 次失败 61 次（74%）；开发种子 127 对 meta：候选 73 vs 基线 71，+2.0 [−7.6, +11.6]，不显著；候选局里突袭失败率降到 53%。
- 21:20 正在跑：新种子 197（meta 200 局 + champion 150 局）配对确认，脚本 `~/wt/pk1015/_notes/fresh_base.sh` / `fresh_cand.sh`，日志 `~/wt/pk1015/_notes/fresh_*.log`，状态 `bash ~/wt/pk1015/_notes/st.sh`，比较 `python -m local.panel --summary RUN --compare BASE` 或 `_notes/paired.py`。
- 笔记全文副本：`docs/overnight_1015/pk1015_progress.txt`，脚本副本 `docs/overnight_1015/pk1015_notes/`。
- **合并标准**：新种子上对至少一个对手显著变强、对任何对手不显著变差；然后 pytest（至少 tests/strategy + 宝可梦相关）、`scripts/replay_real.py` 和 `--no-calc` 0 失败；再把开关默认改成开、写 docs/progress.md 中文小节。达不到就不合（主线宝可梦保持现状）。

## 3. 红警：分支 `ra/tom-1015`（worktree `~/wt/ra6`）

- 截至 21:00：候选 d4e46b5（v4.9 candidate），默认开 `rally_step=5`（集结点更靠家）和 `prior_hold=1`（「看不见的敌军」估计在 900 s 后不再骤降，修 f1cd3ba4 里冲进炮兵的那波）。关掉用 `ARENA_RA_RALLY_STEP=0 ARENA_RA_PRIOR_HOLD=0`（= v4.8 默认，即 main）。
- 新模拟对手 `tom2` / `tom2p`（按真实录像量出的 Tom 新版节奏）在 51845bb（`local/ra_bots_strong.py`）。试过不用的：`hold_home`（对 tom2 −10，已删），`split_gate`（拆分开火的门控，保持关）。
- 21:00 起在跑：最终测量（新种子 1651–1666，`final7.sh fin A / B`，基线 fb_*，候选 fin_*）和上线检查（`gate6.sh` → `~/wt/_ra6_gate`，日志 `~/wt/_ra6_out/gate.out`），以及 `ident7.sh`（main 快照与基线逐局相同的检查），汇总 `fsum.sh`。**上线检查打包完成前不要改 worktree**；文档草稿在 scratchpad（ra6 笔记里写了位置）。
- 笔记全文副本：`docs/overnight_1015/ra6_progress.txt`，全部脚本副本 `docs/overnight_1015/ra6_scripts/`（原件在本机 `C:\Users\LINMEN~1\AppData\Local\Temp\claude\…\0e5ee86a-…\scratchpad\ra6\`，`~/r6` 可能链接过去）。
- **合并标准**：新种子上对 tom2 明显变好（或总体变好），对任何对手 / 压力场景不显著变差，0 非法动作 / 0 异常；上线检查全过（全量 pytest、真实重放两种模式 0 失败、selfcheck ready、打包 + 包检查、红警端到端）。达不到就不合（主线红警保持 v4.7 + anchor2）。

## 4. 合并和发布的步骤（接手的 agent 照做）

1. 合并前确认比赛 agent 空闲：`bash scripts/run_agent.sh status` 第二行 `workers=0`；`git status` 干净。
2. `git merge --no-ff <分支> -m "Merge <分支>: …"`（提交只署主人名字，**不加任何 Claude 署名**）；合并后在 main 上跑相关测试 + `scripts/selfcheck.py`，`git push origin main`。比赛 agent 不用重启：每局新开进程读代码。
3. 同步提交仓库（`~/yuriko-red-agent` 是它的 clone）：
   ```bash
   cd ~/yuriko-red-agent && git fetch -q origin && git reset -q --hard origin/main && cd ~/ucla-agent-tournament
   bash scripts/publish_sync.sh --commit main --out ~/yuriko-red-agent --replace   # 必须打印 READY，记下 tree 哈希
   cd ~/yuriko-red-agent && git add -A && git commit -m "Sync from the development repository at <main 短哈希>" && git rev-parse 'HEAD^{tree}'   # 要和 READY 打印的一致
   git push origin main
   ```
4. 截止前确认：`yuriko-red-agent` 是 Public（不登录 `curl -s -o /dev/null -w '%{http_code}' https://github.com/Astro-wen/yuriko-red-agent` 返回 200）、平台登记的地址已改、记下最终提交号。
5. **截止后（09:30 起）不再往提交仓库推任何东西**；比赛中不改配置、不重启（要重启得主办方同意）。

## 5. 机器和注意事项

- 比赛 agent：systemd 用户服务 `arena-agent@main`（`systemctl --user status arena-agent@main`），从 main checkout 运行，`--tournament --match`。
- 两个夜间任务同时跑时机器负载很高（21:20 load 37/16 核）。**10:00 比赛开始前必须停掉所有本地重任务**（`pkill -f 'local.panel|local.ra_platform'`，确认 load 降下来），避免拖慢比赛 agent。
- 正式赛（Tournaments 页面）出现后，主人要对宝可梦、狼人杀、红警各点一次 "Register my agent"。
- 电脑插电、关睡眠；WSL 重启后服务会自动起（第一次打开 WSL 时）。
