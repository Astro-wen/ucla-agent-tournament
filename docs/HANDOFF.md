# 交接：在另一台机器上继续（2026-09-27）

## 现在到哪了

- **2026-09-27 起在新机器上开发**：Windows 11 Pro + WSL2 Ubuntu 24.04，仓库在 `~/ucla-agent-tournament`。从 Windows 侧改 WSL 里的文件会变成 root 所有，改完要 chown 回来。
- **冠军 v2（main / `champion/`，09-28）**：v1 + 双打一回合联合前瞻（sm-k6 参数）。对 v1 1200 局 60.1%，8 对手面板平均 70–72%。详见 `docs/progress.md`。
- **两个 agent 已注册并认领**：Yoriko_Red（主力）、Apocalypse_Red（陪练）。密钥不在 git 里（本机备份在 `~/.config/altruagent/`）。
- **第三轮（选秀模型、90×90 首发博弈、速度与守住、计算器修正）本地都过了擂台，正在整合成 v3**，工作目录在本机 `~/exp4`（`~/exp2/patches/` 是各方向补丁）。这些实验目录不在 git 里。
- **比赛日由主办方的服务器运行我们交上去的 agent**（不是我们自己挂机），提交形式待确认。

## 下一步（按优先级）

1. **把 v3 合进 main**：整合完成后按 `~/exp4/MERGE_NOTES.md` 落地，默认参数保持 sm-k6，冻结为新冠军并存档 `versions/`。
2. **真实平台 A/B**：新冠军 vs 旧冠军（`ARENA_DIR_APOCALYPSE=<旧版目录> python scripts/sparring.py play latest`，房间在 Human dashboard 创建，同一账号同时只能有一个进行中的房间）。
3. **计算器健壮性**：`DamageCalc.batch` 一次往返失败就永久关闭计算器，应改为自动重启；缓存清理顺序（v3 已修）。
4. **狼人杀**：合并 `candidates/r2-ww-seer-and-villager`；发言说服需要大模型（Nebius 学生认证审核中）。
5. **提交打包**：主办方运行我们的 agent，确认他们要的格式（代码仓库 / 容器 / 入口命令）、是否有 Node、能否调用外部 API。
6. **官方模板更新**：`upstream`（UCLA-Trustworthy-AI-Lab/altruagent-starter）有新提交，`git fetch upstream && git merge upstream/main` 后跑全套检查。

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
