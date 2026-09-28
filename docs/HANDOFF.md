# 交接：在另一台机器上继续（2026-09-27，09-28 更新）

## 现在到哪了

- **2026-09-27 起在新机器上开发**：Windows 11 Pro + WSL2 Ubuntu 24.04，仓库在 `~/ucla-agent-tournament`。从 Windows 侧改 WSL 里的文件会变成 root 所有，改完要 chown 回来。
- **冠军 v3（main / `champion/`，09-28）**：v2.1 前瞻 + 计算器修正 + 速度 / 守住 + 90×90 首发博弈 + 模型化选秀。对 v2 冠军 86.0% / 85.0%（种子 7 / 11），对 v1 90.8%，8 对手面板 92.9–94.3%。**尚无真实平台对局**。v2 对 v1 真实平台 10 局 8 胜 2 负。详见 `docs/progress.md`。
- **两个 agent 已注册并认领**：Yoriko_Red（主力）、Apocalypse_Red（陪练）。密钥不在 git 里（本机备份在 `~/.config/altruagent/`）。
- **赛事情报 09-28 大更新，先读 `docs/competition_brief.md`**：提交窗口改为 10/6–10/13；游戏列表改为宝可梦、红警、王者荣耀、狼人杀（平台上只有宝可梦和狼人杀能玩）；VGC 计时 90 s 预览 / 55 s 每回合 / 420 s 总时间；谁运行 agent 仍未知（按自托管准备）。
- 旧版本对比需要各自的计算器：`champion/agent/js/node_modules` 和 `versions/*/agent/js/node_modules` 是指向 `agent/js/node_modules` 的符号链接（已 gitignore），新机器上要重建，否则旧版会用新 calc.js，擂台测不准。
- 实验目录（不在 git 里）：`~/exp`（v2）、`~/exp2`（第三轮各方向）、`~/exp3`（v2.1、8 对手面板 panelkit、参数扫描）、`~/exp4`（v3 整合，`MERGE_NOTES.md`）。

## 下一步（按优先级，依据 docs/competition_brief.md）

1. **真实平台 A/B：v3 vs v2**（`ARENA_DIR_APOCALYPSE=~/ab/v2 python scripts/sparring.py play <id>`；房间在 Human dashboard 创建，需要人登录；同一账号同时只能有一个进行中的房间）。至少 20 局，15 胜以上算明显领先，10 胜以下就停下来对比真实卡池和本地卡池。
2. **送分题：控时与防崩溃**：按真实计时（55 s / 420 s / 90 s）给每步设硬预算并在总时间紧张时降级；未知游戏 / 阶段 / 动作模板只记日志、安全出手，不进入「崩溃 → 冷却」循环；断线重连；`DamageCalc` 一次失败就永久关闭的问题改为自动重启；多场同时进行时的压力测试。
3. **公开理由**：每步附简短可读的理由（会显示给观众），绝不写底牌。
4. **宝可梦其他格式冒烟测试**：官方演示是 Gen 9 OU 公开选秀单打；单打 / teambuild 路径、无 Node 时的实力都要测。和官方示例 agent（basic / smoke / llm_agent）以及开源强 bot 做基准。
5. **选秀模型换成混合对手群体重新拟合**，并把 4 张新卡加进目录（`python -m local.pool --harvest`）。
6. **狼人杀**：合并 `candidates/r2-ww-seer-and-villager` 并按模拟结论调整；注意狼人杀可能不计分（技能文档写「Standalone competitions only」），单个座位对投票影响有限，投入要看回报。发言说服需要大模型（Nebius 已不是赞助商，额度来源要重新核对）。
7. **自托管方案**：美西云主机（平台在 AWS us-west-2）、自动重启、装 Node、无人值守演练数小时；同时保证代码可打包、不依赖 LLM 也满实力。
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

- **同一个 agent 密钥同一时间只在一台机器上运行 `python -m agent`**，否则两个进程会抢同一局。
- 原 Mac 上 `.claude/worktrees/` 里还有第二轮的工作区，内容都已推到 `candidates/r2-*` 分支，可以不管。
- `logs/` 里只有 `yoriko/`、`apocalypse/` 两个目录会进 git；新机器上打的真实对局记完后记得提交。
