# 版本进化记录

每个版本上位前都必须：擂台赢当前冠军（`local.ladder`，95% 置信区间下限 > 50%）、重放全部真实平台决策 0 失败（`scripts/replay_real.py`）、全部测试通过。历史版本存档在 `versions/`，`python -m local.benchmark` 会让新版打遍所有历史版本和参照对手。

| 版本 | 日期 | 核心改动 | 对上一冠军 | 对官方 baseline | 对随机 |
|---|---|---|---|---|---|
| v0_heuristic | 09-26 | 属性克制 + 威力估算 + Fake Out/顺风等手写加分 | — | 97.7% | 97.3% |
| v1_exact_damage | 09-26 | Smogon 精确伤害计算（每回合一次批量）、按对手当前血量算击杀概率、两槽联合选招；修正对手血量百分比导致「多重鳞片」误判 | **66.8%**（400 局，CI 62–71%） | 99.5% | 99.0% |
| v2_joint_lookahead | 09-28 | 双打一回合联合前瞻：我方最多 5 种双槽组合 × 对手最多 6 种应对排成收益表，按「0.4×最坏 + 0.6×按威胁 softmax 加权的平均」选（sm-k6，8 对手面板扫出的最优）；修正前瞻偷换主动换人 / 对队友出招；计算器冷启动预热；对队友出招按真实效果计分；纳什混合代码保留、默认关闭 | **60.1%**（种子 7/11/23 共 1200 局，CI 57.3–62.8%；7→242/400，11→246/400） | 99.5% | 99.5% |
| v3_draft_preview | 09-28 | 模型化选秀（6,165 局 v3 自对弈重新拟合；卡牌指纹 + 双特征先验；节点预算）+ 90×90 首发矩阵博弈 + 速度线 / 守住 + 计算器修正（多段攻击、气势披带 / 结实、大将、烧伤大小写、缓存顺序）+ v2.1 前瞻（α0.4、等权、K4——v3 所有检查都用这组；在 v2 上与 sm-k6 打平） | **86.0% / 85.0%**（种子 7 / 11，各 400 局，CI 82.3–89.1 / 81.2–88.2） | 100.0% | 100.0% |
| v4_engine_search | 09-29 | 对战改用引擎搜索：在 Showdown 引擎的克隆上把我方约 8 种双槽组合和对手的应对都真打一回合，对「50% 均衡对手 + 50% 贪心对手」取最优；冠军的打法当回退和候选之一；选秀 / 首发不变 | **57.5% / 63.5%**（种子 7 / 11，各 400 局，CI 52.6–62.3 / 58.7–68.1；和同一批局的冠军镜像配对 +9.8 / +10.8 个百分点） | 100%（100 局） | 100%（100 局） |

真实平台：v0 vs v0 已打 5 局（1 局单打 + 4 局 VGC 双打），v1 vs v0 5 局（3 胜 2 负），**v2 vs v1 10 局（09-28，8 胜 2 负）**，**v3.2 vs v2 20 局（09-28，18 胜 2 负 = 90%，Wilson CI 69.9–97.2%；事先定的「≥15 胜算明显领先」）**，**v4 vs v3.2 31 局 20 胜（09-29：v4.0 11 局 8 胜，其中 1 局 v4 发出非法动作；修好后 v4.1 20 局 12 胜、0 非法，见 v4.1 节）**；到 09-28 为止的 1341 个真实决策全部可重放，0 次非法动作 / 回退 / 超时。公开理由被服务器接受，观战接口能看到。VGC 计时实测：预览 90 s（另有 90 s grace）、每回合 55 s、总时间 420 s，78 局里总时间一次都没被扣过；gen9same 单打是 300 s。

### v2 复核要点（09-27/28，Windows + WSL 新机器）
- 8 对手面板（official_baseline、random、v0、v1、v2 镜像、cautious、greedy、protect_spam，同种子配对）平均胜率：v1 63.0% → la-fixed（0.6 最坏 / 等权 / K4）67.7% → sm-k6 70.2%；新种子 53 上 sm-k6 72.5% vs la-fixed 70.6%（+1.9，p<0.001），没有任何对手显著变差。
- 对 v1 的优势有一部分是专门克制 v1：同种子配对 1000 局对 v0，la-fixed 68.4% vs v1 65.2%（+3.2）。对陌生对手要打折扣。
- 纳什混合（sample / argmax）：对 v1 48.8% / 49.3%（各 1200 局），对 v0 也掉 11–14 个百分点 → 淘汰，代码保留默认关闭。
- 走棋决策平均约 34 ms、p99 约 130 ms（平台上限 300 s），擂台 0 次超时。
- 面板工具与实验记录在本机 `~/exp3`（panelkit、sweep 结果），尚未进仓库。

### v3 复核要点（09-28）
- 对 v1：1089/1200 = 90.8%（CI 89.0–92.3）；8 对手面板平均 94.25%（种子 41）/ 92.85%（种子 97），最弱对手（la-fixed）也有 88.4 / 84.4%。
- 基准（种子 31，每个对手 200 局）：official_baseline 100、random 100、v0 94.5、v1 90.0、v2 88.0。
- 逐层贡献（对 v1 配对）：计算器修正 +3.1、速度 / 守住 +1.5（不显著）、90×90 首发 +10.9、模型化选秀 +15.9 个百分点。v3 加固（节点预算、卡牌指纹）不改变强度（与未加固版本对打 48.5%），但同种子两次运行结果完全一致。
- 最坏延迟（半个 CPU）：选秀 1.4 s、队伍预览 2.7 s、对战 84 ms；平台实测时限 90 s / 55 s / 420 s 总时间。
- **风险：所有增益都来自本地模拟，v3 还没在真实平台打过**；选秀模型的卡牌价值是「v3 自己打自己时」的价值；真实平台出现过 4 张目录外的卡（pelipper、scream-tail、ursaluna、tyranitar），按未知卡先验处理。
- 整合记录：本机 `~/exp4/MERGE_NOTES.md`、`~/exp4/evalv3`。

### v3.1（09-28）：控时 + 防崩溃 + 公开理由（不改变正常对局）
- **控时**（`agent/timecontrol.py`）：每种决策按平台显示的计时（对战 55 s / 回合、420 s 总时间，预览按 90 s 保守估 30 s）算硬截止；选秀搜索、首发求解、前瞻和计算器在截止前自动降级；策略在看门狗线程里跑，超时就用安全合法动作代答。
- **防崩溃**（`agent/fallback.py`）：`choose_action` / `choose_message` 永不抛异常；未知游戏类型、未知阶段、奇怪的负载或动作模板一律记日志并给出确定性的安全合法动作。计算器进程挂掉会带退避自动重启（最多 6 次），不再整局关闭。
- **公开理由**（`agent/reasoning.py`，说明见 `docs/deploy.md`）：默认 `safe` 模式只写公开信息（狼人杀夜里不写、白天投票统一一句；选秀写刚选的卡；预览和对战只写回合与公开击倒数），`ARENA_REASONING=off` 可完全关掉。
- 检查：pytest 629 通过；682 个真实决策重放 0 失败；擂台种子 7 / 11 = 195 / 204（与 v3 自对弈完全相同）；基准与 v3 完全相同；擂台中看门狗代答 0 次。
- 未验证：平台如何扣 420 s 总时间（录像里一直是 420）；公开理由从未真正发到服务器（首次真实对局要确认观战页能看到）。`scripts/stress_concurrency.py` 可测多场并发时的延迟。

### v3.2（09-28）：单打格式 + 贴近真实对手的评测工具（VGC 双打不变）
- **单打**（`agent/pokemon/singles.py`）：官方演示视频是 Gen 9 OU 公开选秀单打，平台还有 same_team / random / OU 组队。新增单打专用策略（精确伤害计算、换人记忆、单打选秀威胁表），本地单打模拟器 `local/singles_env.py` / `local/singles_eval.py`。留出种子 5、每格 200 局，对 official_baseline / random / v0 / v1 / 冠军：same 96.5 / 99.5 / 95.0 / 97.0 / 96.5；random 97.5 / 98.0 / 86.0 / 85.0 / 86.0；teambuild 98.0 / 100 / 82.5 / 83.5 / 83.5；draft 99.0 / 99.0 / 97.0 / 98.0 / 93.0。v3 在同样条件下对 v0 只有 56 / 46 / 47.5。4000 局 0 次非法动作。真实单打录像 36 个决策 0 非法，伤害预测 30/30 在区间内。
- **评测工具**：`local/panel.py`（8 对手面板 + 官方示例 agent：basic = 永远第一个合法动作、smoke、llm_agent 的确定性替身），候选按真实 VGC 计时跑；用法见 `docs/local_sim.md`。种子 43 上 v3 面板均值 92.5%（CI 91.5–93.6），2600 局最慢决策 1.49 s，0 次被计时器代打、0 次超时判负。v3 比 v2 高 +20.1 个百分点（按局聚类的 CI 17.4–22.8）。
- 检查：pytest 712 通过；682 个真实决策重放 0 失败；VGC 真实决策与合并前逐个相同；擂台种子 7 / 11 = 195 / 204、基准与 v3 完全相同。
- 未知：平台真实的 OU 选秀卡池、组队目录、组队座位发模板时平台怎么处理；单打没有外部对手可测，组队是最弱的格式（约 83%）。

### 选秀模型重拟合（drafter-refit，09-28，实验：模型**未**上位）
- **本地环境更像平台**（不改变 agent 的下法：1269 个真实 VGC 决策 + 30 局本地对局与冠军逐个相同）：目录补上真实记录里出现过的 5 张卡（pelipper、scream-tail、ursaluna、tyranitar，以及 09-28 实战 A/B 里新出现的 toxapex；每张的完整配置在 39 局真实选秀里都只有一种，48/48 通过 Showdown 校验）；每局 18 张卡按真实出现频率抽（`data/vgc_pools.json`，39 个完整真实卡池；最新 20 局里新卡明显变多，平台的卡池分布在变）；和平台一样把撞道具、不能选的卡也列进 `available_cards`（真实记录 624 次，全部是道具冲突）；新发现的「死局」（剩下的卡全撞道具、一张都不能选，以前本地直接崩溃，约万分之一局）改为这一手放宽道具条款并计数（擂台 / 基准 / 面板输出里的「draft dead ends」）。`ARENA_POOL=legacy` 复现旧环境（只从旧的 43 张卡里抽；擂台 / 基准的 1000 个对局种子抽到的卡池与旧环境逐个相同；整局复现：擂台种子 7 / 11 = 195 / 204，和上面 v3.1 / v3.2 的记录一样）。采集只读 VGC 双打选秀（按平台的 `game_type` 过滤），单打 OU 选秀的卡不会混进来。换了抽卡分布，擂台 / 基准 / 面板的绝对数字和旧记录不能直接比。
- **新环境下的基准线**（48 张卡、39 个真实卡池的频率；agent 与 aa48e95 完全相同，以后的改动和这些数字比，旧数字要用 `ARENA_POOL=legacy` 才能比）：擂台（冠军自己打自己）种子 7 / 11 = 191 / 211（400 局；种子 7 的 47.8% 只是座位 / 种子噪声，两边代码相同）；基准（200 局）official_baseline / random / v0 / v1 / v2 / v3 = 100 / 100 / 96.0 / 92.0 / 88.5 / 51.5；13 对手面板（种子 43）均值 92.4%（CI 91.1–93.7），最低 v3 镜像 48.5%。全部 0 次非法动作，0 局选秀死局。
- **混合对手数据**：7,486 局，我方（随机性选秀）对 7 类对手：v3 自己、card_score 选秀 + 我方驾驶、v2、v1、v0、「最高种族值」选秀 + v1 驾驶、随机选秀 + v2 驾驶；加上原来的 6,165 局 v3 自对弈一起拟合（`scripts/draft_population.py`、`scripts/fit_draft_model.py`，每类对手单独一个强度偏置，避免把弱驾驶员的输算到它选的卡头上）。
- **留出检验**（拟合时整类对手不给看，只在它身上测；按驾驶员分组 v1+bst / v2+rand / v0 / v3+cs3）：新模型的对数损失在每一组都不比旧模型差，平均 −0.0024（±0.0008），外来驾驶员 −0.0033（±0.0009）；去掉含 4 张新卡的对局也成立。「对手手里的卡另算价值」的非对称版本对外来驾驶员更准（−0.0063），但在自对弈上更差，面板也更弱，没有继续。
- **对非自己的对手没有可测的提升**（两种卡池频率都测了）：
  - 19 个真实卡池的频率（v3.2 上）：擂台对冠军 204 + 214 / 800 = 52.3%（CI 下限约 48.8%）；13 对手面板（种子 43、每个对手 200 局、按真实计时）与旧模型**逐局配对差正好为 0**（95% CI ±1.0；面板工具显示 REJECT 只是浮点误差 −2.7e−19 < 0，按它自己的规则是 HOLD）；选秀专用面板（种子 57 / 58，对 v0/v1/v2/最高种族值/随机选秀共 2000 局）+1.35 个百分点（p≈0.08），对「card_score 选秀 + 我方驾驶」−3.6（400 局，p≈0.14）。注意：这几类对手正是训练数据里的对手类型（种子和卡池不重叠，但对手是「见过的」）；而且当时随机选秀对手的随机数没有逐局配对（那 400 局实际上不配对，只增加噪声，已修）。
  - 39 个真实卡池的频率（最新 main 上，新卡更常见）：擂台 223 + 210 / 800 = 54.1%（种子 7 的 CI 下限 50.9%；同种子下旧模型「自己打自己」是 191 + 211）；面板 +0.6（CI −0.8 ~ +2.0，HOLD，没有对手显著变差 / 变好，去掉 v3 镜像后 2400 局只多赢 7 局；只看训练时完全没见过的 6 个对手 greedy、protect_spam、basic、smoke、llm_fallback、llm_timeout：1200 局多赢 9 局，+0.75，CI −0.3 ~ +1.8）；基准（v0 / v1 / v2 也是训练见过的对手） v0 / v1 / v2 / v3 = 97.0 / 95.0 / 93.5 / 47.0，旧模型 96.0 / 92.0 / 88.5 / 51.5。
  - 按事先定的规则（留出更准 + 擂台不差 + 对非自己的对手更强）**不上位**，模型留作实验（`~/exp5/patches/drafter-refit-model-on-infra.patch`，套在本改动 `drafter-infra-final.patch` 之上）。在新卡多的卡池下，擂台种子 7 单独过了 CI 下限 > 50%，但换种子 11 复查没过（下限 47.6%），所以也不满足 CLAUDE.md 的晋级规则。对外来对手的提升若有也只有约 +0.5~1 个百分点，而且这是在训练见过的对手类型上估的；真正没见过（样本外）的只有面板里的 greedy、protect_spam、basic、smoke 和两个 llm 替身，要在它们身上用更多局数才能确认。
- 目录里 5 张新卡暂由旧模型的「未见过的卡」先验打分（平台上本来就是这样）：`local/pool.py` 的 `NOT_IN_DRAFT_MODEL` 明确列出，测试要求「目录 − 模型 = 这个列表」，以后再采集到新卡不会悄悄漏掉；拟合脚本只写进有训练对局的卡（以前目录里没有对局的卡也会以 0 分 + 指纹写进模型，冒充「已拟合」），所以只有真正用含新卡的数据重拟合后，才能把卡从这个列表删掉。

### 部署就绪（09-28，对局不变）
- `scripts/run_agent.sh`（start / stop / restart / status / health / tmux / install-systemd；崩溃自动重启、日志轮转、机器级锁保证一个 agent 密钥只跑一个进程）、`deploy/arena-agent@.service`、`requirements.lock`、`scripts/bundle.sh`（打包时自带校验过的官方 Node v22.23.3）、`deploy/Dockerfile`、`scripts/selfcheck.py`。用法见 `docs/deploy.md`。
- **没有计算器（Node）时宝可梦只剩 11–13% 胜率（对有计算器的自己）**，所以 selfcheck 和 run_agent.sh 默认在计算器跑不起来时拒绝启动（`ARENA_ALLOW_NO_CALC=1` 才放行）。
- 演练（本地假平台 + 各种故障注入）：130 分钟 systemd、两次 12 分钟全故障演练，0 非法 / 0 超时 / 0 判负 / 停止后 0 残留进程；内存约 400–450 MB。
- 检查：pytest 926 通过；1341 个真实决策重放 0 失败；擂台种子 7 = 191/400（与之前完全相同）。

### 真实大模型对手（09-28，Nebius，只用于本地评测）
- `local/llm_client.py`（OpenAI 兼容，只从 `.env` / 环境变量读 `NEBIUS_*`；每次请求先按最坏情况记账，超过 `NEBIUS_BUDGET_USD`（默认 5 美元）就拒绝，调用方退回规则；账本在 `~/.cache/altruagent/llm_usage.jsonl`）和 `local/llm_seats.py`：宝可梦的 `llm_real` 对手（官方 `examples/llm_agent.py` 接 Nebius）、狼人杀的 `--llm-real N`（同一副牌同时跑替身作配对对照）。默认模型 Qwen/Qwen3-30B-A3B-Instruct-2507。
- 宝可梦：当前冠军对 `llm_real` 20 局 19 胜（95%，CI 76–99%），对方 7% 的回答不合法、0 次超时。
- 狼人杀（新策略，40 局）：**每桌只有我们一个座位时，对真实大模型每座 52.5%，对替身 67.5%（−15，CI −28~−2）**，主要输在村民；5 个座位时两者持平。说明之前的替身偏向我们，狼人杀对真人 / 真模型的收益要打折扣——这是做发言层的理由。
- 到 09-28 累计花费约 0.35 美元；狼人杀 LLM 层试验后（09-28 20:17）真实累计 **1.04 美元**（所有者额度 25 美元）。
- **预算上限加固（09-29）**：曾以为狼人杀试验花到 3.25 美元、超过 1.84 的上限，复查发现是**看账本的方法错了**——旧格式每个请求写两行（先按最坏情况「预留」，再写实际花费替换它），把 cost_usd 一列直接相加会重复计算；按上限检查自己用的算法重放账本，最高 1.04 美元，从未超限。但对抗测试找出并修好了 10 个真能多花钱的漏洞：请求发出后出错被记成 0 元（可无限重试）、数字多的提示 token 估少了（改成 1 字节 1 token 的真上限）、账本最后一行写了一半、运行中删除 / 截短 / 替换账本会让预算归零（现在一律拒绝）、账本设成 /dev/null、预算写错（如 '$1.84'）悄悄回到默认 5 美元（现在全部拒绝）、代码里的上限能超过环境变量设的上限、不带账本的客户端各花一份等。新增硬上限 20 美元和按次运行的小上限 `NEBIUS_RUN_BUDGET_USD`。57 个测试全是假服务器，0 次真实调用。看花费请用 Ledger 重放（`local/llm_client.py` 的 `Ledger`），不要直接加 cost_usd 列。

### 外部风格对手 simbot（09-28，评测工具，agent 不变）
- `local/ext_opp.py` + `local/js/simbot.js`：每个对战回合在**真 Showdown 引擎的克隆**上把双方的双槽组合都打一遍（选定回合 + 双方贪心再打 1 回合），排成零和矩阵博弈，用 regret matching 求均衡后抽样；不用伤害计算器、没有手写战术。选秀和首发借用冠军（`simbot`）或 v1（`simbot_v1`）的代码。用法：`python -m local.panel --opps simbot [--same-team] [--save-logs] [--only N]`，`scripts/extopp_analyze.py` 出输局分析（见 `docs/local_sim.md`）。默认不启用，已有评测逐局不变。
- **结果（种子 71）**：冠军对 simbot 正常选秀 95/200 = **47.5%**（CI 40.7–54.4），同队 95/200 = 47.5%；对 simbot_v1 84/100 = 84.0%（CI 75.6–89.9）。**冠军的领先来自选秀和首发，对战本身只和一回合引擎搜索持平**，所以 v4 的方向是对战。
- 输局里最大的 5 个问题：从不主动换人（每局 0.01 次，simbot 1.65 次）；留在更快对手的击杀线上；几张卡的招式估值有错（计算器把 Ting-Lu 的 Ruination 算成 0）；被群攻 / 集火击倒；控速和击掌奇袭用得少。明细和局号在本机 `~/exp8/extopp/NOTES.md`、`~/exp8/out/analysis_*.md`。
- 检查：pytest 937 通过；1341 个真实决策重放 0 失败；擂台种子 7 = 191/400（与之前完全相同）；simbot 冒烟 10 局 0 非法、0 超时。

### 开源学习型对手 VGC-Bench（09-28/29，评测工具，agent 不变）
- **是什么**：VGC-Bench（MIT，Angliss 等，AAMAS）用人类天梯对局做行为克隆（BC，模仿人类出招），再加自我对弈强化（SP）训练出来的 VGC 双打策略网络。这是别的参赛者最可能直接套用的「现成强 bot」。用的是和我们环境一致的「朱紫」版本（tag `Scarlet/Violet`，Reg I 那一代；main 分支已改成 Pokémon Champions 格式，不适用），HF 权重 revision c15e5bc：`saves_bc/seed1/100.zip`（BC）和 `saves_bc_sp/reg_all/seed1/98304000.zip`（BC+SP）。经所有者批准下载，装在 **D 盘** `D:\altruagent`（WSL 里是 `/mnt/d/altruagent`，CPU 版 torch，共约 1.9 GB），C 盘不放。安装方法见 `docs/local_sim.md`。
- **接入**（`local/vgcbench_opp.py` + `local/vgcbench_worker.py`，worker 跑在 D 盘的独立 Python 环境里）：它只收到真实 Showdown 客户端会收到的本方协议（`bridge.js` 新增 `sideview` / `preview_view`，不泄露对方私有信息），由 poke-env 自己的消息处理和 VGC-Bench 原版 `PolicyPlayer` 出招、带人；选秀借用冠军的代码（和 simbot 一样）。面板名 `vgcbench_sp` / `vgcbench_bc`；`VGCBENCH_DETERMINISTIC=1` 改成每步取概率最大的动作（argmax）。没装 D 盘时这两个名字会被拒绝并说明原因，测试自动跳过。默认不启用，已有评测逐局不变（A/B 120/120 相同）。
- **保真检查**（`scripts/vgcbench_check.py`，160 局 1527 个决策 + 独立复核）：worker 看到的局面和引擎真值逐项一致（己方 6108 项、对方 2728 项、场地 1527 项，0 不一致）；复核者用另一个 poke-env 客户端重建 256 个观察向量，**逐位相同**；动作掩码、动作解码、带人全部正确；0 次回退、0 个未知词表。同种子重跑 40/40 局逐步相同。
- **结果**（冠军按真实 VGC 计时，双方座位各半）：

| 条件 | 冠军胜率 | 95% CI |
|---|---|---|
| 对 VGC-Bench SP（抽样，和作者 play.py 一样），正常选秀，种子 71 + 72 | 216/400 = **54.0%** | 49.1–58.8 |
| 对 VGC-Bench SP **argmax**，种子 71，200 局 | 99/200 = **49.5%** | 42.6–56.4 |
| 对 VGC-Bench SP，同队，种子 71 + 72 | 98/200 = 49.0% | 42.2–55.9 |
| 对 VGC-Bench BC（只模仿人类），种子 71 | 155/200 = 77.5% | 71.2–82.7 |
| （参照）VGC-Bench SP 对 simbot，60 局 | VGC-Bench 34/60 = 56.7% | 44.1–68.4 |

- **结论**：一个能直接下载的学习型 bot，在对战上已经和冠军打平（选秀还是借我们的；真有人照搬的话要自己选秀，对我们只会更有利）。这和 simbot 的结论一致：**冠军的优势在选秀和首发，v4 必须在对战上变强**。输局里再次出现 simbot 研究的问题：几乎不主动换人（每局 0.03 次，对方 0.99 次；对方换人 ≥2 次的局冠军只赢 29–47%）、留在更快对手的击杀线上、Ruination / Drain Punch 估值错、被群攻 / 集火；新问题：10 回合以上的消耗战只赢 30–46%，对方会用寄生种子、再来一次、祈愿、换人循环威吓 / 再生力、换人抢天气。明细在本机 `~/exp11/NOTES.md`、`~/exp11/out/analysis_*.md`。
- 以后 v4 的外部门槛：`vgcbench_sp`（抽样 + argmax）和 `simbot`，每步约 0.05 s，200 局约 7 分钟（3 个进程）。调参不要用种子 71 / 72。

### v4_engine_search（09-29）：对战改用引擎搜索（选秀、首发不变）

- **改了什么**（`agent/pokemon/engine.py`、`battlestate.py`、`battlelog.py`、`agent/js/search.js`，`policy.py` 接入）：每个双打回合先照旧用冠军的打法（精确伤害 + 一回合联合前瞻，下称「经典打法」）选一个动作；再把当前对局按平台能看到的信息重建到 Showdown 引擎的克隆上（对手没亮相的宝可梦靠猜，约 3% 的槽位猜错），把我方最有希望的约 8 种双槽组合（再加经典打法那一种）和对手的应对都真打一回合（每格抽 2 遍，再双方贪心多打 1 回合），排成收益表，选「对 50% 均衡对手 + 50% 贪心对手」期望最好的一行（blend 模式）。名词：**均衡** = 双方都没法单方面改得更好的出招概率分配；**贪心** = 每个位置打当下伤害最高的招。
- **固定工作量**：同一局面、同一种子给同一答案（不随机器快慢变）；**安全网**约 3.9 秒（平台上每步最多给 10 秒，793 个真实状态全是这样；每回合 55 秒）。搜索失败、超时或答案不在动作模板里，就用经典打法（= 冠军走法），不会非法。
- **回退开关** `ARENA_BATTLE=classic`：完全等于 v3.2（种子 7 / 11 的 400 局镜像 191 / 211，和 `local.ladder` 一样；种子 7 逐局胜负和回合数都相同）。
- **部署**：需要 pokemon-showdown 模拟器（压缩包自带裁剪版；git checkout 里要 `(cd local/js && npm ci)`）。没有模拟器时 `selfcheck` 判失败、`scripts/run_agent.sh` 拒绝启动（退出码 2），只有显式设 `ARENA_BATTLE=classic` 才放行；以前会**悄悄变回冠军**，这次评审发现并修好。每场并发对局多一个 230–320 MB 的 Node 进程（2 场并发约 1.1 GB，按 1.2 GB 预留）。

**门槛**（全部没调过参的新种子；「冠军」= v3.2 在完全相同的一批局上的结果；配对差只看一方赢一方输的局，McNemar p < 0.05 算显著；全部对局开 `ARENA_PLOG=1`，即本地状态和平台一样带协议日志）：

| 门槛 | v4 | 95% CI | 冠军（同一批局） | 配对差（百分点）[95% CI] | McNemar p |
|---|---|---|---|---|---|
| 擂台 种子 7（400 局） | 230/400 = 57.5% | 52.6–62.3 | 镜像 191/400 | **+9.8** [+4.0, +15.5] | 0.0014 |
| 擂台 种子 11（400 局） | 254/400 = 63.5% | 58.7–68.1 | 镜像 211/400 | **+10.8** [+5.3, +16.2] | 0.0002 |
| VGC-Bench SP 抽样，种子 73（200） | 108/200 = 54.0% | 47.1–60.8 | 91/200 | **+8.5** [+0.5, +16.5] | 0.0498 |
| VGC-Bench SP 取最大概率（argmax），种子 73（200） | 128/200 = 64.0% | 57.1–70.3 | 97/200 | **+15.5** [+6.7, +24.3] | 0.0010 |
| simbot 同队，种子 83（100，`SIMBOT_BUDGET_MS=100000`） | 43/100 = 43.0% | 33.7–52.8 | 52/100 | −9.0 [−21.8, +3.8] | 0.22（不显著） |
| 回归面板，种子 45（5 个对手 × 60） | 298/300 | — | 296/300 | +0.7 [−0.9, +2.3] | 0.69 |
| 基准，种子 31（每个对手 100 局） | official_baseline 100、random 100、v0 99、v1 97、v2 94、v3 63 | — | 冠军 200 局参考 100 / 100 / 96 / 92 / 88.5 / 51.5 | 不配对，每项都不低于参考 | — |

- 全部门槛对局（2200 局）：0 非法动作、0 次被计时器代打、0 判负、0 出错；simbot 那组有 3 次搜索退回经典打法。每次搜索平均 1.8 秒，每局最慢一步 p99 4.25 秒、最慢 4.82 秒（机器负载 6–14）。
- 检查：`pytest -q` 全绿；`scripts/replay_real.py` 和 `--no-calc` 各 1341 个真实决策 0 失败（723 个双打决策里 708 个用上搜索，15 个退回经典打法，都合法）。

**怎么从两个候选里选的**：这一轮两个方向各自过了全部门槛——**search**（就是 v4）和 **battlefix**（不换打法，只修冠军一回合前瞻里的建模错误：主动换人 / U-turn 类换人行、被迫换人按精确对位选、速度、吸血和回复、讲究道具、Ruination 这类固定伤害；对冠军擂台种子 7 / 11 = 55.0% / 60.2%，VGC-Bench 抽样 +10.0、argmax +7.5，simbot +3.0 个百分点）。

| 比较 | 结果 | 结论 |
|---|---|---|
| 直接对打：search 打 battlefix，种子 13，400 局 | search 208/400 = **52.0%**，CI 47.1–56.9；双方 0 非法 | 持平（点估计 search 略高） |
| 同一批门槛局逐局配对（search − battlefix） | 擂台种子 7 +2.5、种子 11 +3.2；VGC-Bench 抽样 −1.5、argmax **+8.0**（p = 0.056）；simbot **−12.0**（p = 0.081）；三个外部对手合计 500 局 +0.2 [−4.8, +5.2] | 持平 |
| 组合：search + battlefix，打 search 单独，种子 17 | 组合 85/200 = **42.5%**，CI 35.9–49.4 | **明显更差**，不要叠加 |

- 选 search 的理由：直接对打点估计领先，对 v3.2 的正式擂台两个种子领先更大，对学习型 bot 更强。**这是接近五五开的选择**，battlefix 是现成的备选（`~/exp13/patches/battlefix-final.patch`）。在上一轮中途修掉的一个 battlefix 真 bug：双倒后只剩一只替补时回答「pass, pass」（非法），已修（`FORCED_PASS`）——这个补丁没合入。

**已知风险**：
1. **对「按均衡出招」的搜索型对手偏弱**：对 simbot 比 v3.2 少 9 个百分点、比 battlefix 少 12 个百分点（都不显著）。如果强队多是这种打法，v4 可能不占便宜；那时可以换 battlefix。
2. **机器负载**：负载约 14 时约 30% 的搜索只抽 1 遍、约 5% 碰到安全网（强度略降，不会超时）。跑 agent 的机器最好别同时跑别的重活。
3. **部署**：必须用压缩包，或在 checkout 里 `(cd local/js && npm ci)`；缺模拟器会拒绝启动。比赛日模拟器坏了修不好时，临时设 `ARENA_BATTLE=classic`。Dockerfile 在本机仍没真正构建过。
4. 真实状态上 2.1% 的双打决策（15/723）重建失败，退回经典打法（合法）。
5. **冻结以后的工具问题**：`champion/` 也会读 shell 里的 `ARENA_BATTLE` / `ARENA_SEARCH_*`，做实验只能用 `local.panel --env KEY=VALUE`。擂台和基准会**慢很多**（两边都搜索时每局约 35–50 秒，单进程 `local.ladder --games 400` 要约 4 小时）；请用 `local.panel --opps champion --jobs 4`（每个进程约 1 GB 内存）。

本机文件：候选说明 `~/exp13/search/CANDIDATE.md`、门槛明细 `~/exp13/search/GATES.md`、两个候选的比较在 `~/exp13/v4/`，脚本 `~/exp13/scripts/v4_*`。

### v4.1（09-29）：真实平台上的两个 bug（本地对局不变）

真实 A/B 的第 11 局暴露了问题，两个修复一起合入，版本记作 v4.1（修复类，不追求变强，标准见 `docs/v5_standard.md`）。

- **事故**：真实对局 f525d6ac（Yoriko_Red = v4）第 2 回合，我方场上两只同时倒下，后备只剩 Iron Treads 一只。平台给两格的选项都是「换 Iron Treads / pass」，v4 回答了「pass, pass」。平台用 Showdown 的规则：两格不能换同一只；只要还有空着的替补，就不许 pass。服务器拒收，官方 runner 连续 3 次杀掉对局进程，之后平台替我们随机出招。v3.2 也有同样的隐患（强制换人格里 pass 记 −100 分），只是没碰到过。
- **根因**（`~/exp15`，经 `~/exp17` 独立复核）：真实平台的 `observation.team` 列出**全部 6 只**（包括赛前选人时没带的 2 只，永远满血、未亮相）；本地模拟器只给带上场的 4 只，所以本地门槛从没遇到。v4 的局面重建按顺序取前 4 只，**936 个真实双打决策里 44% 的引擎队伍是错的**：替补里放了没带的满血宝可梦、真正带来的那只引擎不认识。能看到的症状是约 3% 的决策退回经典打法（「no engine option」「switch 3, switch 3」），以及这次的「pass, pass」；看不到的代价更大：修好后 15.6% 的真实决策会改选别的动作，残局里引擎常以为自己还多一只。
- **修复 1：局面重建只用真正带上场的 4 只**（`battlestate._in_battle()`）：只用平台给我们的信息认（在场的、倒下的、已亮相的、协议日志里换上来过的、模板里能换上的），936/936 个真实状态全部认对；认不出正好 4 只时（例如双方都被困住 / 锁招、替补还没上过场）不猜，直接用经典打法。本地模拟器加开关 `ARENA_SIX_TEAM=1`，模仿平台的 6 只写法（以后门槛建议打开）。在这种写法下本地对 v4 各 100 局：修好的 65%、没修的 55%（+10 个百分点，p ≈ 0.10），没修的有 79 次搜索退回和 5 次非法动作，修好的都是 0。
- **修复 2：两格合起来必须合法**（`fallback.joint_problems / doubles_problems`、`repair_doubles`、`Contestant._joint_guard`）：强制换人格里的 pass 记 −1000 分；`doubles_ok`、`_template_ok`、搜索结果和 search.js 的候选行都用同一条两格规则；发送前的最后一道防线对任何来源的回答（策略、兜底、看门狗超时、崩溃）做最小修补。两位验证者（逐条路径检查 + 6 万多个变异局面的模糊测试）找到的纵深防御漏洞都已修好。
- **检查**：两份补丁都经过独立验证。全部真实双打决策的两格审计：原来 4 个非法回答（都是「pass, pass」），现在 0 个，前后答案不同的只有这 4 个；搜索在真实状态上几乎全部用上（原来退回 28 次）；本地（默认 4 只写法）对冠军 40 局与修复前逐局完全相同。合入当天的 pytest / 重放结果见提交说明。

**真实平台 A/B：v4 对 v3.2（09-29，Yoriko_Red = v4，Apocalypse_Red = v3.2）**：前 11 局 v4 **8 胜 3 负**；其中第 11 局 v4 的进程因上面的 bug 被杀了 3 次（这局算 v4 胜，但不干净）。这 11 局里 v4 在 44% 的回合用的是错的队伍。之后的对局用 v4.1，单独计数：**v4.1 对 v3.2 20 局 12 胜 8 负（60%，Wilson CI 38.7–78.1%）**，0 次非法动作、0 次搜索退回、0 次两格修补。两段合计 v4 对 v3.2 真实平台 31 局 20 胜（64.5%，CI 47–79%），和本地擂台（57.5% / 63.5%）一致。按 `docs/v5_standard.md` 的真实 A/B 标准（20 局 ≥ 11 胜、0 非法）算过关，但还不到「明显领先」（15 胜）。

### v5 第 2 轮（09-29，没有上位；v4.1.1 仍是冠军）

按 `docs/v5_standard.md` 走漏斗，两个方向都过了开发代理筛选，都没过正式擂台：

| 方向 | 做法 | 开发代理（对 v4 配对） | 正式擂台种子 7（400 局，对 v4.1） |
|---|---|---|---|
| deep | 搜索第一遍之后，把额外模拟集中在前 4 行、每格再打 4 遍再重选（固定工作量） | 合计 +7.1 [+0.8, +13.4]，p = 0.035；VGC-Bench argmax +14.0（p = 0.009） | **170/400 = 42.5%**（CI 37.7–47.4），明显更差，按规则提前停 |
| preview | 赛前选人用引擎推演开局，和首发矩阵混合 | 合计 +7.1 [+1.1, +13.2]，p = 0.029 | **202/400 = 50.5%**（CI 45.6–55.4），持平，不过 |

- deep 的这次测量是干净的（精炼 93% 做完，0 非法、0 退回、每步最慢 4.1 秒），输在 13 回合以上的消耗战（36%）。可能的原因：多看几遍前 4 行，挑中的仍是「运气好的那一行」，又丢掉了第一遍对其余行的信息。
- preview 改了阵容的局反而赢得少（247 局 47.8%，没改的 153 局 54.9%），代理上的收益大概是噪声；赛前选人一步最慢 6.7 秒，超过 5 秒的测试上限。
- 教训已写进 `docs/v5_standard.md` 第 3 节：代理（P1 只有 120 局、负载高时双方都降级）对擂台的预测力很弱，以后代理过了先在低负载下跑擂台种子 7 的前 200 局再决定。补丁留在 `~/exp19/patches/`。

### v4.1.2（09-30 写成，10-03 合入）：官方正式赛的部署（本地对局不变）

09-29 的上游更新定下正式赛方式：参赛者自己一直开着 `python -m agent --tournament`（Official Agent Key，`eak_live_…`），我们按自托管准备。09-29 部署审计（`~/exp21/audit_findings.json`）里确认的部署问题这次一起修，修复类版本，不改打法。
- **官方模式**：`scripts/run_agent.sh` / systemd 单元 / Docker / 压缩包 / selfcheck 都认官方模式：有 eak key 就自动 `--tournament`（`ARENA_MODE` 可强制），只查 eak key 的格式，健康行显示 `mode=tournament(primary|standby)`；`run_agent.sh check-tournament` 一次性检查；出包的密钥扫描认得 `eak_live_`；同一把 eak key 的第二个进程按热备启动（sk key 仍然一机一个；另一台机器上用 key 文件里的 `ARENA_ROLE=standby` 标明）。热备只打没人持有租约的座位：新座位先让主进程 20 s（平台是「谁先申请谁拿」，`ARENA_NEW_SEAT_DELAY_S`），之后每次轮询用自己的 execution id 请求一次 `grant`，`seat_busy` 就继续等（不起 worker、不新建 agent session），主进程的租约一过期就当轮接管（以前靠 SDK 的 worker 撞 `seat_busy` 再等 35 s，dry-r5 接管 68.8 s、被代打 1 回合）。
- **上游缺陷绕开**（不改 `altruagent/`，缺陷清单在 `docs/deploy.md` 给主人报告）：重新认证碰上一次 429 / 5xx 不再结束运行时、不再杀掉所有对局（`_SteadyOfficial`）；启动时暂时连不上平台按退避重试；冷却 / 查询 10 s 也用于官方模式；看门脚本给每个 run 循环一个 execution id，agent 重启后直接续上自己的座位租约（实测对战中 kill -9 运行时，7.4–8.5 s 后下一步，0 次 seat_busy）。
- **其他部署修复**：`ARENA_ALLOW_NO_CALC=1` 单独设就够（node 坏了时自动 classic）、`ARENA_CALC=off` 不再关掉引擎搜索；systemd `OOMPolicy=continue`，Node 子进程优先被 OOM 杀；直接跑 `python -m agent` 时默认控制面地址、打醒目的 DEGRADED 横幅、10 s 冷却；LOG_DIR 不可写报错退出 2（不再误报「已在运行」）；录像默认写 LOG_DIR、失败会警告；平台要求 / 离线安装 / 不要 `pip install .` 写进 BUNDLE_README；包里带一整场真实对局的两个座位，`replay_real.py` 在包里能跑；包里带 `.dockerignore`，Dockerfile 加 HEALTHCHECK、`check-tournament` 可直接跑；Node 子进程不再继承任何密钥。
- **演练**：模拟平台默认按真实录像的 payload 形状（6 只队伍、协议日志和计时行、盲状态），新增官方控制面（座位租约）和 7 人狼人杀桌。从解开的压缩包跑完整流程（2 个并发分配、kill worker、kill 运行时、认证 503、热备、主进程停止后接管），最终代码 dry-r7：13 项检查全过，102 个决策 0 非法 / 0 计时器代打，热备在主进程在时 0 个 worker，接管 30.7 s 且那一回合没被代打，非持有租约的进程 0 步；dry-r1…r6 的问题和改法见 `docs/deploy.md`「官方模式演练」。
- **对局不变**（v5_standard 第 1 节）：改动都在进程启动、运行时对 SDK 的包装（`agent/__main__.py`）、子进程环境 / OOM 分数、日志和部署脚本里，不碰决策代码。唯一的行为变化是 deploy-1 要求的：`ARENA_CALC=off` 时引擎搜索照常（以前连带关掉）；默认设置下的本地对局不受影响。核对：2349 个真实状态用经典打法重放，修复前后答案逐个相同；引擎搜索用 `~/exp18` 的确定性设置（安全网调高）重放三场真实对局，逐个相同。Docker 镜像仍没真正构建过（本机没有 docker）。
- **独立审阅补的两处**：`run_agent.sh status` / `health` 以前按「问的那个 shell」重新算 `mode=` 和 `key=#`，看门脚本是用环境变量 `ARENA_MODE=discovery` 起的、key 文件里又有 eak key 时，会对着一个发现模式的循环显示 `mode=tournament(primary)`（比赛日手册正是靠这一项确认模式）；现在循环在跑时显示它状态文件里记的模式和 key 哈希。计算器找 node 时的 `node --version` 也改用和 calc.js / search.js 一样的白名单环境（以前带着全部密钥）。
- **10-03 合入 main 376048d**：补丁是在 40309a0 上写的。合入后 16 个代码和脚本文件与「40309a0 + 复审过的补丁」逐字节相同；只有 `docs/deploy.md` 的「加入锦标赛」和本文件需要手工合并，main 上 10-03 的情报和 v5 第 2 轮记录都保留。`HANDOFF.md`、`competition_brief.md`、`status_1003.md` 里「部署脚本还不支持 `--tournament`」的说法已改成现状。`~/exp21/final` 里 run_agent.sh / Dockerfile 的未暂存改动要用到 v4.2 的 `agent/health.py` 和对局状态文件，留给 v4.2 一起提交。
- **合入后的检查**：
  - `python -m pytest -q`：1301 个全过（548 s）。
  - `replay_real.py`：2349 个真实决策 0 失败、0 次两格修补，1296 个双打决策全部用上搜索；`--no-calc` 同样 0 失败。
  - `selfcheck.py`：ready。配上假的 eak key 时显示 `mode tournament (python -m agent --tournament)`；格式不对的 key 判 FAIL。
  - 新压缩包（`376048d-dirty`：提交前从同一工作区出的包，代码和本次提交相同）：干净环境里 selfcheck ready，包里的 replay_real 60 个决策 0 失败。
  - 从这个压缩包跑 `rehearsal_official.py`（dry-i1）：13 项检查全过。宝可梦 7 场、狼人杀 3 桌，110 个决策，0 非法、0 计时器代打。kill worker 后 13.5 s 起新 worker、15.5 s 后下一步；kill 运行时后 3.4 s 起新运行时、8.4 s 后下一步。热备接管用了 32.7 s，那一步等了 34.2 s，没被代打。不持有租约的进程 0 步，key 没进日志。接管的那桌狼人杀有 1 次 `WRONG_PHASE`：讨论窗口换阶段时撞上的竞态，runner 没退出，这桌照常打完。
  - `rehearsal.py` 发现模式 5 分钟：开了 9 场、打完 8 场（最后 1 场到时还在打），120 个决策，0 非法、0 计时器代打、0 个 Traceback；停止用了 0.6 s，没有残留进程。
- **合入后对局仍不变**（和 main 376048d 比）：2349 个真实状态用经典打法、3 场真实对局（140 个决策）用确定性搜索，答案逐个相同；本地对冠军 20 局经典打法加 10 局确定性搜索，Showdown 协议日志逐局相同。

### v4.2（09-29/30 写成，10-05 合入）：重启、慢机器、内存下的自我保护（本地对局不变）

修复类版本（标准见 `docs/v5_standard.md` 第 1 节），不让 agent 变强，只保证官方运行时（`python -m agent --tournament`）下 worker 重启、CPU 被抢、内存紧张时不掉链子，并且出问题时看得见。细节和开关见 `docs/restart_load.md`。来源：09-29 就绪审计（`~/exp21/audit_findings.json`）和复核（`~/exp21/reverify_results.json`）。

- **重启后接着用原来的记忆**（intel-4 / robust-1）：worker 重启后新实例以前什么都不记得（选秀时才看得到的对手完整配置、预览的首发分布、Fake Out / 保护的回合记忆、狼人杀前几天的发言）。现在每个决策后把这一局的记忆原子写进私有状态目录（`logs/state/<agent>--<session>.json`，0700/0600，单个 ≤ 1 MB，3 小时或 200 个以上自动清理，只有对局信息、没有密钥），新 worker 读回后和没重启时逐步相同。
- **慢 CPU 不再陷入「杀进程—重启—超时」**（robust-2 / deploy-2）：超时的搜索不杀，在后台算完；等过加载或迟到答案的请求把安全网缩到剩下的时间；Python 多等最多 1.5 秒（仍在 10 秒预算内）；真超时 2 次才改为每次只抽 1 遍（被安全网截断只计数）。每次因机器原因退回经典打法都打日志，比例高时打 `ENGINE SEARCH DEGRADED`。
- **整台机器的搜索进程上限**（deploy-10）：按内存算槽位（8 GB→12 个），另要求 500 MB 可用内存；占不到时这一局用经典打法并打 `ENGINE SEARCH NOT STARTED`，5 秒后再试。狼人杀不占槽位。
- **看得见**（deploy-9）：录像每行多 `decision`（path、battle=search/classic、resumed）和 `search` 字段；worker 退出时打一行 `[match-summary]`；健康行的 `search=` 改成看最近 30 分钟真实对局用上搜索的比例，没对局时真跑一次搜索（`python -m agent.health`）。
- **输入健壮性**：双打模板不在第一个时不再发空的 `doubles_turn`（robust-4）；`game_type` 大小写 / 写法不同也认得出（robust-7）；同一个状态被问两次答案不变（robust-3）；狼人杀重启后不重复发言、不超 5 条（robust-9）；卡牌 `"moves": null` 不再让选秀崩溃（robust-10）。
- **检查**（全部在 `~/exp21/out/agent`）：
  - 本地对局不变（det.sh 设置，默认 4 只写法，不重启）：对冠军 40 局 seed 171 与 v4.1.1 逐局相同（40/40，550 个决策）；2349 个真实决策的答案 0 个不同；经典打法的真实决策和 40 局本地狼人杀逐字节相同。
  - **每个决策前都重启 worker**（新实例从状态文件接上）：对冠军 40 局仍与 v4.1.1 逐局相同；4 场真实录像在每一步模拟重启（包括「答完还没发出去就死了、再问一次」），答案和写出的记忆都与不重启时相同；本地狼人杀每步重启，消息逐条相同。
  - 官方运行时的 worker 路径（`run_tournament_worker`，假的官方客户端，不联网）：真实录像在选秀中途 / 对战中途换 worker，后面的答案与不换相同。
  - 端到端（真的 `python -m agent` 进程树对本地假平台，假 key）：对战中途和选秀中途各 SIGKILL 一次 worker，新 worker 从状态文件接上，打完整局，0 非法动作，搜索用上 5/5、7/7，`[match-summary]` 行正常。
  - 负载场景（`~/exp21/scripts/ag_load.py`，整局钉在 1 个 CPU 核上重放真实对局，v4.1.1 对 v4.2）：搜索进程被杀、之后启动慢 1.5 秒时，v4.1.1 在两局里分别有 2/21、7/17 个回合因「超时—杀—重启」连锁退回经典打法，v4.2 都是 0；worker 在对战中途重启时，v4.1.1 丢了对手的 6 张卡（重建只能猜），v4.2 全部接上；1 或 3 个抢 CPU 的忙循环时两版都 100% 用上搜索（这台机器上还没慢到出问题）。
  - `replay_real.py` 和 `--no-calc`：2349 个真实决策都是 0 失败、0 修补，搜索用上 1296/1296。
  - pytest 全部通过（新增 `tests/strategy/test_match_state.py`、`test_engine_load.py`）。
- **要报给主办方的**（`altruagent/` 不改）：运行时没有「对局结束」回调，所以每局汇总只能在 worker 退出时打、状态文件只能按时间清理；README 说重连后 "Nothing is saved locally"，重启丢记忆是设计如此，我们在自己代码里补上了，但另一台机器的热备接管时仍会丢。
- **复审（09-30）补的三处**：(1) 状态文件名加上座位（`--p<座位>`），读回时核对座位：自对弈测试里同一个 agent 占两个座位时，一个座位第一次决策会读到另一个座位的记忆（实测 12 局里 3 局打法变了、12 次读错；修后 0）；(2) 整机槽位和内存检查只管部署的 agent，本地评测（`ARENA_RECORD=0`）不受影响，免得 panel / ladder 的结果取决于机器上同时跑着什么；(3) 清理只删状态文件自己的文件。单座位的对局（重启或不重启）不受影响。
- **10-05 合入**（`integrate/v42` 分支，打在 v4.1.2 da5adb7 上）：`agent-reviewed.patch` 用 `git apply --3way` 打上，只有本文件冲突（手工合并）；另带上 `~/exp21/final` 里没暂存的集成改动：`scripts/run_agent.sh`（健康行调 `agent/health.py` 时读 `ARENA_RECORD_DIR`（默认 LOG_DIR）下的计数、固定 `ARENA_BATTLE=search`，`ARENA_HEALTH_PROBE=0` 时加 `--no-probe`；tmux 环境透传加上 `ARENA_STATE*`、`ARENA_SEARCH_*`、`ARENA_SUMMARY`、`ARENA_HEALTH_PROBE`；文件头的变量说明）和 `deploy/Dockerfile`（HEALTHCHECK 带 `ARENA_HEALTH_PROBE=0`，20 s 超时内不另起探测搜索）。代码文件和 `~/exp21/final` 逐字节相同。检查（输出在 `~/wt/_v42_out2`）：
  - pytest 全部通过：1346 个，604 s。
  - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，决策 p99 1682 ms、最慢 2053 ms；`--no-calc` 同样 0 失败、0 修补，p99 1704 ms、最慢 1994 ms（标准：p99 ≤ 5 s、最慢 ≤ 10 s）。
  - 双打两格合法性审计（`~/exp16/scripts/fin_audit.py`：独立的两格判定 + SDK 校验，确定性设置）：1296 个真实双打决策，main 和 v4.2 重放出的答案都是 0 个不合法，两边 0 个答案不同，0 次修补、0 次搜索退回。录像里当时实际发出的答案有 3 个违规（f525d6ac 第 10–12 行的「pass, pass」，就是 v4.1 修掉的那个事故）。
  - 和 main 376048d 比对局不变：经典打法重放 2349 个真实决策加 40 局本地狼人杀，输出逐字节相同（`~/exp21/scripts/rv_ident.py`，2389 行）；对冠军 v4.1.1 打 40 局（seed 171，确定性设置），main 和 v4.2 逐局相同（40/40，550 个决策，都是 19 胜，搜索用上 510/510，0 非法、0 计时器代打，最慢决策 2.29 s）。
  - 每个决策前都换一个新的 agent 实例（模拟 worker 重启，`ARENA_STATE=1`，从状态文件接上）：同样 40 局仍和 main 逐局相同，750 个决策每个都打了一次「resumed match … after a worker restart」（panel 把它们计成 warnings 750/0，09-30 的复审结果也是 750）。
  - selfcheck：ready（没配 key，只有这 1 个警告）；配上假的 eak key 时认出官方模式，key 打印 0 次；`python -m agent.health` 没有对局时真跑一次搜索 → `idle(probe=1.5s)`，`--no-probe` → `idle(no_battle)`；`run_agent.sh health` 两种都对。
  - 新压缩包（提交前的树）：包里有 `agent/matchstate.py`、`agent/health.py`、`data/search_probe.json`，46 MB / 415 个文件，密钥扫描干净；只有 `/usr/bin:/bin` 的干净环境下 selfcheck ready（54 s），包里 `replay_real.py` 60 个决策 0 失败，`agent.health` → `idle(probe=1.4s)`；格式不对的假 key 时 `--check-tournament` 退出 1。
  - `rehearsal_official.py`（从这个包跑）：13 项全过，4.4 分钟，96 个决策 0 非法、0 计时器代打、0 竞争错误；kill worker 后 13.5 s 起了新 worker、16.5 s 后下一步；kill 运行时 3.4 s / 6.5 s，0 次 seat_busy；热备接管 28.9 s，那一回合没被代打（这一步等了 31.3 s，就是汇总里对战延迟 p99 31.3 s 的来源，不是算得慢）；非租约持有者 0 步，key 没进日志，停止后 0 残留。演练默认 `ARENA_RECORD=0`，所以这里状态文件是关的（kill worker 后的新 worker 是从零开始接着打的）。
  - `rehearsal.py` 发现模式 5 分钟：开了 9 场、打完 8 场，127 个决策，0 非法、0 计时器代打、0 个 Traceback，每场都有 `[match-summary]` 行；停止 0.8 s，0 残留。
  - 回退开关 `ARENA_BATTLE=classic`（两边都用经典打法打擂台，种子 7，400 局）：191/400，双方 0 非法，平均 7.7 回合，和 v3.2 镜像的数字一样。
  - 提交后补了一个小修：压缩包带上 `docs/restart_load.md`（`docs/deploy.md` 有几处指向它；`tests/test_deploy.py` 76 个通过）。从这个提交（2ef08e4）新出的包：46 MB / 416 个文件，干净环境 selfcheck ready（67 s），包里重放 60 个决策 0 失败、p99 1.8 s，`agent.health` → `idle(probe=1.6s)`。
  - 用这个包再跑 `rehearsal_official.py`：13 项全过，111 个决策 0 非法、0 计时器代打。另用演练脚本的临时副本（不进仓库）打开状态文件和整机槽位（`ARENA_STATE=1`、`ARENA_SEARCH_MAX_PROCS=8`）跑了一次：13 项全过，98 个决策 0 非法、0 计时器代打；kill worker 和 kill 运行时之后，新 worker 都从状态文件接上了（3 次 `resumed match`：宝可梦 2 次、狼人杀 1 次）；对局中健康行是 `search=ok(used=…)`，整机槽位用了 1/8；状态文件都是 0600、目录 0700，里面没有 key。数字见 `docs/deploy.md`「官方模式演练」的 dry-v42 几行。
- **冻结为冠军**（10-05）：`./scripts/freeze_champion.sh`，`champion/` = 提交 d14a524 的 `agent/`（`champion/VERSION` = d14a524），冻结后全量 pytest 1346 个通过（586 s）。从此擂台、panel 里的「冠军」就是 v4.2，打法和 v4.1.1 相同（上面的本地对局逐局相同），所以以前对 v4.1.1 冠军测的擂台 / panel 数字可以接着比。
- **10-06 复审修复**（两份独立复审：合并保真、运行时实测，结论都是「可以发布」；确认的问题 1 个中等、7 个低，下面改了 6 个。每个都先重现再改，测试加在 `tests/test_deploy.py`；本地假平台的测量在 `~/wt/_fx_out`，脚本在本次会话的 scratchpad `fix-integrate/`）：
  - **看门脚本整个重启后沿用 execution id**（中等，`scripts/run_agent.sh`）：以前每个 run 循环都生成新 id，systemd 重启看门脚本、`run_agent.sh restart`、看门脚本死掉后重新 `start` 时，新进程的座位还记在旧 id 名下：`seat_busy` 到租约过期（30 s）再加 SDK 的 35 s 重试。修复前在本机重现：宝可梦对战中 `restart`，65.8 s 后该局才有下一步，被计时器代打 1 次，2 次 seat_busy（复审在 systemd 下测的是 63–65 s）。现在 id 存在 `LOG_DIR/agent-NAME.execid`（0600，和 boot id、key 哈希、NAME@LOG_DIR 写在一起），新循环只在拿到本机这把 key 的锁、并杀掉上一个循环留下的 agent 和 worker（包括 agent 已死、worker 还活着的情况）之后才沿用；换了开机、key、目录，或者文件是从别的机器拷来的，都用新 id。修复后：`restart` 两次 8.4 / 9.1 s 后下一步；systemd 单元里 `kill -9` 看门脚本两次，5.6 s 起新循环、13.1 / 14.8 s 后下一步；四次都是 0 次 seat_busy、0 次接管、0 次计时器代打、非持有者 0 步。重启时正在打的狼人杀桌有 1–2 次 `WRONG_PHASE`（修复前的重现和复审自己的 kill 测试里也有，换阶段的竞态，runner 不退出），0 次违规、0 条重复发言。
  - **systemd 下的 `stop` 不再「停了又被拉起」**（低）：从没有 `XDG_RUNTIME_DIR` 的 shell（su、sudo -u、cron）调 `stop`，以前 `systemctl --user` 连不上就当成「不在 systemd 下」，把看门脚本停掉，systemd 随即用新 id 拉起来。现在按 cgroup 判断是哪个 systemd 管的，用户单元自动补上 `XDG_RUNTIME_DIR=/run/user/<uid>`，实在问不到也拒绝并给出 `systemctl --user stop …`。
  - **`status` 的退出码**（低）：以前在第一条健康行写出之前（默认 5 分钟）返回 1、没有日志时返回 2；现在看门脚本在跑就是 0，没在跑是 3。
  - **平台故障后的退避最长 30 s**（低，`agent/__main__.py`）：启动重试 5/10/20/40/60 s 改为 5/10/20/30 s，限流暂停 15/30/60 s 改为 15/20/30 s。复审测到平台恢复后还要再等 35 s / 56 s 才看得到新座位（狼人杀一步 120 s、队伍预览 90 s）。这是这次唯一的 `agent/` 改动，两个常数只在官方运行时连控制面出错时用，不碰任何决策代码（`git diff 85b803f -- agent/` 只有它们），所以本地对局不变，冠军按惯例重新冻结（`champion/VERSION` = 97b2909）。
  - **HANDOFF**（低 ×2）：快进合并之后不会再说「main 上还是 v4.1.1、还没合」；写明合上游会在 `agent/__main__.py`（一行 import）和 `README.md` 冲突、合完要重跑哪些检查（在临时工作区里试合并确认过，修复后的分支仍然只有这两处冲突）。
  - **没改的**（都是低）：结束了但还留在分配列表里的座位，10 s 冷却下大约每 20 s 起一个 worker、用 key 认证一次（复审用改过的假平台测的；真实平台会不会留、留多久不知道；要改得包住 SDK 回收 worker 的逻辑，不算小改，先记着）；健康行看不出控制面连不上（要新加「最后一次成功连上平台」的记录，比赛日前用 `check-tournament` 和日志里的 `Could not connect …` 代替）。
  - **检查**（树 a3a1271，输出在 `~/wt/_fx_out`）：全量 pytest 1350 个通过（580 s，多了 4 个新测试，其中 systemd 那个在本机真起了一个临时用户单元）；`replay_real.py` 2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 1755 ms、最慢 2762 ms；`--no-calc` 同样 0 失败，p99 1743 ms、最慢 1987 ms；selfcheck ready（只有没配 key 的警告）。新包 `arena-agent-a3a1271`（46 MB / 416 个文件，密钥扫描干净，`agent/`、`run_agent.sh`（755）、Dockerfile 和工作区相同）：干净环境 selfcheck ready（61 s），包里重放 60 个决策 0 失败，`agent.health` → `idle(probe=1.3s)`，格式不对的 key `--check-tournament` 退出 1。用这个包跑 `rehearsal_official.py`：13 项全过（dry-v42r），状态文件打开的临时副本也 13 项全过（dry-v42rs），数字见 `docs/deploy.md`「官方模式演练」。

### v4.2.1（10-06，分支 `merge/upstream-1005`）：合入上游的实时 SDK，红警整场回答 WAIT（本地对局不变）

修复类版本（`docs/v5_standard.md` 第 1 节），不让 agent 变强，宝可梦、狼人杀的打法不变。起因：上游 10-04/05 给 SDK 加了实时游戏（红警，`red_alert`，平台 10-02 上线），我们合进来以后如果被分到红警，旧的「未知游戏」兜底会每个画面发一个不合法的动作。

- **合上游**（aadfc08，合到 upstream/main 6ba2e9c）：PR #3（6fb86b7 + 9965d2e）让 runner 支持实时游戏——按状态里的 `pacing.mode == "realtime"` 判断（不按游戏名）；`choose_action` 可以回答 `altruagent.WAIT`（「这一刻什么都不发」，runner 等下一个画面再问）；实时游戏里一整批命令被拒（`INVALID_ACTION`）不再结束 worker，而是重新读状态；`RUNTIME_TEMPORARILY_UNAVAILABLE` 1 s 后重试；新增 `DecisionContext.game_config` 和示例 LLM 红警玩家 `examples/llm/redalert.py`。6ba2e9c：`--claim` 碰到测试大厅的对局还没准备好（`match_not_ready`）时按服务器给的时间等待重试。冲突只有两处：`agent/__main__.py` 的 import（我们的 `re` + 上游的 `random`），`README.md` 保留我们的（上游的 README 放到 `docs/STARTER_README.md`）。回合制游戏（宝可梦、狼人杀）在新 runner 里走的路径和以前一样。
- **红警整场回答 WAIT**（26b2d09，`agent/agent.py`）：合并后的复现：红警画面（没有 `legal_actions` 列表）走到「未知游戏」的安全动作 `{"type": "pass"}`，它不是命令批。旧 runner 下 worker 每个画面崩溃一次（冷却 10 s 再起、再崩），新 runner 下会变成每秒约 5 个被拒的批次，并且每个都录像。现在只要状态是实时的、又不是我们会玩的游戏，就整场回答 `WAIT`：什么都不发，日志只写一行 `real-time game 'red_alert': no policy of ours plays it; answering every view with WAIT …`，第一个画面录一次（以后写红警策略时有真实 payload 可看），`[match-summary]` 行带 `waits=<次数>`。宝可梦、狼人杀的状态没有 `pacing`，不进这条路。代价：红警这一局等于认输（我们没有红警策略）。
- **模拟平台的红警**（4526660）：`local/fake_redalert.py` + `local/fake_platform.py --red-alert N --ra-seconds S`，按平台形状的实时对局（每 0.2 s 一个画面、`legal_actions` 是字典、只收命令批、`get_game_config` 读 `data/red_alert_config.json`），官方控制面把这个座位列成 `red_alert`。测试：SDK 自己的官方 worker 通过 HTTP 用真客户端把这个座位打到结束（退出码 0、没发过一批命令、只取一次配置）。
- **文档**（72959b7）：`docs/deploy.md` 新增「测试赛」（`--claim`：等开放座位、等待时的提示、重试、认领前可以 Ctrl+C、先跑 selfcheck）和「被分到红警」；README 加上测试大厅、红警、怎么合上游；HANDOFF 写明这个分支和主人要合什么。（`--claim` 这部分当天就过时了：平台 10-06 停用了认领码，见下面「10-06 晚的文档更新」。）
- **检查**（输出在 `~/wt/_up_out`）：
  - pytest：合并 + 修复后的树 1587 个全过（542 s）；冻结冠军后的最终树 bfa28d3 也是 1587 个全过（574 s）。新增的测试主要是上游的 `tests/test_realtime.py`、我们的 `tests/strategy/test_realtime_safety.py`（整场红警走 `run_game` 和官方 worker、selfcheck 和 replay_real 读红警录像）和 `tests/test_main_claim.py`。
  - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，决策 p99 1536 ms、最慢 1911 ms；`--no-calc` 同样 0 失败、0 修补，p99 1562 ms、最慢 1807 ms。
  - 本地对局不变：对冠军打 20 局（seed 171，确定性设置），main 2a20b35 和本分支 72959b7 逐局相同（20/20，比了 280 个决策），两边都是 11 胜，搜索用上 260/260，0 非法、0 计时器代打，最慢决策 1.99 / 2.10 s。
  - 狼人杀官方 runner 路径（`local.werewolf_platform`，种子 1003，40 局）：本分支和 main 都是 0 报错、0 卡死、0 闲置代打、0 被拒的动作或聊天，我方座位都赢 67/112，最慢决策 19.6 / 16.3 ms，stderr 只有 1 行 `[match-summary]`。
  - 红警端到端（`~/wt/_up_out/ra_e2e.sh`：从压缩包起整棵进程树 `run_agent.sh` → `python -m agent --tournament` → 每个座位一个 worker，对本地模拟平台的官方控制面、假 key；2 场 45 s 的红警 + 1 场宝可梦 + 1 桌狼人杀，录像打开）：合并前（a3a1271 的包）2 场红警起了 5 次 worker，5 次 `match failed: … not an orders batch: {'type': 'pass'}`，平台收到 5 个不合法的批次。合并后（bfa28d3 的包）每场红警只起 1 个 worker，打到时间结束正常退出：0 次 `match failed`、0 个批次、0 个不合法动作（平台发了 205 / 186 个画面，我们回答了 204 / 185 次 `WAIT`），`get_game_config` 每场取 1 次，录像每场 1 行；同时的宝可梦（14 个决策，赢）和狼人杀照常；0 个 Traceback，key 没进日志，停止后 0 残留。
  - 新压缩包 `arena-agent-bfa28d3`（46 MB / 416 个文件，密钥扫描干净；`agent/`、`altruagent/`、`data/`、`deploy/`、`run_agent.sh`（755）和工作区相同）：只有 `/usr/bin:/bin` 的干净环境下 selfcheck ready（只有没配 key 的警告；60 个录像决策有 / 没有计算器都 0 被拒，最慢 1.81 / 1.66 s），包里 `replay_real.py` 60 个决策 0 失败、p99 1.75 s，`agent.health` → `idle(probe=1.6s)`。
  - 从这个包跑 `rehearsal_official.py`（dry-up）：13 项全过，4.4 分钟；宝可梦 7 场、狼人杀 3 桌，109 个决策，0 非法、0 计时器代打、0 竞争错误；kill worker 后 14.3 s 起新 worker、17.3 s 后下一步；kill 运行时 4.2 s / 8.2 s，0 次 seat_busy、0 次新 grant；认证接口 503 的 15 s 里运行时没换、对局没停；热备接管 29.7 s，那一回合没被代打（这一步等了 32.0 s）；非租约持有者 0 步，key 没进日志，停止后 0 残留。数字也记在 `docs/deploy.md`「官方模式演练」。
- **冻结为冠军**（bfa28d3）：`champion/` = 72959b7 的 `agent/`（`champion/VERSION` = 72959b7）。打法和 v4.2 相同（上面 20 局逐局相同），以前的擂台 / panel 数字可以接着比。
- **10-06 晚的文档更新**（5692a46、dac7375，只改文档和 `scripts/sync_docs.sh`）：平台 10-06 停用了测试赛认领码（`--claim` → 410 `claim_codes_retired`）和旧的平台 agent（sk key、发现模式 → 410 `platform_agents_retired`），测试座位改成 Official Agent Key 的分配（`context: "testing"`），开着的 `--tournament` 两种都接（我们的 SDK 不看 `context`）。`docs/platform/` 按官方文档 v0.5.0 重新同步（新增 `official-agent.md`，删掉 6 个停用的页面，`sync_docs.sh` 的列表跟着改）；`docs/deploy.md`「测试赛」改写，加了大厅里的座位每 10–20 s 起一个短命 worker 是正常的、红警「5 秒窗口」模式下会回到崩溃重起、`GAME_NOT_STARTED` 不在 runner 重试列表里（上游缺陷第 9 条）。当时的检查脚本因为关机没跑；10-07 复审在 dac7375 上补跑：全量 pytest 1587 个通过（601 s）；`replay_real.py` 2349 个决策 0 失败、0 修补，搜索用上 1296/1296，p99 1623 ms、最慢 2063 ms；`--no-calc` 0 失败、0 修补，p99 1565 ms、最慢 2426 ms。

### v4.2.1 补丁（10-07，分支 `merge/upstream-1005`；分支上的提交和文档叫它 v4.2.2，合并时为了版本号唯一记在 v4.2.1 下）：重新登录时平台签发会话失败，不再停掉运行时（本地对局不变）

修复类版本，不让 agent 变强，宝可梦、狼人杀的打法不变。起因是 10-07 的第 3 轮复审：用真的 SDK 客户端对一个假控制面（httpx MockTransport，不联网），装上我们的 `_SteadyOfficial`，跑 SDK 自己的 `run_tournament_forever`，把「agent session 过期后重新登录」的各种结果都试了一遍。

- **问题**：agent session 大约一小时过期，运行时要用 Official Agent Key 重新登录（比赛中大约每小时一次）。上游 1372ccc（10-06，在上游 main 5bc61a2 里，不在本分支）写明：平台自己的登录服务拒绝或限流时，认证接口回 401 `invalid_official_agent_key`，`detail` 以 `Failed to mint agent session` 开头（「签发会话失败」，是平台的问题，不是 key 的问题）。我们的 `_transient` 把所有 401 都当成 key 被拒。复审在 dac7375 上测到、本次修之前重现：(A4) 重新登录碰上它，运行时退出（`RUNTIME STOPPED … invalid_official_agent_key`），`finally` 里把正在打的对局 worker 全部终止，看门脚本 3 s 后才重启；(A6) 重新登录成功、但接着的查询仍然 401（新会话没被接受，SDK 抛普通的 `AuthenticationError`），同样退出；(A7) 启动时碰上它，试 1 次就退出，还打印 SDK 的「The Official Agent Key was not accepted. Copy it again …」，可能让人在比赛中途去换一把好好的 key。503 `agent_session_unavailable`、429、500 本来就没事（A1–A3）。main 上的代码一样，不是合并带进来的。线上文档 v0.5.0 现在给签发失败写的是 503（我们本来就当暂时性），平台还会不会回这个 401 不确定，所以按上游的说法两种都接住。
- **修复**（a5f8648，只改 `agent/__main__.py` 的官方运行时，`altruagent/` 不动）：`_transient` 照上游的 `is_fatal_auth_error` 把「401 + `Failed to mint agent session`」和「重新登录后新会话仍 401」当成暂时性。效果：运行时和所有 worker 照常，这一轮不查分配，查询暂停 15 / 20 / 30 s（每次重试都会签发一个新会话，不能每 10 s 刷一次）；日志写 `the platform could not start an agent session (HTTP 401: Failed to mint agent session: …); a temporary problem on the platform, not the key` 或 `the control plane did not accept a freshly issued agent session (HTTP 401: …)`，不再叫人重新复制 key；启动时按 5 / 10 / 20 / 30 s 退避一直重试；`--check-tournament` 也不再把它说成「Official Agent Key rejected」。真正的 key 被拒（`detail` 是别的，比如 `Invalid official agent key`、Oracle 托管）、403 `registration_incomplete` 和其他 4xx 仍然立刻退出。比上游保守的一点：上游没点名的 4xx（比如 403 forbidden）和格式不对的回答，我们仍当致命（交给看门脚本按退避重启）。
- **测试**（`tests/test_deploy.py`，新增 / 扩展 5 个）：真客户端 + 假控制面走 SDK 的 `run_tournament_forever`，签发失败、503 `agent_session_unavailable`、新会话仍 401 三种都只起 1 个 worker、从没被停、恢复后有 `Assignment discovery recovered.`，日志里没有「copy it again」；新会话仍 401 时 10 轮查询里一共只登录 3 次（开始 1 次 + 两次失败的重新登录，暂停期间不再登录）；分类表（签发失败、新会话 401 是暂时性；真的 key 被拒、Oracle 托管、`registration_incomplete`、无代码的 401、`detail` 不是字符串都不是）；启动时签发失败退避 5 s、10 s 后连上，真的 key 被拒仍试 1 次就退出；`check-tournament` 的说法。这 5 个在修之前的代码上全部失败，修之后通过。复审脚本 `r3_harness.py` 在修好的树上重跑：A4、A6 运行时不再退出、worker 不被终止，恢复后照常查询；A5（真的 key 被拒）仍然退出；A7 一直按退避重试。
- **文档**（b60a2cf、f9e01be）：README、`docs/deploy.md`、HANDOFF 不再说 PR #5 没合：它 10-06 已合进上游 main（5bc61a2，比本分支多 9 个提交），协议没变，10-16 不必需；合它时要注意的事（`run_agent.sh` 传 `--tournament --match`、手工移植加固、v4.2.2 就是 1372ccc 的分类、按座位退避 4cb083a 没移植）写在 README「同步官方更新」；上游缺陷第 1 条上游已修，不用再报。`docs/platform/` 按 10-07 的线上文档重新同步（还是 v0.5.0，5 个文件：Oracle 托管的 agent 现在由平台代打、新的就绪代码 `no_submission`；自托管运行时、开局门、错误处理都没变）。
- **检查**（树 f75722c，输出在 `~/wt/_upfix2_out`）：
  - 全量 pytest（1592 个，比 v4.2.1 多 5 个新测试）：第一次 1591 过、1 个失败（543.6 s）：`tests/strategy/test_engine_search.py::test_engine_modes_answer_legal_moves[extra1]`（引擎搜索的 guard 选择，要的是 `guard`、得到 `free`）。这个测试的搜索有 1.5 s 时间预算（`budget_ms` 1500），整套测试跑到一半机器忙时搜索迭代次数变少，结果就会变；引擎代码（`agent/pokemon`、`agent/js`）和 dac7375 一模一样，这次也没碰。单独在重任务锁下重跑：这个测试 8 次 × 5 个参数全过，整个文件 40 个全过。同一棵树第二次全量：1592 个全过（540 s）。
  - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，决策 p99 1456 ms、最慢 1791 ms；`--no-calc` 同样 0 失败、0 修补，p99 1437 ms、最慢 1845 ms。
  - `rehearsal_official.py`（dry-up2，直接用工作区 b0ac3a6，不是压缩包；只对本地假平台、假 key）：13 项全过，4.5 分钟；宝可梦 6 场、狼人杀 3 桌，104 个决策，0 非法、0 计时器代打、0 竞争错误；kill worker 后 14.5 s 起新 worker、20.6 s 后下一步；kill 运行时 3.4 s / 7.4 s，0 次 seat_busy、0 次新 grant；认证接口 503 的 15 s 里运行时没换、0 个 worker 被停（日志 1 行 `re-authentication failed`、1 行 `recovered after 2 failure(s)`）；热备接管 31.1 s，那一回合没被代打；非租约持有者 0 步，key 没进日志，停止后 0 残留。
  - 本地对局不变：`agent/` 里只有 `__main__.py` 变了，而且只在官方运行时连控制面出错时起作用，决策代码一行没动（`git diff dac7375 -- agent/` 只有 `__main__.py`），所以没跑擂台 / panel。
- **冻结为冠军**（f75722c）：`champion/` = f9e01be 的 `agent/`（`champion/VERSION` = f9e01be）。打法和 v4.2 / v4.2.1 相同，以前的擂台 / panel 数字可以接着比。上次的压缩包 `arena-agent-bfa28d3` 里是旧的 `agent/__main__.py`，部署前要从这个分支重新出包。

### v4.2.2（10-06，分支 `pk/platform-1005`；分支上的提交叫它 v4.2.1，合并时改成 v4.2.2）：对照平台 10-04/05 的宝可梦文档改动（本地对局不变）

平台 10-04/05 改了宝可梦文档（公开的 `/skill/pokemon`，和仓库里旧的 `docs/platform/pokemon.md` 逐行比较；`docs/platform/` 由另一个分支刷新，这里没动）。每一条都用真实录像的 payload（平台发来的原始数据）写了测试，新文件 `tests/strategy/test_platform_1005.py`（26 个）。大部分不用改代码，只有「选秀 15 秒时钟」要改。

- **对战的决策计数器开战时从 0 重新数**（`state_version`：每次提交动作要带上的版本号；例：选秀数到 12，开战变回 0，文档说「别以为它只会变大」）：录像里本来就是这样（140 场：选秀 0..11、队伍预览 0、对战从 1 往上）。重启记忆（`agent/matchstate.py`）比较「进度」时先比阶段（选秀 < 预览 < 对战）再比计数器，所以 12 → 0 不会被当成倒退。测试：用 SDK 自己的 `run_game` 把一整场真实对局走一遍，计数器到 12、开战回到 0 的那一刻杀掉 worker（另一种：假设对战第一步又回到 0），新 worker 读回选秀记忆（对手 6 张卡），发出的每个动作、带的版本号都和不中断时相同。**不用改。**
- **轮到我们（`is_current_actor`）却没带 `legal_actions`**（`next_actions` 提示去调 `get_legal_actions`）：SDK 的 runner 本来就会去取，我们的 `_raw_with_legal` 把取到的动作补进状态。同一个测试里每隔一个决策就缺一次，答案逐个相同。**不用改。**
- **`wait_for_update` 超时返回 `updated: false`**：runner 只是接着等。同一个测试里每次等待都先超时 1–2 次。**不用改。**
- **结束约 5 分钟后会话被删（再问就是 `SESSION_NOT_FOUND`）、游戏服务器重启算弃局、`termination_reason` 可以是 `"failed"`（对战服务器坏了，没有胜者）**：worker 看到 `"failed"` 正常退出；在已删的会话上起来的 worker 以失败退出，但官方运行时对两种退出一视同仁：冷却 10 s，座位还在分配列表里才再起一个，列表里没了就停，不会死循环（测试模拟了「结束后还挂在列表里 60 s」）。这就是 10-06 复审记下「没改的」那条，仍然不改。
- **-1 / -2 是固定的位置**（0 号位：-1 是自己、-2 是队友；1 号位反过来；每个槽有 `board_position`）：所有录像的模板里 `board_position` 都是 -(槽号+1)，平台从没给过「打自己位置」的目标；我们的前瞻和引擎搜索本来就按固定位置读，历史上发出的 10 次负目标（帮助、花粉团）全是队友，今天的代码重答这些状态也是。**不用改。**
- **两只同时倒下、只剩一只替补**（模板带 `notice`）：录像里有 9 个这样的状态（7 场）。策略、引擎搜索、崩溃兜底、看门狗超时（看门狗：决策超时后替策略答一个安全动作的保护）、原样交回模板、通用安全动作，6 条路径都答「一边换上、一边 pass」。**不用改**（09-29 那次 pass + pass 事故在 v4.1 已修）。
- **组队赛（teambuild）**：每只可以带 `ivs`；队伍字段在但格式不对，现在也要用掉 8 次机会中的一次。我们第 0–7 次提交的队伍和兜底队伍都格式正确。**不用改。**
- **要改的：选秀每一手现在有 15 秒时钟**（从轮到我们算起，`observation.decision_timeout_seconds`；过时服务器替我们随机选一张，我们迟到的选择被当成 `STALE_STATE` 拒绝）。以前选秀的时间预算是 20 秒（看门狗再宽限 2 秒），比 15 秒还长；而且一个对局进程的第一手还要等计算器（node + @smogon/calc）启动，冷机器上要好几秒，这段等待也只受 20 秒预算的限制。正常情况下选一手只要几十毫秒（真实状态重放：中位 17 ms、最慢 0.42 s），所以只有机器很慢或卡住时才会出事，但一出事就是随机拿牌。
  - 修 1（`agent/timecontrol.py`）：选秀预算 = 这一手的时钟（`decision_timeout_seconds`，没有就按文档的 15 s）减 5 s，最多 20 s，即 10 s；剩下 5 s 留给察觉轮到我们、来回网络和看门狗宽限。实验用的 `ARENA_TC_DRAFT_S` 仍可直接指定预算。测试重现：计算器 8 s 才起来、时钟 6 s（按比例缩小）时，main 的第一手 8.1 s 才发出，现在 1 s 左右发出合法的一手；策略卡住 4 s、时钟 3 s 时，main 4 s 后才答，现在看门狗 0.6 s 内答。
  - 修 2（`agent/pokemon/policy.py` 队伍预览）：我们的最后一手如果是服务器替我们选的，那张卡的完整配置从没进过选秀记忆，整场对战都按通用配置算伤害。现在队伍预览时像补对手的卡一样，从选秀里见过的所有卡按种类补上我们自己的。30 场真实录像模拟「最后一手被替选」（经典打法重放）：main 有 7 场的 9 个对战决策因此答得不一样，修后 0 个。
  - 迟到的一手被拒（`STALE_STATE`）时 runner 会重读状态接着打，测试里整场照常打完。
- **检查**（输出在 `~/wt/_pk_out`）：
  - 新测试在 main 上 4 个失败（都是 15 秒时钟那几个），其余 22 个在 main 上也通过；在本分支 26 个全过。`tests/strategy/test_timecontrol.py` 里 3 处「选秀预算 20 s」改成 10 s。
  - 本地对局不变：对冠军 20 局（seed 171，确定性设置），main 和本分支逐局相同（20/20，候选的 280 个预览 / 对战答案、协议日志都相同，都是 11 胜，0 非法、0 计时器代打，选秀最慢 0.43 s）。本地模拟器的选秀没有 15 秒时钟、也不会替我们选，所以两处修改在本地都不会触发。
  - 冻结后的树 78e622d：`replay_real.py` 2349 个真实决策（142 个录像文件）0 失败、0 修补，搜索用上 1296/1296（0 次退回经典打法），每个决策平均 485 ms、p99 1578 ms、最慢 1838 ms；`--no-calc` 同样 0 失败、0 修补，p99 1528 ms、最慢 1751 ms。全量 pytest 1568 个通过（639 s）；selfcheck ready（0 失败，只有没配 key 的警告；重放 80 个决策带 / 不带计算器都 0 被拒，最慢 2.45 s / 2.07 s）。
- **冻结为冠军**：`champion/` = 39d0841 的 `agent/`（提交 78e622d）。
- **顺带发现、没改的**（都不在宝可梦决策代码里）：本地评测用的平台模拟（`local/field_opps.py`、`local/fake_platform.py`）的选秀还没有 15 秒时钟，示例 LLM 对手在平台上选得慢会被随机替选，本地看不出来；平台 10-06 起令牌被拒时工具可能直接答 `UNAUTHENTICATED`（工具结果，不是 HTTP 401；10-06 之前签发的令牌都会碰到一次），而 SDK 只在 HTTP 401 时重登，这种情况下 worker 退出、冷却 10 s 后重起（选秀中就是丢一两手），比赛前重启一次运行时可避开旧令牌那一次，对局中令牌过期会不会这样要看平台（SDK / 部署范围，留给主人报告）；平台锦标赛（不是 UCLA 赛事）改成每局自己 `join_session`，我们的发现模式不会去加入，UCLA 赛事走官方 key（`--tournament`）不受影响。

### v4.2.3（狼人杀）：平台 10-04/05 改了措辞（10-06，分支 `ww/platform-1005`，宝可梦不变）

平台文档 10-04/05 把狼人杀里的 "lynch"（私刑处决）全换成 "eliminate / vote"（投票出局）。`docs/platform/` 已按 10-06 拉下来的原文同步（当时另外新增了 `testing-seat-claim.md`；10-07 按线上 v0.5.0 重新同步时这一页和另外 6 页都已下线、删掉了，见下面「平台文档 10-07 重新同步」一节），对照表见 `docs/real_payloads.md`「Werewolf wording 10-05」。我们**从没见过真实的狼人杀 payload**，文档可能比服务器早改也可能晚改，所以部署的代码**新旧两种写法都认**。修复类版本，不改打法。

- **改了什么**（`agent/werewolf/state.py`）：死亡行认 `(lynch)` / `(night_kill)` 和 `(eliminated by vote)` / `(killed at night)`；投票行认 `Player1 lynched` 和 `Player1 eliminated by vote`；死因统一成内部的一个值（`night_kill` 或 `vote`，`norm_cause`），结构化字段 `cause` 是旧的 `lynch` 还是新的 `vote` 都一样；预言家第一次查验前的 `You have not investigated anyone yet.` 算预言家线索（身份行认不出来时才用得上）；`Vote to eliminate PlayerN` 本来就算白天动作（`_DAY_LABEL` 里早有 eliminate）。结束时多出的 `Game over — WOLVES win` 一行不影响任何解析。**聊天长度上限**：平台新写明超过 1024 个字符回 `MESSAGE_TOO_LONG`，而 runner 碰到它会让进程退出；我们的模板最长才一百多字符，但现在发出前（`agent/agent.py` 的 `clip_chat`）和策略的发言检查（`chat_guard`）都按 UTF-8 字节卡在 1024 以内（字节数不会比字符数少，所以服务器不管怎么数都不会超）。
- **本地模拟器**（`local/werewolf_env.py`、`local/werewolf_platform.py`，`fake_official.py` 用后者）：默认说新话，`ARENA_WW_WORDING=old` 换回旧话；平台替身在讨论窗口以外把 `new_messages` 清空（投票时也是空的，和新文档一样），窗口开 120 s 强制结束并带 `messaging_seconds_left` / `messaging_deadline`，超过 1024 字符的聊天回 `MESSAGE_TOO_LONG`；`ARENA_WW_MESSAGING=off` / `--no-messaging` 是没有讨论窗口的局（`messaging_enabled: false`，有人发消息就回 `MESSAGING_DISABLED`）；替身 bot 在自己结束发言后也继续读窗口里后来的消息（真 runner 下的 agent 就是这样，不然投票时 `new_messages` 已经空了）。`ARENA_WW_SAME_NAMES=on`（只用于评测）让我方座位看到所有座位同一个名字，这样 agent 只能读 observation 里按座位写的文字（测文字解析那条路：两个座位重名时部署的 agent 就走这条路）。
- **测试**：新增 `tests/strategy/test_werewolf_wording.py`（36 个）：两种措辞、结构化字段和文字两条路读出同样的死亡 / 死因 / 投票；`norm_cause` 对照表；预言家第一夜那句话；Game over 行和新 label 不改身份、不报警；整局对比（我方 3 座 + 4 种替身，6 个种子，两种措辞、名字正常和全重名）逐局相同；替身投票时 `new_messages` 为空、窗口 120 s 到点结束；没有讨论窗口时我方 0 次调 `choose_message`、0 条消息、0 次弃票；策略被改坏、想发 1100 字符消息时，经过真 runner 发出去的都 ≤ 1024，平台 0 次拒绝。
- **测量**（新种子 1007、1008；`python -m local.eval_werewolf --games 1000 --seeds 1007 1008 --bg llm,llmx,llmq,heuristic,first,mixed --ks 1,3,5,7 --pairs agent:champion`，`agent` = 本分支，`champion` = main 2a20b35 的 agent；每次 19 种桌型 × 2 种子 × 1000 局 × 2 个 agent = 76,000 局；输出在 `~/wt/_ww2_out`）：
  - **旧措辞**：38 个条件全部配对差 **+0.00**（逐局相同）；每座 57.48% [57.12, 57.83]（n=38000），村民 58.59、预言家 64.48、狼 45.53。
  - **新措辞**（默认）：同样 38 个条件全部 **+0.00**，而且每个条件的胜率和旧措辞一模一样（两版都走结构化字段，措辞只出现在文字里）。llmq 背景每座 58.80%，我方 1~3 座 58.01%。
  - **新措辞 + 全重名**（逼 agent 读文字）：本分支和它自己走结构化字段时**逐条件相同**（文字解析和结构化字段等价）；main 的文字解析认不出 `(eliminated by vote)` / `(killed at night)` 和 `eliminated by vote`，死亡记录整个读空，每座 **47.09%**，比本分支低 **−10.39 个百分点**（CI −10.67~−10.10），村民 −21.56、预言家 −23.36（main 的狼反而 +20.62：同桌的 main 村民被骗了）。这就是新措辞下 main 唯一会出差错的地方：真实平台上两个座位重名时（比如同一个参赛者的 agent 占了 Testing 局的几个座位）才会走到。
  - **旧措辞 + 全重名**：本分支和 main 逐局相同（+0.00），说明上面的差距全是措辞造成的。
  - **没有讨论窗口**（`ARENA_WW_MESSAGING=off`，新措辞）：本分支和 main 逐局相同；每座 44.31%（有讨论时 57.48%），村民 36.88、预言家 38.97、狼 62.25，全程 0 条消息。说明讨论对我们值 13 个百分点左右；不发言时预言家的信息传不出去，只靠投票记录的打法没调过（仪表盘建的狼人杀都用带讨论的预设，只有用 API 建局才可能关掉）。
  - 五次评测合计 380,000 局：0 崩溃 / 0 非法 / 0 被拒聊天 / 0 超时 / 0 结束发言后再发，最慢决策 68 ms，分片退步 0。
  - **官方 runner 本地路径**（`local.werewolf_platform`，每个座位跑 `altruagent.runner.run_game`）：新措辞种子 1007 共 40 局：我方 0 报错、0 卡死、0 闲置代打、0 被拒，最慢决策 20.5 ms，我方赢 71/110，214 张票 0 弃票，241 条聊天；没有讨论窗口（种子 1008，20 局）：0 报错 / 0 被拒，106 张票 0 弃票，0 条聊天，赢 25/56；旧措辞（种子 1008，20 局）：0 报错 / 0 被拒，赢 32/56。冻结后又跑新措辞种子 1009 共 60 局：0 报错 / 0 卡死 / 0 闲置代打 / 0 被拒，最慢决策 15.9 ms，赢 100/173，339 张票 0 弃票，355 条聊天。stderr 里只有 `[match-summary]` 行，没有 role check 报警。
- **检查**：`scripts/replay_real.py` 2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 1532 ms、最慢 1827 ms；`--no-calc` 0 失败、0 修补，p99 1522 ms、最慢 1681 ms（本分支没动宝可梦）。冻结为冠军（`champion/VERSION` = 877dca4，提交 09874e1）后全量 pytest **1578 个全过**（563 s）；`scripts/selfcheck.py`：ready（0 失败，只有没配 key 的 1 个警告）。冻结前本分支的 `champion/` 就是 main 的 agent，所以上面的 `agent:champion` 就是「本分支对 main」。
- **新文档里和我们有关、这次只记下来没改的**：宝可梦选秀每一手 **15 秒**，超时服务器随机替我们选（`decision_timeout_seconds` / `decision_deadline_at`），战斗开始时 `state_version` 归零；UCLA Testing / 正式局在所有座位连上之前动作会回 `GAME_NOT_STARTED`；对局结束约 5 分钟后 session 被删。（**10-07 更正**：这里原来还写了「平台自己的锦标赛是瑞士轮、要在 4 分钟内 `join_session`，不是 UCLA 正式赛，发现模式不会去加入」和「10-06 之前的 token 要重新登录」。10-07 的线上文档说法不同：瑞士轮 + 淘汰赛 / 狼人杀决赛**就是 UCLA 正式赛的赛制**，每局都作为 eak key 的 assignment（分配给我们的座位）自动送来，`join_session` / `tournament_matches` 和整套 `/auth/agent/*` 登录都已下线，发现模式也就不能用了。见下面「平台文档 10-07 重新同步」一节。）还有：worker 在投票阶段重启、又没有状态文件时，`new_messages` 已经是空的，这一天的发言就丢了（SDK 不给 `choose_action` 调 `get_messages` 的口子）。

### v4.2.3（狼人杀）：讨论时「多听一会儿」（`ARENA_WW_LISTEN`，10-06，分支 `ww/platform-1005`，默认关，宝可梦不变）

背景：讨论窗口开着时，官方 runner 每次有新消息（或等满 20 s）就再问一次 `choose_message`，每次必须回一条聊天或 terminate（结束发言）。我们几毫秒就回答，所以每天都是第一个开口；之后第一次被叫醒时如果没话可说，就 terminate 了。terminate 之后 runner 还会继续叫醒我们，`observe()` 照样记下后来的聊天，投票时都用得上：也就是说**听得到，但不能再回应**（比如有人后来跳预言家说我们是狼）。「多听一会儿」打开后，开场发言后不马上 terminate，而是在 `choose_message` 里面用 runner 自己的会话做只读的 `wait_for_update`（读不算活动，不重置闲置时钟）：有值得回应的话就补一条（每天最多 1 条），否则在停止规则触发时结束：其他活着的座位都结束了、窗口长度的 1/5（120 s 窗口是 24 s）没人说话、离关闭只剩 1/8（15 s）、补发用掉了，或 5 条额度用完。runner 不把会话交给我们，所以要沿调用栈找 SDK `run_game` 的局部变量 `game`（下面叫 frame walk），这是它脆弱的地方。代码是上一步写的（6a085c1、2237e11），这一步做验证和决定。

- **关闭时（默认）安全**：
  - 逐个决策对照（新种子 1010、1011，照 `eval_werewolf` 发牌，6 种背景 × 我方 1/3/5 座 × 每格 100 局 × 两种讨论节奏；同一局分别让 champion（= 第一步末端 36d278e 的 agent，冻结版 877dca4）、本分支经 `message_step`（模拟器走的入口）、本分支只经 `choose_message`（真 runner 走的入口）来打）：**14,400 局全部逐个决策、逐条聊天、逐张票相同**（rounds 节奏 77,393 个决策，rt 节奏 235,629 个）。听的四个入口（`_runner_game`、`_listen`、`_listen_wait`、`_may_hold`）换成「一碰就记数并报错」的绊线，**被调用 0 次**。反过来打开听做对照，只有 32/72、36/72 局相同，说明这个对照查得出差别。
  - 配对评测（`python -m local.eval_werewolf --games 1000 --seeds 1010 1011 --bg llmq,llm,llmx,heuristic,first,mixed --ks 1,3,5 --pairs agent:champion`，每种节奏 36,000 局配对）：`ARENA_WW_TIMING=rt` 和 `rounds` 下所有条件、所有身份的差都是 **+0.00，而且没有一局不同**；聊天条数相同（220,681 / 228,392）；0 崩溃 / 0 非法 / 0 被拒 / 0 超时 / 0 结束后再发 / 0 窗口超时。每座胜率 rt 56.95%、rounds 57.28%。
  - 真 runner 替身（`local.werewolf_platform`，24 s 窗口、替身每次发言想约 1.5 s、llmq，种子 1010、1011 各 30 局）关闭时 0 次 hold、0 报错。
  - 新增 4 个测试（`tests/strategy/test_werewolf_listen.py`）：不设开关时，经过真 `run_game`、两种模拟节奏、多线程平台替身，绊线都没被碰到。
- **对上游新 runner**（upstream/main 6ba2e9c 为实时游戏改了 `altruagent/runner.py`）：在临时 worktree 里把 upstream/main 合进本分支（没提交，用完已删；冲突的 README.md、agent/__main__.py 取 merge/upstream-1005 的合并结果，`altruagent/` 与 upstream/main 完全相同）。讨论那段代码上游没动，`run_game` 还是用 `game` 这个参数名。听的测试、措辞测试、上游的 realtime / runner 测试共 139 个全过。平台替身（10 s 窗口、替身想 0.5 s，各 10 局）：正常情况 frame walk **35/35 次都找到会话**，5 条补发，0 错误；再故意制造三种「SDK 变了」的故障：找不到会话、`wait_for_update` 参数改名（TypeError）、返回值类型变了，各 10 局全部退回直接 terminate（分别记 39 次 no_game、52 次 error、37 次提前结束），**0 个异常传给 runner**、0 闲置代打、0 被拒、40 局全部打完。120 s 真实长度窗口（3 局，替身想 6 s）在合并树里和本分支结果一模一样（13 次 hold、1 条补发、最长听 29.2 s、赢 3/8）。部署的三条路（`--tournament` 的 worker、发现模式的 `run_match`、`--claim`）最后都调 `altruagent.runner.run_game`（10-07 的线上文档说后两条对应的平台接口已下线，只剩 `--tournament` 这一条，见下面「平台文档 10-07 重新同步」）。
- **打开时的测量**（同样的命令加 `--pairs agent:champion,agent/champion:champion,champion/agent:champion`、`ARENA_WW_LISTEN=on`；输出在 `~/wt/_ww2c_out`）：
  - **rt 节奏**（官方 runner 真实的叫醒方式）：整桌每座 **+0.18** 个百分点 [+0.11, +0.24]（n=36,000）；三种大模型替身背景合起来每座 **+0.33** [+0.21, +0.45]；**我方 1 座**（正式赛一桌只有我们 1 座）的大模型背景每座 **+0.62** [+0.34, +0.90]（n=6000），几乎全来自狼 +1.94 [+1.03, +2.85]（村民 +0.03、预言家 +0.35，都不显著）；分背景 1 座：llm +0.80、llmx +0.60、llmq +0.45 [−0.04, +0.94]、mixed +0.15（不显著），heuristic / first 正好 0（它们不跳预言家，没什么可回应）。只换一边的配对：狼 +0.67、村民 +0.13、预言家 +0.24，都显著为正。但有 **6 个条件显著变差**：都是我方 3 座时整桌配对里的村民（−0.62 ~ −2.27）：这种桌上我方的狼也在听、更会反咬，赢的是我方的狼、输的是我方的村民（只换村民那边的配对是正的）；正式赛一桌只有我们 1 座，碰不到这种情况。0 崩溃 / 0 非法 / 0 被拒 / 0 超时 / 0 结束后再发 / 0 窗口超时；聊天多 4.1%（229,653 对 220,681）。
  - **rounds 节奏**：36,000 局里只有极少几局结果不同（只出现在 6 个「条件 × 身份」格子里），都不显著（每座 −0.00）；有 3 次 `window_close`（窗口关闭时我方还在听）：这个节奏每 20 s 才问我方一次，最后一次问时离关闭还剩 15 s 以上就继续听，下次再问已经关了。真 runner 下听的循环最多 5 s 就看一次倒计时，到 15 s 一定结束（真 runner 替身里一次都没发生）。
  - **真 runner 替身**：24 s 窗口两组 30 局，打开 / 关闭我方赢的局**完全一样**（68/111、50/90）；打开时 202 + 172 次 hold、31 + 23 条补发、最长听 8.56 / 7.52 s（窗口 24 s），0 报错 / 0 卡死 / 0 闲置代打 / 0 被拒；`WRONG_PHASE_NOOP` 从 37 + 26 次变成 91 + 76 次（我们结束发言更晚、离窗口关闭更近，runner 那次多余的重复 terminate 更常撞上关闭；runner 当作竞争重读，无害）。120 s 窗口、替身想 6 s：种子 1012 打开 / 关闭都赢 3/8（打开 13 次 hold、1 条补发、最长听 29.18 s），种子 1013 打开 21 次 hold、3 条补发、最长 32.33 s，都是 0 报错。没有讨论窗口的局（20 局）0 次 hold、0 条聊天。替身全都瞬间回答、一桌我方约 3 座（种子 1014，40 局）：打开赢 78/135、关闭 79/135；打开 213 次 hold、39 条补发、最长听 24.67 s，0 报错；但打开整批要 1691 s，关闭只要 2 s：一桌我方好几座都在听时，彼此等对方，直到 24 s 安静规则触发，每个窗口白白拖长 20 多秒（正式赛一桌只有我们 1 座，碰不到；Testing 局里同一个 agent 占几个座位时会这样）。
- **决定：默认保持关闭**。打开的收益只在 rt 模拟节奏里看得到，而且小（1 座时每座 +0.6 个百分点，十几局正式赛里期望多赢不到 0.1 局）；rounds 节奏和真 runner 替身上看不出差别；整桌配对有 6 个显著变差的条件（解释得通，但不满足「哪里都不显著变差」）；24 s 窗口的替身里 `WRONG_PHASE` 竞争多了约 2.6 倍；靠 frame walk 读 SDK 内部，而我们**从没见过真实狼人杀服务器**的 `wait_for_update` 表现（比如读得太勤会不会被限流；限流时 runner 自己的 terminate 也可能失败，让进程退出）。代码留着、默认关：以后拿到真实狼人杀日志、看到大模型对手常在讨论后半段跳预言家时，再用 `ARENA_WW_LISTEN=on` 重测。注意冻结后 champion 也认这个开关，以后再比「开 vs 关」要让两边分别设（比如一个工厂函数里强制 `listen=True`），只设环境变量会两边一起开。
- **检查**：冻结为冠军（`champion/VERSION` = e18ab75，提交 18df2a2；e18ab75 只比 2237e11 多了上面的 4 个测试）后全量 pytest **1610 个全过**（600 s；第一步 1578 + 上一步 28 + 这次 4）；`scripts/replay_real.py` 2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 1674 ms、最慢 2041 ms；`--no-calc` 0 失败、0 修补，p99 1536 ms、最慢 1727 ms（宝可梦部分没改，和第一步的时间差别只是机器负载不同）；`scripts/selfcheck.py`：ready（0 失败，只有没配 key 的 1 个警告）。没合进 main。

### v4.2.3（文档）：平台文档 10-07 重新同步（分支 `ww/platform-1005`，只改文档，agent 不变）

审查发现 10-06 03:52 同步的 `docs/platform/` 已经过时：线上文档当天又改了一版（skill v0.5.0），把旧的「平台 agent 账号 + 自己找局加入」那一套整个下线，换成 Official Agent Key（eak key）的 assignment 流程。

- **同步了什么**：10-07 00:30 UTC 只读拉取公开文档，线上现在只有 10 个主题 + `SKILL.md`。删掉已下线（访问回 404）的 7 页：`01-signup`、`02-auth`、`03-competitions`、`08-commands`、`09-end-to-end`、`avalon`，以及本分支 0048cbb 新加的 `testing-seat-claim.md`（它写的认领接口已下线）；新增 `official-agent.md`；其余 10 个文件按原文覆盖。文件和 `scripts/sync_docs.sh`（新主题表，先全部拉完再写入）与 `merge/upstream-1005` 的 5692a46 **逐字节相同**，两个分支合并时这些文件不会冲突（`git merge-tree` 试合并确认：合并后的 `docs/platform/` 与本分支完全一样）。同步后再跑一次 `scripts/sync_docs.sh`，没有任何改动。
- **狼人杀**：规则、eliminate / vote 措辞、1024 字符上限、5 条 / 50 词、120 s 窗口都没变，所以 `agent/` 不用改、不重新冻结。新写明的：座位在建局时定好（`your_position` = seat grant 的 `seat_position`）；7 个座位都连上之前状态里有 `waiting_for_players`，动作和消息回 `GAME_NOT_STARTED`（官方 runner 只在轮到我们时出手，按文档碰不到；这个码不在 runner 的竞争错误码里，万一碰到这一局的进程会退出、由 supervisor 重试座位；SDK 是上游的，不改）；每一局 Testing 和锦标赛狼人杀都用同一套带讨论的设置。对照见 `docs/real_payloads.md` 狼人杀那节的最后一段。
- **对部署的影响**（只读了代码和文档，**没有对真实服务器验证**）：Testing 认领码接口回 `410 claim_codes_retired`，所以 `python -m agent --claim`（SDK 的 `SEAT_CLAIM_PATH`）不能用了；旧的平台 agent 账号（`/auth/agent/*`）回 `410 platform_agents_retired`，所以不带参数的 `python -m agent` 发现模式（sk_agent key）也不能用了；MCP 的 `list_sessions` / `join_session` / `leave_session` / `get_tournament` 回 `TOOL_REMOVED`。现在 Testing 局是 `context: "testing"` 的 assignment，官方 starter 用 `--match` 打它、`--tournament` 只打 `context: "tournament"`。我们的 SDK 没有 `--match`，但 `altruagent/supervisor.py` 的 `run_tournament_once` 对列出来的每个 assignment 都起 worker，不看 `context`，所以我们的 `python -m agent --tournament` 会把 Testing 局也一起打（相当于 starter 的 `--tournament --match`）。这需要 owner 在真实平台上建一局 Testing 确认。
- **UCLA 正式赛的狼人杀赛制**（`04-tournaments.md`）：默认 4 轮瑞士轮，每轮一桌 7 人；赢的一方每人 +1（活着死了都算），输、平局、无结果都是 0；轮空 +1；有人没在连接窗口（默认 4 分钟）内连上，这桌不打，连上的每人 +1、没连上的 0；排名先比分数，再比 Buchholz（所有同桌对手当前分数之和），最后是开赛时抽好的随机顺序。瑞士轮前 14 名进决赛：2 桌、打 4 局，每局前按分数蛇形重新分桌，决赛分最多的赢；一局拖过 120 分钟会被判无结果。
- **和另外两个分支合并时**（审查指出原来的说明过时了）：`git merge-tree` 试合并（不建 worktree、不动任何分支），本分支 + `pk/platform-1005`（600af52）只在 `champion/VERSION` 冲突；本分支 + `merge/upstream-1005`（5692a46）在 `agent/agent.py`、`champion/agent/agent.py`、`champion/VERSION` 冲突。`agent/agent.py` 只有一处：`Contestant._summary` 的 match-summary 行，本分支加了听的统计后缀 `{listen}`，upstream-1005 加了 Red Alert 的 ` waits=` 后缀 `{waits}`；两个都留，写成 `...resumed={resumed}{waits}{listen}`，`champion/agent/agent.py` 用同样的结果，然后 `./scripts/freeze_champion.sh`、全量 pytest、两种 `replay_real`。审查在临时 worktree 里这样合过一次：狼人杀测试 + realtime / runner 测试 424 个全过；真 runner 替身关闭听 20 局 0 报错、0 次 hold、赢 35/59，打开听 10 局 0 报错、57 次 hold、14 条补发。`docs/progress.md` 两边以后都可能再改，到时候会有文字冲突，手工合并即可。（10-07 在 `integrate/v423` 上就是这样合的；另外 `merge/upstream-1005` 10-07 又同步了一次文档（f9e01be），所以 `docs/platform/` 的 5 个文件也冲突，取了较新的 f9e01be 原文。）
- **检查**：这一步只改了文档和这段说明（审查修复的文字，在 `integrate/v423` 合并时补进来），`agent/` 不变；合并后的整套检查见下面「v4.2.3 合并」一节。

### v4.2.3 合并（含上游 10-06 事件 agent 流程）（10-07，分支 `integrate/v423`，本地对局不变）

把三个做完的分支和上游 PR #5 合成一个分支，跑完整套检查，主人快进合进 main 即可（main 2a20b35 没动过）。修复 / 适配类版本，不让 agent 变强：宝可梦和狼人杀的决策代码只多了上面 v4.2.1–v4.2.3 几节写的改动。

- **合并顺序**（每一步先跑相关测试再提交）：
  1. `merge/upstream-1005`（3460952）：干净合并，树和该分支 1dd71a6 完全相同。
  2. `pk/platform-1005`（d6729c3）：只在 `champion/VERSION` 和本文件冲突；本文件两边都留，版本号改成唯一：上游合并 = v4.2.1（它 10-07 的重新登录修复在分支上叫 v4.2.2，这里记作「v4.2.1 补丁」），选秀 15 秒时钟 = v4.2.2（分支上叫 v4.2.1）。相关测试 159 个通过。
  3. `ww/platform-1005`（1859c07）：`agent/agent.py` 只有 match-summary 一行冲突，`{waits}{listen}` 都留，`champion/agent/agent.py` 同样；`docs/platform/` 的 5 个文件取 upstream-1005 10-07 那次较新的同步；ww 审查修复里还没提交的两处文字（本文件、`docs/real_payloads.md`）补进来；狼人杀两节改称 v4.2.3、挪到 v4.2.2 后面。狼人杀 + realtime 测试 409 个通过。三个分支合完的树（1859c07 的副本）全量 pytest **1686 个全过**（566 s）。
  4. **上游 main 5bc61a2（PR #5「event agents only」，ddf548a）**：`altruagent/`、`examples/`、`GAMES.md`、上游的测试原样取；`tests/test_main_claim.py` 随 `--claim` 删掉；`agent/agent.py` 取我们的；README、`.env.example`、`scripts/check_connection.py` 手工合并（`.env.example` 取上游的说明行再补回我们的本地评测几行；`check_connection.py` 用上游的写法，另外补上控制面默认地址）；`docs/STARTER_README.md` 换成上游新 README。
- **`agent/__main__.py` 怎么合的**：以上游的运行时为准（不带参数和 `--claim` 只打印停用说明；`--tournament` / `--match` / 两个一起，按分配的 `context` 过滤；重新登录的暂时性失败由 SDK 的 supervisor 自己处理：worker 照常、查询退避 10 / 20 / 40 / 60 s；按座位退避：同一座位连续失败时冷却翻倍到 600 s）。我们的加固只留还有用、又不和上游重复的：启动时平台暂时连不上按 5 / 10 / 20 / 30 s 重试（`_authenticate`；上游试一次就退出）、看门脚本给的 execution id（重启马上续上自己的座位）、热备（`_HotStandby`，原来 `_SteadyOfficial` 里管热备的那一半）、10 s 查询和 10 s 基础冷却（上游的按座位退避就从这 10 s 起翻倍）、不经看门脚本时的控制面默认地址和本机检查横幅。删掉的：`_SteadyOfficial` 的重新登录包装（上游 1372ccc 已经做了同样的事；上游 429 后停 60 s，我们以前是 30 s，这里跟上游）、发现模式（`_SteadyDiscovery`、`_run_discovery`）、`--claim` 那一整套。
- **部署工具**（3445521、f584061）：`scripts/run_agent.sh`（systemd、Docker、压缩包都走它）固定跑 `python -m agent --tournament --match`；`-- --tournament` 或 `-- --match` 可以只打一种；只有停用的 sk key、`.env.example` 的占位 key、`ARENA_MODE=discovery`、`--claim` 都拒绝启动并写明平台的 410 原因；同一把 key 的第二个运行时一律当热备；`status` / `health` 显示正在跑的看门脚本自己的角色和 key。`selfcheck.py` 只看 Official Agent Key。`bundle.sh` 的密钥扫描认得 `.env.example` 的 eak 占位。模拟平台的官方控制面（`local/fake_official.py`）：分配带 `context`（测试赛 / 正式赛轮流）、正式赛带比赛名、轮次、对手和 `connect_deadline_at`；正式赛座位 240 s（红警 270 s）内没被 grant 就算 no-show（从分配里消失，宝可梦这局判我们负），测试赛没有期限。`rehearsal_official.py` 加了三项检查：日志里确实是 `--tournament --match`、两种对局都打了、每个正式赛座位都在窗口内连上；`rehearsal.py` 也换成官方控制面。测试：`tests/test_deploy.py` 按新运行时改写（上游 SDK 自己挡住重新登录失败、key 被拒仍结束运行时、启动退避、按座位退避从 10 s 起翻倍、热备、run_agent.sh 的新拒绝和 `-- --match`、假控制面的 no-show）；pk 分支的「会话已删的座位按冷却重试」测试改成上游的按座位退避（结束的局每 15 s 一个新 worker，被弄丢的局 15 s、25 s 后再试，座位离开分配列表就停）。
- **文档**（42b4da3）：10-07 11:47 UTC 重新只读拉取线上 skill v0.5.0，和仓库里的 `docs/platform/` 逐字节相同（10 个主题 + `SKILL.md`，没有要删的停用页）；`docs/competition_brief.md` 最上面加「2026-10-07 更新」（event agent、两种托管、一把 key、Testing / Tournaments 页面、瑞士轮 + 淘汰赛 / 狼人杀决赛的赛制和计分、连接窗口、红警是正式赛项目、选秀 15 秒）；`docs/deploy.md` 改成主人的事件流程步骤；HANDOFF 写现状和下一步。
- **冻结为冠军**（1cbe2fe）：`champion/` = 42b4da3 的 `agent/`（`champion/VERSION` = 42b4da3）。之后只改了一个测试（ec49587），`agent/` 没动。
- **检查**（树 ec49587，`agent` 从工作区导入；输出在 `~/wt/_combine_out`）：
  - 全量 pytest：第一次 1780 过、1 个失败（上面那个 pk 测试：它用的假分配没有 `context` 字段，而上游的 supervisor 现在要读它；真的分配对象总有这个字段），改好后（ec49587）**1781 个全过**（573 s）。
  - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 1441 ms、最慢 1755 ms；`--no-calc` 同样 0 失败、0 修补，p99 1435 ms、最慢 1562 ms。`selfcheck.py`：ready（只有没配 key 的 1 个警告）。`python -m agent --help` 里有 `--tournament` 和 `--match`。
  - 新压缩包 `arena-agent-ec49587`（46 MB / 417 个文件，密钥扫描干净；`agent/`、`altruagent/`、`data/`、`deploy/`、`run_agent.sh`（755）、`selfcheck.py` 和工作区相同）：只有 `/usr/bin:/bin` 的干净环境里 selfcheck ready，包里重放 60 个决策 0 被拒，格式不对的假 key `--check-tournament` 退出 1。包留在 `~/wt/_combine_out/dist/`（已被下一节复审修复后 74defe4 的包取代：这个包的运行时还是 600 s 的按座位退避，不要再部署它）。
  - 红警端到端（从这个包起整棵进程树 `run_agent.sh` → `python -m agent --tournament --match` → 每个座位一个 worker，对本地模拟平台的官方控制面、假 key；2 场 45 s 的红警 + 1 场宝可梦 + 1 桌狼人杀，测试赛 / 正式赛各 2 个座位）：每场红警 1 个 worker、0 次 `match failed`、0 个命令批、0 个不合法动作（平台发了 206 / 188 个画面，我们回答了 205 / 187 次 `WAIT`）；宝可梦赢（14 个决策），狼人杀照常；运行时 0 次重启，4 个座位 4.7–8.7 s 内连上，0 次 no-show，0 个 Traceback，key 没进日志，停止后 0 残留。
  - `rehearsal_official.py`（dry-c1，从这个包跑）：**16 项全过**，4.5 分钟；宝可梦 7 场、狼人杀 3 桌，118 个决策，0 非法、0 计时器代打、0 竞争错误；kill worker 后 13.5 s 起新 worker、16.5 s 后下一步；kill 运行时 3.5 s / 7.6 s，0 次 seat_busy、0 次新 grant；认证接口 503 期间运行时没换、0 个 worker 被停（上游 SDK 自己的 `Could not renew your agent session` 1 行、恢复 1 行）；热备接管 30.8 s，那一回合没被代打（这一步等了 32.6 s）；两种对局各 5 个座位；正式赛座位连上用时中位 4.7 s、最慢 29.6 s（503 那段时间新开的座位，等 SDK 退避），窗口 240 s，0 次 no-show；非租约持有者 0 步，key 没进日志，停止后 0 残留。
  - 本地对局不变：对 main 的冠军打 20 局（seed 171，确定性设置），main 2a20b35 和本分支（agent 是 ec49587 的，冠军换成 main 的）逐局相同（20/20，比了 280 个决策），两边都是 11 胜，搜索用上 260/260，0 退回，0 计时器代打，最慢决策 1.94 / 1.91 s。选秀 15 秒时钟和「最后一手被替选」两处修改在本地模拟器里都不会触发（本地没有选秀时钟），所以没有例外。
  - 狼人杀官方 runner 路径（`local.werewolf_platform`，新措辞，种子 1003，40 局，听关）：本分支 0 报错、0 卡死、0 闲置代打、0 被拒，最慢决策 6.0 ms，我方赢 67/112，216 张票 0 弃票，233 条聊天（另有 117 次 `WRONG_PHASE_NOOP`，就是 runner 在讨论窗口里重复发 terminate 的无害竞态）；main 用它自己的模拟器同样 0 报错、赢 67/112。
- **没做 / 留给主人的**：真实平台上还没跑过（10-06 改版后）：主人按 HANDOFF「下一步」登录、选 Self Hosted、生成 key、`check-tournament`、建一局 Testing。`CLAUDE.md`「容易错的事实」里有几行已经过时（`python -m agent` 的参数、红警 SDK、一把 key 一台机器），这次没改（规矩：`CLAUDE.md` 只由主人改），建议的写法在交接报告里。长时间演练 `rehearsal.py`（几个小时、注入故障）换成官方控制面后还没重跑。红警打不打等主人决定。`tests/strategy/test_engine_search.py::test_engine_modes_answer_legal_moves[extra1]` 有 1.5 s 的时间预算，机器很忙时可能偶尔失败（v4.2.1 补丁那次见过一次），这次没出现。

### v4.2.3 复审修复（10-07，分支 `integrate/v423`，本地对局不变）

10-07 对上面「v4.2.3 合并」做了一次对抗式复审（结论：修完再发），找到 1 个中等问题（代码）和 4 个低问题（文档）。修复是 3 个提交：fc81fa3（文档）、8859053（运行时 + 测试 + 文档）、74defe4（冠军重新冻结，`champion/VERSION` = 8859053）。每一条修之前都先在合并分支上复现过。

- **按座位退避的上限从 600 s 改成 30 s**（中，`agent/__main__.py`）。名词：**按座位退避** = 同一个座位的 worker 连续失败时，下一个 worker 等得越来越久（上游 4cb083a，随 PR #5 合进来）。上游从冷却（我们设的 10 s）起每次翻倍，最多 600 s，只有打完一局、遇到 `seat_busy` 或座位从分配里消失才清零（worker 先打了一阵再失败也不清零）。复审用真实的 supervisor 加假时钟测到，修复前我在 dba7285 上用同一个脚本复现了同样的数字：
  - 正式赛座位开头 5 个 worker 都在拿座位时登录失败（平台回 503 `agent_session_unavailable` 或 429，worker 以退出码 2 结束，算失败），第 6 个 worker 要到 **360 s** 才起，超过 240 s 连接窗口，就是缺席判负（main 以前的固定 10 s 冷却是 100 s）；
  - 一局里 worker 反复崩溃时，间隔是 10、20、40、80、160、320 s，后两次超过狼人杀 120 s、宝可梦 300 s 的超时代打。

  现在 `_tournament_tuning` 在运行时期间把 SDK 的 `SEAT_FAILURE_BACKOFF_MAX_SECONDS` 设成 30 s（`ARENA_SEAT_BACKOFF_MAX_S` 可调，运行时结束后还原，`altruagent/` 不改）：worker 在 0、20、50、90、130、170、210 s 起，连续失败 6 次仍在窗口内；一局里的间隔变成 10、20、30、30… s；被游戏服务器弄丢的局（`SESSION_NOT_FOUND`，直到平台关掉它）也最多每 30 s 起一个 worker、登录一次（平台限每个地址每分钟 30 次登录）。新测试：`tests/test_deploy.py` 的退避测试按 4 种设置参数化（SDK 本身 600 s；我们的 30 s；旋钮改回 600 s；低于冷却 = 固定 10 s），`main()` 跑起来时上限是 30 s、结束后还原成 600 s；`tests/strategy/test_platform_1005.py` 用真实的正式赛分配形状测上面两组时间表。
- **重新登录时查分配的暂停没改，只改了文档**（低）：上游按 10 / 20 / 40 / 60 s 暂停（我们以前 30 s 封顶）。复审测到 90 s 的登录故障里新开的正式赛座位 121 s 接上（窗口 240 s），但故障持续约 190 s 以上时，故障一开始就出现的座位会错过窗口。官方 `official-agent.md` 对 503 写明的就是这个节奏（10、20、40 s，之后每 60 s），所以按官方来。`docs/deploy.md` 原来写「正式赛的连接窗口是 4 分钟，来得及」，现在写清只对短故障成立，以及要改的话怎么改（`_tournament_tuning` 里设 `AUTH_RETRY_MAX_SECONDS`），由主人决定；HANDOFF 同样补了一句。
- **文档**（低）：每个压缩包的 `BUNDLE_README.txt` 和 `agent/__main__.py` 的说明指向 deploy.md 里 10-07 已经改名的小节（「官方正式赛」「官方模式下看门脚本和 agent 额外做了什么」），已改成现在的名字，并加了一个测试检查这两处指向的小节确实存在；`docs/competition_brief.md` 的同一个指向也改了，正文里旧的 `--claim` 步骤标上「10-06 起停用」；`docs/入门指南.md`（README 的第一个链接）按 10-07 的流程重写名词表（Official Agent Key、分配、座位授权、瑞士轮 + 淘汰赛 / 狼人杀决赛、4 分钟连接窗口、输了 0 分）、第 1 步和第 7 步、「我们现在在哪」。
- **检查**（树 74defe4，`agent` 从工作区导入；输出在 `~/wt/_fix423_out`）：
  - 全量 pytest **1786 个全过**（564 s；比合并时的 1781 多 5 个新测试）。
  - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 1477 ms、最慢 1798 ms；`--no-calc` 同样 0 失败、0 修补，p99 1467 ms、最慢 1569 ms。`selfcheck.py`：ready（只有没配 key 的 1 个警告）。`python -m agent --help` 有 `--tournament` 和 `--match`、没有 `--claim`。
  - 新压缩包 `arena-agent-74defe4`（46 MB / 417 个文件，密钥扫描干净；`agent/`、`altruagent/`、`data/`、`deploy/`、`run_agent.sh`（755）、`selfcheck.py` 和工作区相同；`BUNDLE_README.txt` 指向「比赛日手册」）：只有 `/usr/bin:/bin` 的干净环境里 selfcheck ready，包里重放 60 个决策 0 被拒，格式不对的假 key `--check-tournament` 退出 1。**部署用这个包**（`~/wt/_fix423_out/dist/`；之后只改了文档），或者重新出包。
  - `rehearsal_official.py`（dry-f1，从这个包跑）：**16 项全过**，4.9 分钟；宝可梦 7 场、狼人杀 3 桌，118 个决策，0 非法、0 计时器代打、0 竞争错误；kill worker 后 13.5 s 起新 worker、16.5 s 后下一步（日志 `retrying that seat in 10s`）；kill 运行时 3.4 s / 6.4 s，0 次 seat_busy、0 次新 grant；认证接口 503 的 15 s 里运行时没换、0 个 worker 被停（`Could not renew` 1 行、恢复 1 行）；热备接管 32.6 s，那一回合没被代打（这一步等了 33.8 s）；两种对局各 5 个座位，正式赛座位连上用时中位 5.2 s、最慢 33.3 s（窗口 240 s），0 次 no-show；非租约持有者 0 步，key 没进日志，停止后 0 残留。
  - 红警端到端（同一个包的整棵进程树，本地模拟平台的官方控制面）：2 场红警各 1 个 worker，回答了 207 / 190 次 `WAIT`，0 个命令批、0 个不合法动作、0 次 `match failed`；宝可梦赢（15 个决策），狼人杀照常；运行时 0 次重启，4 个座位 4.7–7.1 s 内连上，0 次 no-show，0 个 Traceback，key 没进日志，0 残留。
  - 没有重跑对冠军的 20 局逐局核对和狼人杀 runner 的 40 局：这次 `agent/` 只改了 `__main__.py`（运行时的一个上限和说明文字），决策代码一行没动（`git diff dba7285 -- agent/` 只有这个文件）。

### v4.2.4（10-08，分支 `integrate/v424`）：合入上游 PR #7（运行时扛住各种小故障），不为变强（本地对局不变）

从 main 111d14b 拉分支，合上游 main 951f4f7（PR #7「Runtime: survive hiccups (no lost games or no-shows), Pokémon clock, keep running after a Rules update」，连同之前的 PR #6 文档）。修复 / 适配类版本：宝可梦的决策代码一行没动，狼人杀只改了「发言没送到服务器时怎么记数」。

名词（一行一个）：
- **局内重试**：一局里的某次调用暂时失败时，不结束这局的进程，停一下、重读状态、接着打。
- **退避**：失败后等一会儿再试，连续失败就等得越来越久（有上限）。
- **回显**：我们发出的狼人杀发言，会出现在下一次读到的状态的 `new_messages` 里（服务器对自己发的话也显示）。

- **上游带来的**（`altruagent/` 原样取，和 upstream/main 逐字节相同；`examples/`、`GAMES.md`、上游的测试也原样取）：
  - 出招 / 发言 / 读状态碰到暂时故障（断线、5xx、`RUNTIME_TEMPORARILY_UNAVAILABLE`、`TOO_MANY_WAITS`、服务器答不上来的读请求）在局内重试，最多 90 s：停 1–5 s，重读状态再决定，**从不盲目重发**（可能已经送达）。以前 worker 直接崩溃，冷却 10–30 s 再起。
  - MCP 调用 40 s 读超时：连接无声断掉 40 s 内就发现（以前要 300 s，宝可梦约扣 245 s 银行时间）。
  - Official Rules 更新后（平台回 403 `registration_incomplete`）**不再停运行时**：在打的局继续，最多约 1 小时；新局要等主人在 dashboard 重新同意；运行时每 30 s 再查，最多每 5 分钟提醒一次该做什么。
  - 拿座位时的暂时故障：worker 自己重试 60 s（3、6、12、15 s），再失败就以 `EXIT_NOT_CONNECTED` 退出，连接窗口开着（至少 5 分钟）时 supervisor 同一轮就起新 worker，不计入按座位退避。
  - key 被拒（有人按了 Rotate key、或不再是 Self Hosted）：说一次，不再开新局，在打的继续；supervisor 把自己的 agent session 交给 worker（每局少登录一次）。
  - 狼人杀认输只让认输者出局（我们从不认输）；日志每行一次写完（多个 worker 不再把行粘在一起），worker 最后一行写「我们赢了 / 输了」。
- **合并**（d8cc810）：4 个冲突。`.env.example`：我们那行 run_agent.sh 说明 + 上游的「泄露就按 Rotate key」；`README.md`、`agent/agent.py` 取我们的（我们的 Contestant 对红警回 `WAIT`，不会走上游占位 agent 的「Your agent can't play Red Alert」）；`docs/STARTER_README.md` 换成上游新 README。`agent/__main__.py`：上游的运行时 + 我们留下的加固（启动时平台暂时连不上按 5 / 10 / 20 / 30 s 重试的 `_authenticate`、看门脚本给的 execution id、热备、10 s 查询和冷却、按座位退避上限 30 s）。`_authenticate` 遇到 `registration_incomplete` 不再退出：和上游一样照常进入等待（不打印 `Connected …`），supervisor 第一轮就打印该怎么做，之后每 30 s 再查。
  - **复核 `SEAT_FAILURE_BACKOFF_MAX_SECONDS=30` 在新代码里的含义**：上游现在还拿这个常数当「worker 跑够这么久再失败，就重新计数」的门槛。所以在我们这里：跑了 30 s 以上才崩的 worker 每次 10 s 后重开（原来 10、20、30 s）；30 s 内就崩的照旧 10、20、30、30… s。正式赛连接窗口的保证不变：拿不到座位的暂时故障同一轮重来（上游），其他失败按 0、20、50、90、130、170、210 s 起 worker（我们的 30 s 上限），6 次失败后仍在 240 s 内。
  - 3 个按旧行为写的测试：`tests/test_realtime.py` 里用真实 `agent.agent` 模块的那个红警占位测试，改成检查我们回 `WAIT`、一个命令都不发、整局打完；`tests/test_deploy.py` 里 `registration_incomplete` 那组参数拿出来单独测（重新登录遇到它时运行时和在打的 worker 都保留、提示只说一次、30 s 再查、同意后恢复，热备也一样），另加「启动时遇到它也照常运行」；`tests/strategy/test_platform_1005.py` 的退避期望值按上面的新含义改（并加上 `EXIT_NOT_CONNECTED` 同轮重来）。
- **我们配合的改动：狼人杀发言没送达不计数**（edb5b11）。PR #7 之后，发言送出失败时 runner 不再结束这局，而是重读状态、再问我们一次。我们的 Contestant 在把发言交给 runner 时就记了数，所以一句没送到的话会占掉 5 条额度，「同一句一天不说两遍」还会挡住重发。现在下一次被问时先核对：状态里的 `new_messages` 包含这一窗口所有消息、也包括我们自己的；这句不在里面、而且我们自己的发言条数少于记的数，就判定没送到：不计数（5 条额度、说过的话、追加发言名额、保存的对局记忆都不算它），放回队首重新决定。两道保险：这局见过我们自己发言的回显之后才这样判断（万一平台不显示自己的发言，也不会把每句都重发）；计数从不低于服务器显示的条数（不会因此发第 6 条，第 6 条会被拒并结束进程）。日志 `our chat did not reach the server …`，局末 `[match-summary] … undelivered_chats=N`。
  - 新测试 `tests/strategy/test_resend_after_hiccup.py`（10 个，走真实的 `altruagent.runner.run_game`）：狼人杀投票和宝可梦一个真实对战回合在送出失败后被重新决定、带着重读状态的版本号再发一次；发言送出失败（断线、503、引擎忙、`TOO_MANY_WAITS` 四种）后重发一次、只计一次；没送到又不能再发（服务器已把我们列为结束发言）时保存的对局记忆里没有它；其实已经送到（只是回复丢了）的不会发两遍；还没见过回显时第一句失败照旧计数、不重发。`tests/strategy/test_werewolf_listen.py` 加一个「追加发言没送到、worker 重启后不计数」。原来 3 个聊天测试用的是「我们发完后状态里没有这句」的假状态，改成和服务器一样显示我们自己的发言。
- **文档**（dc907f2）：`docs/deploy.md` 写清新运行时的行为（规则更新不停运行时、局内重试、key 被拒、按座位退避的新含义、新的日志行和对应处理）；上游缺陷第 2 条（每个 worker 各自登录）标成上游已修。
- **冻结为冠军**（eab2edd）：`champion/` = dc907f2 的 `agent/`（`champion/VERSION` = dc907f2）。冻结后把用到冠军的 6 个测试文件重跑，191 个全过。
- **检查**（树 dc907f2，`agent` 从工作区导入；输出在 `~/wt/_v424_out`）：
  - 全量 pytest **1962 个全过**（755 s；比 v4.2.3 的 1786 多 176 个：上游新增的测试和我们的新测试）。
  - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 2202 ms、最慢 2784 ms；`--no-calc` 同样 0 失败、0 修补，p99 1544 ms、最慢 1774 ms。带计算器那次比 10-07 的 p99 1477 ms 慢：宝可梦的决策代码和 main 逐字节相同（`agent/pokemon/` 没动，`agent/agent.py` 只改了狼人杀发言，SDK 的改动不在决策路径上），所以只能是机器状态（这次 WSL 刚重启过）；都远在平台每步 50 s、我们自己 10 s 的预算内。想在同样条件下复测 main 和本分支的第二次重放，被同机别的会话的重活（`local.redteam_wolfpack`）干扰，停掉了，没有结果。`selfcheck.py`：ready（只有没配 key 的 1 个警告）。`python -m agent --help` 有 `--tournament` 和 `--match`、没有 `--claim`。
  - 新压缩包 `arena-agent-dc907f2`（46 MB / 418 个文件，密钥扫描干净；`agent/`、`altruagent/`、`data/`、`deploy/`、`run_agent.sh`（755）、`selfcheck.py` 和工作区相同）：只有 `/usr/bin:/bin` 的干净环境里 selfcheck ready，包里重放 60 个决策 0 被拒，格式不对的假 key `--check-tournament` 退出 1。**部署用这个包**（`~/wt/_v424_out/dist/`；之后只改了冠军副本、文档，以及从 main 合进来的 CI 配置和规则原文，包里的文件都没变）。
  - `rehearsal_official.py`（从这个包跑，含认证接口 503 故障）：**16 项全过**，5.0 分钟；宝可梦 7 场（全胜）、狼人杀 3 桌，120 个决策，0 非法、0 计时器代打、0 竞争错误；kill worker 后 15.9 s 起新 worker、16.9 s 后下一步（日志 `Retrying in 10s`）；kill 运行时 3.4 s / 6.4 s，0 次 seat_busy、0 次新 grant；认证接口 503 期间运行时没换、0 个 worker 被停（`Could not renew` 1 行、恢复 1 行）；热备接管 33.8 s，那一回合没被代打（这一步等了 32.4 s）；两种对局各 5 个座位，正式赛座位连上用时中位 5.6 s、最慢 33.8 s（窗口 240 s），0 次 no-show；非租约持有者 0 步，key 没进日志，停止后 0 残留；每局结束 worker 都打了一行「finished: your agent … won / lost」（10 行）。
  - 红警端到端（同一个包的整棵进程树，本地模拟平台的官方控制面）：2 场红警各 1 个 worker，回答了 205 / 185 次 `WAIT`（平台发了 206 / 186 个画面），0 个命令批、0 个不合法动作、0 次 `match failed`、没有崩溃循环；宝可梦赢（13 个决策），狼人杀照常；运行时 0 次重启，4 个座位 5.1–7.5 s 内连上，0 次 no-show，0 个 Traceback，key 没进日志，0 残留。
  - 本地对局不变：对 main 的冠军打 20 局（seed 171，确定性设置），main 111d14b 和本分支（冠军换成 main 的）**逐局相同**（20/20，比了 280 个决策），两边都是 11 胜，搜索用上 260/260，0 退回，0 计时器代打，最慢决策 2.50 / 2.67 s。
  - 狼人杀官方 runner 路径（`local.werewolf_platform`，新措辞，种子 1003，40 局，听关）：本分支 **0 报错**、0 卡死、0 闲置代打、0 被拒，最慢决策 7.3 ms，我方赢 67/112，216 张票 0 弃票，232 条聊天（另有 111 次无害的 `WRONG_PHASE_NOOP`），0 次「发言没送到」。main 用它自己的代码同样 0 报错、赢 70/112。这个模拟器 7 个座位是 7 个线程，发言顺序随线程调度变，所以每次跑都不完全一样：再各跑两次，main 64 / 65、本分支 67 / 67，都是 0 报错、0 次「没送到」。
- **没做 / 已知局限**：
  - 宝可梦「上一回合用过守住」这类记忆（`last_move`、`last_protect`）是在决定时记下的。同一回合因为送达失败被重新决定时，会把没送到的那一步也当成用过（例如更不愿意再守住）。worker 重启、竞态重读时本来就这样，PR #7 之后多了一种触发方式；影响只在那一回合。要改得在 Contestant 里按「同一个决策点」回滚这几项记忆，会动宝可梦的决策路径，这次没改。（**v4.2.5 已修**，见下一节。）
  - 真实平台上还没跑过（10-06 改版后一局都没有）：主人建一局 Testing（宝可梦 1 局、狼人杀 7 座全选 Mine）。
  - main 在这期间多了主人的 2 个提交（8b094af GitHub Actions 的 CI、17a57f4 规则原文），本分支最后把 main 合了进来（只多这两个新文件，没有冲突），所以 main 可以直接快进到本分支。CI 里的 flake8 检查本机没装，没跑；pytest 就是上面那次全量。

### v4.2.5（10-08，分支 `pk/tracker-1008`）：没送到的招式不再被当成「用过了」，平台倒计时（本地对局不变）

从 main 7a59a65（v4.2.4）拉分支。修正类版本（v4.x），不为变强：只在「同一步被问第二次」和「招式被服务器拒收」时才有区别，正常对局一步不变。

名词（一行一个）：
- **决策点**：对战里轮到我们选招的那一刻。由回合数、哪个槽必须换人、双方场上的宝可梦（种类、血量、是否倒下）、协议日志里已经发生的战斗事件数（出招、换人、倒下、伤害、新回合）确定。同一个决策点被重读时，状态版本号（`state_version`，平台给每次读到的状态的编号）可能不同，但还是同一步。
- **回合记忆**：策略为下一步记下的东西：谁上一回合就在场（击掌奇袭 Fake Out 只有上场第一回合有效）、我们上一步出了什么（连续两次守住 Protect，第二次只有 1/3 成功，所以策略几乎不连守）、顺风 / 戏法空间是哪一回合开的、单打的同类记忆（`singles_prev`）。
- **被拒的招式**：服务器明确没收下**这一次**送出的招式：`STALE_STATE` / `NOT_YOUR_TURN` / `WRONG_PHASE`。这一步第一次送出就被拒，意思是这一步已经过时，平台计时器已经替我们随机出了招；如果之前同一步送出过一次却没收到回答（5xx、超时），也可能是**那一次其实送到了**（`NOT_YOUR_TURN`：这回合已经交过；`STALE_STATE`：版本号因为那一步变了），见下面「评审修正」。`RUNTIME_TEMPORARILY_UNAVAILABLE`（引擎忙）不算：SDK 对回合制游戏把它当暂时故障、「可能已经送到」（评审修正里去掉了）。
- **`observation.clock`**：平台 10-08 线上文档新写明的字段 `{"decision_seconds_left": 41, "bank_seconds_left": 312}`，是「读状态那一刻」这一步还剩几秒、整场银行还剩几秒。

- **问题**（v4.2.4 合并报告的遗留项）：策略在**做决定的时候**就把这一步写进回合记忆。同一个决策点被问第二次时（PR #7 之后：送出失败 → runner 重读状态再问；worker 重启后新 worker 重问；竞态重读），第二次会读到第一次留下的记忆，好像那一步已经打出去了。v4.2 起已有一道保护（`_same_decision`），但只认「版本号完全相同」而且只管双打。实测（新测试在 main 7a59a65 上跑）：版本号相同的双打重问本来就对；**版本号 +1 时**，141b61de 第 1 回合两只的 Fake Out 第二次被丢掉（以为不是首回合了），dbff3ec4（多次守住的一局）也有决策第二次答得不一样；worker 重启后重问（版本 +1）第一个对战决策就不一样；单打（c8efbea9）第二次问时读到的记忆是第一次留下的。另一种情况版本号保护完全管不到：招式被拒（平台已替我们随机出招，下一个状态就是下一回合），我们没打出去的守住仍被记成「上一回合守住了」，下一回合就不肯守。
- **改动**：
  - `agent/pokemon/policy.py`：新函数 `decision_point()` 定义上面的「决策点」（没有回合数时才退回去用版本号）；`_same_decision` 用它判断「同一步」，单打也走（回合记忆里加了 `singles_prev`）；同一步再问时恢复第一次之前的记忆。新方法 `undelivered()`：招式被拒时只撤回「我们做了什么」（守住的回合恢复成那一步之前；这一步选了招的槽，上一步记成「不知道」，因为计时器替我们随机出了招），「看到了什么」（谁在场、顺风开了几回合）保留，因为那一回合确实过去了；如果重读到的还是同一步，`_same_decision` 照样整份恢复。新对战（首发阶段）清掉快照；计数 `redecided`（同一步被再问几次）进对局记忆。
  - `agent/agent.py`：实现 SDK 的可选回调 `Contestant.on_action_result`（runner 每次出招后都会调用，带服务器的回答或 `{"error": 错误码}`；以前我们没实现）。只认上面 3 个「这一次没收下」的错误码（最初还有引擎忙，评审修正去掉了）；5xx 之类可能已经送到，不撤回，交给重读后的决策点判断。撤回后马上重存对局记忆（重启的 worker 从那里接上）。这个回调永不抛异常（抛了 runner 会结束这局）。`[match-summary]` 行在发生过时多 `redecided=N`、`refused_moves=N`（没发生时这行和以前一样）。
  - `agent/timecontrol.py`：见下面的计时对照。
- **计时对照**（10-08 读线上文档 `https://api.altruagent-game.com/skill/pokemon` 第 8 节「Move timer」，和 `agent/timecontrol.py` 比）：

  | 项目 | 线上文档 | 我们原来 | 结论 |
  |---|---|---|---|
  | 每个对战决定 | Showdown 55 s；**平台 50 s 不答就随机替你出招** | 注释写 55 s；预算 10 s（+ 看门狗 2 s） | 预算远在 50 s 内；注释改成 50 s |
  | 首发（Team Preview） | Showdown 90 s（不扣银行）；**平台 85 s** 随机 | 注释写 90 s；预算 30 s（+2 s） | 同上，注释改成 85 s |
  | 银行（整场总时间） | 7 分钟 = 420 s | 420 s（从计时行读） | 一致 |
  | 选秀每一手 | 15 s，超时服务器随机替选 | 15 s，预算 10 s（+2 s） | 一致 |
  | `observation.clock` | 新字段，读状态时还剩几秒 | 没读 | **改了**（下一条） |
  | 版本号 | 计时器替你出招时**也 +1** | （仓库里的 `docs/platform/pokemon.md` 还是旧文：300 s、不 +1） | 文档副本过时，见「没做」 |

  为什么改 `clock`：我们原来只看 Showdown 的计时行 `Time left: T sec this turn`，这一行是**发请求那一刻**写的，重读同一个状态时还是那个数。PR #7 之后，送出失败会重读再决定：最坏情况一次送出卡满 40 s（MCP 读超时）才失败，重读时平台只剩约 10 s，而我们照旧给自己 10 s + 看门狗 2 s，会超过平台的 50 s，被随机代打。现在预算再多两个上限：(`decision_seconds_left` − 3) × 0.5 和 (`bank_seconds_left` − 30) / 20（和原来计时行的公式一样），首发阶段只用前一个。正常读到的状态还剩 41 s 时上限是 19 s，比 10 s 宽，不起作用；只有读晚了（剩不到 23 s）才收紧。字段缺失或格式不对时完全照旧。
- **测试** `tests/strategy/test_redecide.py`（29 个）：走真实的 `altruagent.runner.run_game` 和真实录像：每一步第一次送出都失败（HTTP 503）、重读的版本号不变或 +1，发出去的招和不受干扰的整局逐步相同、最后的记忆相同（141b61de 含首回合 Fake Out，dbff3ec4 含多次守住）；首回合 Fake Out 被问两次仍是 Fake Out；招式被拒（3 种错误码）后下一回合不认为我们守过、但认为那一回合发生过，保存的对局记忆一致；引擎忙被拒后重读到同一步，答案和第一次一样；5xx、已接受的回答、狼人杀、乱七八糟的输入都不撤回、不抛异常；worker 重启后用版本 +1 重问，每个对战决策的答案和记忆都和原 worker 相同；单打两份录像每一步问两次（第二次版本 +1）答案相同、`singles.decide` 两次读到的记忆相同；决策点本身（重读相同、计时 / 聊天行不算、同一回合两次强制换人不同、没回合数时看版本号、存进对局记忆再读回不变）；`clock` 的 10 种战斗情况和 4 种首发情况。**这 29 个在 main 7a59a65 上有 21 个失败**（新函数、新回调不存在，或答案 / 记忆不同）。注意（评审）：其中「每步第一次送出失败、版本号不变」那几个，在 main 上只是因为没有新计数 `redecided` 才失败，去掉这条断言就通过：PR #7 最常见的情况（没送到，版本号不变）main 本来就对。本版真正补上的是：版本号变了的同一步、单打、计时器已替我们出招后的拒收、`clock` 上限。
- **检查**（树 4da1c25 = 621ba74 加一处注释，`agent` 从工作区导入；输出在 `~/wt/_pk2_out`）：
  - 全量 pytest **1991 个全过**（687 s；比 v4.2.4 的 1962 多 29 个，都是 `test_redecide.py`）。
  - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，每步 p99 1748 ms、最慢 1995 ms；`--no-calc` 同样 0 失败、0 修补，p99 1868 ms、最慢 2169 ms。`selfcheck.py`：ready（只有没配 key 的 1 个警告）。
  - 本地对局不变：对 main 的冠军打 20 局（seed 171，确定性设置），main 7a59a65 和本分支（冠军换成 main 的）**逐局相同**（20/20，比了 280 个决策），两边都是 11 胜，搜索用上 260/260，0 退回，0 计时器代打，最慢决策 2.71 / 2.70 s。本地模拟器从不重问同一步、也不会拒收招式，所以本来就应该相同。
  - 狼人杀官方 runner 路径（`local.werewolf_platform`，种子 1003，20 局；新回调对狼人杀什么都不做）：本分支和 main 都是 0 报错、0 卡死、0 闲置代打、0 被拒，我方都赢 37/61。
- **冻结为冠军**（aed0709，初版；评审修正后已重新冻结，见下）：`champion/` = 4da1c25 的 `agent/`（`champion/VERSION` = 4da1c25）。冻结后把用到冠军的 6 个测试文件和本版的新测试重跑：**220 个全过**（191 s）。
- **评审修正**（10-08 下午，独立评审结论「修完再发」；提交 bd920b9，冠军 dad8804）：
  - **问题 1（中，比 main 退步）**：一种少见的竞态里，我们会把**已经打出去**的守住忘掉。步骤（评审用真实 runner 和录像 dbff3ec4 复现）：① 我们出守住，网关回 504（没有错误码，runner 不调回调），但服务器稍后其实收下了；② runner 重读，读到的还是旧状态（服务器还没处理），同一步再决定一次，再送一次；③ 第二次被拒 `NOT_YOUR_TURN`（这回合已经交过）或 `STALE_STATE`（版本号变了），因为第一次送到了；④ 原来的回调把它当「计时器替我们出招」撤回了守住，下一回合以为没守过、又去守（连守第二次 2/3 失败）。main 没有回调，这时反而记得对。评审测了 60 个真实的守住决策：忘掉守住时下一步有 47 个变了，大多变成再守一次。**修正**：策略记下「这一步上一次送出有没有被明确拒收」；同一步再问而上一次没有被拒收（说明那次送出没收到回答，可能已经送到），这一步就标成「不确定」；之后在这一步上的拒收不再撤回，按「打出去了」记（和 v4.2.5 之前一样）。第一次送出就被拒（计时器替我们出招）照旧撤回。
  - **问题 2（低）**：上一个策略调用超时还在跑时，下一步由「忙」路径直接回退作答，策略根本没被问，`_asked` 还是上一步的；这时回退的招被拒，会把上一步（已经打出去的）撤回。**修正**：只有这一步确实是策略自己答的（`path` 是 policy / repaired）才撤回；忙、超时、崩溃、回退路径都不动记忆（那时记忆本来就不对应发出去的招）。
  - **问题 3（低）**：`RUNTIME_TEMPORARILY_UNAVAILABLE`（引擎忙）从「拒收」里去掉。SDK 的 runner 对回合制游戏把它当暂时故障（注释原话：可能已经送到），没送到时重读到的是同一步，`_same_decision` 本来就整份恢复；送到了的话撤回反而会忘掉打出去的招。
  - **测试**：`test_redecide.py` 改成 35 个：新加「504 后重发被拒」3 种（504 + `NOT_YOUR_TURN`、504 + `STALE_STATE`、先 `WRONG_PHASE` 再 504 再 `NOT_YOUR_TURN`）都记得守住；每次都被明确拒收（`WRONG_PHASE` 再 `NOT_YOUR_TURN`）照旧撤回；忙路径的拒收不撤回上一步；回退 / 超时 / 崩溃 / 忙路径和引擎忙都不撤回；被拒后同一步再问（`WRONG_PHASE`、引擎忙两种）答案和第一次相同。**新测试在修正前的 2764a00 上 5 个失败**，修正后全过（连同 `test_resend_after_hiccup.py` 共 45 个）。
  - **重新检查**（树 dad8804，冠军 = bd920b9 的 `agent/`，`agent` 从工作区导入；输出在 `~/wt/_pkfix_out`）：
    - 全量 pytest **1997 个全过**（737 s；比初版多 6 个，都是新测试；用到冠军的测试也在里面）。
    - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，每步 p99 2081 ms、最慢 2990 ms（这段时间机器上还有别的会话在跑重任务，比初版的 1748 / 1995 ms 慢；和经典打法相同的 425 个，初版 427，搜索有墙钟截断，机器忙时会差一点）；`--no-calc` 同样 0 失败、0 修补，p99 1856 ms、最慢 2043 ms（和经典打法相同 314 个，与初版一样）。修正只在招式被拒时起作用，重放里没有拒收。`selfcheck.py`：ready（只有没配 key 的 1 个警告）。
    - 本地对局不变：换新种子 173，对 main 的冠军打 20 局（确定性设置），main 7a59a65 和本分支 dad8804（冠军换成 main 的）**逐局相同**（20/20，比了 271 个决策），两边都 9 胜，搜索用上 251/251，0 退回，0 计时器代打，最慢决策 2.94 / 2.96 s。
    - 狼人杀官方 runner 路径（`local.werewolf_platform`，种子 1003，20 局）：0 报错、0 卡死、0 闲置代打、0 被拒，我方赢 37/61（与初版相同），最慢决策 5.6 ms。
  - **重新冻结为冠军**（dad8804）：`champion/` = bd920b9 的 `agent/`（`champion/VERSION` = bd920b9）。
- **没做 / 已知局限**：
  - 引擎搜索（部署用的 `ARENA_BATTLE=search`）的随机种子里有版本号：版本号变了的同一步，搜索可能抽到不同的样本、给出不同（但都正常）的答案。这不是记忆偏差；要改种子就会改变所有本地对局，所以没动。
  - 选秀的预算用的是 `decision_timeout_seconds`（固定 15 s），没用 `decision_deadline_at`（绝对时间）：那要相信本机时钟和服务器一致，WSL 的时钟睡眠后会漂；选秀一步 p99 0.17 s，只在计算器卡住时才会用满 10 s。没改。
  - 我们的招式被看门狗替换（超时回退）或在发送前被修补时，回合记忆记的是策略原来选的招，不是实际发出的（很少见：门禁里 0 次超时）。没改。
  - 仓库里 `docs/platform/pokemon.md` 的「Move timer」和版本号两段还是 10-07 的旧文（300 s、计时器出招不 +1），线上已改；需要重新同步（`scripts/sync_docs.sh`），这次没动，免得和别的分支的文档同步冲突。
  - `observation.clock` 只在文档里见过，还没在真实 payload 里见过（10-06 改版后一局都没打）。
  - 「不确定」时按「打出去了」记：如果那次没回音的送出其实没送到、之后又被计时器替我们出了招，下一回合会少守一次（比忘掉打出去的守住代价小）。更彻底的做法是下一步从协议日志（`|move|p1a: X|Protect|`）核对我们上一回合实际出了什么，不再依赖送达记账；改动大，比赛前不做。
  - 重启后的 worker 第一次重问同一步时也按「不确定」处理（前一个 worker 的送出结果不知道），同上，偏向「打出去了」。

### 狼人杀改进（09-28，宝可梦不变）
- 合入 `candidates/r2-ww-seer-and-villager`，加上模拟得出的规则（村民不弃票、集中投票、不假跳；预言家查到狼或快被投出时跳；对跳时投两个跳的人之一；狼只在被点名时对跳、先刀已跳的预言家），聊天只读有限事实并识别注入，狼的私信默认关闭（`ARENA_WW_DM=on` 可开；观战数据流可能暴露私信）。
- 留出种子 7（没参与调参），每种条件 1000 局、37 种条件：狼 +3.9、村民 +15.9、预言家 +23.7、每座 +7.3 个百分点。**每桌只有我们一个座位时**：每座 +5.4（对 LLM 类 +11.8、对措辞多变的 LLM 替身 +15.2；对简单 bot +0.4~+1.4）。私信开关对胜率无影响。18.5 万局 0 崩溃 / 0 非法 / 0 超时，最慢决策 68 ms。
- **已知弱点**：同桌有 2–3 个我们的副本且其余是演示类 bot 时，预言家 −4.8、每座 −1.0（每桌只有一个我们时没有这个问题）。修正思路见本机 `~/exp6/werewolf/NOTES.md`。仍然 0 局真实平台狼人杀。

### 狼人杀 v4.x：身份交叉检查 + 读懂真实大模型的跳预言家（10-03，分支 `ww/reading`，宝可梦不变）
- **身份交叉检查**（防"悄悄打错"）：以前平台只要把身份行（`Your role: WOLF`）换个写法，我们就当自己是村民；没有 `Your fellow wolves:` 行就以为没有队友。现在先按文档写法读，读不到再认常见变体（`Your role is: Werewolf`、`You are the Seer`、`Your wolf ally: Player4` 等），再用平台的"行为证据"核对：夜里只有狼会拿到 `Kill` 动作，而且可杀名单里不会有队友（名单里少掉的那个活人就是队友）；只有预言家拿到 `Investigate`；死亡公示的狼一定是队友；payload 里如果有身份字段也算。文字和证据冲突时听证据，每局每类问题只打一条 ERROR 日志（`agent.werewolf` 的 "werewolf role check"）。村民夜里没有动作，但"没被问到"不当证据（进程重启或看门狗代答的夜晚看起来一样）。身份行认出来以后，队友行、查验行这类较弱的文字线索不再推翻它（只报警，10-05 审查后改）；变体写法的队友行必须在同一行写出座位号才算。
  - 设计好的不利场景（身份行写成 `WEREWOLF`，300 次）：狼投自己队友 **202/300 → 0/300**，公开点名队友 202/300 → 0/300；身份行和队友行都换成没见过的写法、但当晚行动过一次：202 → 0；身份行写 VILLAGER 却拿到 Kill：202 → 0。完全没有证据（一次夜里行动都没见过）时仍会投错，但会报警。
  - 文档写法下完全不变：本地 20 局里我方看到的每个状态，身份、队友、查验结果都和旧解析逐项相同且不报警；种子 1003 的 11 种桌型 × 300 局（70,106 个决策）逐个决策与 main 相同。
- **读真实大模型的发言**：回归集 `tests/strategy/data/ww_llm_chat.json` = 本地缓存里 262 条真实 Qwen3-30B 发言 + exp9 录下的 127 条大模型发言（这批没拿来改规则，留作检验），逐条人工标注（是否跳预言家、报了谁是狼、谁是好人）。新认的说法：查验后用代词下结论（"I investigated Player3 and confirmed they are a wolf"、"I checked Player3 and Player4 — they're wolves"、"I investigated Player3 as a villager"）、回指本句唯一的座位（"Player5 is lying — they're a wolf too"、"Player0 claims to be the Seer but is actually a wolf"）、更多修饰（"the real wolf"、"is indeed a villager"、"one of the two wolves"）、并列（"Player0 and Player3 are villagers"）、定语从句（"Player6, who is a villager"）、狼说漏嘴（"Player0, my wolf ally"）、"Seer claim:"、"turned out to be"。代词前面隔着别的座位、转述、假设、问句时一律不读；10-05 审查后又加上：前面有否定或怀疑（"that doesn't mean they're a wolf"）、"it"、复数结论只指一个座位、所指的那句在说这个座位死了，都不读（见下面"审查后修正"）；10-06 第二次审查后再加上：别的座位的立场或出处（"Player3 keeps insisting they're innocent"、"According to Player2, they're a wolf"、"……, says Player2"）、所指的那句在附和或感谢这个座位（"Player2 is right. They're a wolf."）、猜测（"I suspect they're a wolf"、"chances are"、"likely"），都不读（见下面"第二次审查后修正"）。
  - 审计那 20 句跳预言家报狼：**11/20 → 20/20**；回归集全部 39 句报狼的跳预言家都读全；跳预言家判断 85/85 正确、0 误判；304 句非跳预言家里**新增误读狼 0 次**；注入过滤不变。注意：回归集里恰好没有审查找到的那几类句子，所以"0 次误读"只说明回归集内没有，不能说明没有副作用（第一版就有，见下）。
  - 自己另写、语料里没有的说法（防过拟合）：第一轮一次就对 30/34、第二轮（第一轮修完后才写）14/17（main 13/34、9/17）。没对的 7 句里，3 句是旧规则本来就有的误读（问句 "Is Player3 a wolf?"、"wolf: Player1 or Player2"、"the wolf's main target"），这次没动旧规则；4 句是没覆盖的说法，已补上（"and so is Player5"、"turned out to be"、"isn't a wolf"、"one of the two wolves"），都写进了单元测试。
- **本地对局**（种子 1003、1004，以前没用于狼人杀；每条件每种子 1000 局，配对；`--bg llm,llmx,heuristic,first,mixed --ks 1,5,7`，共 88,000 局）：所有条件、所有身份的配对差都是 **+0.00**（逐局相同），每座 59.5% [59.0, 60.0]（n=22000），村民 65.0、预言家 71.2、狼 38.4；我方 1 座时：对 llm 替身 67.8%、llmx 61.3%、heuristic 56.0%、first 49.4%、mixed 50.5%（与 main 完全相同）。0 崩溃 / 0 非法 / 0 被拒聊天 / 0 超时，最慢决策 74 ms。原因：本地替身 bot 的说法都在旧规则里，所以本地既看不出读聊天的收益，也看不出读错的代价（10-05 审查找出的误读在这里一次都不会出现）。审查修正后重跑同样 88,000 局：输出和修正前逐行相同（只差计时），仍是逐局与 main 相同。
- **补充估计（临时脚本，不进仓库）**：把 `llmx` 替身跳预言家的说法换成回归集里真实 Qwen 的句式（"I investigated PlayerN and confirmed they are a wolf"、"PlayerN is the real wolf"、"PlayerN is lying — they're a wolf" 等），替身之间照样互相听得懂，只有我们读不读得懂有区别。种子 1003、1004，每种子每条件 1000 局：我方 1 座时每座 58.6% → **61.0%（+2.45，CI +1.38~+3.52）**，村民 46.7 → 50.0（+3.31，CI +1.71~+4.90），狼 92.8 → 94.9（+2.13，CI +0.08~+4.18），预言家不变；3 座 +1.25（CI +0.74~+1.76）、5 座 +0.90（CI +0.56~+1.24）；按"只换一边"的配对，村民 +3.66（CI +3.04~+4.28）、狼 +0.84（CI +0.11~+1.57）、预言家 −0.12（CI −0.29~+0.05），分片退步 0。这是替身上的估计，真实大模型桌上的收益还要真实对局确认。审查修正后重跑：所有数字完全不变（修正没有丢掉任何收益）。
- **审查后修正（10-05）**：独立审查发现第一版读聊天**有副作用**，上面"没有坏处"的结论不成立：
  - 四类误读：① 否定 / 怀疑的代词句被当成指控（"Player3 voted against me, but that doesn't mean they're a wolf"、"I doubt they're a wolf"、"no proof they are werewolves"、"a wolf or a scared villager"）；② "Player3 was killed last night. It was the wolves" 把昨晚的死者读成狼；③ 普通闲聊被当成跳预言家（"Seer claim: none yet"、"Player4 is a villager, I checked her votes"）；④ 身份行明明写着 VILLAGER，却被一行松散匹配的"队友行 / 查验行"（"Your teammates: everyone who is not a wolf"、"Wolves remaining: 2"、"Investigation results: none"）改成狼或预言家，结果去假跳预言家（只在平台改写法时才会发生）。①② 出自被信任的真预言家时，会让真预言家"报的狼太多"或"报了公示的村民"而被判成说谎，我方村民转头投真预言家；③ 会让真预言家看起来被对跳，我方预言家还会公开把说这句话的村民打成"假预言家"。
  - 审查的压力测试（种子 1003、1004，`llmx` 替身闲聊里加进①③这类话，24,000 局）：第一版比 main 每座 **−2.99**（CI −3.70~−2.28），村民 −4.10、预言家 −5.18，6 个分片退步，不能发。
  - 修正（`agent/werewolf/state.py`）：代词句和新加的直接句式前面有否定 / 怀疑（不分第一人称）、假设、转述时不读；"or" 不再算结论结束；"it" 不再当座位代词；复数结论（"they're wolves"）不指单个座位；所指的那句在说这个座位死了时不读；"Seer claim:" 后面必须紧跟对某个座位的结论或 "I am the seer"，问句不算；"PlayerN is X, I checked them" 要求代词在分句末尾，并排除问句、假设、转述；身份行认出来后弱线索只报警不改身份，只有狼才有队友；`legal_actions` 不是列表时不再抛异常。回归集结果完全不变（39/39、审计 20/20、85/85、0 误读狼、好人 85/85）；审查列出的例子全部改正，补了 50 多个单元测试。
  - **新替身 `llmq` 并入评测门槛**（`local/werewolf_bots.py`，`--bg` 默认包含它）：说话照真实 Qwen 的写法——跳预言家用回归集里的句式，闲聊里夹着上面①②③那种其实没说任何人是狼的怀疑口吻。它同时衡量"读懂"的收益和"读错"的代价。种子 1005、1006（新种子），我方 1/3/5 座，每种子每条件 1000 局：
    - 修正前（d945ce9）：每座 **−1.30**（CI −2.09~−0.50），只换一边：村民 −2.46、预言家 −5.60，6 个分片退步，SHIP False——这个门槛能抓到第一版的问题。
    - 修正后：每座 **+1.47**（CI +1.08~+1.87），只换一边：村民 **+3.07**（CI +2.46~+3.67）、狼 +0.67（CI −0.08~+1.42）、预言家 −0.12（CI −0.32~+0.09，不显著），**分片退步 0，SHIP True**。我方 1 座时每座 58.2% → 60.5%（+2.30，CI +1.32~+3.28），村民 48.1 → 50.9，狼 93.8 → 96.3；3 座 +1.32（CI +0.75~+1.88）；5 座 +0.80（CI +0.46~+1.14）。0 崩溃 / 0 非法 / 0 超时，最慢决策 65 ms。
  - 审查的三个压力测试（①③混合、只有①、只有③）修正后重跑：都和 main **逐局相同**（配对差全是 +0.00）。
  - 文档写法下的身份不变性：种子 1004 的 11 种桌型 × 200 局（47,261 个决策）逐个决策与 main 相同；设计好的不利身份场景结果不变（有证据时 0/300）；20,000 个畸形 payload 的模糊测试里 `night_evidence` 的异常消失，其余异常点和 main 相同。
  - 仍未修的小问题（低优先级）：反讽（"Oh sure, Player3 is the real wolf. Totally. Not."）、伪装成游戏通知的文字（"Game: Player5 was revealed as a wolf."，不加进注入过滤，因为注入会给发言者加嫌疑，误判人类玩家代价更大）、"Player3 says the seer is lying and is a wolf" 这种转述后省略主语的句子，仍会读成指控（来自非跳预言家时只加 0.25 嫌疑）。
- **第二次审查后修正（10-06）**：对 d98bb76 的独立审查又找到两类误读（真实语料里都是 0 次，但替身对局里一出现就很贵）：
  - ① 新的假跳预言家：村民看投票记录说 "Player5 is town, I checked them and their votes look fine"，被当成跳预言家。结果我方村民以为真预言家被对跳，转投真预言家；我方预言家公开把这个村民打成假预言家；我方狼把它当预言家杀。审查的压力测试（`llmx` 替身闲聊里加这句，24,000 局）：d98bb76 比 main 每座 **−4.17**（CI −4.79~−3.56），只换一边：村民 −7.62、预言家 −8.17，6 个分片退步。
  - ② 转述、附和、猜测被当成真预言家的结论："Player3 keeps insisting they're innocent" 把 Player3 记成真预言家报的好人，Player3 被公示为狼后真预言家就被判说谎；"According to Player4, they're a wolf" 把 Player4 记成被报的狼；"Player2 is right. They're a wolf." 把 Player2 记成狼；"Player4 is likely a wolf. Player1 is likely a wolf too." / "I suspect they're a wolf" 让真预言家"报了三只狼"而被判说谎。
  - 修正（`agent/werewolf/state.py`）："PlayerN is X, I checked them" 只有在 X 是预言家的结论词（wolf / villager / clean / innocent / clear，不含 good / town / safe / human）、而且代词就是句子结尾（后面最多跟 last night / yesterday / tonight）时才算跳预言家；结论前面是别的座位的立场（keeps saying / insisting、is adamant / convinced / sure、acts like、trying to convince）或出处（according to、per PlayerN、in PlayerN's view）时不读，结论后面跟着 ", says PlayerN" 或句子以 "PlayerN:" 开头时不读；代词所指的那一句在附和、感谢、引用这个座位（agree with、is right、good point、thanks、pointed out、vouched 等）时不读；"likely" 不再当修饰语，第一人称猜测（I suspect / guess / bet / fear / worry / feel like、chances / odds are）后面的结论不读。所有改动都只会让读法回到 main 的读法。
  - 结果：回归集 389 条逐条和 d98bb76 相同（审计 20/20、39/39、85/85、0 误读狼、好人 85/85）；自写说法 32/34、16/17（剩下 3 句都是旧规则本来就有的误读）；审查列的 46 句和自己补的例子全部改成和 main 一样的读法；审查的策略场景（村民投真预言家、预言家打村民、狼杀村民、真预言家被判说谎、跟错狼）全部恢复成 main 的选择。`test_werewolf_reading.py` 新增 51 个测试用例（139 → 190），其中 44 个在 d98bb76 上失败，其余是确认真预言家的结论照读的对照。读一条 600 字的刁钻消息最慢 2.2 ms（d98bb76 1.9 ms，main 0.45 ms）。
  - 本地对局（都在修正后的分支上重跑）：标准 88,000 局（种子 1003、1004）逐局与 main 相同；`llmq` 门槛（种子 1005、1006）结果和修正前完全一样（每座 +1.47，CI +1.08~+1.87，村民 +3.07，分片退步 0，SHIP True），说明没有丢掉读懂真实大模型的收益；真实 Qwen 句式的补充估计也不变（我方 1 座 +2.45）；审查的三个压力测试（只有①、只有转述、两者都有）和第一次审查的压力测试都 **逐局与 main 相同**（+0.00）。另写临时替身 `seer_noise`：真预言家每次发言后面多带一句②类的话（转述 / 出处 / 附和 / 猜测），d98bb76 比 main 每座 **−2.82**（CI −3.32~−2.33），村民 −5.41，5 个分片退步；修正后逐局与 main 相同。文档写法下的身份不变性：种子 1003 的 11 种桌型 × 300 局（70,106 个决策）逐个决策与 main 相同。官方 runner 本地路径 40 局：0 报错 / 0 卡死 / 0 闲置代打 / 0 被拒，最慢决策 31.6 ms，我方赢 67/112。
  - 代价：像 "Player5 is a villager, I checked them, trust me" 这种代词后面还接着话的真预言家写法，不再算跳预言家（main 本来也不算）；真实语料里没有这种写法。
  - **重放门槛的漏洞**（审查发现，已修）：`python scripts/replay_real.py` 在 worktree 里运行时，sys.path 里只有 `scripts/`，venv 的 editable 安装就把 `agent`、`local` 指到 main 的目录，所以之前报告的"重放 2349 个决策 0 失败"其实测的是 main 的代码（本分支只改狼人杀，录像里没有狼人杀状态，结论不受影响；但以后在 worktree 里改宝可梦就会漏检）。`replay_real.py`、`preview_bench.py`、`preview_ladder.py`、`calibrate_calc.py`、`compare_vgc.py` 现在都先把自己所在的仓库放到 sys.path 最前面（`selfcheck.py` 早就这样做），`tests/test_deploy.py` 加了检查，以后新脚本漏了也会报错。
- 官方 runner 本地路径（`local.werewolf_platform`，种子 1003，40 局，混入 llm / heuristic / 示例 bot）：我方 0 报错、0 卡死、0 闲置代打、0 被拒的动作或聊天，最慢决策 15.9 ms，stderr 0 行（没有任何 role check 报警）；我方座位赢 67/112。审查修正后跑 3 次：每次都是 0 报错 / 0 卡死 / 0 闲置代打 / 0 被拒，最慢决策 19.1 ms，没有报警；我方赢 65、65、67/112——main 同样跑两次是 67、66/112，这条路径是多线程的，胜场随线程时序有 1~2 局的波动，不是行为变化。
- 检查：第一版（d945ce9）全量 pytest 1343 个全过（新增 `tests/strategy/test_werewolf_reading.py` 82 个）；`test_werewolf_chat.py` 里 "I checked p5, he is clean" 的期望从"读不出好人"改成"读出 Player5 是好人"（这正是新认的代词说法）。分支末端（10-06，两次审查修正之后）：全量 pytest **1453** 个全过；`scripts/replay_real.py` 和 `--no-calc` 各重放 2349 个真实决策 0 失败、0 次 joint repair（这次确认跑的是本分支的代码）；`scripts/selfcheck.py` ready；打出的 bundle 解开后 selfcheck 也 ready。没合进 main，也没 freeze 冠军。

## 第三轮（09-27/28，本地实验，已合入 v3）


每个方向都在单独副本里实现 → 独立审查 → 修复 → 3×400 局擂台 + 基准；补丁和笔记在本机 `~/exp2/patches/`、`~/exp2/<方向>/NOTES.md`，整合进度在 `~/exp4`（尚未进仓库）。以下均为单独对 v1 的胜率：

| 方向 | 对 v1（1200 局） | 结论 |
|---|---|---|
| draft：模型化选秀（3 万局自对弈训练，4 步极小化极大 + 抢对手要的牌） | **82.4%**（CI 80.2–84.5） | 通过；质疑方验证：无信息泄漏，对 6 类对手均 +12~+37 个百分点，9 个真实卡池 89%（有效样本约 18） |
| preview-matrix：带 4 选 2 首发做成 90×90 矩阵博弈 | 58.5% | 通过 |
| speed-protect：速度线 / 击杀竞赛 / 守住估值 | 56.2% | 勉强通过 |
| calc-fixes：多段攻击、气势披带 / 结实、大将；另补烧伤状态大小写（平台发 `BRN`，计算器只认 `brn`）和缓存清理顺序 | 51.3%（配对 +2.2，p<0.001） | 合入 |
| weight-tuning：自动调参 | 66.4%（实际只改了选秀参数） | 被 draft 取代，不单独合入 |

## 第二轮候选（2026-09-27）

| 分支 | 方向 | 状态 |
|---|---|---|
| `candidates/r2-joint-lookahead` | 一回合联合前瞻（收益矩阵） | **已合入 main → v2**（加换人修正 + v2.1 加固 + sm-k6 参数） |
| `candidates/r2-ww-seer-and-villager` | 狼人杀：预言家避开当天会被投出的人、查到狼立刻跳；村民跟随 | 已完成提交，待复核 |
| `candidates/r2-speed-and-protect` | 速度线 / 击杀竞赛 / 守住 | 以第三轮版本合入 v3 |
| `candidates/r2-team-preview` | 6×6 精确对局矩阵选 4 带 2 | 以第三轮版本（90×90 矩阵博弈）合入 v3 |
| `candidates/r2-draft` | 基于卡牌目录的选秀对局表 | 以第三轮版本（模型化选秀）合入 v3 |
| `candidates/r2-switching-endgame` | 主动换人、U-turn 类、残局风险 | 半成品（WIP） |

真实对局记录：`logs/yoriko/`、`logs/apocalypse/`（10 局，已确认不含密钥）现在也在仓库里，`scripts/replay_real.py` 和 `scripts/calibrate_calc.py` 在任何机器上都能直接用。
