# 交接：在另一台机器上继续（2026-09-27 起，最近一次 10-07 更新）

## 现在到哪了（10-07）

- **冠军 v4.2.3**（`champion/VERSION` 是冻结时的提交，分支 `integrate/v423`，主人快进合进 main）。宝可梦打法和 v4.1.1 逐局相同（本地对冠军 20 局逐局核对过），多出来的都是稳定性和平台适配：
  - v4.1.2 / v4.2：官方正式赛部署、重启后接上记忆、慢机器和内存下的自我保护（`docs/progress.md` 同名两节）。
  - 狼人杀身份核对 + 读懂大模型的跳预言家（`ww/reading`，10-03）。
  - **v4.2.1**：合上游的实时 SDK（红警），被分到红警时整场回答 `WAIT`（不发命令、不崩）；补丁：重新登录时平台签发会话失败不再停运行时（这一条现在由上游 SDK 自己做）。
  - **v4.2.2**：宝可梦选秀每一手 15 秒时钟（选秀预算 10 秒以内）；最后一手被服务器替选时，那张卡的完整配置也带进对战。
  - **v4.2.3**：狼人杀新旧两种措辞都认（"lynch" / "eliminate by vote"）、聊天按 1024 字节截断、「多听一会儿」（`ARENA_WW_LISTEN`，默认关）；**合上游 PR #5（event agent 流程）**：`python -m agent --tournament --match`，所有部署工具都跑这条。
- **10-06/07 平台改版（先读 `docs/competition_brief.md` 最上面的「2026-10-07 更新」）**：每人一个 event agent（UCLA Google 登录，Self-hosted / Oracle-hosted 二选一，我们选 Self-hosted），一把 **Official Agent Key**，测试赛（Testing 页面）和正式赛（Tournaments 页面 "Register my agent"）都作为分配自动送来。正式赛 = 瑞士轮 + 淘汰赛（狼人杀是决赛），每局 4 分钟连接窗口（红警 4.5 分钟），没连上判负。旧的 sk key、发现模式、`--claim` 全部停用（410）。
- **部署**（`docs/deploy.md`「比赛日手册」）：`scripts/run_agent.sh`（systemd / tmux / Docker / 压缩包都走它）固定跑 `python -m agent --tournament --match`；key 不对、只有停用的 sk key、`ARENA_MODE=discovery`、`--claim` 都拒绝启动并写明原因。我们在上游运行时之上保留的：启动时平台暂时连不上会退避重试、看门脚本重启后沿用 execution id（马上续上自己的座位）、热备、10 s 查询和冷却、本机计算器 / 引擎搜索检查。重新登录的暂时性失败和按座位退避现在由上游 SDK 自己做；按座位退避的上限我们从上游的 600 s 改成 30 s（10-07 复审：600 s 时，开头几个 worker 登录失败的正式赛座位会错过 4 分钟连接窗口）。重新登录失败时查分配按官方建议暂停 10 / 20 / 40 / 60 s：登录故障持续约 190 s 以上时，故障一开始就出现的新正式赛座位会错过窗口（短故障没事，见 `docs/deploy.md`）。
- **本地验证都过了，但还没在真实平台上跑过一局**（10-06 改版之后）：本地模拟平台（官方控制面，测试赛 / 正式赛两种分配、连接窗口、no-show）上的演练、全量 pytest、真实录像重放等数字见 `docs/progress.md`「v4.2.3 合并」和「v4.2.3 复审修复」两节。部署用 `~/wt/_fix423_out/dist/arena-agent-74defe4.tar.gz` 或新出的包（`_combine_out` 里 ec49587 的旧包还是 600 s 的按座位退避，不要用）。
- **红警**：正式赛项目，我们没有策略（整场 `WAIT` = 每局认输）。要不要报名红警比赛、要不要现在写一个红警 bot，**等主人决定**。不报名就不会有红警的局。资料：`docs/platform/redalert.md`、`data/red_alert_config.json`。

## 下一步（按顺序；1–5 只能主人本人做，会连真实平台）

1. **登录**：UCLA Google 账号登录 <https://platform.altruagent-game.com/tournament>，把报名做完（问卷、Official Rules、agent 名字、公开代码仓库）。
2. **Agent Configuration 页面**：选 **Self Hosted**，生成 Official Agent Key。
3. **放进比赛主机的 `.env`**：`ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_…`（`chmod 600 .env`；不要贴到聊天、GitHub、截图里；`.env.example` 的占位行删掉）。然后 `.venv/bin/python scripts/selfcheck.py` 要以 `selfcheck: ready` 结尾。
4. **`bash scripts/run_agent.sh check-tournament`**：每一行都要是 ✓。
5. **在比赛主机上常驻**：`bash scripts/run_agent.sh install-systemd && systemctl --user enable --now arena-agent@main && sudo loginctl enable-linger $USER`，`bash scripts/run_agent.sh status` 要显示 `mode=tournament(primary) agent=up calc=ok(...)`。
6. **Testing 页面建测试赛**（宝可梦、狼人杀各至少一局；座位选 "Mine (self-hosted)"，也可以加入 *Open matches* 里别人的座位）：看日志里 `Match assigned: … (Testing)` → `Match finished.`，打完 `git` 提交新录像（只放 `logs/yoriko/`、`logs/apocalypse/` 这类检查过的目录，见 CLAUDE.md）。第一局真实对局之后，对照 `docs/real_payloads.md` 看新 payload 有没有变。
7. **Tournaments 页面报名**宝可梦和狼人杀比赛（Register my agent）；红警看主人的决定。
8. 比赛日（10-16）前一天按 `docs/deploy.md` 的「上线前检查清单」和「比赛日时间表」走一遍；问主办方的问题（热备是否允许、开赛时间、同时几场）见 `docs/competition_brief.md`。
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
