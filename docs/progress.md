# 版本进化记录

每个版本上位前都必须：擂台赢当前冠军（`local.ladder`，95% 置信区间下限 > 50%）、重放全部真实平台决策 0 失败（`scripts/replay_real.py`）、全部测试通过。历史版本存档在 `versions/`，`python -m local.benchmark` 会让新版打遍所有历史版本和参照对手。

| 版本 | 日期 | 核心改动 | 对上一冠军 | 对官方 baseline | 对随机 |
|---|---|---|---|---|---|
| v0_heuristic | 09-26 | 属性克制 + 威力估算 + Fake Out/顺风等手写加分 | — | 97.7% | 97.3% |
| v1_exact_damage | 09-26 | Smogon 精确伤害计算（每回合一次批量）、按对手当前血量算击杀概率、两槽联合选招；修正对手血量百分比导致「多重鳞片」误判 | **66.8%**（400 局，CI 62–71%） | 99.5% | 99.0% |

真实平台：v0 vs v0 已打 5 局（1 局单打 + 4 局 VGC 双打），216 个真实决策全部可重放。

## 第二轮候选（2026-09-27，未审查，未合入 main）

第二轮被会话中断打断，6 个方向的代码都已推到单独分支，**均未经过擂台 + 审查验证，不要直接部署**：

| 分支 | 方向 | 状态 |
|---|---|---|
| `candidates/r2-joint-lookahead` | 一回合联合前瞻（收益矩阵） | 已完成提交，待擂台复核 |
| `candidates/r2-ww-seer-and-villager` | 狼人杀：预言家避开当天会被投出的人、查到狼立刻跳；村民跟随 | 已完成提交，待复核 |
| `candidates/r2-speed-and-protect` | 速度线 / 击杀竞赛 / 守住 | 半成品（WIP） |
| `candidates/r2-team-preview` | 6×6 精确对局矩阵选 4 带 2 | 半成品（WIP） |
| `candidates/r2-draft` | 基于卡牌目录的选秀对局表 | 半成品（WIP，早期） |
| `candidates/r2-switching-endgame` | 主动换人、U-turn 类、残局风险 | 半成品（WIP） |

真实对局记录：`logs/yoriko/`、`logs/apocalypse/`（10 局，已确认不含密钥）现在也在仓库里，`scripts/replay_real.py` 和 `scripts/calibrate_calc.py` 在任何机器上都能直接用。
