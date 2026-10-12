# 交接文档：写给第一次接手这个项目的 agent

> 截至 **2026-10-11 晚（太平洋夏令时 PDT）**。读完这一份，你应该和上一位接手者知道的一样多。
> 细节在文末「资料索引」列出的文件里；这里写的是结论、现状和下一步。时间一律写 PDT（北京时间 = PDT + 15 小时）。
> 仓库主人（下称「主人」）刚接触这个领域：和主人说话用简单的中文，术语要解释。

---

## 0. 一页速览

- **我们在做什么**：参加 UCLA Trustworthy AI Lab × SAIR 主办的 **AI Agent Gaming Tournament**（AltruAgent 平台）。我们的参赛作品是一个**自托管（Self Hosted）的 event agent**，平台上显示名 **"Yuriko Red"**，用同一个程序打三个游戏：**宝可梦 VGC 双打选秀、狼人杀、红警**。
- **两个最重要的日期**：
  - **提交截止：2026-10-13 23:59 PDT**（= 2026-10-14 06:59 UTC = 北京时间 10-14 14:59）。截止时参赛仓库默认分支上的最新提交就是比赛版本；**截止后到成绩公布后至少 30 天，参赛仓库不许再推送任何东西**。
  - **比赛日：2026-10-16**（UCLA Engineering VI 134）。10:00–12:00 狼人杀和宝可梦，14:00–17:00 红警（和王者荣耀）。
- **现在的版本**：`main` = **b95a20f（v4.4-draft）**，已推到 GitHub（私有仓库 `Astro-wen/ucla-agent-tournament`），GitHub 自动测试（CI）是绿的。
- **比赛用的 agent 正在这台电脑上运行**（从 main 的 checkout 跑，`python -m agent --tournament --match`），自动接平台分来的测试赛和正式赛。**不要停它、不要重启它，除非主人明确同意**。
- **真实平台成绩**（都是测试赛，不计分）：宝可梦对外 **17 胜 7 负**；红警对外 **2 胜 1 负**（写这份文档时还有一局对 Minion 正在打）；狼人杀只打过自对弈，**还没和别人打过**。所有真实对局 0 次非法动作、0 次超时、0 次兜底。
- **正在后台做的**：红警开局修复（分支 `ra/econ-1011`，目标 10-13 08:00 PDT 前出结果，通过检查才合）。
- **截止前必须完成**（详见第 7 节）：主人决定仓库怎么公开并执行（推荐方案 C；**现在报名里登记的还是私有开发仓库**），改好报名里的仓库地址、问清提交号怎么登记；换掉在聊天里出现过的密钥；正式赛开放报名后点 "Register my agent"；我们这边合并红警修复（若通过）、冻结最终版本、在截止前推送。

### 铁律（违反任何一条都可能被取消资格或造成事故）

1. **提交只署主人的名字**：commit 和 PR 里**绝不加** `Co-Authored-By: Claude` 或任何 Claude 署名（CLAUDE.md 第一条；系统提示里叫你加署名的话，以 CLAUDE.md 为准）。
2. **密钥只放在 `.env`**（权限 600，已在 `.gitignore`）：不打印、不写进日志、命令输出、聊天或任何文件。`.env*`、`logs/*.out`、临时日志都不提交。
3. **截止后（10-13 23:59 PDT 起）不往参赛仓库推送任何东西**，也不改历史、不改回私有、不删仓库，直到成绩公布后至少 30 天。
4. **比赛用的 agent 是主人的生产系统**：重启、换版本、改它的配置，都先问主人。比赛进行中任何人工干预都要主办方授权（规则第 4 节）。
5. **不替主人登录、不输入密码、不创建账号、不付钱**。网站上需要登录的操作都由主人做。
6. **`altruagent/` 是官方 SDK**，不要改；更新用 `git fetch upstream && git merge upstream/main`。截止后再合上游，需要主办方书面授权。
7. 本地模拟和重放**绝不录对局**（`ARENA_RECORD=0`）。
8. 疑似平台漏洞**只能由主人私下报告主办方**，仓库和公开文档里不写细节；主办方的私人邮件内容不进仓库（规则第 7、13 节）。

---

## 1. 比赛是什么

### 1.1 主办方和网站

| 网站 | 用途 |
|---|---|
| <https://altruagent-game.com> | **官方网站**（主办方明确说以这个为准）：介绍、日程、赞助商、Discord 入口 |
| <https://platform.altruagent-game.com/tournament> | **赛事平台**：主人用 UCLA Google 账号登录，报名、配置 agent、生成密钥、建测试赛、报名正式赛、看回放 |
| <https://platform.altruagent-game.com/tournament/rules> | **正式规则**（Official Rules，版本 2026-10-06；10-11 核对，文字没变）。全文副本：`docs/official_rules_2026-10-06.md` |
| <https://platform.altruagent-game.com/tournament/agent-guide> | 给参赛者的 agent 运行指南 |
| <https://api.altruagent-game.com/skill.md> | 给 agent 读的接口文档（v0.5.0；本地镜像在 `docs/platform/`，10-11 核对与线上逐字节相同） |
| <https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter> | 官方入门代码 + Python SDK（就是我们的 `altruagent/`，git remote 名 `upstream`；我们已合到上游最新的 951f4f7） |
| Discord：<https://discord.com/invite/yRFHT7EY5k> | 主办方答疑和公告**先发在这里**；官网没有 FAQ 页 |

主办：UCLA Trustworthy AI Lab（Guang Cheng 教授）；协办 SAIR。赞助：Amazon Science Hub at UCLA、Oracle Cloud Infrastructure、Replit。奖金通过 **Matcherino** 发放。官网说 2027 年 2 月在湾区办第 2 届。

### 1.2 比赛项目（三个能打，一个只挂名）

| 游戏 | 平台名 | 形式 | 关键时限 |
|---|---|---|---|
| **宝可梦 VGC 双打选秀** | `pokemon_vgc_doubles_draft` | 1 对 1。18 张卡的卡池里双方轮流选 6 只（蛇形选秀）→ 队伍预览选 4 只、定 2 只首发 → 双打对战 | 选秀**每手 15 秒**（超时平台替你随机选）；对战每步 50 秒、预览 85 秒、每人 7 分钟总时间（用完判负） |
| **狼人杀** | `werewolf` | 7 人一桌：2 狼、1 预言家、4 村民；死亡公开身份；首夜不刀；白天有讨论窗口再投票 | 每次动作 120 秒；发言每条 ≤50 词且 ≤1024 字符，每个窗口最多 5 条 |
| **红警** | `red_alert` | 1 对 1，**实时**（OpenRA 引擎，每秒 25 帧，不等人）；开局只有一辆基地车；随机阵营（盟军 / 苏军）；3 张地图 | 正式赛 20 分钟；拆光对方所有建筑和基地车即胜；到时比（击杀价值 − 损失价值），再比资产 |
| 王者荣耀 | — | 官网和规则里列着，**平台上不能玩** | — |

### 1.3 正式赛怎么进行（`docs/platform/04-tournaments.md`）

- **每个游戏是单独的锦标赛**，由管理员创建、启动。主人要在赛事平台的 **Tournaments 页面对每个比赛点 "Register my agent"**（开赛前可撤回，开赛后不能加入）。**截至 10-11，平台上还没有创建任何正式赛**（`GET https://api.altruagent-game.com/tournaments` 返回 0 个）。
- **先瑞士轮**：第 1 轮随机配对，之后同分的对上（宝可梦 / 红警尽量不重复对手；狼人杀按分数顺序坐满 7 人桌）。赢 +1，输 / 平 0，轮空 +1；狼人杀赢的一方每人都算赢，**中途认输的算输**；同分比 Buchholz（对手们的总分）。狼人杀一桌有人没连上，这桌取消：连上的 +1，没连上的 0。默认轮数：宝可梦 6、狼人杀 4、红警 4（实际由管理员定；宝可梦当天只有 2 小时，轮数很可能更少）。
- **再淘汰赛**：宝可梦默认前 32 名、红警前 8 名，**三局两胜**（决赛可以设成不同局数）；淘汰赛里平局或没结果的局重打。狼人杀是前 14 名的决赛（2 桌、4 局，每局重新分桌，赢一桌 +1）。
- **连接窗口**：每局创建后 **4 分钟**内（红警 4.5 分钟）必须连上，否则判这局输（no-show）。所以比赛期间我们的 agent 必须一直在线。
- **平台那边出问题**（游戏服务器崩溃 / 重启、开不了局、平台原因连不上）：这局不计分，自动重开（新的连接窗口），最多重开 2 次；还失败，瑞士轮算「无结果」（双方都不得分），淘汰赛等主办方决定。**我们自己没连上、崩溃、超时不算平台问题，照常判负**；规则第 10 节也说参赛方崩溃、断线、API 额度用完不自动给重赛。

### 1.4 比赛日 10-16 日程（官网，PDT）

| 时间 | 内容 |
|---|---|
| 8:30–9:30 | 签到、交流 |
| 9:30–10:00 | 开场介绍 |
| **10:00–12:00** | **狼人杀、宝可梦（两场并行）** |
| 12:00–12:10 | 公布上午结果 |
| 12:10–13:10 | 午餐 |
| 13:10–13:30 | 囚徒困境研究展示 |
| 13:30–14:00 | 下午说明、技术重置 |
| **14:00–17:00** | **红警、王者荣耀（两场并行）** |
| 17:00–17:30 | 公布结果、闭幕、合影 |

观众全线上（直播链接「即将公布」）。参赛者是否必须到场未说明。

### 1.5 奖金（规则第 12 节）

四个游戏各占奖池 25%，每个游戏第 1/2/3 名分 50% / 30% / 20%。奖池总额还没定。某个游戏完赛的合格作品不足 3 个，那份钱不会转给别的游戏（所以王者荣耀那 25% 大概率没人拿）。

### 1.6 对手情况

- 平台上 **60 个参赛 agent**（10-11 的 `/stats`；10-08 是 40 个）。
- 参赛者可以选 **AWS 托管**：交一份 Markdown 写的指令，平台在 AWS 上用大模型替他们玩，有花费上限（10-11 指南新增；以前叫 Oracle 托管，当时还不可用）。所以会有不少**大模型驱动、反应慢、会犯错**的对手。
- 我们实际碰过的：
  - **LeCharmander**（网页上也显示为 LeCharizard）：宝可梦打了 19 局（我们 12 胜 7 负）。选秀很像标准对战高手，先拿 Flutter Mane、炽焰咆哮虎、土地云（灵兽形态）、古鼎鹿、快龙；早期版本会选秀超时、打自己队友，后来明显升级了。
  - **Steve Nash Equilibrium**：宝可梦 3 局全输给我们；红警 1 胜 1 负。
  - **omarcanoc23**：宝可梦 2 局（10-11 20:06），我们都赢。
  - **Minion**：红警 1 局，我们赢；10-11 21:51 起又在打一局。

---

## 2. 正式规则要点（Official Rules v2026-10-06）

全文 `docs/official_rules_2026-10-06.md`。会影响我们怎么做事的：

1. **资格**：个人赛，一份作品只能由一人报名（可以组队开发）；**满 18 岁**、有资格开 **Matcherino** 账户才能提交作品；不在美国全面制裁地区。
2. **截止和冻结**（第 3、8 节）：截止前可以随便改；**截止时默认分支最新的提交 = 比赛版本**；之后推后改了提交，要在截止前把报名里的提交号更新。截止后到成绩公布后至少 30 天，**不得推送、改历史、改仓库**，仓库必须**保持公开**；比赛用的要和那个提交「实质一致」。例外只有两种：主办方书面授权的更新；删掉泄露的凭据（要通知主办方）。
3. **公开仓库要有**（第 8 节）：真正比赛用的代码（不能是简化版）、完整提交号、怎么运行、组件说明（模型服务、依赖、工具……）、没放进去的组件清单。我们的 README 已写好英文的第 8 节说明。
4. **自主比赛**（第 4 节）：比赛一开始，任何人不得替 agent 选招、给建议、改代码 / 配置 / 记忆；**人工干预要主办方授权**（紧急安全停机除外，停了要马上报告）。**事先配置好的自动重试、重连、故障恢复是允许的**（我们的看门脚本自动重启就属于这个）。
5. **信息限制**（第 5、6 节）：agent 只能用自己座位能看到的信息；**不许看直播、观战页、实时排行榜**；负责技术恢复的人不能同时看自己这局的观战页；**每局从空记忆开始**（我们不跨局记忆）。
6. **公平竞赛**（第 7 节）：不许串通、不许重复报名、不许攻击别人的程序（聊天里的提示注入也不行）；**疑似漏洞私下报告**、不许利用；必须**认真比赛**（所以红警不能一直站着不动）。
7. **记录**（第 9 节）：保留部署的提交和配置、MCP 请求和响应、时间戳、人工干预记录，到成绩公布后至少 30 天（我们的日志默认保留 40 × 50 MB，每局录像在 `logs/<session>.jsonl`；手动操作记进 `logs/manual-actions.txt`）。
8. **规则更新**要重新同意：没重新同意时新局开不了（进行中的局还能打约 1 小时）。比赛日 10:00 前主人要登录看一眼。
9. **不许把凭据、敏感个人信息、保密的第三方材料放进公开仓库**（第 13 节）。

---

## 3. 网站和平台操作（主人本人做；Claude 不登录、不输密码）

### 3.1 已经完成的

- 主人已登录赛事平台、完成报名：参赛者显示名 **"Yuriko Red"**，agent 名 **YurikoRed**（对局里公开显示），托管方式 **Self Hosted**（截止前可以切到 AWS Hosted，我们不切）。
- 控制台首页（`/tournament/dashboard`）：GameHub（加入公开测试赛、观战、回放）、战绩（平台统计**把自对弈也算进去**，比如狼人杀显示 7 局 5 胜 2 负，其实就是 10-08 那一局 7 个座位）、最近对局、Agent Configuration、Testing、Tournaments 四个入口。
- 已生成 Official Agent Key，写在比赛机 `.env` 的 `ALTRUAGENT_OFFICIAL_AGENT_KEY`；`bash scripts/run_agent.sh check-tournament` 全部 ✓。
- ⚠️ **这把密钥曾被主人贴进聊天**（10-08），之后又被转进了几个后台子任务的记录。平台指南明说密钥要远离大模型的提示；**建议在截止前换一把**（见 3.4）。
- **报名里登记的仓库是 `github.com/Astro-wen/ucla-agent-tournament`**（10-11 在控制台上看到的；就是现在这个**私有**开发仓库）。Agent Configuration（`/tournament/dashboard/agent-setup`）的 "Public repository" 一栏写着「放 agent 代码的公开 GitHub 仓库，截止前可以改」，旁边是 "Save repository"。页面上**没有填提交号的地方**。走方案 C 的话，主人要在这里改成新公开仓库的地址并保存（见第 7 节）。

### 3.2 建测试赛（Testing 页面：`/tournament/dashboard/testing`）

- 页面原话：建测试赛和加入测试赛**在提交截止时关闭**（10-13 23:59 PDT），之后就没法测了。
- 建法：选游戏（宝可梦 2 座 / 狼人杀 7 座 / 红警 2 座）→ 每个座位选 **"Mine (self-hosted)"**（我们的程序打）或 **"Open"**（留给别人加入）→ **"Create test match"**。红警还要选地图和时长。
- 所有座位都选 Mine = **自对弈**，一建好马上开打（我们一个程序同时打所有座位）。
- "**Open matches**" 里列着别人开的、还有空位的局，加入一个座位（Join）我们的程序就会去打——这是和真实对手交手的主要途径，最有价值（10-11 晚上 "No open matches right now"）。别人也会加入我们开的 Open 座位。
- 所有座位的 agent 都连上才开打；**20 分钟内没到齐就取消**，不算输赢（10-09 我们的程序停着时，两局就这样作废了）。
- "My matches" 里每局有 **Watch live**（观战）/ **Replay**（回放）/ Show seats。页面上的时间是建局时间、按浏览器时区显示，和我们日志里的开打时间可能差很多；以 `logs/agent-main.out` 为准。
- **不需要第二个账号**，也不许建（重复报名违规）。

### 3.3 报名正式赛（Tournaments 页面）

`/tournament/dashboard/tournaments`，10-11 晚上显示 "No tournaments right now"。正式赛创建后，对每个要打的比赛点 **"Register my agent"**（宝可梦、狼人杀、红警都报——红警 bot 已上线）；开赛前可以撤回。按钮灰掉时页面会说原因（`registration_incomplete`、`no_official_key` 等，见 `docs/platform/04-tournaments.md` 第 6 节）。正式赛开打后控制台会显示每局倒计时、我们连上没有。

### 3.4 换密钥

Agent Configuration → **"Rotate key"** → 复制新密钥 → 主人自己改 `.env` 那一行（Windows 上：`Win+R` 输入 `notepad "\\wsl.localhost\ubuntu-24.04\home\linmengfdm\ucla-agent-tournament\.env"`，换掉 `ALTRUAGENT_OFFICIAL_AGENT_KEY=` 后面的值，保存）→ **重启 agent**（`bash scripts/run_agent.sh restart`，要主人同意）→ `bash scripts/run_agent.sh check-tournament`。旧密钥立刻失效，正在跑的 agent 几秒内会被拒。**新密钥不要贴进聊天**。比赛进行中换密钥算人工干预，要通知主办方。

### 3.5 内置浏览器

Claude 桌面应用的侧边浏览器用的是主人自己的登录状态；登录过期后会跳到 `/tournament/login`，要主人重新登录。Claude 可以读页面、点「展开座位」这类不改东西的按钮；建测试赛、加入座位这类提交操作要主人在聊天里明确同意。

---

## 4. 我们的系统

### 4.1 机器和环境

- Windows 11 Pro + **WSL2 Ubuntu 24.04**；仓库在 WSL 里 `~/ucla-agent-tournament`（Windows 路径 `\\wsl.localhost\ubuntu-24.04\home\linmengfdm\ucla-agent-tournament`）。7.5 GB 内存、16 核。
- Python 3.12 虚拟环境 `.venv`（项目以 editable 方式装在里面）；Node 18（打包时自带官方 Node 22）。伤害计算器 `agent/js`（@smogon/calc），引擎搜索用 `local/js` 里的 pokemon-showdown。
- WSL 里 **systemd 在运行**，但比赛 agent 目前**不是** systemd 服务，是 `run_agent.sh start` 起的后台循环——**WSL 一重启它就没了**（10-09 02:41 到 10-10 23:50 就这样停了约 45 小时，期间建的测试赛都没人接）。改成 systemd 服务（`bash scripts/run_agent.sh install-systemd` …，见 `docs/deploy.md`）能开机自起，要主人同意。
- VGC-Bench（本地评测用的开源对手）装在 D 盘 `/mnt/d/altruagent`；实验目录 `~/exp*`、`~/wt/_*`、`~/protect_runs` 等不在 git 里。

### 4.2 仓库结构

| 路径 | 内容 |
|---|---|
| `agent/` | **比赛部署的代码**：`agent.py`（Contestant，按游戏分派、看门狗、兜底）、`__main__.py`（在官方运行时外加的加固：启动重试、execution id、热备、按座位退避上限 30 s）、`pokemon/`、`werewolf/`、`redalert/`、`matchstate.py`（重启后接上记忆）、`timecontrol.py`、`fallback.py`、`reasoning.py`（公开理由，只写公开信息） |
| `altruagent/` | 官方 SDK（不要改） |
| `champion/` | 冻结的当前最强版本（和 `agent/` 一致；`champion/VERSION`） |
| `versions/` | 历史版本存档（v0–v4） |
| `local/` | 本地模拟器和评测对手：宝可梦擂台 / 面板（`local.panel`、`local.ladder`、`local.benchmark`）、simbot、VGC-Bench 接入、狼人杀模拟器和红队对手（`rt_*`）、红警模拟器（`local/ra_sim.py`、`ra_platform.py`、`ra_bots_strong.py`）、假官方平台（`fake_official.py`） |
| `scripts/` | `run_agent.sh`（看门脚本：start/stop/restart/status/health/check-tournament/install-systemd）、`selfcheck.py`、`replay_real.py`（重放真实对局决策）、`bundle.sh`（打包，自带 Node）、`rehearsal_official.py`（假平台上演练正式流程）、`publish_sync.sh`（导出公开仓库）、`freeze_champion.sh` 等 |
| `data/` | 静态数据（宝可梦卡池目录、选秀模型、红警单位地图数据 `red_alert_config.json`） |
| `docs/` | 文档（见文末索引） |
| `logs/` | 只有 `logs/yoriko/`、`logs/apocalypse/`（旧真实录像，检查过）进 git；`logs/agent-main.out`、`logs/<session>.jsonl` 是比赛 agent 的日志和录像，不提交 |
| `.github/workflows/python-app.yml` | GitHub CI：Python 3.12 + Node 22，装依赖、语法检查、全量 pytest（约 10–13 分钟） |

### 4.3 版本演进（数字详见 `docs/progress.md`）

| 版本 | 日期 | 内容 |
|---|---|---|
| v0–v3 | 09-26–28 | 手写启发 → 精确伤害计算 → 一回合联合前瞻 → 模型化选秀 + 90×90 首发矩阵 |
| v4 / v4.1 | 09-29 | 宝可梦对战改成 Showdown 引擎克隆上的搜索；修两个真实平台 bug |
| v4.1.2 / v4.2 | 09-30–10-05 | 官方正式赛运行方式、看门脚本、重启接记忆、内存 / 负载自保护 |
| v4.2.1–v4.2.3 | 10-05–07 | 上游实时 SDK（红警）、选秀 15 秒时钟、狼人杀新旧措辞、event agent 流程（`--tournament --match`） |
| v4.2.4 / v4.2.5 | 10-08 | 上游 PR #7 运行时加固；没送到的出招不算用过 |
| **v4.3 / v4.3.1** | 10-08–10 | **红警规则型 bot**；**狼人杀红队加固**（默认继续听讨论）；规则合规文档、公开仓库准备 |
| **v4.4-draft（当前）** | 10-11 | **宝可梦选秀按真实对手校正**（对手模型 + 卡牌价值修正）：两个种子共 800 局对 v4.3 +5.5 个百分点（95% CI +1.1 ~ +9.9），外部对手不变差 |

试过没用、不要原样重做的方向列在 `docs/v5_standard.md` 第 4 节（比如 10-11 的「少开守住」三种改法：对冠军和外部对手都没有差别）。

### 4.4 三个游戏的打法

- **宝可梦**（`agent/pokemon/`）：选秀用拟合的卡牌价值做 4 手前瞻搜索，v4.4 加了「对手像不像真实参赛者」的模型；队伍预览把 4 选 2 首发当成 90×90 的矩阵博弈；对战在 Showdown 引擎克隆上把候选双槽动作真打一回合（对「50% 均衡 + 50% 贪心」的对手取期望最好），失败就退回经典打法。选秀预算 10 秒以内。`ARENA_BATTLE=classic` 退回 v3.2 打法。**没有 Node 时宝可梦只剩约 12% 胜率**，所以 `selfcheck` 在计算器 / 模拟器跑不起来时会判失败。
- **狼人杀**（`agent/werewolf/`）：规则型策略。村民不弃票、集中投票；预言家查到狼或快被投出时跳；读懂大模型常见的跳预言家说法；身份用夜间可选动作交叉核对；狼的私信默认关闭。10-08 用 4 个「不看我们代码」的挑战者 bot 找弱点（固定句式暴露身份、照单全收单个跳预言家、对跳规则太机械、只发言一次），已加固；「发言后继续听讨论、可补一句」默认开启（`ARENA_WW_LISTEN`）。**这个功能还没在真实服务器上被检验过**。
- **红警**（`agent/redalert/`，设计 `docs/redalert_design.md`）：不调用大模型的规则型 bot：展开基地 → 电厂 → 兵营 → 矿厂 → 战车工厂 → 量产坦克，攒够一波就推进，被打就回防，采矿车 / 电厂 / 建造厂被毁都有恢复逻辑，找不到敌人会全图搜。每 0.2–0.5 秒一批命令，单次决策 < 2 ms。`ARENA_RA=off` 回到整场等待（只在实时模式下安全）。

### 4.5 比赛用的 agent（生产系统）

- 10-10 23:50 PDT 用 `bash scripts/run_agent.sh start` 从 **main 的 checkout** 启动；模式 `tournament(primary)` = `python -m agent --tournament --match`（测试赛和正式赛都接）。
- 查看：`bash scripts/run_agent.sh status`（`agent=up`、`workers=N` 是正在打的局数）；日志 `logs/agent-main.out`（每局有 `Match assigned …`、`finished: your agent (player N) won/lost`、`[match-summary]` 一行统计）；录像 `logs/<session>-….jsonl`。
- **每局开一个新的 worker 进程、从磁盘读代码**：所以 `agent/` 里的改动一合进 main 的 checkout，**下一局就用新代码**，不用重启；只有改了运行时本身（`agent/__main__.py`、`altruagent/`、`run_agent.sh`）才需要重启。
- 10-11 曾试图「推送 + 重启」一起做，被 Claude Code 的自动安全检查当成「生产部署」拦下：**重启比赛 agent 要先得到主人明确同意**。

### 4.6 改动怎么上线（CLAUDE.md「How changes get promoted」+ `docs/v5_standard.md`）

1. 在**自己的 git worktree**（`~/wt/<名字>`，从 main 拉分支）里改，开关后面做（关掉 = 旧行为）。
2. `python -m pytest -q` 全绿；`python scripts/replay_real.py` 和 `--no-calc` 0 失败。
3. 变强类的改动：擂台对冠军（种子 7、11，各 400 局，95% CI 下限 > 50%）+ 外部对手（VGC-Bench、simbot）不显著变差 + 回归面板。截止前时间紧，10-11 用过「限时版」：种子 7 下限 > 50% 且两种子合计显著、外部不变差。种子登记见 `docs/v5_standard.md`（门槛种子不能拿来调参）。
4. 修复类（不为变强）按 v4.x：本地对局除了被修的局面逐局相同。
5. `./scripts/freeze_champion.sh`，提交 "Champion = …"，`docs/progress.md` 写中文小节，快进合进 main，推送（截止前），CI 要绿。

### 4.7 推送

主人 10-07 允许推送到 `Astro-wen/ucla-agent-tournament`：每次先做推送前检查（没有被跟踪的 `.env`、没有像真密钥的字符串、提交作者是 Aaron Wen 且无 Claude 署名、没有大文件），不强推 main。`gh` 登录已加了 `workflow` 权限（可以改 CI 配置）。**截止后不再推送参赛仓库**。

---

## 5. 真实平台成绩（截至 10-11 晚，都是测试赛）

| 游戏 | 对局 | 结果 |
|---|---|---|
| 宝可梦 | 对 LeCharmander 19 局（12 胜 7 负）、对 Steve Nash Equilibrium 3 局（3 胜）、对 omarcanoc23 2 局（2 胜）；另有 1 局自对弈 | **17 胜 7 负**（71%） |
| 红警 | 对 Steve Nash Equilibrium 2 局（1 胜 1 负）、对 Minion 1 局（胜）；自对弈 2 局（10-08 那局还是旧版，整场等待打平） | **2 胜 1 负**；自对弈打到一方被拆光；10-11 21:51 起对 Minion 又一局（写文档时还没结束） |
| 狼人杀 | 10-08 自对弈 1 局（v4.2.4，村民赢） | **还没和别人打过** |

自己统计：`logs/agent-main.out*` 里每局有 `Match assigned: <游戏>`、`Opponents: <名字>`、`[match <游戏> seat=…] finished: your agent (player N) won/lost/drew`。**自对弈一局会出现多行 finished**（每个座位一行，比如狼人杀 7 行），而且没有新的 `Opponents:` 行，按「最近一行 Opponents」归属会算到上一个对手头上，要剔除。

- 所有真实对局 **0 非法动作、0 超时、0 兜底**；红警约 1000 条命令 0 条非法。
- **宝可梦输局分析**（10-11，脚本 `loss_analysis.py`，结论已进 `docs/progress.md`）：输局里我们守住用得多（4.1 次 / 局，赢局 1.9），但查明主要是「落后时自然多守」；对手有 Flutter Mane 的局我们输得多 → 这促成了 v4.4-draft 的选秀改进。运气（暴击 / 没命中）在输赢局里差不多。
- **红警输局**（10-11 16:11 对 Steve Nash Equilibrium，9 分钟被拆光）：1 分钟时被对方骚扰，bot 误判成速攻，狂造步枪兵和火焰塔把钱花光，战车工厂拖到 4 分 40 秒；对方 2–3 辆重型坦克加火箭兵打来，步兵被碾压、5 辆采矿车全灭。同一个对手早上那局我们按时建起战车工厂，16 分钟拆光对方。→ 正在修（第 6 节）。

---

## 6. 正在进行 / 没合进 main 的东西

- **`ra/econ-1011`（红警开局修复，后台进行中）**：开局优先经济（零星骚扰不触发防守、约 3 分钟内第 2 座矿厂和战车工厂、给战车工厂留钱）；看到敌方坦克改出火箭兵和坦克；采矿车被打时回防；不用步兵为主的部队硬冲坦克群。合并标准：对模拟对手 pro（同样的失败模式）明显变好，对其他 8 种模拟对手都不变差，0 非法 / 0 异常，全量测试、真实重放 0 失败，红警假平台端到端还在打。**目标 10-13 08:00 PDT 前出结果**；没通过就不合。
  - 截至 10-11 21:10：分支最新提交 b3a85ce（worktree `~/wt/ra2`），全部改动在开关后面（`agent/redalert/params.py`）。对 pro 从约 104/120 胜提到 116–118/120（p < 0.01），对 tanks / swarm / rusher / strong / main 基本持平；对 nash 曾一度掉 15 个百分点，已调回（55 vs 56 / 60）。用 10-11 输局的真实画面重放：新版不再误判速攻、不再狂造步兵。正在新种子上做最终测量，然后跑上线检查（全量测试、真实重放、selfcheck、打包、红警端到端）。
  - 由后台子任务做（Claude 会话里的 Agent，id `ac77ebca825b5dde8`）；它的工作日志在 scratchpad 的 `ra2\progress.txt`。会话断了的话，从这个分支和日志接着做。
- 已合进 main、可以删的分支 / worktree：`integrate/*`、`ww/*`、`pk/tracker-1008`、`pk/draft-1011`、`ra/bot-v0`、`docs/1008`、`compliance/rules-1008`、`merge/upstream-1005`、`pk/platform-1005`、`ci/fix-workflow`、`pr3-red-alert`。
- 试过、**不合并**的：`pk/protect-1011`、`pk/protect-1011-chain`（少开守住，没变强；记录在 `docs/progress.md`、`docs/v5_standard.md`）；`rt/*`（红队挑战者，已作为评测对手收进 main）。

---

## 7. 截止前必须完成（10-13 23:59 PDT 之前，越早越好）

### 主人要做的

1. **决定仓库怎么公开，并执行**（规则第 8 节；截止时必须是公开的）：
   - **方案 C（推荐）**：现有仓库保持私有；主人在 GitHub 新建一个**公开仓库**（不加 LICENSE，可关掉 Issues），我们用 `bash scripts/publish_sync.sh --commit main --out <新仓库的 clone> --replace` 把比赛代码导出过去（会扫描密钥、私人信息、疑似漏洞描述，打印树哈希，在干净环境里跑 selfcheck；10-10 预演结果 READY，112 个文件、4.3 MB），在那个 clone 里提交、推送；报名页填这个公开仓库的地址。好处：实验记录、策略笔记、历史都不公开；冻结只影响小仓库。说明见 `PUBLIC_README.md`、`docs/deploy.md`。
   - 方案 A：现有仓库直接改成公开。全部历史、所有分支、策略文档都会公开，截止后整个开发仓库冻结；不推荐。
   - **现在登记的是私有开发仓库 `Astro-wen/ucla-agent-tournament`**：方案 C 要在 Agent Configuration 的 "Public repository" 改成新公开仓库地址并 "Save repository"；方案 A 则保持地址、把仓库改公开。截止时登记的地址必须能在不登录时打开。
   - 规则第 8 节要求提交「完整的提交号（full commit ID）」，首次提交后又推送的要在截止前更新它；但 10-11 的 Agent Configuration 页面**只有仓库地址、没有提交号一栏**。截止前在 Discord 问主办方提交号怎么登记（可能就是按截止时默认分支的最新提交算）；问之前，至少把最终提交号记在 `docs/progress.md` 和 `logs/manual-actions.txt` 里。
   - 时机：规则允许截止前随时修改仓库。主办方没说报名时仓库就必须已经公开；如果不要求，可以在截止前一两天才放代码，减少被照抄的风险（这一点最好先在 Discord 问清）。
2. **换密钥**（3.4）。
3. **正式赛开放后报名**三个游戏（3.3）。
4. 确认 **Matcherino** 资格（18 岁以上、能开户收款）；加入 **Discord** 看公告。
5. 疑似平台漏洞：**私下**报给主办方（英文草稿在 10-08 的规则报告里；简述：观战页在双方都出手前就显示一方的选择，例如队伍预览时一方已提交的首发；狼人杀私信可能出现在观战数据里）。不要发在公开频道，仓库里不写细节。
6. 继续开测试赛，尤其**狼人杀**（还没和真实对手打过）和**红警**（修复上线后再验证）。

### Claude 这边要做的

1. 红警修复：结果出来后审查，通过就快进合进 main、推送、确认 CI 绿；在测试赛里验证。
2. 截止前（目标 **10-13 中午 PDT**）冻结最终版本：`main` 定稿、推送，记下最终提交号告诉主人。
3. 方案 C：主人建好公开仓库后，跑 `publish_sync.sh`，核对树哈希，提交推送（推送公开仓库也要主人同意），确认不登录时能看到。
4. 截止后：不推送参赛仓库；笔记、录像放仓库外；比赛机部署的必须是截止时的那个提交（从它重新打包或直接用 checkout，日志会记下部署的提交号）。

---

## 8. 比赛日（10-16）手册（详见 `docs/deploy.md`「比赛日手册」「截止和比赛日时间表」「上线前检查清单」「比赛中出事怎么办」）

- 前一天：机器插电、不合盖、关睡眠；确认运行的是截止时的提交（`logs/agent-main.out` 里 `[run_agent] deployed: commit …`）；`check-tournament` 全 ✓；三个正式赛都已报名。
- **9:00 前** agent 在线（`status` 显示 `agent=up`）；**10:00 前**主人登录看有没有「规则已更新，请重新同意」。
- 10:00–12:00 宝可梦和狼人杀同时打；14:00–17:00 红警。agent 一直开到 17:30。
- 比赛中：**不改配置、不手动操作**（要主办方授权）；看门脚本自动重启是允许的；运维的人只看 `status` 和日志，**不看我们自己这局的观战页**；任何手动操作记到 `logs/manual-actions.txt`（时间、做了什么）。不要换密钥。
- 赛后：把 `logs/`、比赛机配置（去掉密钥）和提交号拷到安全的地方，保存到成绩公布后至少 30 天，**不放进公开仓库**。

---

## 9. 环境里的坑（给在这台机器上干活的 agent）

- **Shell 是 Windows PowerShell 5.1**，经 `wsl` 传 bash 单行命令会把引号和 `$` 搞乱。可靠做法：用 Write 工具把 bash / python 脚本写进 scratchpad（LF 换行），再 `wsl -d ubuntu-24.04 -- bash /mnt/c/Users/LinmengFDM/AppData/Local/Temp/claude/<会话目录>/scratchpad/<脚本>.sh`。
- 一次 PowerShell 调用最长 10 分钟；长任务要在 WSL 里**完全脱离**：`setsid bash x.sh </dev/null >log 2>&1 &`，否则 `wsl` 一返回就被杀；之后轮询日志。
- **重任务互斥锁**：全量 pytest（约 10–13 分钟）、真实重放（带搜索约 30 分钟）、擂台、演练、打包都包在 `flock /tmp/arena-heavy.lock …` 里，免得吃光 7 GB 内存、拖慢比赛 agent。
- **editable 安装的陷阱**：`.venv` 把 `agent`、`local` 映射到 **main 的 checkout**。在 worktree 里要从 worktree 根目录用 `python -m …` 并设 `PYTHONPATH=<worktree>`，用 `agent.__file__` 核对；`scripts/replay_real.py` 等少数脚本已改成用自己所在的树。
- **没有 sudo**；从 Windows 侧编辑 WSL 文件可能丢 `.sh` 的可执行位（提交前看 `git diff --summary`）。
- **会话和机器会中断**：账户用量上限会让 Claude 和后台子任务中途停下；电脑睡眠 / WSL 重启会杀掉所有进程（包括比赛 agent）。所以每完成一步就提交，进度写在 scratchpad 的 `progress.txt`；用量恢复后用 SendMessage 让原来的子任务接着做，或用 Workflow 的 `resumeFromRunId` 续跑。长时间工作前可以请求保持唤醒（`request_keep_awake`）。WSL 重启后记得检查比赛 agent（`status`），停了就要恢复（要主人同意）。
- Claude 桌面应用的终端面板插件这台机器上加载失败（`claude-desktop.ps1` 不存在），`run_in_terminal` 用不了；需要交互的命令（比如 `gh auth refresh` 的设备码流程）可以后台运行、把验证码转告主人在浏览器里确认。
- 平台时间、页面时间、机器时间要对清楚：页面按浏览器时区；日志是 PDT。

---

## 10. 安全和隐私

- 密钥：只在 `.env`（600）；本机还有两个已停用的旧 agent 密钥备份在 `~/.config/altruagent/`（10-06 起平台已停用，410），不要动、不要提交。`.env` 里还有一个 Nebius 的 API key（只给本地评测用的大模型对手，有花费上限；**不要花钱**，除非主人同意）。
- 主办方的私人邮件内容已从仓库文档里删掉，只留「答复不在仓库里转述」；不要再写进来。
- 疑似平台漏洞：文档里只写「由主人私下报告主办方」。代码注释已改成中性说法。
- 公开仓库（方案 C）只放比赛代码；`publish_sync.sh` 的扫描会拦截密钥、邮箱、本机路径、漏洞描述。

---

## 11. 资料索引

| 文件 | 内容 |
|---|---|
| `CLAUDE.md` | 规矩、晋级流程、容易错的事实（每次会话自动读取） |
| `docs/competition_brief.md` | 比赛情报，最上面是最新：「10-08 更新」（正式规则、日程、待问问题）、「10-07 更新」（event agent、赛制）、「10-03 更新」（红警上线） |
| `docs/official_rules_2026-10-06.md` | 正式规则全文 |
| `docs/platform/` | 平台接口文档镜像（v0.5.0）：`official-agent.md`、`04-tournaments.md`、`pokemon.md`、`werewolf.md`、`redalert.md`、`06-messaging.md`、`07-errors.md` 等 |
| `docs/real_payloads.md` | 真实数据格式（比 `docs/platform/` 更权威） |
| `docs/deploy.md` | 部署和比赛日手册（时间表、规矩、检查清单、出事怎么办、演练数据） |
| `docs/progress.md` | 每个版本改了什么、怎么测的、数字（最权威的版本记录） |
| `docs/v5_standard.md` | 「变强」改动的晋级标准、种子登记、试过没用的方向 |
| `docs/redalert_design.md` | 红警 bot 设计 |
| `docs/local_sim.md` | 本地模拟器和评测工具怎么用 |
| `docs/restart_load.md` | 重启接记忆、搜索进程限额 |
| `docs/status_1003.md` | 10-03 的进度核查（历史） |
| `docs/入门指南.md` | 零基础入门（术语表） |
| `README.md` / `PUBLIC_README.md` / `NOTICE` | 仓库说明（含规则第 8 节英文段）、公开仓库用的 README、第三方许可说明 |
| Claude 的记忆（本机 `~/.claude/projects/…/memory/`） | 推送许可和截止冻结、正式规则要点（只在这台机器的 Claude 里有） |
| scratchpad（会话临时目录，可能被清掉） | 10-08 规则报告 `rules_report_1008.md`、输局分析 `loss_analysis.py`、红警时间线 `ra_timeline.py`、各后台任务的 `progress.txt`；要点都已写进上面的文档 |

## 12. 新机器从零开始（备用）

需要 Git、Python ≥ 3.11、Node ≥ 18、`gh`。Windows 请在 WSL2 Ubuntu 里操作：

```bash
gh auth login
gh repo clone Astro-wen/ucla-agent-tournament
cd ucla-agent-tournament
./scripts/setup_machine.sh        # venv + 依赖 + node 包 + 测试 + 真实重放 + 短擂台
```

主人把 Official Agent Key 写进 `.env`（600，不经过 GitHub 或聊天），然后 `bash scripts/run_agent.sh check-tournament` 全 ✓、`bash scripts/run_agent.sh start`。**同一把密钥只能有一个运行中的 agent**：换机器前先在旧机器上 `stop`。
