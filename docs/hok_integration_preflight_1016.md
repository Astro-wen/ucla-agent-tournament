# HoK 本地合并预检（2026-10-16）

## 06:15 后明确批准的设计与修复

以下更新取代本文件后半部分 05:20 历史检查点的未决状态；历史失败记录不删除。
仍然只改本地整合分支，没有更新 main、推送、部署或接触生产进程/凭据。

1. **满血但明显以少打多也撤退**：保留 `tests/test_hok_team.py` 原满血三敌撤退
   断言，`rule_macro` 仅把已知人数劣势移出 HURT 条件；塔邻近单独触发仍要求受伤。
   增加满血/受伤塔边界、只有多一敌人、队友靠近、敌人隐藏/死亡/距离过远的回归测试。
   这是明确批准的保守安全选择，不是实战强度结论。
2. **保持规则仍采用强化版**：测试先真实发出仍可用的技能，再断言 0.6 秒前保持、
   之后推进及紧急撤退立即绕过保持；另测技能消失立即换令、首次看到的服务端旧令
   不能凭空获得“刚发出”的年龄。没有改 HOLD_S 或调度规则。
3. **拒绝重试仍为 0.4 秒且需要新状态**：旧测试改为 RETRY_S 前、到期但原拒绝
   状态、到期且新状态三条明确边界断言。没有改 RETRY_S、公平调度或旧状态屏蔽。
4. 精确挑入三个独立测试提交，没有再次合入其已经继承的策略祖先：
   `435e8e3 → 6e83981`、`ac502ccd → a41d677`、`b698548c → 0c5a5b5`。
   新增的独立回归断言和 fake-process 文件保持来源提交逐字节相同。
5. **双重异常的最后兜底**：战术决策和主纯兜底同时失败时，不再让通用 safe_action
   选择不待动的 0 号英雄。独立末级选择只使用当前我方待动座位，复用有界解析和
   raw 参数补全；找不到合法待动动作时不编造其他英雄动作。补测两阵营、无动作/
   只有宏/需填 raw 参数等边界；原独立 `pending=[4]` 断言不变。
6. **录制异常隔离**：即使异常来自 recorder 的普通 I/O handler 之外，两种节奏下
   都保留已决定动作；已有采样限流不变。R5 所怀疑的 `_fresh` 未重置 refusal flag
   不成立：原代码已有重置，未重复修改，只加回归确认。

### 环境、隔离和当前验证

- `agent/js`、`local/js` 的 package-lock 与 `/tmp/altruagent-loss-fixes-1016`
  逐字节相同后，复用其已安装包；本地 `node_modules/` 是 git 忽略目录，里面仅为
  指向既有包的链接，没有改锁文件、依赖版本或父目录文件。
- Node 存在后的 `tests/strategy/test_draft_meta.py tests/test_hok.py`：
  **31 passed**。之前的两个 draft-meta 失败均不再出现，未改其他游戏算法或测试。
- 当前策略、旧团队、独立 runner/进程测试：**112 passed**。
- 新增边界保护、策略安全、旧团队测试：**137 passed**。
- 严格 CI flake8 为 **0**，`git diff --check` 通过。
- 全量带 Node 的 pytest 正在验证，最终结果另列于此处；不能预报全绿。

当前隔离审计仍要求非 HoK agent **44 文件**、SDK **17 文件**、main 已有 data/local
及 draft-meta 测试 **75 文件**、champion/versions **157 文件**、官方示例等
**12 文件**与更新 main 完全相同。HoK policy 相对强化版父提交只有 `rule_macro`
函数 AST 有明确批准的改变；其他策略函数、保持/重试/调度不变。

---

## 05:20 历史检查点（下述失败已按上面的明确决定处理）

## 范围与状态

仅在 `integrate/hok-main-preflight-1016` 整合：

- 强化版父提交：`e13bf140e5bff9284b51164d7dc488762ba50b29`。
- 合入的已存在本地远端跟踪提交：`fb421a2562c8480add765889ddf11c19cbc926ba`，
  包含独立 HoK 实现 `8f507fd` 和官方上游 `686e756`。
- 没有 fetch、更新 main、推送、部署、读取密钥或调用真实游戏/模型。
- **这不是可推广版本：三个策略分歧仍由原断言报错，等待明确决定。**
  没有删除、跳过或 xfail 这些断言，也没有更改强化版 `agent/hok/policy.py`。

## 冲突处理

1. `agent/hok/__init__.py`：保留强化版的轻量导出，实际策略仍在 `policy.py`。
2. `agent/agent.py`：保留明确名称路由、只对当前待动座位做纯兜底、50 个游戏步
   （10 游戏秒）采样、首次异常/拒绝采样及重复故障限流。清除自动合并留下的重复
   `HOK_ENV` / `hok_enabled`。保留独立实现有用的 `fallbacks=` 对局摘要计数，
   增加正常与异常两条测试断言；不是打法修改。
3. `GAMES.md`：以 04:12 PDT 读到的公开指南修正旧“未开放”推断，保留上游的
   MCP-only、拒绝回调与 LLM 示例说明。公开指南不等于已经能开对局。
4. `docs/progress.md`：两条开发历史都保留，明确旧记录不代表当前可用性/合并状态。

`examples/`、官方 `tests/test_hok.py`、`docs/STARTER_README.md` 保持合入版本原样。
旧 `tests/test_hok_team.py` 的接口适配包括：raw 字典输入、合法 action_id 输出、
注入时钟代替旧步数计时接口、异常测试移至 Contestant 边界、明确名称路由。
按当前公开契约，远距离 fixture 不再提供不应出现的普攻宏。断言仍核对完整合法动作、
五个英雄、理由、计数、锁步兜底和官方 runner 的发送结果。

README 只补充一段现有操作说明所缺的 Agent Configuration、两个模式参数和等待不耗
AI tokens 的事实；原文其余内容、图片、LICENSE、NOTICE 不变。没有改测试来规避
原有 README 文档检查。

## 必须明确决定的策略差异

下列测试仍失败；不能把“接口适配”当作改变打法的理由。

| 原断言 | `8f507fd` 要求 | 当前强化版 | 决策选择 |
|---|---|---|---|
| `test_outnumbered_heroes_back_off_and_a_hurt_hero_leaves_tower_range` | 满血单人面对三个附近敌人也撤退 | 人数/塔下撤退要求 HP < 60%；本例推进 | 保留强化版并明确重订旧测试的满血预期，或授权增加满血以少打多撤退并加边界测试 |
| `test_realtime_breaking_off_a_fight_waits_for_the_hold_but_retreats_do_not` | 首次观察到交战命令，就认为刚开始并保持 0.6 秒 | 只保持本机近期发出的、仍可用的命令；消失的宏立即替换 | 当前 fixture 的普攻已不在合法列表，不能为过测试保留不可用宏；明确采用“本机已发、仍可用的技能”fixture 检查保持，另保留消失宏立即替换断言，或另行讨论扩大保持范围 |
| `test_a_refused_order_is_not_repeated_at_once` | 第 10 步被拒绝，第 12 步仍不重试；旧屏蔽为 5 步且到期当步仍屏蔽 | 0.4 秒后且 state_version 已更新即可重试；本例正好到期 | 保留 0.4 秒并明确在到期前/后各测一次，或授权改成至少 1 秒并验证不会损害紧急撤退/活性 |

还核对了没有直接触发现有断言的参数差别：旧版恢复 HP 90%（强化版 85%）、
附近 12000（10000）、塔邻近 9000（8800）、塔下受伤阈值 45%（60%）、
一名额外敌人且受伤也撤退（强化版需要多两名）、普攻/技能距离放宽、
发送确认宽限 1 秒（强化版 2 秒）。没有偷偷迁入任何一项。

强化版新增的紧急指令优先与防饥饿、可见目标去重、输入有界读取、出生/死亡及服务端
清空指令后的缓存恢复、同一被拒状态不重试、raw 模板完整填值均保留。
独立实现的额外撤退/错误统计不值得替换这些安全机制；运行边界已有明确 fallback 计数。

## 源码隔离证据

逐文件读取目标提交 blob，与当前文件 bytes 比较：

- 相对 `fb421a2`，除 `agent/agent.py` 和 `agent/hok/` 外，`agent/` 的 **44 个文件相同**。
- 官方 SDK `altruagent/` **17 个文件相同**。
- 更新 main 已有的 data/local 文件及 `tests/strategy/test_draft_meta.py`
  **75 个文件相同**；新增 HoK 离线工具不在这个 main 旧文件集合中。
- `champion/` 与 `versions/` **157 个文件相同**。
- 官方 examples、HoK 示例测试、starter README **12 个文件相同**。
- 相对强化版父提交，HoK policy/导出文件、LICENSE、NOTICE、assets **10 个文件相同**。
- `agent/agent.py` 中 **50 个其他函数/方法 AST 相同**；差别仅在 HoK 开关/识别、
  HoK 决策方法以及初始化、重置、回调、摘要中的 HoK bookkeeping。

只读审计脚本位于 `/tmp/hok-integration-isolation-1016.py`，不属于提交或发布内容。
本次检查时本地 main 仍为 `cda2dd9130c18b403a5f5dc323887f8f38990e1e`，
`origin/main` 仍为上述 `fb421a2`。

## 离线验证

统一使用 `/tmp/altruagent-fix-venv/bin/python`、本 worktree 的 `PYTHONPATH`、
`ARENA_RECORD=0`。没有调用 LLM 或真实游戏。录制测试只写 pytest 临时目录。

- 严格 CI lint：
  `/tmp/altruagent-hok-lint-1016/bin/flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude .git,node_modules`
  为 **0**，保留父提交修正的 7 个 F821 显式 helper import。
- `git diff --check` 通过。
- `tests/hok tests/redalert tests/test_runner.py tests/test_hok_delivery.py`：
  **822 passed，2 skipped**；跳过的两项要求 Linux/GNU 工具。
- `tests/test_realtime.py`：**34 passed**。
- `tests/test_hok.py tests/test_hok_team.py`：**33 passed，3 failed**（上表）。
- README 补充说明后，
  `tests/test_main.py tests/test_publish_sync.py tests/test_hok_delivery.py tests/test_hok.py tests/test_hok_team.py`：
  **127 passed，2 skipped，3 failed**，失败仍仅为上表的三个策略分歧。

### 全量测试与限定修复

首次全量 `python -m pytest -q`：**3234 passed，158 skipped，6 failed**
（333.32 秒）。除上述三个 HoK 断言外：

1. `tests/test_deploy.py::test_scripts_that_import_the_agent_put_their_own_tree_first_on_sys_path`
   发现父分支的 `scripts/hok_eval.py` 未把自己的 worktree 放在导入路径首位。
   已补同项目其他离线脚本一致的 `sys.path.insert(0, ...)`；不修改模拟器、策略、
   SDK 或运行进程。新增隔离模式 `python -I .../hok_eval.py --help` 回归测试，
   从任意临时目录启动，不执行任何模拟/模型/真实游戏。
   此项加 `tests/test_hok_delivery.py tests/hok/test_sim.py` 重跑：
   **123 passed，2 skipped**。
2. `tests/strategy/test_draft_meta.py::test_the_weight_follows_how_the_opponent_drafts`：
   `min(w_meta)=0.72`，原断言要求 `>0.8`；相关源码、数据、测试相对更新 main
   未改变。没有改另一个游戏的策略或削弱断言；单独重跑仍失败，留给其负责人。
3. `tests/strategy/test_draft_meta.py::test_meta_seat_drafts_by_the_fitted_scores_and_fixes_its_pilot_memory`：
   本 worktree 没安装 `local/js/node_modules`，Showdown 明确报缺依赖。
   没有因此改源码、伪造通过或隐藏 skip；单独重跑仍失败。

全量中的 158 项 skip 含未安装 Node 依赖和本机不是 Linux 等环境限制，
**不能用这次结果声称完整 CI 或引擎搜索已验证**。上游原 `docs/progress.md` 中
也记录过两个 draft-meta 基线失败；本次保留事实，不扩展到其他游戏修复。
目前没有全量绿灯：已定位、已修复脚本导入问题，尚有 3 个待定打法断言、
1 个选秀基线断言和 1 个缺依赖问题。

脚本修复后的最后一次聚焦复查（deploy 的导入隔离检查、HoK delivery、官方
HoK 示例、旧团队测试、main 文档测试、HoK simulator 测试）：
**178 passed，2 skipped，3 failed**，失败仍严格对应上表。
没有在脚本修复后重跑整套测试；不把推算结果当作实测结果。
