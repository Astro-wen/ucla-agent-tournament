# 交接：在另一台机器上继续（2026-09-27 起，最近一次 10-08 更新）

## 现在到哪了（10-08）

- **提交截止：2026-10-13 23:59 PDT（2026-10-14T06:59Z，北京时间 10-14 14:59）。从这一刻到成绩公布后至少 30 天，参赛仓库一律不推送**（不提交、不改历史、不改回私有、不删仓库；Official Rules 第 8 节，全文 `docs/official_rules_2026-10-06.md`）。截止后的笔记、录像、日志放在仓库以外；上游 SDK 再更新，要主办方书面授权才能合。下面「下一步」里要提交东西的步骤只能在截止前做。比赛版本 = 截止时 `main` 上最新的提交，比赛机部署的就是它（`docs/deploy.md`「截止和比赛日时间表」）。CLAUDE.md 里的同一条规则（和 INVALID_ACTION 那条里过时的「300 s」）写成了补丁，等主人确认后再加。
- **文档、合规和公开准备（10-08，分支 `docs/1008`，从 main 7a59a65 开，包含 `compliance/rules-1008` 的全部提交，等主人合进 main）**：在下面这些之上又做了：删掉了主办方私人邮件的摘要和由它来的人名、计划（只留一句「主办方的邮件答复不在仓库里转述」）；疑似平台问题在文档里只剩一句「由主人私下报告主办方」（规则第 7、13 节）；README 写成红警由规则型 bot 打（bot 在 `ra/bot-v0` 上，**截止时 main 上必须真有它**）；方案 C 的工具：`scripts/publish_sync.sh`（从一个提交导出比赛代码到单独的公开仓库，扫描、树哈希、干净 venv 里 selfcheck 和测试）、`PUBLIC_README.md`、`NOTICE`。**公开前还差一步**：`agent/` 和 `champion/`、`versions/` 里有几处代码注释还描述那个问题，`publish_sync.sh` 会报 `ISSUE`，要在改 `agent/` 的分支上把注释改成中性说法（只改注释，按 v4.x 走）。CLAUDE.md 的冻结规则只写成了补丁（这个文件只能主人改），见 `docs/progress.md`「10-08 文档和公开准备」。以下是 `compliance/rules-1008` 做的：`README.md` 最上面加了规则第 8 节要求的英文说明（是什么、比赛版本、怎么运行、组件、比赛用的设置、记录、没放进仓库的组件）；`docs/platform/` 刷新到 10-08 的实时文档；`docs/competition_brief.md` 最上面加「2026-10-08 更新」（规则要点、截止和冻结、10-16 日程、要问主办方的问题）；`docs/deploy.md` 比赛日手册改成绝对时间，加了「比赛进行中的规矩」（人动手要主办方授权、运维不看观战、规则更新马上同意、换 key 后 restart 并通知主办方、手动操作记进 `logs/manual-actions.txt`）；`run_agent.sh` 默认保留 40 × 50 MB 日志，每次启动记下部署的完整提交号和 `ARENA_*` 设置，压缩包带 `COMMIT` 文件。`agent/` 没动。检查数字见 `docs/progress.md`「v4.2.4 合规」。**部署用这个分支出的包**（它会在日志里写出提交号）；截止前 main 定下来以后再从 main 重新打一次。

- **v4.2.5（10-08，分支 `pk/tracker-1008`，从 main v4.2.4 拉出，等主人合进 main）**：宝可梦同一步被问第二次（送出失败后重读、worker 重启）时从第一次之前的回合记忆开始，不管版本号变没变，单打也一样；被服务器拒收的招式（平台已替我们随机出招）不再被记成「打出去了」（但同一步之前有一次送出没收到回答、可能已经送到时，照旧记成打出去了：10-08 下午评审修正，免得忘掉已经打出去的守住）；对战 / 首发的预算还受平台新字段 `observation.clock`（读状态时还剩几秒）限制，状态读晚了也不会超过平台的 50 / 85 s。本地对局不变（对 main 的冠军 20 局逐局相同），真实录像重放 2349 个决策 0 失败；冠军已冻结成这一版（`champion/VERSION` = bd920b9，含评审修正）。数字和计时对照表见 `docs/progress.md`「v4.2.5」。
- **v4.2.4（10-08，分支 `integrate/v424`，等主人合进 main）**：合上游 PR #7（局内重试 90 s、40 s 发现断线、Official Rules 更新后不再停运行时、拿座位的暂时故障在连接窗口内立刻重来、key 被拒后不开新局），我们的狼人杀发言没送到时不计数、重新决定；宝可梦打法不变（本地对冠军 20 局逐局相同）。数字见 `docs/progress.md`「v4.2.4」。部署用 `~/wt/_v424_out/dist/arena-agent-dc907f2.tar.gz`（下面 74defe4 的包是 PR #7 之前的运行时）。
- **冠军 v4.2.3**（`champion/VERSION` 是冻结时的提交，分支 `integrate/v423`，主人快进合进 main）。宝可梦打法和 v4.1.1 逐局相同（本地对冠军 20 局逐局核对过），多出来的都是稳定性和平台适配：
  - v4.1.2 / v4.2：官方正式赛部署、重启后接上记忆、慢机器和内存下的自我保护（`docs/progress.md` 同名两节）。
  - 狼人杀身份核对 + 读懂大模型的跳预言家（`ww/reading`，10-03）。
  - **v4.2.1**：合上游的实时 SDK（红警），被分到红警时整场回答 `WAIT`（不发命令、不崩）；补丁：重新登录时平台签发会话失败不再停运行时（这一条现在由上游 SDK 自己做）。
  - **v4.2.2**：宝可梦选秀每一手 15 秒时钟（选秀预算 10 秒以内）；最后一手被服务器替选时，那张卡的完整配置也带进对战。
  - **v4.2.3**：狼人杀新旧两种措辞都认（"lynch" / "eliminate by vote"）、聊天按 1024 字节截断、「多听一会儿」（`ARENA_WW_LISTEN`，默认关）；**合上游 PR #5（event agent 流程）**：`python -m agent --tournament --match`，所有部署工具都跑这条。
- **10-06/07 平台改版（先读 `docs/competition_brief.md` 最上面的「2026-10-07 更新」）**：每人一个 event agent（UCLA Google 登录，Self-hosted / Oracle-hosted 二选一，我们选 Self-hosted），一把 **Official Agent Key**，测试赛（Testing 页面）和正式赛（Tournaments 页面 "Register my agent"）都作为分配自动送来。正式赛 = 瑞士轮 + 淘汰赛（狼人杀是决赛），每局 4 分钟连接窗口（红警 4.5 分钟），没连上判负。旧的 sk key、发现模式、`--claim` 全部停用（410）。
- **部署**（`docs/deploy.md`「比赛日手册」）：`scripts/run_agent.sh`（systemd / tmux / Docker / 压缩包都走它）固定跑 `python -m agent --tournament --match`；key 不对、只有停用的 sk key、`ARENA_MODE=discovery`、`--claim` 都拒绝启动并写明原因。我们在上游运行时之上保留的：启动时平台暂时连不上会退避重试、看门脚本重启后沿用 execution id（马上续上自己的座位）、热备、10 s 查询和冷却、本机计算器 / 引擎搜索检查。重新登录的暂时性失败和按座位退避现在由上游 SDK 自己做；按座位退避的上限我们从上游的 600 s 改成 30 s（10-07 复审：600 s 时，开头几个 worker 登录失败的正式赛座位会错过 4 分钟连接窗口）。重新登录失败时查分配按官方建议暂停 10 / 20 / 40 / 60 s：登录故障持续约 190 s 以上时，故障一开始就出现的新正式赛座位会错过窗口（短故障没事，见 `docs/deploy.md`）。
- **本地验证都过了，但还没在真实平台上跑过一局**（10-06 改版之后）：本地模拟平台（官方控制面，测试赛 / 正式赛两种分配、连接窗口、no-show）上的演练、全量 pytest、真实录像重放等数字见 `docs/progress.md`「v4.2.3 合并」和「v4.2.3 复审修复」两节。部署用 `~/wt/_fix423_out/dist/arena-agent-74defe4.tar.gz` 或新出的包（`_combine_out` 里 ec49587 的旧包还是 600 s 的按座位退避，不要用）。
- **红警**：正式赛项目。main（v4.2.4）上我们没有策略（整场 `WAIT` = 每局认输）。**分支 `ra/bot-v0`（v4.3 + 10-08 复审修复 v4.3.1，已合 v4.2.4，等主人决定合不合进 main）**：主人同意后写的规则型红警 bot（`agent/redalert/`，不调用大模型），本地模拟器上对 4 种脚本对手 144 局赢 141 局（输的 3 局都是龟缩对手、20 分钟按击杀差判负），对复审写的 5 种更强的脚本对手 120 局赢 115 局（弱点是像 OpenRA 普通 AI 的 pro：24 局赢 20 局）；复审后采矿车全灭、电厂全灭、建造厂早早被毁、基地藏在角落这几种局面都能恢复或找到；宝可梦 / 狼人杀不变；`ARENA_RA=off` 一键回到整场 `WAIT`。模拟器不是真游戏，**合进 main 之后先在 Testing 页面自对弈几局**再决定报不报名红警比赛（不报名就不会有红警的局）。正式 agent 从 main 的 checkout 跑、每局的 worker 新起，所以合进去以后下一局红警就是 bot 在打，不用重启；想先合、暂时不打，就先在 `.env` 加 `ARENA_RA=off` 并重启 agent 再合。数字和只有真实对局才能确认的事见 `docs/progress.md`「v4.3」和「v4.3.1」，打法见 `docs/redalert_design.md`。资料：`docs/platform/redalert.md`、`data/red_alert_config.json`。

## 下一步（按顺序；1–5 只能主人本人做，会连真实平台）

1. **登录**：UCLA Google 账号登录 <https://platform.altruagent-game.com/tournament>，把报名做完（问卷、Official Rules、agent 名字、公开代码仓库）。
2. **Agent Configuration 页面**：选 **Self Hosted**，生成 Official Agent Key。
3. **放进比赛主机的 `.env`**：`ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_…`（`chmod 600 .env`；不要贴到聊天、GitHub、截图里；`.env.example` 的占位行删掉）。然后 `.venv/bin/python scripts/selfcheck.py` 要以 `selfcheck: ready` 结尾。
4. **`bash scripts/run_agent.sh check-tournament`**：每一行都要是 ✓。
5. **在比赛主机上常驻**：`bash scripts/run_agent.sh install-systemd && systemctl --user enable --now arena-agent@main && sudo loginctl enable-linger $USER`，`bash scripts/run_agent.sh status` 要显示 `mode=tournament(primary) agent=up calc=ok(...)`。
6. **Testing 页面建测试赛**（宝可梦、狼人杀各至少一局；座位选 "Mine (self-hosted)"，也可以加入 *Open matches* 里别人的座位）：看日志里 `Match assigned: … (Testing)` → `Match finished.`。测试赛在提交截止时关闭，要在 10-13 23:59 PDT 前打完；想提交新录像（只放 `logs/yoriko/`、`logs/apocalypse/` 这类检查过的目录，见 CLAUDE.md）也只能在截止前，截止后录像留在本机。第一局真实对局之后，对照 `docs/real_payloads.md` 看新 payload 有没有变。
7. **Tournaments 页面报名**宝可梦和狼人杀比赛（Register my agent）；红警看主人的决定。
8. **截止前**（最晚 10-13 23:59 PDT）：最终分支快进进 `main` 并推送；公开仓库按规则第 8 节：方案 A（这个仓库直接公开）或方案 C（推荐：这个仓库保持私有，主人自己在 GitHub 建一个新的公开仓库，`bash scripts/publish_sync.sh --commit main --out <公开仓库的 clone> --replace` 结尾是 `READY` 后在那个 clone 里提交、推送，报名页填它的地址）；比赛机部署截止时参赛仓库 `main` 的最新提交、从它重新打包（`docs/deploy.md`「截止和比赛日时间表」第一行）。比赛日（10-16）前一天按「上线前检查清单」和同一张时间表走一遍；问主办方的问题（提交号填在哪、热备、开赛时间、同时几场）见 `docs/competition_brief.md`「2026-10-08 更新」D 节。
9. 之后的增强（v5，按 `docs/v5_standard.md`）：v4 对「按均衡出招」的对手偏弱（simbot −9）；选秀模型用混合对手群体重新拟合；多场并发压力测试（`scripts/stress_concurrency.py`，在比赛机器上 N=8）。

## 背景（较早的事实，仍然成立）

- **2026-09-27 起在新机器上开发**：Windows 11 Pro + WSL2 Ubuntu 24.04，仓库在 `~/ucla-agent-tournament`。从 Windows 侧改 WSL 里的文件会变成 root 所有、`.sh` 会丢可执行位，改完要检查（`git diff --summary`）。
- 打法本身是 v4.1：v4 + 两个真实平台 bug 的修复（平台的 `team` 列出全部 6 只 → 局面重建只用带上场的 4 只；两格回答合起来必须合法），见 `docs/progress.md` 的 v4.1 节。
- **v4_engine_search（09-29）**：v3.2 的选秀和首发不变，对战改成在 Showdown 引擎克隆上的搜索（blend）。对 v3.2 擂台 57.5% / 63.5%（种子 7 / 11），对开源学习型 bot VGC-Bench +8.5 / +15.5 个百分点，对引擎搜索 bot simbot −9（不显著）。需要 pokemon-showdown 模拟器（压缩包自带；checkout 里 `(cd local/js && npm ci)`），`ARENA_BATTLE=classic` 退回 v3.2 打法。详见 `docs/progress.md`。
- 真实平台（旧的发现模式时代）：v2 对 v1 10 局 8 胜 2 负；v3.2 对 v2 20 局 18 胜 2 负；v4.1 对 v3.2 20 局 12 胜。录像在 `logs/yoriko/`、`logs/apocalypse/`。
- 旧的两个平台 agent（Yoriko_Red、Apocalypse_Red，sk key）10-06 起在真实平台上停用（410）。密钥不在 git 里（本机备份在 `~/.config/altruagent/`），只剩本地模拟平台的旧接口用得上。
- 外部对手（本地评测）：`simbot`（引擎搜索）和 `vgcbench_sp` / `vgcbench_bc`（VGC-Bench，装在 D 盘 `/mnt/d/altruagent`，所有者要求大文件不放 C 盘）。
- 旧版本对比需要各自的计算器：`champion/agent/js/node_modules` 和 `versions/*/agent/js/node_modules` 是指向 `agent/js/node_modules` 的符号链接（已 gitignore），新机器上要重建，否则旧版会用新 calc.js，擂台测不准。
- 实验目录（不在 git 里）：`~/exp`（v2）、`~/exp2`（第三轮各方向）、`~/exp3`、`~/exp4`、`~/exp13`（v4）、`~/exp21`（v4.1.2 / v4.2 的审计和补丁）。

## 新机器准备

需要：Git、Python ≥ 3.11、Node ≥ 18、GitHub CLI（`gh`）。Windows 请在 **WSL2 Ubuntu** 里操作。

```bash
gh auth login                                   # 登录能访问私有仓库的 GitHub 账号
gh repo clone Astro-wen/ucla-agent-tournament
cd ucla-agent-tournament
./scripts/setup_machine.sh                      # venv + 依赖 + node 包 + 测试 + 真实重放 + 短擂台
```

真实平台只需要 `.env` 里的 `ALTRUAGENT_OFFICIAL_AGENT_KEY`（权限 600，**不要**经过 GitHub 或聊天传），用 `bash scripts/run_agent.sh check-tournament` 验证（见上面「下一步」和 `docs/deploy.md`）。

## 在新机器上继续用 Claude

安装 Claude Code，在仓库目录里启动会话。`CLAUDE.md` 会被自动读取（规矩、晋级流程、容易错的事实）。开场说一句：

> 读 docs/HANDOFF.md 和 docs/progress.md，从「下一步」继续。

## 注意

- **一把 Official Agent Key 只跑一个运行时**（官方规则）：两个进程会抢同一局的座位，第二个拿到 `seat_busy`。唯一的例外是我们的热备（另一台机器、key 文件里写 `ARENA_ROLE=standby`，只接主进程放掉的座位，见 `docs/deploy.md`），而且要等主办方同意才开。
- 原 Mac 上 `.claude/worktrees/` 里还有第二轮的工作区，内容都已推到 `candidates/r2-*` 分支，可以不管。
- `logs/` 里只有 `yoriko/`、`apocalypse/` 两个目录会进 git；新的真实对局录像检查过（没有密钥）再按同样方式提交。
