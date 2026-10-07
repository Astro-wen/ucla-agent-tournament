> 2026-09-28 由多路线上调查（主办方、平台代码与接口、公开公告、各游戏最优解、我们的覆盖缺口）汇总，之后经独立审查。CONFIRMED = 一手来源；LIKELY = 证据较强但官方未明说；UNKNOWN = 没找到。文中提到的 scratchpad 路径是当时的临时目录，不在仓库里。**以本页末尾「审查意见」为准修正正文。** **最新情况在最上面（10-07，然后 10-03），和下面的正文冲突时以最上面的为准。**

# 2026-10-07 更新：event agent、一把 key、瑞士轮赛制

来源：平台公开技能文档 v0.5.0（10-07 11:47 UTC 只读拉取，存于 `docs/platform/`，和 10-07 上午那次逐字节相同）和官方 starter 上游 PR #5（10-06 合并，10-07 合进我们仓库）。都是**一手来源（CONFIRMED）**，但我们还没在真实平台上实际跑过一局，下面「怎么接局」的部分要等主人第一次跑通才算验证过。

## A. 参赛方式：每人一个 event agent

- **每个参赛者只有一个 agent**（event agent）。主人用 **UCLA Google 账号**登录 <https://platform.altruagent-game.com/tournament>，做完报名：问卷、接受 Official Rules、起 agent 名字、选托管方式、填**公开的代码仓库**。报名没做完时什么都不能打（`registration_incomplete`）。
- **两种托管方式**（在 dashboard 的 **Agent Configuration** 页面选）：
  - **Self-hosted（自托管，我们选这个）**：代码跑在我们自己的机器上，用 **Official Agent Key**（`eak_live_` + 64 位十六进制，在同一页面生成）连平台。
  - **Oracle-hosted（甲骨文托管）**：不跑代码，交一份 **Markdown 写的提示词**，平台在 Oracle 上用大模型代我们下。开赛时冻结当时最新交的那一版。选了它，我们的 key 会被拒绝。我们的 agent 是写好的程序（引擎搜索、伤害计算），提示词做不到，所以一定选 Self-hosted。
- **旧方式全部停用（10-06 起，平台回 HTTP 410）**：以前的 `sk_agent_` 平台账号（Yoriko_Red、Apocalypse_Red）和「发现模式」、测试赛认领码 `--claim seatclaim_…`、MCP 的 `list_sessions` / `join_session` 都不能用了。

## B. 一把 key，两种对局，一个进程

- 平台把每一局都作为**分配**（assignment：「你在哪场对局的哪个座位」）发给这把 key，我们的进程每 10 秒查一次、自动接上。分配里的 `context` 写明是哪种：
  - **Testing（测试赛，练习，不计分）**：主人在 dashboard 的 **Testing** 页面自己建对局，座位选 **"Mine (self-hosted)"**，或加入别人在 *Open matches* 里开的座位。官方 starter 用 `python -m agent --match` 打它。测试赛没有连接期限。
  - **Tournament（正式赛）**：主人在 **Tournaments** 页面对每个比赛点 **"Register my agent"**（开赛前可撤回），之后每一局自动出现。官方 starter 用 `python -m agent --tournament` 打它。
  - 两个参数一起写（`python -m agent --tournament --match`）就是一个进程两种都打：**我们的 `scripts/run_agent.sh` 固定这样跑**。只开 `--match` 的进程碰到正式赛只打一句警告，不会去打，那局判负。
- **一把 key 只跑一个运行时**（官方规则）：第二个进程拿到 `seat_busy`、什么都不打。我们的「热备」就是这样的第二个进程，只在主进程停了以后才接手（见 `docs/deploy.md`）。

## C. 正式赛的赛制（`docs/platform/04-tournaments.md`）

- **每个游戏一个比赛**，管理员建好后开赛。**一局一局地打**：同一个比赛里我们永远只在一局里。
- **第一阶段：瑞士轮**（所有游戏）：轮数默认宝可梦 6、狼人杀 4、红警 4。第一轮随机配对，之后**同分的互相配对**（尽量不重复对手）。**赢 +1，输 0，平 0**。人数凑不齐时轮空，轮空 **+1**（轮流轮空）。排名先比分数，再比 **Buchholz**（所有对手 / 狼人杀同桌的当前分数之和，越高说明对手越强），最后比开赛时抽好的随机顺序。
- **第二阶段，1 对 1 游戏（宝可梦、红警）：淘汰赛**。瑞士轮前 K 名进（K 是不超过 `min(top_cut, 人数)` 的最大的 2 的幂；默认 top cut 宝可梦 32、红警 8）。按名次种子配对（1 对 K、2 对 K−1……，1、2 号只可能在决赛相遇）。每对打 **三局两胜**（`best_of` 默认 3），输了就出局。平局和无结果的局重打；连续 3 局不算数时种子高的晋级。
- **第二阶段，狼人杀：决赛**。每桌 7 人，**赢的一方每人 +1**（两只狼，或预言家加 4 个村民；活着死了都算）。瑞士轮前 F 名进决赛（F 是不超过 `min(top_cut, 人数)` 的最大的 7 的倍数，默认 14 人、2 桌），打 4 局，每局前按分数蛇形重新分桌，决赛分最多的夺冠。
- **连接窗口和 no-show（缺席）**：每局创建后有 **4 分钟**（`join_window_seconds`，比赛可另设；**红警最多 4.5 分钟**）让我们连上（「连上」= 拿到座位后第一次调用 `get_game_state`）。窗口内没连上就**判负**，连上的对手 +1；狼人杀一桌有人没来，这桌不打，来了的每人 +1、没来的 0。轮与轮之间几秒到一分钟就开下一轮，所以**整个比赛期间进程必须一直开着**。
- **对局中掉线不会马上判负，但会被代打**：宝可梦对战 300 秒没出手就随机出招，狼人杀 120 秒没动作就默认动作；红警掉线的一方只是落后。重启进程会接着打同一个座位。
- 一局拖得太久会被判**无结果**（宝可梦 60 分钟、狼人杀 120 分钟、红警 50 分钟）：瑞士轮里双方都是 0，淘汰赛里重打。
- 排名和对阵表公开：`GET https://api.altruagent-game.com/tournaments/<tournament_id>`，网站上也有。

## D. 各游戏这次的要点

- **宝可梦**：正式赛和测试赛只打 `pokemon_vgc_doubles_draft`（双打选秀）。**选秀每一手 15 秒**，超时服务器随机替我们选（v4.2.2 已经把选秀预算压到 10 秒以内，见 `docs/progress.md`）；对战每步 300 秒。
- **狼人杀**：7 人一桌；措辞 10-04/05 从 "lynch" 改成 "eliminate / vote"，v4.2.3 新旧都认。
- **红警是正式赛项目**（实时，20 分钟一局，有自己的瑞士轮 + 淘汰赛）。我们没有红警策略：被分到时整场回答 `WAIT`（不发命令、不崩），等于这一局认输。**要不要报名红警比赛、要不要现在写一个红警 bot，等主人决定**（见 `docs/HANDOFF.md`）。不报名就不会有红警的局。

## E. 还不确定的

- 正式赛的具体时间表（10-16 当天几点开哪个比赛）、每个比赛的 `top_cut` / `best_of` 实际值：开赛后从 `GET /tournaments/<id>` 看。
- 热备是否被允许（官方只说「一把 key 一个运行时」，没说能不能放一个只在主进程停掉后才接手的备用）：问主办方。
- 我们的 agent 还没在真实平台上打过一局 Testing（10-06 改版之后）：主人生成 key 后第一件事就是建一局测试赛跑通。

# 2026-10-03 更新：红警已上线平台

Guang Cheng 要求只认 altruagent-game.com。以下是 10-03 对官网、平台接口、官方仓库和 Guang 回信的核对结果。

## A. 红警（Red Alert）已经能在平台上玩（最大变化）
- **CONFIRMED（平台技能文档 `docs/platform/redalert.md`，10-02/03 新增）**：游戏名 `red_alert`，1 对 1，用 OpenRA 引擎（开源重制的红警）。**实时制**：游戏每秒走 25 个 tick（引擎的时间单位），不等任何人。我们随时可以发一批命令，每批最多 20 条，两批之间至少隔 0.2 秒。没有聊天。
- **怎么赢**：摧毁对方所有建筑和 MCV（基地车，开局唯一的单位，展开后变成建造场）得 1.0。到时限（房主选 10–20 分钟，默认 20）还没分胜负，比 `(击杀价值 − 损失价值, 资产价值)`，先比第一项。双方 10 分钟都没有被接受的命令也按这个比。
- **支持单场和锦标赛（循环赛）**。单场由人类在 dashboard 建房，agent 自己 `join_session`。10-03 平台上已经有一个等待中的 `red_alert` 房间（10-02 23:59 创建）。
- **地图和开局**：地图池 3 张（sidestep 96×96、trapped 97×97、shadowfiend-ii 83×90，都是 2 人图）；阵营每局随机（盟军或苏军，兵种不同）；只有一辆 MCV；地图未探索；有随机奖励箱。起始资金：文档写 5,000，`get_game_config` 写默认 10,000，**以对局里的 `match_rules.starting_cash` 为准**。
- **完整数据已存**：`data/red_alert_config.json`（`python scripts/fetch_game_config.py red_alert`，只读）：42 种建筑、46 种单位的造价 / 建造时间 / 电力 / 前置 / 阵营 / 血量 / 速度 / 武器，3 张地图的出生点和地形图，命令格式。引擎版本 OpenRA commit `9b271c1a`。
- **官方仓库 PR #3（10-02，Warren Wu，未合并）**：SDK 增加实时模式（`WAIT` = 这次不发命令、`on_action_result` = 收到命令结果的回调、`game_config`）；官方 LLM 示例 agent 能玩红警。主办方实测：gpt-4o 能展开基地、造电厂 / 兵营 / 矿场、出兵进攻，两个 gpt-4o 一局 4.9 分钟分出胜负，198 次模型调用。
- **对我们的意义**：
  - **漏洞**：现在的 SDK 没有实时模式，红警的 `legal_actions` 结构也不同（是一组清单，不是可选动作列表），我们的 agent 会解析出 0 个合法动作 → 兜底失败 → 进程被杀。**碰到红警等于每局白送。**
  - **机会**：LLM agent 每次思考要几秒，脚本 bot 每 0.2–0.5 秒就能下一批命令，还不会犯低级错误。对 LLM 对手（包括 Oracle 要提供的通用 agent）这是四个游戏里最容易拉开差距的。
  - 红警是否计分仍未定（见 B），但 09-28 简报的条件「除非官方发布接口」已经满足。

## B. Guang Cheng 回信（Aaron 收到）
- **肯定有**：狼人杀、宝可梦 Showdown。
- **王者荣耀和红警**：还在做，希望两个都进正式比赛，**10/6 提交窗口开始前公布**。
- 官方网站是 altruagent-game.com。
- **Replit 可能给所有参赛者 pro 账号；Oracle 会提供通用 agent**；最终规则「很快」公布；鼓励学生自带 agent，成绩更好。
- 抄送了 Sandra，之后她会在官网上做 Q&A。
- **解读**：Oracle 的通用 agent 很可能就是大量对手的样子（LLM 驱动、慢、通用）。Replit pro 账号暗示可能在 Replit 上运行 agent，所以我们的包要能在 Replit 跑（需要 Node 和伤害计算器）。

## C. 官网（Last-Modified 10-03）
- **新增 Discord 社区**：https://discord.com/invite/yRFHT7EY5k 。更新可能先在这里发，建议 Aaron 加入并留意公告。
- 新增赞助商 Amazon Science Hub at UCLA（只在首页；赞助页仍只列 OCI 和 Replit）。
- 赞助页：**第 2 届比赛 2027 年 2 月在湾区举办**。
- 新官网明写「Ranked on a public leaderboard」（09-28 审查意见说这句只来自旧站，现在新站也有：CONFIRMED）。另写「或者用我们提供的通用 agent」「agent 提前提交，现场和同伴实时对战」。
- 提交入口仍没开放（HTML 注释：等平台能收作品再换成真实链接）。没有 FAQ / 规则页（faq.html、rules.html 都是 404）。
- 日期不变：提交 10/6–13；现场 10/16，9:00 PDT（暂定），UCLA Engineering VI 134。游戏仍列 4 个。

## D. 平台和官方仓库
- **官方仓库 altruagent-starter 是什么**：主办方的官方入门代码加 Python SDK。我们仓库里的 `altruagent/` 就是它（git remote 名 `upstream`）。0 star，唯一的 fork 是主办方自己（wu-warren），还看不到其他参赛者。
- **09-29 官方锦标赛运行方式（已合并进我们的 main）**：在锦标赛 dashboard 生成 Official Agent Key（`eak_live_...`，只显示一次），`python -m agent --check-tournament` 自检，比赛开始前 `python -m agent --tournament` 一直开着，平台分配的每一场自动接、各开一个进程、可同时多场；断了重开会接着打。**这基本说明正式比赛是我们自托管（LIKELY → 接近 CONFIRMED）。**
- 10-01 提交 b03b633：只改官方 LLM 示例的提示（双打目标写清楚是谁），不影响我们。从中得知 GameAPI 的双打招式选项新增 `target_options`（每个目标是 self / ally / opponent 加物种名），我们不读它，不受影响。
- 宝可梦、狼人杀、锦标赛的技能文档与 09-28 完全相同；`list_sessions` 改为列最近 25 个。
- `/stats`：887 个 agent（09-28 为 855）、60 个用户（58）、累计 2,124 局（1,996）。

## E. 由此产生的缺口（按急迫程度）
1. **红警 agent（P0）**：从零写规则驱动的脚本 bot（建造顺序、经济、电力、集结后进攻、基地防守），不靠 LLM。PR #3 合并前，先在分支上按 PR #3 的接口开发；本地没有引擎，测试要在平台上打（需要 Aaron 在 dashboard 建红警房间）。
2. ~~**部署支持 `--tournament`（P0，小）**~~：v4.1.2 已做。`scripts/run_agent.sh`、systemd 单元、Docker、压缩包、`selfcheck` 都认 `ALTRUAGENT_OFFICIAL_AGENT_KEY`，有它就跑 `python -m agent --tournament`（见 `docs/deploy.md`「官方正式赛」）。剩下的是拿到真 key 后由主人跑一次 `bash scripts/run_agent.sh check-tournament`。
3. **问主办方**：红警 / 王者荣耀是否计分；官方 key 由谁生成（dashboard 上线了吗）；Replit 是否是指定运行环境。

# 比赛情报简报（截至 2026-09-28）

写给团队负责人（Aaron）。本文只使用本轮线上调查得到的结果。

**标签含义**
- **CONFIRMED**：有一手来源证实。
- **LIKELY**：证据较强，但官方没有明说。
- **UNKNOWN**：没有找到可靠信息。

**小词典**
- **agent（智能体）**：自动玩游戏的程序，也就是我们要交的东西。
- **starter / SDK**：主办方提供的官方入门代码库 altruagent-starter。
- **LLM**：大语言模型，例如 GPT。
- **自托管**：比赛时由我们自己的电脑或云服务器运行 agent，再连到平台。
- **循环赛**：每两人都打一场。**瑞士制**：按战绩配对，打若干轮。**淘汰赛**：输了就出局。
- **VGC 双打选秀**：宝可梦官方双打规则，队伍通过选秀组建（双方轮流从公共卡池挑宝可梦）。
- **队伍预览**：开战前从 6 只里选 4 只上场、定 2 只首发。
- **兜底（fallback）**：出错时的保底走法。
- **公开理由（reasoning_summary）**：每步棋附带的一两句解释，观众能看到。
- **观战数据流（spectate feed）**：平台公开的实时对局数据接口。

---

## 1. 一句话结论

**大方向是对的，但有四处要马上调整。** 主攻「宝可梦 VGC 双打选秀」和「狼人杀」没错，这是平台上仅有的两个真正能玩、并且出现在锦标赛工具里的比赛游戏。要调整的是：

1. **截止日期变了。** 官网 9/27 更新后，提交窗口改为 10/6–10/13，不再是 10/9。
2. **囚徒困境和阿瓦隆降为"便宜保险"。** 新官网已把这两个游戏删掉，不再当重点。
3. **红警、王者荣耀暂不投入。** 平台上根本没有这两个游戏的引擎和接口。
4. **精力从"再强一点"转向"稳"。** 重点变成：绝不超时、绝不崩溃、能自己托管跑满全天，并让狼人杀第一次在真实平台上验证（目前真实对局数为 0）。

另外，比赛的四个游戏都没有被破解（没有"beat the game"的已验证最优解）。唯二被数学上解出的平台游戏（井字棋、四子棋）不在比赛里。

---

## 2. 已确认的比赛规则与形式

| 项目 | 目前知道的 | 状态 | 来源 |
|---|---|---|---|
| 以哪个官网为准 | 以 altruagent-game.com 为准（9/27–28 更新）。旧站 tournament-assets.vercel.app（9/17）已过时，但平台导航里的 "Hackathon" 仍链接到旧站 | CONFIRMED | https://altruagent-game.com/ ；https://tournament-assets.vercel.app/ |
| 提交窗口（Stage I） | **10/6–10/13**。旧写法 9/25–10/9 已作废。site.js 注释写着"2026-09-26 确认，10/6–13 是提交窗口，10/16 是活动本身" | CONFIRMED | https://altruagent-game.com/assets/site.js |
| 提交入口 | 还没开放。页面 HTML 注释写着"等平台能收作品时再换成真正的提交链接"。平台目前没有任何上传代码、仓库或容器的地方，"Upload your agent" 按钮只通向 API 注册页 | CONFIRMED | https://altruagent-game.com/ ；https://agent-acp.vercel.app/agent/signup |
| 到底交什么 | 官网只写"agent 提前提交，现场实时对战"，没说交的是代码、仓库还是一个注册好的 agent | UNKNOWN | 同上 |
| 比赛日谁运行 agent | SDK 唯一的正式运行方式是参赛者自己一直开着 `python -m agent`。测试座位也明确写着"自托管"。所以我们自托管的可能性更大，但这与官网"提前提交"的说法矛盾。docs/HANDOFF.md 说"主办方运行我们的代码"，没有一手来源支持 | UNKNOWN（偏向自托管，LIKELY） | https://raw.githubusercontent.com/UCLA-Trustworthy-AI-Lab/altruagent-starter/main/README.md |
| 现场（Stage II） | 10/16 "线下 hackathon"，地点 Engineering VI 134 Multi-Purpose West，9:00 PDT 开始（暂定）。观众"100% 线上"，直播"即将推出" | CONFIRMED（时间暂定） | https://altruagent-game.com/ |
| 比赛游戏 | 新官网列 4 个：宝可梦 Showdown、红警、王者荣耀（5v5）、狼人杀。阿瓦隆和对话式囚徒困境已被删掉，但阿瓦隆的演示视频还在页面上 | CONFIRMED | https://altruagent-game.com/ ；https://raw.githubusercontent.com/UCLA-Trustworthy-AI-Lab/altruagent-starter/main/GAMES.md |
| 实际能玩的 | 只有宝可梦（锦标赛唯一格式是 `pokemon_vgc_doubles_draft`）和狼人杀（7 人）。红警、王者荣耀标着"TODO — not yet available"，平台游戏配置里也没有它们 | CONFIRMED | GAMES.md；https://gameapi.altruagent-game.com/config/games |
| 赛制 | 文档只写了"2 人循环赛，所有对局一次性创建，进行中不显示排名"。平台占位表单提供循环、单败、双败、瑞士四种，并注明"尚未接通"。比赛当天用哪种不知道 | 文档 CONFIRMED；当天赛制 UNKNOWN | https://api.altruagent-game.com/skill/04-tournaments ；https://agent-acp.vercel.app/_next/static/chunks/9345ae7d235bc03b.js |
| 计分与排行榜 | 旧站写"公开排行榜排名"。排行榜有 wins/losses/draws/total_payoff/rank 这些字段。以下都没公布：按胜场还是按总收益排名；狼人杀怎么算（引擎按阵营 +1/−1）；几个游戏的成绩怎么合并 | 有排行榜 CONFIRMED；排名规则 UNKNOWN | https://tournament-assets.vercel.app/ ；https://api.altruagent-game.com/tournaments |
| 时间限制 | 宝可梦 VGC 实测用的是 Showdown "VGC Timer"（见下方说明）。单打 gen9same 实测每步 300 s。狼人杀和阿瓦隆闲置 120 s 后由平台代为出手 | CONFIRMED | https://gameapi.altruagent-game.com/games/5d946e30-dfbc-4e40-adc5-8d2b6c8377e2/spectate/state ；https://raw.githubusercontent.com/smogon/pokemon-showdown/master/data/rulesets.ts |
| 奖励 | 没公布。赞助表单把"奖品"列为待赞助项，说明奖品可能还没定。先例：该实验室 2024 年的 hackathon 奖金为 $700/$500/$300，另有实验室暑期实习机会 | UNKNOWN（先例 CONFIRMED） | https://altruagent-game.com/sponsors.html ；https://trustworthy-ai-ges-ucla.devpost.com/rules |
| 队伍与资格 | 没公布队伍人数，也没公布每队能有几个 agent。官网说参赛者是"各行各业的学生和专业人士"，不限 UCLA | UNKNOWN | https://altruagent-game.com/ |
| LLM 能不能用 | 没找到禁止条款。SDK 写的是"可以用任何框架、提供商或策略"，官方示例用参赛者自己的 OPENAI_API_KEY。主办方会不会提供 key 或算力，完全没提 | 允许：LIKELY；提供 key：UNKNOWN | README；https://raw.githubusercontent.com/UCLA-Trustworthy-AI-Lab/altruagent-starter/main/examples/llm_agent.py |
| 赞助商 | 新站列的是 Oracle Cloud（OCI）和 Replit，Nebius 已被移除。没找到给参赛者的算力额度计划 | 赞助商 CONFIRMED；额度 UNKNOWN | https://altruagent-game.com/ |
| 其他限制 | SDK "不是安全沙箱"。每个人类账户同一时间只能有 1 个进行中的 competition。公开理由上限约 280 字符 | CONFIRMED | README；平台 dashboard 前端代码 |

**VGC 计时器详细说明**
- 队伍预览有 90 秒。
- 每回合最多 55 秒。
- 整场共 420 秒总时间，不会随回合增加。
- 单回合超时，系统会替你出默认招式。
- 总时间用完直接判负。
- 文档写的"300 s 后随机出招"永远不会先触发。

---

## 3. 主办方画像与行为预测

### 3.1 主办方是谁

- **主办实验室** [CONFIRMED]：UCLA Trustworthy AI Lab，主任是 Guang Cheng 教授（统计/计算机，Amazon Scholar）。来源：https://faculty.stat.ucla.edu/guangcheng/2.html
- **同日活动：SAIR 峰会** [CONFIRMED]：10/16 当天有 SAIR 的"Science x AI Summit 2026 (UCLA)"。SAIR 是陶哲轩参与创办的非营利机构，演讲者有陶哲轩、Barry Barish 等。SAIR 原文："同日，中心举办首届 AI Agent Gaming Competition，由 UCLA Trustworthy AI Lab 组织"。来源：https://sair.foundation/events.md
- **比赛性质** [LIKELY]：SAIR 网站代码里的活动代号是 `ai-innovation-center-kick-off-ucla`，所以这场比赛很可能是 UCLA 一个新 AI 中心的开幕展示。
- **SAIR 的角色** [LIKELY]：只是联合主办和提供场地。规则和平台都来自 UCLA 实验室。
- **平台开发团队** [CONFIRMED]：主要是 UCLA 本科生（Daniel Zhou 负责 SDK，另有 Warren Wu 等），团队页共列 9 人。最新的 PR #2 在开出 12 秒后就合并，没有描述，也没有审查，作者里有 Claude 联合署名。来源：https://altruagent-game.com/team.html ；https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter/pull/2
- **顾问委员会** [CONFIRMED]：成员来自游戏行业。主席 Mike Fischer（前 Square Enix America CEO），另有 Mike Goslin、Chris Heatherly 等，多与 USC 游戏项目有关。
- **实验室研究方向** [CONFIRMED]：以合成数据、隐私、水印为主。2026 年的 agent 研究是"agent 失败归因"和"针对工具型 LLM agent 的攻击"。正在做的"对话式囚徒困境"基准会记录每轮的信息、信念、理由、行动，并允许信息不诚实。来源：https://faculty.stat.ucla.edu/guangcheng/1.html ；https://github.com/UCLA-Trustworthy-AI-Lab/Memory-Conv-PD-Benchmark
- **平台起源** [LIKELY]：大概率是 2026 春季的 STATS C163/C263 课程。平台上 855 个"已验证 agent"大多来自那门课，不代表现在的对手。

### 3.2 行为预测

| # | 预测 | 依据 | 状态 | 对我们意味着 |
|---|---|---|---|---|
| 1 | 当天是给 VIP、投资人和直播看的展示，主办方最在意流程顺畅：不卡、不崩、出手快、理由好读 | SAIR 峰会页面、演讲者和赞助商名单 | LIKELY | 稳定性和"看起来聪明"跟胜率一样重要。卡住或报错的 agent 可能被踢掉 |
| 2 | 规则、文档、游戏列表会在最后时刻变，平台会有 bug | 学生小团队，每 3–4 天一次无预告推送，几个网站的日期和游戏列表互相矛盾 | LIKELY | 每天盯上游。遇到未知阶段或游戏，agent 绝不能崩 |
| 3 | 比赛日很可能要我们自己让 agent 在线 | SDK 只有参赛者自己运行的模式；没有上传入口；测试座位写着"自托管"；Stage II 改叫"线下 hackathon" | LIKELY（官方未说） | 准备稳定的云主机加自动重启，同时保持代码可打包 |
| 4 | 真正计分的是宝可梦 VGC 选秀和狼人杀 | 占位表单的游戏列表只有宝可梦和狼人杀；Testing 文档只提这两个游戏的座位数；VGC 是"锦标赛唯一允许的宝可梦类型" | LIKELY | 主攻这两个 |
| 5 | 红警最多是表演 | 没有引擎和接口；演示视频是普通 OpenRA 桌面客户端的录屏，看不出谁在操控；9/19 之后没更新 | LIKELY | 不投入 |
| 6 | 大量对手会是官方的 gpt-4o-mini 通用 agent，或"永远选第一个合法动作"的示例 bot | 官方 llm_agent.py（对应官网说的"我们提供的通用 agent"）以及 basic/smoke 示例；GitHub 上几乎看不到其他参赛者 | LIKELY | 专门针对这两类对手优化 |
| 7 | 比赛日志会被拿去做研究（公开理由、聊天）。"可信 AI"是研究和展示的角度，不是额外的打分项 | 实验室研究方向；官网写按排行榜排名 | LIKELY | 不对别人的 LLM 做提示注入，不钻平台漏洞；同时防止别人通过聊天注入我们 |
| 8 | 狼人杀可能有真人坐进来，至少在表演赛里 | 官网写"识别另一个 agent——或人类——在撒谎"，并把"人机交互"列为考察能力；但平台文档说 7 个座位都是 agent | UNKNOWN | 狼人杀要读得懂真人随意、带错字的发言 |
| 9 | 奖励不高，主要是曝光和实习 | 2024 年 hackathon 先例；赞助表单里奖品待定 | UNKNOWN | 不影响技术方向 |
| 10 | 希望参赛者到场 | "线下 hackathon"；旧站写"参赛者请到现场" | 希望到场：LIKELY；是否强制、是否要 SAIR 峰会通行证：UNKNOWN | 到场时带能热修复的电脑，但 agent 仍跑在云主机上，不依赖会场 Wi-Fi |

---

## 4. 平台现状（2026-09-28 实测）

### 4.1 有哪些游戏

- **后端游戏引擎** [CONFIRMED]：OpenSpiel（约 100 个棋类/博弈游戏）加上自定义的 repeated_pd、avalon（5 人）、werewolf（7 人、8 种动作、最长 100 步）。没有红警，也没有王者荣耀。来源：https://gameapi.altruagent-game.com/config/games
- **宝可梦** [CONFIRMED]：走单独的适配器，下面跑私有的 Showdown 服务器。支持 gen9same、gen9random、gen9ou_teambuild、gen9ou_draft、vgc_doubles_draft。
- **官方宝可梦演示视频** [CONFIRMED]：展示的是 Gen 9 OU 公开选秀单打，不是 VGC 双打。比赛日用哪种格式：UNKNOWN。

### 4.2 锦标赛与测试阶段机制

- **公开锦标赛列表** [CONFIRMED]：`GET /tournaments` 不需要登录，最多返回 10 条。现在只有 10 个旧的等待中测试赛（7 个 repeated_pd、3 个 tic_tac_toe），都由同一个账号创建。没有宝可梦或狼人杀的正式赛事。
- **新增的调度字段** [CONFIRMED]：锦标赛记录里新出现了 `queue_id`、`min_participants`、`queue_timeout_seconds`、`scheduled_start_at` 等字段。这说明可以定时开赛，人数够了也会自动开赛，所以我们的进程必须提前在线。
- **容易误触发的开赛** [CONFIRMED]：报名把名额填满时会立刻开赛。随手 `GET /tournaments/{id}` 也可能触发一个已经到时间的开赛。只在准备好的时候加入，不要随便轮询单个赛事。
- **测试阶段（Testing phase）** [CONFIRMED]：9/28 合并进 SDK。流程如下：
  1. 人在"锦标赛 dashboard"里创建一场测试赛。
  2. dashboard 给每个座位一条命令，形如 `python -m agent --claim seatclaim_...`。
  3. 每个进程占一个座位：宝可梦 2 个，狼人杀 7 个。不需要 API key，每个座位都可以换成不同的 agent。
  4. token 只能用一次，并绑定领取它的那个进程。进程一死，这个座位就作废，只能重开一场测试赛。
  5. 我们的仓库已经合并了 `--claim` 功能。
- **测试阶段还不能用** [CONFIRMED]：部署中的前端仍是 build 6km10EQHb8aHG6EOBtk2v，里面没有测试赛界面。"创建锦标赛"仍是占位表单，横幅还写着旧日期 9/25–10/9。

### 4.3 最近的代码变化

- **9/24 PR #1** [CONFIRMED]：文档从 tic_tac_toe/repeated_pd/avalon 改为"狼人杀和宝可梦"，并写明"VGC 双打选秀是锦标赛格式"。可用的合法动作改为直接包含在 `get_game_state` 里；新增长轮询 `wait_for_update`；狼人杀里被淘汰的 agent 不再需要出手。
- **9/28 PR #2** [CONFIRMED]：加入测试阶段和公开理由接口（`WithReasoning`，理由会"显示给观众"）。同时新增四个示例 agent：
  - `basic_agent`：永远选第一个合法动作。
  - `messaging_agent`：每天说一句固定的话。
  - `smoke_agent`：选第一张卡、带前 4 只、打第一个对手。
  - `llm_agent`：gpt-4o-mini，单次请求超时 60 s，失败重试 1 次，再失败就选第一个合法动作。
- **更新规律** [CONFIRMED]：上游提交日期是 9/11–12、9/15、9/21、9/24、9/27–28，没有任何预告。按这个节奏，下一批（可能是提交/报名流程）大约在 10/1–10/3 [LIKELY]。
- **其他没变的** [CONFIRMED]：线上技能文档仍是 v0.3.0，和我们本地的副本完全一致。我们的本地仓库已同步到上游最新提交 5f25a31。

### 4.4 基础设施与活跃度

- **服务器** [CONFIRMED]：控制面是 AWS API Gateway 加 Express，在 us-west-2（俄勒冈）。游戏服务 GameAPI 用 FastAPI，前端在 Vercel。自托管时选美西机器，延迟最低。
- **活跃度** [CONFIRMED]：`/stats` 显示 855 个 agent、58 个用户、共 1996 局、0 局进行中。最近 20 局全是我们自己的 Yoriko_Red 对 Apocalypse_Red。GitHub 上除了官方 starter 找不到其他参赛者的仓库。
- **观战数据流是公开的** [CONFIRMED]：`/games/{id}/spectate` 和 `/spectate/state` 不需要登录。GameHub 每秒刷新一次，内容包括每步的公开理由、双方队伍预览时的选择、选秀理由和聊天接收者。
  - 对手能否在比赛中实时读到我们的理由和选择：LIKELY 能。
  - 是否会在双方同步出手之前就泄露：UNKNOWN。
  - 处理方式：只私下报告给主办方，不利用，公开理由里也不写底牌。

---

## 5. 各游戏：最优解、我们的准备、差距与工作量

| 游戏 | 会不会出现 | 有没有已验证最优解 | 最强已知方法 | 我们现在 | 接近最优需要什么 | 预估工作量 |
|---|---|---|---|---|---|---|
| **宝可梦 VGC 双打选秀** | 会，这是锦标赛唯一的宝可梦格式 [CONFIRMED] | 没有。有隐藏信息和随机性，队伍组合约 10^139 种 [CONFIRMED]（https://arxiv.org/abs/2506.10326） | PokaiTrainer（信念状态搜索）对约 1320 分的人类赢 59% 的三局两胜（https://arxiv.org/abs/2608.29197）。顶尖方案都是强化学习或搜索，LLM agent 容易连续出错和超时（https://arxiv.org/html/2603.15563v1） | **强。** 联合动作前瞻加精确伤害计算；真实平台 682 步决策，0 个非法动作；v3 正在集成 | 选秀 18 张卡全公开、没有太晶化，对搜索有利。最关键的是控时（55 s/回合、420 s 总时间），以及没有 Node 时伤害计算的兜底。对 smoke 和"第一个合法动作"bot 预期接近 100% 胜 | 控时：约 1 天内。v3 加基准测试：2–4 天（增量改进） |
| **宝可梦其他格式**（gen9 OU 公开选秀单打、teambuild） | 官方演示用的是这个；比赛日是否用：UNKNOWN | 没有 | 同上 | 单打在真实平台只打过 1 局；teambuild 从没在真实平台试过 | 冒烟测试（跑通一遍，确认不出错） | 约 0.5 天 |
| **狼人杀**（7 人：2 狼、1 预言家、4 村民；死亡公开身份；首夜平安） | 会 [LIKELY]；怎么计分 UNKNOWN | 没有。现有理论的规则都和我们的不同（https://arxiv.org/html/2408.17177v2） | 按我们的规则做的模拟（方法见表下）：村民方胜率约 23–45%，关键是预言家的信息和集中投票 [LIKELY]。LLM 研究发现：短、具体、前后一致的发言更有说服力；识破对手靠比对他"说了什么"和"投了谁"（https://arxiv.org/html/2407.13943） | **中等。** 规则型策略，真实平台 0 局；预言家/村民改进分支（09-28 时）还没合并（**后已合入，见 progress.md 狼人杀改进节、提交 43fb31d**）；每天只发一条模板消息 | 合并策略 → 真实 7 座自对弈 → 可选的 LLM 说服层（没有 key 时退回规则）→ 防注入、能读真人发言 | 3–5 天 |
| **红警** | 官网列了，但没有引擎和接口 [CONFIRMED]；当天计分的可能性低 [LIKELY] | 没有 | 如果用 OpenRA-RL 这类环境，脚本化的"建造顺序加定时进攻"很可能胜过 LLM；LLM 在里面连仗都不打（https://huggingface.co/blog/jadetan/openra-rl） | 无 | 等接口出来再说 | 未知，现在不投入 |
| **王者荣耀（5v5）** | 官网列了，没有引擎；公开环境 hok_env 只有 1v1 和 3v3，且需要 Windows 游戏核心 | 没有 | 腾讯 JueWu 用超大规模强化学习对顶级玩家胜 99.8%，我们复制不了 | 无 | — | 不投入 |
| **阿瓦隆（平台简化版）** | 新官网已删，但引擎还在 | 没有。标准 5 人抵抗组织的精确均衡是好人胜 30%，但规则不同（https://arxiv.org/abs/2308.16017） | 我们的模拟：好人"默认赞成、有证据才反对"，胜率 65–70% [LIKELY]。坏人组合只有 10 种，可以精确推理 | **很差。** 兜底走法是永远提议 P0+P1、永远投反对，当坏人时永远投失败 | 在 10 种坏人组合上做贝叶斯推理，再加简单规则 | 1–2 天（保险项，低优先） |
| **对话式囚徒困境** | 新官网已删；平台上还挂着旧的等待中赛事 | 没有统一最优，取决于排名规则 | **按胜场排名**：永远背叛，不会输掉任何一场 [CONFIRMED]。**按总收益排名**：先合作，被背叛就惩罚 2 轮（Two-Tits），已知的最后一轮背叛；已知只有 3 轮或更少时，一直背叛 [LIKELY] | **差。** 大概率永远合作、从不说话，任何背叛者都能剥削我们 | 按运行时的动作标签判断合作/背叛（不写死"0=合作"），收益表和轮数做成可配置 | 几小时（保险项） |
| **井字棋 / 四子棋** | 不在比赛里，只是平台测试游戏 | **有。** 井字棋双方完美下法必和；四子棋先手下中间必胜 [CONFIRMED]（https://en.wikipedia.org/wiki/Solved_game） | 井字棋用 minimax（穷举搜索），四子棋用位棋盘 negamax | 兜底走最左边或编号最小的格子，遇到像样的对手必输 | — | 小于 2 小时 / 0.5–1 天，不建议做 |

"我们的模拟"指 `scratchpad\ww_sim.py`，每种情况跑 20 万局，模型是简化的，按平台规则搭建。

---

## 6. 方向调整建议：接下来 10 天（9/28–10/8）按优先级

**关于日期**：任务模板写的是"10/9 截止"，但官网 9/27 已经改成 10/6–10/13。建议这样安排：
- 10/6 前准备好一个随时能交的版本。
- 10/9 当作内部保险截止日，防止日期再变。
- 10/13 是真正的最终截止。

**什么是"送分题"**：成本很低，能直接避免白白丢分的事。

### P0：今天到明天

1. **【送分题】Aaron 亲自给主办方发邮件。** 草稿已经写好。收件人是 Guang Cheng 教授（邮箱在实验室主页公开：guangcheng@ucla.edu），可以再通过官网链接的 LinkedIn 提醒 Warren Wu。问题见第 7 节。观战数据流的问题只能私下问，不要发 GitHub issue。
2. **【送分题】控时，绝不超时。**
   - 宝可梦：每步设 15–20 s 的硬预算；从对局日志的 "Time left" 读出剩余总时间，时间紧时自动降级；队伍预览控制在 90 s 以内。
   - 狼人杀：每步控制在 120 s 以内。
   - 原因：总时间用完直接判负，这是最不该丢的分。而官方 llm_agent 单次请求超时 60 s 还要重试一次，碰到 55 s/回合很可能超时，所以我们只要自己不超时就占便宜。
3. **【送分题】防崩溃。**
   - 遇到未知游戏或结构化动作模板时，只记日志、安全处理，不要陷入"崩溃 → 冷却 60 s → 重启"的循环。那样平台会替我们出手。
   - 做好断线重连；遇到未知阶段也不崩。
4. **【送分题】给每步加简短可读的公开理由。** 直播会显示这些理由，但里面绝不写底牌：狼人杀身份、查验结果、宝可梦下一步计划。练习时也不要在理由里写策略。
5. **更正过时文档**：docs/intel.md、docs/strategy.md、docs/HANDOFF.md。需要改的内容：
   - 日期改为 10/6–10/13。
   - 游戏列表改为 4 个。
   - 赞助商改为 OCI 和 Replit。
   - 删掉"官方示例在队伍预览会崩"（已不成立）。
   - 删掉"主办方托管我们的代码"（没有依据）。
   - 把时限改成 55/420/90 s。

### P1：9/30 到 10/4

6. **宝可梦**：
   - 完成 v3 集成。
   - 对 `examples/llm_agent.py`（gpt-4o-mini）和 `examples/smoke_agent.py` 跑基准测试。
   - 冒烟测试单打公开选秀和 teambuild 两条路径。
   - 测试没有 Node 时的兜底。
7. **狼人杀：合并改进分支，落实模拟得出的规则。**
   - 村民：不弃票，尽早把票集中到一个目标上。票数分散时狼方胜率可达约 97%。
   - 预言家：查到狼再跳出身份；快被投出时立刻跳。
   - 村民绝不假跳预言家。出现对跳时，投两个跳的人之一，死亡公开身份会证明谁是真的。
   - 狼：用私信统一投票和刀人目标；只在被指控时才对跳；刀掉已跳的预言家；避免投票明显同步。
8. **自托管方案，按"我们自己跑"来准备。**
   - 美西云主机（靠近 AWS us-west-2），自动重启，装好 Node，保留日志。
   - 一台机器只用一个 API key。
   - 做一次无人值守、连续数小时的演练。
   - 同时保证代码可打包：依赖锁定版本；没有 Node、没有 LLM 也能满实力运行；代码里不带任何密钥。
9. **每天只读监控**。只读脚本 `scratchpad\dashboard_watch.ps1` 已写好，每天盯：
   - 上游 starter 的提交（用 `git fetch`，别用有频率限制的 GitHub API）。
   - altruagent-game.com 的最后修改时间。
   - 平台前端 build id（一变就说明测试 dashboard 可能上线了）。
   - `/tournaments` 和 `/config/games`。

### P2：10/4 到 10/8

10. **测试阶段一开放，马上跑真实对局。** 建测试赛是人工操作，需要 Aaron 在 dashboard 里做。
    - 真实 7 座狼人杀：我们的 agent 加 llm_agent 或 smoke_agent 混坐。
    - 宝可梦：对 llm_agent。
    - 每个座位开一个终端；token 用 `--claim -` 手动输入，不存盘；比赛中不要关进程。
11. **狼人杀可选 LLM 说服层。**
    - key 和 base URL 从环境变量读取，方便接入主办方可能提供的端点。
    - 没有 key 时退回规则。
    - 过滤对手聊天里的注入指令。
    - 能处理真人的随意文字。
12. **10/6 冻结"可提交 v1"。** 提交窗口一开，先看表单要什么（只选一个注册 agent，还是要代码、仓库、容器）。"交了之后还能不能更新"要先问清楚。
13. **便宜保险，最后做，可以跳过**：囚徒困境（策略按排名规则切换）、阿瓦隆推理。红警和王者荣耀一律不投入，除非官方发布接口。
14. **隐私**：之后练习改用测试座位或中性名字。现在 GameHub 的公开列表里只有我们的对局，任何人都看得到。

**明确不要做**：
- 不利用观战数据流偷看对手。
- 不对其他 agent 做提示注入。
- 不按旧信息把囚徒困境和阿瓦隆当主线。

---

## 7. 仍不确定、需要问主办方的问题

以下 7 个问题可以合进同一封邮件：

1. **提交与运行**：提交的到底是"注册好、由我们自己运行的 agent（`python -m agent`）"，还是代码、仓库或容器？
   - 如果由主办方运行：Python 版本、有没有 Node.js ≥18、能不能访问外网、CPU/内存是多少？
   - 如果由我们运行：10/16 需要在线多久？断线怎么处理？
   - 提交后到 10/13 之前能不能更新？
2. **比赛游戏**：10/16 实际计分的是哪几个游戏？宝可梦用 VGC 双打选秀，还是官方视频里的 Gen 9 OU 公开选秀单打？红警和王者荣耀是参赛项目还是只做表演？
3. **赛制与排名**：
   - 用循环、瑞士还是淘汰？
   - 按胜场还是按 total_payoff 排名？
   - 7 人狼人杀按个人怎么计分（打几局、身份是否轮换）？
   - 各游戏成绩怎么合并？是否必须参加所有游戏？
4. **时限**：VGC 实测是 55 s/回合、420 s 总时间、90 s 队伍预览，文档写的是 300 s。比赛日以哪个为准？狼人杀是否仍是 120 s？
5. **LLM**：允许调用外部 LLM API 吗？主办方会不会提供 key 或端点（OCI/Replit）？有没有费用或延迟上限？
6. **观战数据流（只私下问）**：比赛中能实时读到对手的公开理由和队伍预览。agent 可以读这个数据流吗？正式比赛会不会延迟或屏蔽？
7. **现场与报名**：
   - 参赛者是否必须到场？是否需要 SAIR 峰会的现场通行证？
   - 队伍人数上限？每队几个 agent？
   - 有没有奖品？
   - 狼人杀会不会有真人入座？

**另外只有 Aaron 本人能做的事**：
- 测试 dashboard 上线后，登录并记录：阶段名称、报名表字段、截止时间、可选游戏、规则和 FAQ 原文。截图时遮住页面上的 Access token 和所有 seatclaim_ 命令。
- 查看 SAIR 峰会报名页（https://sair.foundation/events/science-ai-summit-2026-ucla/register），确认是否有现场报名截止日。
- 是否批准下载红警演示视频（35.9 MB）。价值很低，已在浏览器里看过，建议不下载。

---

## 审查意见（2026-09-28）

### 审查指出的遗漏
- 狼人杀的反证没写。docs/platform/SKILL.md:17 明写狼人杀是 'Standalone competitions only'，organizers sweep 也引用了这句。另外 7 人局本来就没法跑 2 人循环赛。简报 3.2#4 把狼人杀会计分标成 LIKELY，却没列这条冲突。
- 狼人杀的投入回报没写。solved-games 和 coverage-gap 两份调查都发现：一桌都是 first-legal bot 时，结果完全由座位顺序决定，我们一票改变不了多数。本地评估里，我们单个座位的得分和 baseline 一样，唯一的杠杆是说服。简报给狼人杀排了 3–5 天，却没说回报取决于同桌是谁。这会影响 P1 排序：真正能拉开差距的是宝可梦。
- 观战数据流泄露私信内容，和 P1#7 冲突。email deep sweep 发现 SSE 的 message 事件带着聊天内容和接收者名单。简报只写了'聊天接收者'，漏了'内容'。P1#7 又建议狼人用私信统一投票和刀人。如果私信在公开流或直播里能看到，读这个流的对手或真人就等于看到了狼的身份。
- 实验室 2024 hackathon 的先例只引了奖金。当时的其他要求都漏了：交一个能运行的 GitHub repo 加 3–5 页 slides，每队 3–5 人，两轮提交，由评委打分（Cheng 和业界工程师）。这几点直接关系到'交什么'，也关系到是否有路演或评审环节。
- 托管方式的调查结论互相矛盾，简报没交代。organizers sweep 认为，Testing 座位写'自托管'，而正式比赛用的是 'registered tournament agent'，两者对比说明正式比赛由主办方托管（LIKELY）。upstream-watch 深扫的结论是 UNKNOWN，另外两份深扫则偏向自托管。简报只写了'偏向自托管 LIKELY'。
- SAIR 自己办比赛的先例没写：评测环境由主办方控制，通过 OpenRouter 固定使用便宜模型，测试集不公开，Stage 1 没有奖金。这说明主办方可能指定 LLM 或禁止外网，而简报 LLM 一行只写了'允许 LIKELY'。
- Replit 取代 Nebius 成为赞助商，这条线索没展开。Replit 本身是开发和托管平台，可能意味着 agent 要在 Replit 上构建或托管。raw 里有这个推测。简报没提，也没安排在 Replit 或 OCI 的环境里测一遍能不能跑（有没有 Node、资源上限多少）。
- runner 的两个机制没写。第一，/agents/me/sessions 只返回最近 50 个 membership（STARTER_README.md:328，altruagent/models.py:461）。第二，锦标赛按 max_active_matches 分批开赛，文档说'可能长时间空闲，要继续轮询'（04-tournaments.md:63）。大规模循环赛下，比赛发现和长时间空闲是否正常，都需要实测。
- 宝可梦的两个细节没写。一是 pokemon.md:251 说选秀和组队阶段没有闲置超时，只有对战阶段受 Showdown 计时。二是 skill.md 说对手能看到我们的认输率，所以不要随便认输。
- 测试阶段的两个细节没写。一是深扫提到 dashboard 可以重新签发某个座位的 claim token，简报只说'只能重开一场'。二是'每个人类账户同时只能有 1 个进行中的 competition'这条限制是否适用于测试赛，目前 UNKNOWN。如果适用，P2#10 计划的 7 座狼人杀测试和宝可梦测试只能一场一场排队跑。
- 新官网写参赛者来自'各行各业的学生和专业人士'。announcements sweep 的推论是可能有很强的非学生对手，简报 3.2#6 没有体现这一点。
- Nebius 的额度已经申请过（见 HANDOFF），但 Nebius 已从赛事赞助商中移除。LLM 说服层靠哪份额度需要重新核对。

### 审查指出的依据不足 / 标签过强
- 第 1 节'唯二被数学上解出的平台游戏（井字棋、四子棋）'是错的。scratchpad\probe\config_games.json 里还有 checkers（2007 年弱解）、oware/awari（2002 年强解）、othello（2023 年有弱解的声明）、nim（Bouton 定理）、kuhn_poker 和 leduc_poker（均衡已算出）、blackjack（有最优基本策略）。dashboard 的 competition 选项里也有 blackjack 和 matrix_rps。raw 那条 CONFIRMED 只引了维基百科，没有和平台游戏表对照。'比赛的 4 个游戏都没被破解'这个结论本身仍然成立。
- 宝可梦'我们现在：强'没有依据。682 步决策、0 个非法动作全部来自 Yoriko_Red 对 Apocalypse_Red 的自对弈，只能证明动作合法，证明不了实力。我们从未和任何外部对手打过。'对 smoke 和 first-legal bot 接近 100% 胜'同样没测过。
- '下一批大约在 10/1–10/3 [LIKELY]'只是用 5 个提交日期外推出来的，应标为猜测或 UNKNOWN。
- 3.2#7 的依据'官网写按排行榜排名'出自旧站 tournament-assets（9/17）。raw 没有确认新官网还有这句，而 Stage II 已经改名为'线下 hackathon'。据此把'可信 AI 不是打分项'标成 LIKELY 过强。
- 3.2#6'大量对手会是 gpt-4o-mini 或 first-legal bot'标 LIKELY，依据是 GitHub 上看不到其他参赛者。但提交窗口还没开，参赛者也没必要公开仓库，这条应该是 UNKNOWN。
- '以 altruagent-game.com 为准'标 CONFIRMED 是推断，依据是它的 Last-Modified 更新、site.js 里有注释，而平台导航仍链接旧站。这条应为 LIKELY，其中日期部分可以算 CONFIRMED。
- 3.1'平台开发团队 [CONFIRMED] 主要是 UCLA 本科生'：raw 里对应条目标的是 LIKELY，本科身份来自搜索摘要和个人主页，团队页上 Shiwei Chen 属于 UC Berkeley。'陶哲轩参与创办 SAIR'在 raw 里是 LIKELY（二手来源），却放在 CONFIRMED 条目里。
- 时间限制行有两处问题。一是狼人杀和阿瓦隆的 120 s 引用的是 spectate state 和 rulesets.ts，这两份来源不支持这个数字，真正出处是 docs/platform/werewolf.md 和 avalon.md。二是'整场共 420 秒'漏了日志里的'90 sec grace'。我们的对局日志里 total 一直停在 420 没降过，所以 bank 实际怎么扣还没验证。
- 引用和推广有误。'识破对手靠比对他说了什么和投了谁'出自 Kaggle Game Arena 博客，不是 arxiv 2407.13943。'LLM 在红警里连仗都不打'是把 Qwen3-32B 的 5 局结果推广到所有 LLM。JueWu 99.8% 在 raw 里是 LIKELY（原文是 reportedly），简报没标。
- 工作量估计里，'控时约 1 天'、'v3 加基准 2–4 天'、'其他格式 0.5 天'没有来源。其余估计（狼人杀 3–5 天、阿瓦隆 1–2 天、井字棋 2 小时以内、四子棋 0.5–1 天）来自 coverage-gap。
- P0#1 说'草稿已经写好'，但草稿里 Aaron 的身份信息（姓 Wen、GitHub 账号 Astro-wen、'UCLA student'）在 raw 里没有来源，发送前需要 Aaron 本人核对。

### 还没人查过、可能改变优先级的角度
- Stage I（10/6–13）会不会就是资格赛或预赛？先例都有预选：CodeAlert 线上预赛取前 6 进线下；实验室 2024 年分两轮提交；SAIR 分 Stage 1/2。如果 Testing 或 Stage I 期间的对局会计分或用来筛人，10/6–13 本身就是比赛，而不只是交作品。
- '线下 hackathon'到底指什么，没人查。几个具体问题：当天要不要现场写代码或改 agent？红警和王者会不会只在当天放出？有没有路演、评委或观众奖（官网列的 5 个 discipline 可能对应奖项）？比赛中人工干预或热修复是否允许，还是会被判违规？
- 宝可梦对局的形式没人问。单局还是三局两胜？VGC 标准和 PokaiTrainer 的数据都是三局两胜。如果是淘汰赛加单局，方差会非常大。另外：由 pool seed 决定谁先选秀是否公平？选秀阶段没有超时，对手卡住怎么办？平局或双方都超时怎么判？
- 没有和强对手做基准。参赛者可以直接套用开源的强 Showdown bot，比如 foul-play、poke-env 的 baseline、PokeChamp、VGC-Bench 的 BC+RL 策略。我们只计划和 smoke、llm_agent 比。
- 一个 owner 名下有多个 agent 的问题，包括串通规则。我们账号下有 Yoriko_Red 和 Apocalypse_Red 两个 agent。7 座狼人杀里同一方的多个座位可以串通（2004 年 Southampton 先例），这可能算违规，也可能被别人拿来对付我们。第 7 节只问了'每队几个 agent'。
- 狼人杀的座位由谁补。如果参赛 agent 数不是 7 的倍数，空座是否由主办方的 smoke 或 llm_agent 补上？这决定我们在狼人杀上的投入有没有回报：一桌 first-legal bot 时我们毫无影响力。
- 比赛日负载没人压测。循环赛一次性建好所有对局，可能多场同时进行。我们靠搜索的宝可梦 agent 在一台 VM 上同时打多场时，每步 15–20 s 的预算和 420 s 的总时间是否还够？API Gateway 的 rate_limited 也要一起测。
- 如果由主办方、Replit 或 OCI 托管，运行环境是否兼容：Python 版本、有没有 Node、CPU 和内存多大、能不能访问外网、是否要求 Docker。另外，没有 Node 时伤害计算会降级，胜率损失多少也没人量化。
- 提交的代码是否对其他队伍或公众可见。2024 年先例要求交 GitHub repo，如果要求公开，我们的策略就会泄露。有没有许可证要求也不清楚。
- 截止时间的具体时刻和时区（10/13 几点 PDT）没人确认。10/13 到 10/16 之间、以及比赛当天能否更新 agent，也没人问。
