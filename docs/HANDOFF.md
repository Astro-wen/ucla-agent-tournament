# 交接：在另一台机器上继续（2026-09-27）

## 现在到哪了

- **冠军 v1（main / `champion/`）**：精确伤害计算选招。本地对 v0 胜率 66.8%（400 局），真实平台对 v0 3 胜 2 负，伤害计算器对真实对局 154 次命中 149 次在预测区间内。
- **两个 agent 已注册并认领**：Yoriko_Red（主力）、Apocalypse_Red（陪练）。密钥不在 git 里。
- **第二轮候选在 `candidates/r2-*` 分支**（见 `docs/progress.md`）：前瞻搜索、狼人杀预言家/村民已完成待复核；速度与守住、首发、选秀、换人残局是半成品。

## 下一步（按优先级）

1. **复核并合并第二轮**：对每个 `candidates/r2-*` 分支跑擂台（`local.ladder`，CI 下限 > 50%）和真实重放（0 失败），过关的依次合入 main、冻结新冠军。半成品先补完再测。
2. **计算器小漏洞**：满血气势披带/结实时单次命中的击杀概率应为 0；Kingambit「大将」需要把己方倒下数传给计算器。
3. **真实平台 A/B**：新冠军 vs 旧冠军打几局（`ARENA_DIR_APOCALYPSE=<旧版目录> python scripts/sparring.py play latest`，房间在平台 Human dashboard 创建）。
4. **狼人杀**：本地评测显示单席无杠杆，价值在于发言说服（需要大模型，Nebius AI Builder 额度可用）。
5. **比赛日部署**（`docs/deploy.md`）：一台稳定在线的机器，tmux/systemd 自动重启，确认是否要手动加入锦标赛。
6. **问主办方**（`docs/intel.md` 末尾的问题清单）。

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
