# 王者荣耀 5v5：给 Codex 的持续迭代任务

写于 **2026-10-16 12:05 PDT**。读完 `CLAUDE.md` 和本文档就能开工；背景在 `agent/hok/RESEARCH.md`、`docs/hok_integration_preflight_1016.md`、`docs/hok_sources_1016.md`、`docs/hok_research_20261016/README.md`。

---

## 0. 现在的规则和时间（主人在现场确认的）

- 主办方 10-16 现场宣布：**王者荣耀已经开放**，到 **14:00 PDT** 前可以在 Testing 页面测试，**王者荣耀的代码可以合进提交仓库直到 14:00**。
- **提交仓库就是这个仓库** `Astro-wen/ucla-agent-tournament`（main 分支）。
- 主办方原话要点：每个 agent 控制 5 个英雄，每局是 1 个 agent 对 1 个 agent；考的是多线程编排（同时指挥五个英雄）。
- 王者荣耀比赛 14:00 到 17:00，和红警同时进行。

| 时间（PDT） | 规则 |
|---|---|
| 现在 到 **13:40** | 迭代王者荣耀策略；过检查就合进 main 并推送。每轮都要能独立上线 |
| **13:40** | 最后一次合并的截止（留 20 分钟缓冲）。之后不再合并、不再推送 main |
| 14:00 起 | 冻结：**不往这个仓库推任何东西**（任何分支都不推），不改主人电脑上 main 的 checkout，不重启比赛 agent。比赛中任何人工干预都要主办方授权（规则第 4 节） |
| 14:00 到 17:00 | 可以只读地看日志、分析，把结论写在本地文件里（不提交），给主人看 |

如果主人告诉你新的时间，以主人为准。

**本聊天主人 10-16 更新授权：本任务持续测试、迭代与推送的截止改为 13:50 PDT；以下原 13:40 内部截止均以 13:50 为准。已授权自主进行本轮测试操作并使用多个子任务加速，其他游戏代码仍不改，14:00 起仍冻结。**

---

## 1. 现状（先弄清楚，再动手）

- 部署中的代码：`agent/hok/policy.py`（规则型，不调大模型），由 `agent/agent.py` 的 `_hok()` 分派（开关 `ARENA_HOK`，默认开）。测试：`tests/hok/`、`tests/test_hok.py`、`tests/test_hok_team.py`、`tests/test_hok_delivery.py`。本地合成模拟器：`local/hok_sim.py`、`local/hok_env.py`、假平台 `local/hok_fake_official.py`、`local/hok_fake_session.py`（**只能证明协议和合法性，不能证明强弱**）。
- 打法（`rule_macro`）：血量 < 30% 回家，回家后到 85% 才出门；附近敌人比我方多 2 个以上就回家（满血也回）；受伤（< 60%）且在塔附近回家；有塔可打就普攻塔；目标在 8000 内放技能（大招只对英雄）；有普攻就普攻；否则推进。
- **红旗**：唯一一份真实对局观战记录（`docs/hok_research_20261016/one-completed-real-record.json`）是 3000 步（10 分钟）打满、**双方对塔伤害都是 0** 的平局。不确定那局是不是我们的代码。如果我们的英雄真的一直摸不到塔，最可能的原因是回家条件太保守（以少打多就回、85% 才出门），或者推进 / 攻击的常驻命令被反复覆盖。**第一件事就是用早上的真实对局查清楚这一点**。

---

## 2. 数据从哪里来

主人电脑上（你应该就在这里运行）：

1. **比赛 agent 日志**：`logs/agent-main.out*`。按局找：`Match assigned: honor_of_kings_5v5`、`Opponents:`、`finished: your agent ... won/lost/drew`，以及每局一行 `[match-summary]`（含 `hok_*` 字段：发出的命令数、被拒数、兜底数、耗时）。自对弈一局会有多行 finished。
2. **每局录像**：`logs/<session>.jsonl`。王者荣耀是稀疏记录（第一个画面、之后约每 50 步一次、出异常或被拒时加一次），每行有 `raw`（完整状态：五个英雄的 `summary`、`structures` 的血量、合法动作和标签、`standing_orders`）和 `out`（我们发了什么）。用它算：每个英雄每分钟在家 / 在推进 / 在打的时间比例，塔血量随时间变化，死亡次数，被拒次数。
3. **平台回放**：主人在 Testing 页面 "My matches" 里点 Replay 能看。你不能登录；需要的话请主人把回放截图或导出文件给你。
4. **官方资料**（只读，可以直接访问）：
   - 平台指南 `https://api.altruagent-game.com/skill/hok`（游戏开放后以它为准；每轮开始前 `curl -s` 一次，和 `docs/platform/` 里的本地副本对比，有变化就存一份到 `docs/platform/hok.md`）
   - 官方 starter `https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter`（git remote `upstream`：`git fetch upstream && git log upstream/main -5`，看 `GAMES.md` 的 Honor of Kings 一节、`examples/llm/hok.py`、`tests/test_hok.py`）。**不要 merge upstream**（README 会冲突），用 `git show upstream/main:<文件>` 读。
   - 官网 `https://altruagent-game.com`，Discord 公告（主人转述）。
5. **建测试赛**：只能主人点。请主人在 Testing 页面建王者荣耀局：两边座位都选 "Mine (self-hosted)" 就是自对弈（同一程序打两边，可用来验证不出错，但看不出强弱）；选 "Open" 或加入别人的 Open 局才是真对手，最有价值。

---

## 3. 每一轮迭代怎么做（一轮目标 20 到 30 分钟）

1. **看数据**：拿最新的真实对局（第 2 节），写下这一轮要解决的**一个**具体问题，配证据（哪局、第几步、哪个英雄、发生了什么）。
2. **在分支上改**：`git switch -c hok/codex-<时间> origin/main`。只改 `agent/hok/`（和对应测试）。**不碰** `agent/agent.py` 的其他游戏路径、`agent/redalert/`、`agent/pokemon/`、`agent/werewolf/`、`altruagent/`、`agent/__main__.py`、`scripts/run_agent.sh`。每个新行为放在常量或 `ARENA_HOK_*` 开关后面，关掉 = 现在的行为。
3. **测试**（全部要过）：
   ```bash
   export ARENA_RECORD=0
   python -m pytest -q tests/hok tests/test_hok.py tests/test_hok_team.py tests/test_hok_delivery.py tests/test_realtime.py
   python -m flake8 agent/hok tests/hok --select=E9,F63,F7,F82   # 和 CI 同一套检查
   python scripts/selfcheck.py                         # ready
   ```
   改了 `agent/agent.py` 的话，再跑全量 `python -m pytest -q` 和 `python scripts/replay_real.py`（0 失败）。
   旧测试和新打法冲突时：只有在你能用真实对局证据说明旧断言错了的时候才改断言，提交信息里写清楚证据。
4. **合成模拟器做回归**（不是强弱证明）：用 `local/hok_sim.py` 让新旧版本在同一批种子上对打，确认 0 非法动作、0 异常、没有明显变差。读模块开头的说明找命令行用法。
5. **上线**（只在 13:40 前）：
   ```bash
   git -c user.name="Aaron Wen" -c user.email="siyuanwe@g.ucla.edu" commit -m "HoK: <一句话说明改了什么和证据>"
   git log -1 --format='%an %ae%n%B' | grep -iE 'claude|codex|co-authored' && echo STOP
   git fetch origin main && git rebase origin/main && <重跑第 3 步的测试>
   git push origin HEAD:main
   ```
   推送前检查：没有 `.env`、没有密钥（`git diff origin/main --name-only | xargs grep -ln "eak_live_"` 为空）、没有大文件、没有 AI 署名。然后主人电脑上的 main checkout 要 `git pull --ff-only`（在 `~/ucla-agent-tournament` 里）才会生效；拉之前看 `bash scripts/run_agent.sh status`，有红警或王者荣耀正在打（`workers` 大于 0）也没关系，正在打的局已经加载了旧代码，下一局才用新的。**只改 `agent/hok/` 就不用重启比赛 agent**。
6. **记录**：在本文档第 6 节追加一行（时间、问题、证据、改动、测试结果、提交号），告诉主人一句话结果。然后开下一轮。

---

## 4. 主人点名要看的方向（按我的判断排了优先级）

1. **先确认我们能推塔**（最重要）。如果真实对局里塔血量不掉：
   - 放宽回家条件：满血时不因为「以少打多」回家，或者把人数差门槛从 2 提到 3；出门阈值 85% 降到 70%。
   - 检查 `HOLD_S`（0.6 秒内不改命令）和调度是否让推进命令被反复覆盖：看 `standing_orders` 是否在 advance / retreat 之间抖动。
   - 打塔优先：标签显示目标是塔时，技能也可以打塔（是否造成伤害要看录像里塔血量）。
2. **秒掉对方输出（集火脆皮）**：输出位一般血量上限和护甲低。能做的和不能做的：
   - 宏动作（`macro_attack` / `macro_skill*`）**只打「最近的目标」**，不能指定打谁。所以「集火」只能间接做：看到可见敌方英雄血量低（比如 < 35%）或 `max_hp` 明显低于其他敌人（脆皮）并且在技能距离内时，附近我方英雄**都不回家、全部放技能 / 普攻**（除非自己血量 < 20%），把人留下来。这是第一步，最安全。
   - 原始按钮（raw action，`which_button` 加 `legal_*` 参数，研究文档说是 `[button, move_x, move_z, kill_x, kill_z, target]` 六部分，有 `legal_target`）**可能**能指定目标，但编号含义没有公开，原始动作还会清掉常驻命令。**只在自对弈测试局里试**，确认 `target` 的含义（看录像里谁掉血）之后才能上线；14:00 前确认不了就不做。研究文档明确禁止没有证据就把 `button_7` 当治疗。
3. **选强势英雄**：现在的接口合同里**没有选英雄 / 禁英雄的阶段**（状态里只有每个英雄的 `hero_id`）。每轮读一次 `skill/hok` 指南和 starter 的 `GAMES.md`，如果出现选英雄的阶段（比如 `phase` 是 pick / lineup，或者合法动作里有英雄编号），就加一个按事先排好的强势英雄列表选人的规则（参考公开资料：腾讯开源的王者荣耀 AI 环境 `hok_env`（GitHub `tencent-ailab/hok_env`）和论文 "Honor of Kings Arena" 用过的英雄，如 1v1 常用的狄仁杰、鲁班七号等，但**以平台实际可选的为准**）。没有这个阶段就不做。
4. **开源方案能借鉴什么**：腾讯的 `hok_env`、JueWu（绝悟）论文是大规模强化学习，两个小时内复制不了。能借鉴的是它们公开的宏观经验：抱团推进、兵线到了再推塔、残血先撤、团战集火。把这些写成规则，每条都要有真实对局证据支持才上线。

不要做：调大模型（公开 README 写着不调用任何模型服务）、英雄专属技能表（没有数据）、买装备（接口没有）、任何没有测试覆盖的改动。

---

## 5. 硬性安全要求（每次改动都要满足）

1. 王者荣耀路径永不抛异常（`_hok()` 外层已经兜底，但不能依赖它）。
2. 锁步模式（`pacing.mode == "lockstep"`）下 `WAIT` 和 `INVALID_ACTION` 都是致命的（runner 杀掉这局进程，平台随机替我们打）：每个待行动英雄只能发**当前画面合法列表里有的**动作。实时模式下被拒不致命，但会浪费这一步。
3. 每次决策 < 5 毫秒，不开线程，不读网络。
4. 录像保持稀疏（不能每步写，以前出过 7 GB 的事故）。
5. 其他三个游戏（宝可梦、狼人杀、红警）的行为一个字节都不能变。
6. 只用自己座位能看到的信息；不看观战页、直播、实时排行榜（规则第 5、6 节）；每局从空记忆开始。

---

## 6. 迭代记录（每轮追加一行）

| 时间 PDT | 问题和证据 | 改动 | 测试 | 提交 |
|---|---|---|---|---|
| 12:05 | 交接：真实观战记录对塔伤害 0；早上自对弈日志在主人电脑 `logs/`，还没分析 | 无 | 无 | 无 |
| 12:08 | 第一轮：04:04 `ca125797`、08:26 `60772282` 两局实际走旧 WAIT 路径，双方 decisions=0；每局录像仅双方第 0 步，无法确认英雄对塔伤害，08:26 的 crystal_destroyed 也无伤害归属。离线重放四份开局发现当前代码把附带已填 move 和 legal_move 的合法推进宏全部过滤，五英雄仍等待 | 只修宏动作解析：参数已填且属于合法值域才接纳，继续发送宏编号以保留常驻命令；`RESOLVED_MACRO_INPUTS=False` 恢复旧行为；不改回家/战斗阈值 | 更新远端基线后 827 tests passed；flake8 通过；selfcheck ready（独立工作目录不配置密钥，1 warning）；真实四视图全部从五次等待变为五英雄推进，800 次实测最长 0.45ms；种子 161201/161202、双方换边、五种参数、实时/锁步共 40 局×1000 步：0 非法/异常/协议失败，均平局，源快照未变。合成回归不证明实战强弱 | `69a3ee1` |
| 12:24 | 第二轮：`fea41303` 步 201 座位 3 向 73449 距离的塔选 skill2，步 251 向 41945 距离的塔选 skill1；真实 `(N away)` 未被旧逗号解析器识别，绕过既有 8000 门槛；真实 `toward enemy hero` 也未被识别为大招目标（例 `bbb8f70a` 步 393、座位 4、4639 距离）。另直接核对已结束回放：04:04 双方建筑分 0，08:26 为 18000/0，但 WAIT-only 记录不支持归因英雄伤害 | 兼容真实距离与 toward 目标标签，保留旧格式和原战术阈值；`REAL_MACRO_LABELS=False` 恢复旧解析。主人本聊天把截止更新为 13:50 | 更新基线后 838 tests passed；flake8 通过；selfcheck ready；两份真实座位片段从超远技能变为发送/保留 advance，400 次实测最长 0.61ms；40 局标准合成回归均平、0 错误；额外 16 局双方同样转换为真实技能标签，候选均胜且 0 非法/异常/协议失败，仅为未校准的合成诊断，不是实战胜率 | `8721c8f` |
