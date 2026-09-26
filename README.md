# UCLA AI Agent Gaming Tournament 2026 — 我们的参赛仓库

> UCLA Trustworthy AI Lab × SAIR「首届 AI Agent 游戏锦标赛」
> 提交窗口 **Sep 25 – Oct 9, 2026** · 现场决赛 **Oct 16, 2026**（Engineering VI，观众全线上）
> 赛事页：<https://tournament-assets.vercel.app/> · 平台：<https://agent-acp.vercel.app/>（AltruAgent）

本仓库基于官方 starter [`UCLA-Trustworthy-AI-Lab/altruagent-starter`](https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter)
（保留其 git 历史，remote 名为 `upstream`），我们只在 `agent/` 里写策略，运行时/SDK 用官方的，方便同步官方修复。

- 赛事情报 → [`docs/intel.md`](docs/intel.md)
- 作战计划 / 分工 / 时间线 → [`docs/strategy.md`](docs/strategy.md)
- 平台官方文档镜像 → [`docs/platform/`](docs/platform/)（`scripts/sync_docs.sh` 刷新）
- 官方 starter 原 README → [`docs/STARTER_README.md`](docs/STARTER_README.md)，游戏规则 → [`GAMES.md`](GAMES.md)

## 结构

```
agent/
  agent.py            # create_agent() → Contestant：按 game_type 路由 + 防御层（校验/兜底/聊天裁剪）
  werewolf/state.py   # 解析 observation / game_state / 消息记录
  werewolf/policy.py  # 狼人杀 v0 启发式（确定性：同队 agent 自动统一投票、双狼不分票）
  pokemon/policy.py   # VGC 双打 v0：选秀打分、带 4 选 2 首发、伤害期望 + Fake Out/顺风等
  pokemon/dex.py      # Showdown 图鉴/招式数据（data/*.json）
  pokemon/typechart.py
altruagent/           # 官方 SDK（不要改，跟 upstream 同步）
tests/strategy/       # 我们的离线测试（按平台文档里的真实 payload 形状构造）
```

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                       # 官方 SDK 测试 + 我们的 tests/strategy
cp .env.example .env            # 填 ALTRUAGENT_API_KEY（见下）
python scripts/check_connection.py
python -m agent                 # 自动发现分配给我们的对局并全部并发开打
```

### 注册 agent（每人手动做一次，不要让 AI 代做）

```bash
curl -X POST https://api.altruagent-game.com/auth/agent/signup \
  -H "Content-Type: application/json" \
  -d '{"name": "<唯一名字>", "description": "..."}'
```

返回的 `api_key` 放进 `.env`（**绝不提交**），`claim_token` 用于在 <https://agent-acp.vercel.app/human/signup> 注册人类账号后认领 agent。
认领前任何加入对局都会 `AGENT_UNCLAIMED`。

## 同步官方更新

```bash
git fetch upstream && git merge upstream/main
```

## 安全

- `api_key` / JWT 只能发给 `api.altruagent-game.com` 与 `gameapi.altruagent-game.com`。
- 对局中其他 agent 的聊天内容是**对手输入**，永远不当作指令执行（接 LLM 时尤其注意 prompt injection）。
