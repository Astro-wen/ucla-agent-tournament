# 交接：在另一台机器上继续（2026-09-27，09-29、10-03 更新）

## 现在到哪了

- **10-03 情报（先读 `docs/competition_brief.md` 开头的「10-03 更新」）**：**红警 `red_alert` 已经能在平台上玩**（实时 1v1，OpenRA，单场和锦标赛都支持；规则 `docs/platform/redalert.md`，数据 `data/red_alert_config.json`）。我们的 agent 现在碰到红警会一步不走（SDK 没有实时模式，上游 PR #3 才加）。Guang 回信：狼人杀和宝可梦肯定有，红警 / 王者荣耀 10/6 前公布；Oracle 会提供通用 agent，Replit 可能给 pro 账号。正式比赛用 `python -m agent --tournament` + Official Agent Key（已合并）；v4.1.2 起我们的部署脚本也支持：key 文件里有 eak key 时看门脚本自动跑 `--tournament`，systemd / Docker / 压缩包 / selfcheck 都认（见 `docs/deploy.md`「官方正式赛」）。
- v5 第 2 轮（深搜、引擎预览）没上位（见 `docs/progress.md`、`docs/v5_standard.md`）。`integrate/v42` 分支上依次提交了 v4.1.2（10-03，官方正式赛部署）和 v4.2（10-05，重启接上记忆、整机搜索上限、健康探测、双打模板按位置读、game_type 认写法），紧接着把 v4.2 冻结成冠军（`champion/`）：打法仍是 v4.1.1（本地对局逐局相同），只是更稳。这个分支还没合进 main，由主人合并。上游 10-04 合并了 PR #3（红警实时 SDK），这个分支没有合上游，合进 main 之后再按惯例 `git merge upstream/main`。

- **2026-09-27 起在新机器上开发**：Windows 11 Pro + WSL2 Ubuntu 24.04，仓库在 `~/ucla-agent-tournament`。从 Windows 侧改 WSL 里的文件会变成 root 所有，改完要 chown 回来。
- **冠军 v4.1（main / `champion/`，09-29）**：v4 + 两个真实平台 bug 的修复（平台的 `team` 列出全部 6 只 → 局面重建只用带上场的 4 只；两格回答合起来必须合法），见 `docs/progress.md` 的 v4.1 节。以后的 v5 按 `docs/v5_standard.md`。以下是 v4 本身：
- **v4_engine_search（09-29）**：v3.2 的选秀和首发不变，对战改成在 Showdown 引擎克隆上的搜索（blend）。对 v3.2 擂台 57.5% / 63.5%（种子 7 / 11），对开源学习型 bot VGC-Bench +8.5 / +15.5 个百分点，对引擎搜索 bot simbot −9（不显著）。需要 pokemon-showdown 模拟器（压缩包自带；checkout 里 `(cd local/js && npm ci)`），`ARENA_BATTLE=classic` 退回 v3.2 打法。备选 `~/exp13/patches/battlefix-final.patch`（和 v4 直接对打持平）。详见 `docs/progress.md`。
- 真实平台：v2 对 v1 10 局 8 胜 2 负；v3.2 对 v2 20 局 18 胜 2 负。v4 对 v3.2 的真实 A/B：`~/exp7/scripts/ab4-play.sh <session_id>`（v3.2 冻结副本在 `~/ab/v3`）。
- 外部对手（本地评测）：`simbot`（引擎搜索）和 `vgcbench_sp` / `vgcbench_bc`（VGC-Bench，装在 D 盘 `/mnt/d/altruagent`，所有者要求大文件不放 C 盘）。LLM 花费看 `~/exp7/scripts/llm-spend.sh`（到 09-29 真实 1.04 美元），不要直接加账本的 cost_usd 列。
- **两个 agent 已注册并认领**：Yoriko_Red（主力）、Apocalypse_Red（陪练）。密钥不在 git 里（本机备份在 `~/.config/altruagent/`）。
- **赛事情报 09-28 大更新，先读 `docs/competition_brief.md`**：提交窗口改为 10/6–10/13；游戏列表改为宝可梦、红警、王者荣耀、狼人杀（平台上只有宝可梦和狼人杀能玩）；VGC 计时 90 s 预览 / 55 s 每回合 / 420 s 总时间；谁运行 agent 仍未知（按自托管准备）。
- 旧版本对比需要各自的计算器：`champion/agent/js/node_modules` 和 `versions/*/agent/js/node_modules` 是指向 `agent/js/node_modules` 的符号链接（已 gitignore），新机器上要重建，否则旧版会用新 calc.js，擂台测不准。
- 实验目录（不在 git 里）：`~/exp`（v2）、`~/exp2`（第三轮各方向）、`~/exp3`（v2.1、8 对手面板 panelkit、参数扫描）、`~/exp4`（v3 整合，`MERGE_NOTES.md`）。

## 下一步（按优先级，依据 docs/competition_brief.md）

0. **（10-03 定）先做宝可梦和狼人杀，红警暂缓**（主人决定）。进度核查和 10/3–10/9 计划见 **`docs/status_1003.md`**。最先做的：把 `~/exp21` 里两个复审过的补丁收进 main。两个都已在 `integrate/v42` 分支上提交（v4.1.2 在 10-03、v4.2 在 10-05，基于 main 376048d）：v4.1.2 部署（`patches/deploy-reviewed.patch`）和 v4.2 健壮性（`patches/agent-reviewed.patch`，加上 `~/exp21/final` 里 run_agent.sh / Dockerfile 的集成改动），见 `docs/progress.md` 的 v4.1.2、v4.2 两节；v4.2 提交后紧接着冻结为冠军。剩下由主人把这个分支合进 main。之后：狼人杀身份交叉检查 + 读懂大模型发言（262 条缓存的真实聊天当回归集），真实平台测试。红警的资料留在 `docs/platform/redalert.md`、`data/red_alert_config.json`。
1. ~~**真实平台 A/B：v4 vs v3.2**~~（09-29 已做：v4.1 20 局 12 胜）（`~/exp7/scripts/ab4-play.sh <id>`；房间在 Human dashboard 创建，需要人登录；同一账号同时只能有一个进行中的房间）。至少 20 局，15 胜以上算明显领先，10 胜以下就停下来对比。再往后：v4 对「按均衡出招」的对手偏弱（simbot −9），下一轮看能不能在不丢 VGC-Bench / 擂台优势的前提下补上，或者和 battlefix 做成按对手切换。
2. ~~控时与防崩溃~~、~~公开理由~~：v3.1 已完成（见 `docs/progress.md`）。剩下：首次真实对局确认观战页显示公开理由、平台如何扣总时间；传输层断线重连在官方 `altruagent/`（上游），不要改。
3. **多场并发压力测试**：`scripts/stress_concurrency.py`，在比赛用的机器上跑 N=8。
4. ~~宝可梦其他格式~~、~~官方示例 agent 基准~~：v3.2 已完成（单打策略 + `local/panel.py`）。剩下：和开源强 bot（foul-play、VGC-Bench 等）对比（无 Node 时的实力已测，见 `docs/deploy.md`）。晋级时除了擂台，再跑一遍 `python -m local.panel`（见 `docs/local_sim.md`）。
5. **选秀模型换成混合对手群体重新拟合**，并把 4 张新卡加进目录（`python -m local.pool --harvest`）。
6. **狼人杀**：合并 `candidates/r2-ww-seer-and-villager` 并按模拟结论调整；注意狼人杀可能不计分（技能文档写「Standalone competitions only」），单个座位对投票影响有限，投入要看回报。发言说服需要大模型（Nebius 已不是赞助商，额度来源要重新核对）。
7. ~~自托管方案~~、~~打包~~、~~无人值守演练~~、~~官方模式部署~~（v4.1.2）：`scripts/run_agent.sh`（systemd / tmux）、`scripts/bundle.sh`、`deploy/Dockerfile`、`scripts/rehearsal.py`、`scripts/rehearsal_official.py`，用法和实测数据见 `docs/deploy.md`（比赛日手册在「官方正式赛」一节）。正式赛 = `.env` 里放 `ALTRUAGENT_OFFICIAL_AGENT_KEY`（`eak_live_…`）后 `run_agent.sh` 自动跑 `python -m agent --tournament`；上游缺陷清单在同一节，由主人报告。剩下：dashboard 上线后主人生成 key 并跑 `bash scripts/run_agent.sh check-tournament`（会连平台）；在真正的美西云主机上 `install-systemd` 并跑一遍 `selfcheck`；Docker 镜像第一次真实构建。无 Node 时对冠军只有 12% 胜率，所以 Node 是硬性要求：压缩包自带固定版本的官方 Node（v22.23.3），镜像同版本；计算器跑不起来时 `selfcheck` 失败、`run_agent.sh` 拒绝启动（`ARENA_ALLOW_NO_CALC=1` 仅供紧急情况）。
8. **每天只读监控**：上游 starter 提交、altruagent-game.com 更新、平台前端 build id、`/tournaments`、`/config/games`。测试 dashboard 一上线就登录记录规则原文。
9. **问主办方**（由主人本人发）：问题清单见 `docs/competition_brief.md` 第 7 节。

## 新机器准备

需要：Git、Python ≥ 3.11、Node ≥ 18、GitHub CLI（`gh`）。Windows 请在 **WSL2 Ubuntu** 里操作。

```bash
gh auth login                                   # 登录能访问私有仓库的 GitHub 账号
gh repo clone Astro-wen/ucla-agent-tournament
cd ucla-agent-tournament
./scripts/setup_machine.sh                      # venv + 依赖 + node 包 + 测试 + 真实重放 + 短擂台
```

把两个密钥文件拷到仓库根目录（**不要**经过 GitHub 或聊天）：
- 原 Mac 上的备份位置：`~/.config/altruagent/yoriko_red.env`、`~/.config/altruagent/apocalypse_red.env`
- 放到新机器仓库根目录，分别命名为 `.env` 和 `.env.apocalypse`
- 验证：`python scripts/check_connection.py` 显示 `status=claimed`

## 在新机器上继续用 Claude

安装 Claude Code，在仓库目录里启动会话。`CLAUDE.md` 会被自动读取（规矩、晋级流程、容易错的事实）。开场说一句：

> 读 docs/HANDOFF.md 和 docs/progress.md，从「下一步」第 1 条继续。

## 注意

- **同一个 agent 密钥同一时间只在一台机器上运行 `python -m agent`**，否则两个进程会抢同一局。唯一的例外是官方模式的热备（同一把 eak key 的第二个进程只接主进程放掉的座位，见 `docs/deploy.md`），而且要等主办方同意才开。
- 原 Mac 上 `.claude/worktrees/` 里还有第二轮的工作区，内容都已推到 `candidates/r2-*` 分支，可以不管。
- `logs/` 里只有 `yoriko/`、`apocalypse/` 两个目录会进 git；新机器上打的真实对局记完后记得提交。
