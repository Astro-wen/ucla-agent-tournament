# 部署与比赛日运行手册

比赛日（10/16）的模式：**我们的进程必须一直在线**，平台把对局分配给 agent，`python -m agent` 自动发现并开打。进程挂了 = 该局被平台随机代打 = 基本等于输。

## 机器要求

- Linux/macOS，Python ≥ 3.11，**Node ≥ 18**（精确伤害计算 `agent/js/calc.js` 依赖它；没有 node 时 agent 会自动退回启发式，能打但更弱）
- 稳定网络（MCP 长轮询，每步 ~80 ms 往返）
- 不要用笔记本睡眠/合盖；用一台云主机（OCI/Nebius 若给额度就用它们）

## 一次性准备

```bash
git clone https://github.com/Astro-wen/ucla-agent-tournament.git && cd ucla-agent-tournament
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
(cd agent/js && npm install)          # 伤害计算
(cd local/js && npm install)          # 只有本地自测需要
cp .env.example .env                  # 填 ALTRUAGENT_API_KEY（主 agent Yoriko_Red）
python scripts/check_connection.py    # 必须显示 status=claimed
python -m pytest -q                   # 全绿
```

## 运行

```bash
# 前台试跑
python -m agent

# 比赛日：tmux 常驻 + 崩溃自动重启
tmux new -d -s arena 'cd ~/ucla-agent-tournament && source .venv/bin/activate && while true; do python -m agent 2>&1 | tee -a logs/agent.$(date +%F).out; echo "restart $(date)"; sleep 3; done'
tmux attach -t arena      # 看日志；Ctrl-b d 退出
```

systemd 版本（Linux）：

```ini
[Unit]
Description=AltruAgent tournament agent
After=network-online.target

[Service]
WorkingDirectory=/home/USER/ucla-agent-tournament
ExecStart=/home/USER/ucla-agent-tournament/.venv/bin/python -m agent
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

重启后进程会重新登录、重新发现进行中的对局并继续（平台按 agent_id 认身份，不按 token）。

## 加入锦标赛

`python -m agent` **不会自动加入锦标赛**，它只打已分配的对局。如果主办方不替我们入座：

```bash
python -c "
from altruagent import AltruAgentClient; c = AltruAgentClient.from_env(); print(c.join_tournament('<tournament_id>'))"
```

（确切的 SDK 调用以 `altruagent/client.py` 的 `join_tournament` 为准。）之后 `python -m agent` 会看到子对局并自动开打。

## 每局都在录

`agent/agent.py` 把每次决策的原始局面写到 `logs/<session_id>.jsonl`（可用 `ARENA_RECORD_DIR` 改目录）。比赛后 `python -m local.pool --harvest` 把新见到的卡并入 `data/vgc_cards.json`。

## 上线前检查清单

- [ ] `git pull` 到冻结的 tag；`python -m pytest -q` 全绿
- [ ] `python -m local.ladder --games 200` 确认当前 `agent/` 不弱于 `champion/`
- [ ] `python scripts/check_connection.py` → claimed
- [ ] node 可用：`node agent/js/calc.js < /dev/null` 不报错
- [ ] tmux/systemd 已配好自动重启；磁盘有空间放 logs/
- [ ] 主办方回复：需要自己 join 锦标赛吗？几点开始？
