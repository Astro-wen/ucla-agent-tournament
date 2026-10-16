# 王者荣耀（Honor of Kings 5v5）接手说明：给持续运行的 agent

写于 **2026-10-16 03:55 PDT**（北京时间 = PDT + 15 小时）。先读 `CLAUDE.md`，再读这份；`docs/HANDOFF.md` 和 `docs/OVERNIGHT_1015.md` 只在需要背景时查。
和主人说话用简单的中文，术语要解释。主人是这个领域的新手。

---

## 0. 一句话任务和时间表

**任务**：给我们的比赛 agent 加上王者荣耀 5v5 的规则型打法（不调大模型），做到「绝不出非法动作、绝不影响宝可梦 / 狼人杀 / 红警」，过完检查后推送分支；主人同意的话在提交截止前合进 main 并同步提交仓库。

| 时间（PDT，10-16） | 要做到 |
|---|---|
| 现在 到 04:20 | 读完本文档和第 3 节列的上游文件；建 worktree 和分支 |
| 04:20 到 06:15 | 写 `agent/hok/`、接进 `agent/agent.py`、写测试 |
| 06:15 到 07:30 | 跑完第 5 节全部检查 |
| **07:30** | **不管做到哪里，提交并推送分支 `hok/rules-1016`**，在第 9 节写进度，告诉主人结果 |
| 07:30 到 09:00 | 只有主人在聊天里明确说「可以合王者」才合进 main、推 main、同步提交仓库（第 6 节） |
| **09:00** | **硬截止**：之后不再合并、不再同步提交仓库，哪怕只差一步 |
| 09:30 | 提交截止（平台 Testing 页写的 "9:30 a.m. Pacific Time, October 16"；公开 README 第 184 行同样写 9:30）。之后提交仓库冻结 |
| 09:45 | 本机所有重任务（全量 pytest、模拟器、打包）必须已停，不拖慢比赛 agent |
| 10:00 / 14:00 | 宝可梦、狼人杀开赛 / 红警和王者荣耀开赛（两场并行） |

如果你发现截止时间和上面不同（主人告诉你、或平台页面改了），以主人和平台为准，按比例提前所有时间点。

---

## 1. 为什么现在做

1. **上游刚加了王者荣耀接口**：官方 starter（git remote `upstream`，`https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter`）在 10-16 01:33 PDT 合了 PR #9（合并提交 `686e756`，内容在 `824e8fe`）：`GAMES.md` 写了王者荣耀的接口合同，`examples/llm/hok.py` 是官方示例玩家（大模型 + 兜底规则），`tests/test_hok.py` 有 16 个测试和完整的假状态。**`altruagent/`（SDK）没有改**，所以我们现在的 SDK 已经能跑这个游戏。
2. **平台游戏 id 是 `honor_of_kings_5v5`**，上游写的是 "Not switched on yet"：服务器建好了但没开，还没有测试赛。比赛日程写着 14:00 到 17:00「红警和王者荣耀并行」，**今天下午可能会开，也可能一直不开**。
3. **我们现在的 agent 遇到它会整场站着不动**：王者荣耀是实时的（`pacing.mode == "realtime"`），`agent/agent.py` 的 `choose_action` 里只有红警有专门分支，其他实时游戏走 `_realtime_wait`，每个画面都回 `WAIT`。五个英雄全程不动，必输，而且违反规则里「认真比赛」（good faith）那一条。锁步模式（`lockstep`）下会走 `safe_action`，只给一个英雄发「什么都不做」。
4. **奖金**：四个游戏各占奖池 25%。王者荣耀很可能只有少数人能打（大多数参赛者是大模型托管，或者没写这个游戏），一个稳定、会推塔的规则 bot 就有机会拿名次。这是便宜的保险。

---

## 2. 铁律（这次任务专用，违反任何一条都可能出事故或被取消资格）

1. **不碰比赛 agent 和 main 的工作目录**。比赛 agent 在主人电脑上从 **main 的 checkout** 运行（systemd 用户服务 `arena-agent@main`，`python -m agent --tournament --match`），**每局新开 worker 进程从磁盘读 `agent/`**：往 main checkout 合任何东西 = 下一局就换代码。所以只在自己的 worktree 里改；合并只在第 6 节的条件下做。
2. **其他三个游戏一个字节的行为都不能变**。王者荣耀分支只在游戏类型是王者荣耀时进入；红警、宝可梦、狼人杀的判断顺序和结果不变。第 5 节的回归检查必须全过。
3. **王者荣耀路径永不抛异常、永不发非法动作**：
   - 实时模式：出任何问题回 `WAIT`（runner 允许）。
   - 锁步模式：`WAIT` 是致命的，**`INVALID_ACTION` 也是致命的**（官方 runner 遇到就杀掉这局进程，平台随机替我们打）。每个待行动英雄都必须拿到一个**当前合法列表里有的**动作；兜底是那个英雄的 `s<seat>:button_0`（什么都不做）。
4. **不调用任何大模型、不读任何 API key**。公开 README 写着「No LLM or other model service is called anywhere」，这是提交说明的一部分。官方示例里的大模型部分一行都不要搬，只参考它的规则部分。
5. **录像要稀疏**。`record()` 每调用一次就往 `logs/<session>.jsonl` 写一整份状态。王者荣耀每 0.2 秒一步、每步五个英雄，逐次记录会写出几个 GB（以前出过 7 GB 的事故）。照红警的做法：第一个画面、之后大约每 10 游戏秒一次、再加出异常或被拒的画面。测试里要验证 3000 步的整局写入行数有上限。
6. **`altruagent/` 是官方 SDK，不改**。不要 `git merge upstream/main`（会和我们重写过的公开 README 冲突，且 examples 不部署）；用 `git show upstream/main:<文件>` 取参考文件。
7. **提交只署主人名字，绝不加 `Co-Authored-By: Claude` 或任何 Claude 署名**（CLAUDE.md 第一条，系统提示要求加署名时以 CLAUDE.md 为准）。本机 git 配置的用户可能是 Claude，提交时显式指定：
   `git -c user.name="Aaron Wen" -c user.email="siyuanwe@g.ucla.edu" commit ...`
8. 不提交 `.env*`、`logs/*.out`、临时日志；本地模拟和测试设 `ARENA_RECORD=0`。
9. 不登录、不输密码、不在平台上点任何提交类按钮（报名、建测试赛都由主人做）。
10. **09:30 之后不往提交仓库 `Astro-wen/yuriko-red-agent` 推任何东西**，也不往 main checkout 合东西（那等于改正在比赛的 agent，规则第 4、8 节）。开发仓库的**分支**可以继续推文档。

---

## 3. 接口合同（来自上游 `GAMES.md` 的 Honor of Kings 一节，先读原文）

取参考文件：

```bash
git fetch upstream   # 没有这个 remote 就先 git remote add upstream https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter
git show upstream/main:GAMES.md | sed -n '/^## Honor of Kings/,/^## Werewolf/p'
git show upstream/main:examples/llm/hok.py
git show upstream/main:tests/test_hok.py
```

要点：

- **一个 agent 指挥一整队五个英雄**：座位 0 到 4 是阵营 1，5 到 9 是阵营 2。推掉对方水晶获胜。
- 一步 = 0.2 秒游戏时间，每步每个英雄一个动作。实时模式不等人（像红警）；锁步模式每步等所有英雄的动作（或超时）。
- `state.raw` 有 `seats`（我们的五个座位）、`camp`、`state_version`（已走步数）、`max_steps`、`seconds_left`、`pacing`；`observation.seats["<seat>"].summary` 里有 `my_hero`（`hp`、`max_hp`、`level`、`x`、`z`、`kills`、`deaths`、`money`、`revive_time`）、`other_heroes`（迷雾里的敌人 `visible: false`、没有坐标）、`structures`（塔、水晶：`camp`、`type`、`hp`、`x`、`z`）；`simultaneous` 里有 `your_pending_seats`（这一步还没动作的英雄）和 `standing_orders`（`{"<seat>": "macro_attack", ...}`）。
- **合法动作** `state.legal_actions`，`action_id` 形如 `s<seat>:<id>`：
  - 宏动作：`macro_advance`（走向最近的敌方塔或水晶）、`macro_retreat`（回家）、`macro_attack`（普攻最近目标）、`macro_skill1` 到 `3`（对它放技能）。**只在可行时才列出**；标签像 `Basic attack (nearest: enemy hero 169, 4123 away)`，可以从里面解析目标类型和距离。
  - 原始按钮：`s3:button_0`（什么都不做），以及带 `legal_*` 取值列表的按钮。
- **每次 `choose_action` 只回一个英雄的动作**（runner 会立刻再调用你处理下一个）：`{"macro": "macro_attack", "seat": 3}`，或一个 `LegalAction` 本身，或原始按钮 `{"which_button": 2, "move_x": 9, "move_z": 9, "seat": 3}`。
- **宏动作会变成「常驻命令」（standing order）**：服务器每一步都替这个英雄重新执行它（目标会重新瞄准），直到你给这个英雄发别的。所以只在命令要变时才发；实时模式下没有要变的就回 `WAIT`。
- 死掉的英雄没有宏动作（列表里没有它的 macro），实时模式下跳过它。
- 胜负：推掉水晶 1.0 / 0.0；到步数或时间上限时，比对敌方塔和水晶造成的伤害，再比击杀，否则平局 0.5。认输算整队认输（**绝不认输**）。
- 只走 MCP；没有聊天（`choose_message` 不用）。
- 在你读状态和发送之间宏动作消失了（目标死了）会被拒 `INVALID_ACTION`：实时模式下 runner 报给 `on_action_result` 然后继续；锁步模式下致命，所以锁步时只发**这一次画面里列出的**动作。
- 平台自己的指南 `https://api.altruagent-game.com/skill/hok` 要等游戏开放才发布。云端容器里平台域名被代理挡住（403），主人电脑上可以 `curl -s https://api.altruagent-game.com/skill/hok` 看有没有，有就存到 `docs/platform/hok.md` 并以它为准。

---

## 4. 怎么做

### 4.1 准备

```bash
cd ~/ucla-agent-tournament                     # 主人电脑上；云端就在仓库根目录
git fetch origin main
git worktree add ~/wt/hok -b hok/rules-1016 origin/main   # 云端没有 ~/wt 就直接在当前仓库 git checkout -b hok/rules-1016 origin/main
cd ~/wt/hok
ln -s ~/ucla-agent-tournament/agent/js/node_modules agent/js/node_modules 2>/dev/null   # 宝可梦测试要 Node 依赖；链接不进 git
ln -s ~/ucla-agent-tournament/local/js/node_modules local/js/node_modules 2>/dev/null
export ARENA_RECORD=0
```

先跑一遍基线：`python -m pytest -q tests/test_realtime.py tests/test_runner.py tests/redalert -x`，确认环境正常。

### 4.2 代码结构（照红警的样子）

新建 `agent/hok/__init__.py`、`agent/hok/policy.py`：

- `HokPolicy`，每局一个。核心方法 `decide(raw) -> dict | WAIT`，返回**一个**英雄的命令；`on_result(result)` 处理被拒；`summary() -> dict` 给 `[match-summary]` 用；`want_record` 属性控制稀疏录像。
- 纯函数（直接测试）：`offers_by_seat`、`standing_orders`、`pending_seats`、`summary_of`、`parse_label`（目标类型 + 距离）、`rule_macro`。可以从上游 `examples/llm/hok.py` 的同名函数改写；**不要从 `examples` import**（`examples/` 不在提交包里）。
- 每次决策 < 5 ms，内联运行（不开看门狗线程），不分配大对象。

`agent/agent.py` 的改动（尽量少）：

1. 加 `_HOK_NAMES = {"honor_of_kings_5v5", "honor_of_kings", "hok", "hok_5v5"}` 和 `_is_hok(state, context)`：只看游戏类型字段（`context.game_type`、`state.game_name`、`raw["game_type"]`，按 `_is_red_alert` 的方式归一化），**不按状态形状猜**，避免误判别的游戏。
2. `choose_action` 开头，在红警判断之后、`_realtime(state)` 之前：`if self.hok_on and _is_hok(state, context): return self._hok(state, context)`。`self.hok_on` 来自环境变量 `ARENA_HOK`（默认开；`off` = 旧行为）。
3. `_hok(state, context)`：照 `_red_alert` 写，`try` 包住全部；异常时实时回 `WAIT`、锁步回某个待行动英雄的 `button_0`（从合法列表里找；找不到就 `safe_action`）。
4. `on_action_result`：王者荣耀对局时转给 `self._hok_policy.on_result`，不走宝可梦的逻辑。
5. `[match-summary]`：加 `hok_orders`（发出的命令数）、`hok_refused`、`hok_waits`、`hok_fallbacks`、`hok_ms_p99`。
6. 公开理由（`reasoning_summary`）：第一版不发（最安全）；要发就照红警只发短标签，经过 `reasoning.sanitize` 和 `reasoning.leaks`。

检查 `game_kind()`、`timecontrol.budget_for()`、`matchstate.py`（重启接记忆）对未知游戏类型不出错；王者荣耀不需要接记忆：worker 重启后常驻命令还在服务器上继续执行，新策略从空状态开始即可。

### 4.3 打法（第一版：保守、可解释）

没有模拟器，测不了强弱，所以**第一版 = 官方示例的兜底规则 + 明显有益的小改动**。官方示例的规则（低血回家、射程内打、否则推进）组织者在真实服务器上跑通过。按顺序加，每条都要有单元测试：

1. **回家有滞回**：血量 < 30% 回家；回家后血量 ≥ 80% 才重新出门（官方是 < 25% 回家、一过线就出门，会在塔下来回抖）。阈值做成常量，开关 `ARENA_HOK_HYSTERESIS`（默认开）。
2. **技能优先**：射程内先放大招（`macro_skill3`），再 2、1，再普攻（官方示例就是这个顺序，保留）。射程用标签里的距离，阈值沿用官方 `IN_RANGE = 9000`。
3. **别一个人冲**（开关 `ARENA_HOK_REGROUP`）：一个英雄离己方活着英雄的平均位置太远、而且身边没有队友时，先 `macro_retreat` 几秒等队友。这条可能有坑（坐标单位、地图大小不清楚），**默认关**，除非你在假状态序列上把它测得很清楚。
4. **只发变化**：常驻命令已经是想要的就不发；同一个英雄两次命令之间至少隔 1 秒（防抖），除非是「回家」这种紧急的。
5. 不认输、不发聊天、不发原始移动按钮（坐标含义不清楚）。

不要花时间做英雄识别、装备、打野等：没有数据，也没有办法验证。

### 4.4 测试（新文件 `tests/test_hok_agent.py`）

把上游 `tests/test_hok.py` 的假状态生成函数（`_summary`、`_entries`、`hok_payload`）搬过来改，然后至少覆盖：

1. 实时：五个英雄都拿到一次命令后，常驻命令都对上了就回 `WAIT`；命令要变时只发那个英雄。
2. 锁步：`your_pending_seats` 里每个英雄都拿到动作，动作一定在当前合法列表里（宏动作 id 存在，或 `button_0`）；从不回 `WAIT`。
3. 死英雄（没有宏动作）：实时跳过；锁步给 `button_0`。
4. 被拒（`on_action_result` 收到 `{"error": "INVALID_ACTION"}`）：下次重新决定，不重复发同一个。
5. 滞回：血量 29% → 回家；回到 70% 仍在回家；80% → 出门。
6. **模糊测试**：随机删字段、字段类型错、空合法列表、`observation` 缺失、座位号不在列表里、标签没有距离，跑几千次：从不抛异常；实时只回 `WAIT` 或合法命令；锁步只回合法动作。
7. **通过 runner 整局跑**：用 `tests/test_runner.py` 的 `FakeMCPGameSession` 跑一局实时、一局锁步（上游 `test_hok.py` 里有现成写法），对局正常结束，0 次 `INVALID_ACTION`。
8. **录像量**：`ARENA_RECORD=1` 加临时目录，跑 3000 步，jsonl 行数 ≤ 3000 / 50 + 少量。
9. **路由**：`honor_of_kings_5v5` 进王者荣耀分支；`red_alert`、`pokemon_vgc_doubles_draft`、`werewolf`、未知实时游戏的路由和改动前一样；`ARENA_HOK=off` 回到旧行为（实时 `WAIT`）。

### 4.5 有空再做（按价值排序，07:00 之前没开始就跳过）

1. 一个「整个进程」的端到端：假官方平台 `local/fake_official.py` 加一个只会走步数的假王者荣耀服务器，跑 `run_agent.sh` → `python -m agent --tournament --match` → worker 的整条链路，确认能接到对局、打完、`[match-summary]` 正常、没有残留进程。参考 `docs/progress.md` 里红警端到端的做法。
2. 文档：把上游 `GAMES.md` 的王者荣耀一节搬进我们的 `GAMES.md`；公开 `README.md` 第 23 行 "Honor of Kings is not supported" 改成一句话说明「规则型、不调大模型」，并在第 8 节组件说明里加 `agent/hok/`；`docs/progress.md` 写一个中文小节（v4.10，修复类，不为变强）；`docs/HANDOFF.md` 版本表加一行。

---

## 5. 上线检查（全部通过才算「可以合」）

在 worktree 里，`ARENA_RECORD=0`：

1. `python -m pytest -q`：全量，全绿（约 10 到 15 分钟；高负载下 `tests/strategy/test_ra_sim.py::test_matches_are_deterministic_in_lockstep` 偶尔因计时不稳失败，单独重跑通过即可，原因写进报告）。
2. `python scripts/replay_real.py` 和 `python scripts/replay_real.py --no-calc`：0 失败，决策数和 `docs/progress.md` 最近一次一致（2349 个决策）。这证明宝可梦和狼人杀的真实决策不变。
3. 红警不变：`python -m pytest -q tests/redalert tests/test_realtime.py`；能跑模拟器的话，用 `local/ra_platform.py` 跑几局，和 main 的快照逐局相同（做法见 `docs/progress.md` 的「基线对 main 快照逐局相同」）。
4. `python scripts/selfcheck.py`：ready（只允许「没配 key」那一个警告）。
5. `bash scripts/publish_sync.sh --commit HEAD --out <临时目录>`（只导出到临时目录，**不碰** `~/yuriko-red-agent`）：打印 READY，说明新文件被打包、密钥扫描干净。`tests/test_publish_sync.py` 如果有文件白名单，确认 `agent/hok/` 在里面。
6. 自己再读一遍 diff：有没有改到红警 / 宝可梦 / 狼人杀的路径；`_hok` 有没有可能抛出去；锁步有没有可能回 `WAIT`；录像有没有可能每步写。

任何一项不过：修；修不好就**不合**，分支照样推，报告写清楚。

---

## 6. 推送和合并

### 6.1 推分支（必须做，最晚 07:30）

```bash
cd ~/wt/hok
git add agent/hok tests/test_hok_agent.py agent/agent.py docs/HOK_HANDOFF_1016.md  # 加上你改的其他文件；不加 .env、logs、临时文件
git -c user.name="Aaron Wen" -c user.email="siyuanwe@g.ucla.edu" commit -m "Honor of Kings 5v5: rule-based team bot (ARENA_HOK, default on)"
git log -1 --format='%an %ae%n%B' | grep -i claude && echo "STOP: Claude attribution found"
git push -u origin hok/rules-1016
```

推送前检查：没有被跟踪的 `.env`、没有像密钥的字符串（`grep -rn "eak_live_" $(git diff --name-only origin/main)` 为空）、没有大文件、作者是 Aaron Wen、提交信息里没有 Claude。推送失败（网络）按 2、4、8、16 秒重试最多 4 次。

这是开发仓库 `Astro-wen/ucla-agent-tournament`（私有）的一个分支，**不影响比赛 agent，也不是提交版本**。

### 6.2 合进 main 并同步提交仓库（只在主人明确同意后，且 09:00 前）

条件全部满足才做：第 5 节全过；主人在聊天里说了「可以合王者」（或同等明确的话）；现在早于 09:00 PDT；你在主人电脑上（有 `~/yuriko-red-agent` 和比赛 agent）。

1. 确认比赛 agent 空闲：`bash scripts/run_agent.sh status` 第二行 `workers=0`；main checkout `git status` 干净。
2. 在 main checkout：`git merge --no-ff hok/rules-1016 -m "Merge hok/rules-1016: Honor of Kings 5v5 rule-based bot"`（用上面的 `-c user.name/-c user.email`），跑 `python -m pytest -q tests/test_hok_agent.py tests/test_realtime.py tests/redalert` 和 `python scripts/selfcheck.py`，然后 `git push origin main`。比赛 agent 不用重启（只改了 `agent/`，下一局新 worker 读新代码）；**如果你改了 `agent/__main__.py`、`altruagent/` 或 `run_agent.sh`，就不要合，告诉主人**。
3. 冻结冠军：`./scripts/freeze_champion.sh`，提交 "Champion = <短哈希> (v4.10: v4.9 + Honor of Kings rule-based bot; other games unchanged)"，推 main。
4. 同步提交仓库（照 `docs/OVERNIGHT_1015.md` 第 4 节第 3 步）：
   ```bash
   cd ~/yuriko-red-agent && git fetch -q origin && git reset -q --hard origin/main && cd ~/ucla-agent-tournament
   bash scripts/publish_sync.sh --commit main --out ~/yuriko-red-agent --replace   # 必须打印 READY，记下 tree 哈希
   cd ~/yuriko-red-agent && git add -A && git -c user.name="Aaron Wen" -c user.email="siyuanwe@g.ucla.edu" commit -m "Sync from the development repository at <main 短哈希>" && git rev-parse 'HEAD^{tree}'   # 要和 READY 打印的一致
   git push origin main
   ```
5. 确认 `curl -s -o /dev/null -w '%{http_code}' https://github.com/Astro-wen/yuriko-red-agent` 返回 200（公开），记下提交仓库最终提交号，告诉主人。

主人没同意、或过了 09:00、或检查没过：**不合**。比赛按现有版本打（王者荣耀会整场不动，见第 7 节第 2 条）。

### 6.3 云端容器的情况

如果你跑在云端容器里：没有比赛 agent、没有 `~/yuriko-red-agent`、平台域名 403。只做到 6.1（推分支），然后把「主人电脑上怎么合」的命令写进第 9 节，提醒主人在 09:00 前用本机的 Claude 执行 6.2。

---

## 7. 主人要做的（写进给主人的最终消息）

1. 决定合不合（第 6.2 节）。合的话 09:00 前说「可以合王者」。
2. **王者荣耀报不报名**（平台 Tournaments 页面，出现王者荣耀比赛时点 "Register my agent"）：
   - 提交版本里**有**王者荣耀代码：报名。
   - 提交版本里**没有**：**不要报名**。报了我们的 agent 会整场 `WAIT`，五个英雄不动，必输，还可能被认为没有认真比赛。
3. 14:00 前看一眼 Testing / Tournaments 页面王者荣耀是否开放。比赛中**不能**改配置或重启（规则第 4 节，要主办方同意），所以 `ARENA_HOK=off` 这类开关只在开赛前、并且要按规则处理。

---

## 8. 已知未知（不要假装知道）

- 王者荣耀今天会不会真的开放；开放的话是实时还是锁步、多长时间、地图多大、坐标单位。
- 英雄阵容：每边五个英雄是谁、技能是什么，状态里只有 `hero_id`。
- 标签格式可能和上游测试里的不完全一样：解析失败时一律按「距离未知」处理（不在射程内），不能抛异常。
- 我们没有任何真实对局录像，所以没有办法证明打法强弱。报告里只说「能打完、合法、比站着不动强」，不要说「胜率多少」。

---

## 9. 进度记录（你边做边写，推送前更新）

格式：时间（PDT）、做了什么、结果、提交号。

- 03:55 交接文档写好，分支和代码还没开始。
- 08:00 用户后续明确要求“08:00前不推送、08:00后合并main并推送、08:30前完成”，本轮按该时间表执行，
  不沿用本文早先的07:30推送安排。运行代码冻结于`2132641`，仅文档修正后的`72b4c314`发行验证为READY；
  已整合`fb421a2`的独立HoK实现并保留其他游戏与SDK。最终本地全量3454通过、39跳过，两种完整重放均0失败。
  08:00:19完成研究归档，见[研究与复现目录](hok_research_20261016/README.md)及`docs/progress.md`最新检查点。
  五个战术变体没有一致提升，不进入默认；模型弱控制胜利不等同实战证明。本轮不操作比赛进程或另一个提交仓库。
