# 版本进化记录

每个版本上位前都必须：擂台赢当前冠军（`local.ladder`，95% 置信区间下限 > 50%）、重放全部真实平台决策 0 失败（`scripts/replay_real.py`）、全部测试通过。历史版本存档在 `versions/`，`python -m local.benchmark` 会让新版打遍所有历史版本和参照对手。

| 版本 | 日期 | 核心改动 | 对上一冠军 | 对官方 baseline | 对随机 |
|---|---|---|---|---|---|
| v0_heuristic | 09-26 | 属性克制 + 威力估算 + Fake Out/顺风等手写加分 | — | 97.7% | 97.3% |
| v1_exact_damage | 09-26 | Smogon 精确伤害计算（每回合一次批量）、按对手当前血量算击杀概率、两槽联合选招；修正对手血量百分比导致「多重鳞片」误判 | **66.8%**（400 局，CI 62–71%） | 99.5% | 99.0% |
| v2_joint_lookahead | 09-28 | 双打一回合联合前瞻：我方最多 5 种双槽组合 × 对手最多 6 种应对排成收益表，按「0.4×最坏 + 0.6×按威胁 softmax 加权的平均」选（sm-k6，8 对手面板扫出的最优）；修正前瞻偷换主动换人 / 对队友出招；计算器冷启动预热；对队友出招按真实效果计分；纳什混合代码保留、默认关闭 | **60.1%**（种子 7/11/23 共 1200 局，CI 57.3–62.8%；7→242/400，11→246/400） | 99.5% | 99.5% |
| v3_draft_preview | 09-28 | 模型化选秀（6,165 局 v3 自对弈重新拟合；卡牌指纹 + 双特征先验；节点预算）+ 90×90 首发矩阵博弈 + 速度线 / 守住 + 计算器修正（多段攻击、气势披带 / 结实、大将、烧伤大小写、缓存顺序）+ v2.1 前瞻（α0.4、等权、K4——v3 所有检查都用这组；在 v2 上与 sm-k6 打平） | **86.0% / 85.0%**（种子 7 / 11，各 400 局，CI 82.3–89.1 / 81.2–88.2） | 100.0% | 100.0% |
| v4_engine_search | 09-29 | 对战改用引擎搜索：在 Showdown 引擎的克隆上把我方约 8 种双槽组合和对手的应对都真打一回合，对「50% 均衡对手 + 50% 贪心对手」取最优；冠军的打法当回退和候选之一；选秀 / 首发不变 | **57.5% / 63.5%**（种子 7 / 11，各 400 局，CI 52.6–62.3 / 58.7–68.1；和同一批局的冠军镜像配对 +9.8 / +10.8 个百分点） | 100%（100 局） | 100%（100 局） |
| v4.4-draft | 10-11 | 只改选秀：按对手已选的牌判断它是不是「像真实参赛者那样选标准核心」，是的话按真实参赛者的偏好预测它的下一手；卡牌强度加上 v4 引擎搜索打法下的修正（见下面同名一节） | **58.0% / 53.0%**（种子 7 / 11，各 400 局，CI 53.1–62.7 / 48.1–57.8；和同一批局的 v4.3 镜像配对 +11.2 / −0.2，合计 +5.5 个百分点，p = 0.016） | 回归面板 300/300 | — |

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

### v4.2.4 合规（10-08，分支 `compliance/rules-1008`，基于 `integrate/v424` 7a59a65）：按正式规则补仓库说明、记录和比赛日手册（`agent/` 不变）

为什么：平台的 Official Rules（Version 2026-10-06，全文 `docs/official_rules_2026-10-06.md`）第 8 节要求公开仓库里写清怎么运行、哪些组件影响玩法、哪些组件没放进仓库；比赛版本 = 截止（10-13 23:59 PDT = 2026-10-14T06:59Z）时 `main` 上最新的提交，之后仓库冻结到成绩公布后至少 30 天。第 9 节要求保存部署的提交号和配置、请求和响应、时间戳、人工干预记录。

- **记录保留**（a09a017）：`run_agent.sh` 默认保留 40 个 50 MB 的日志（以前 10 × 20 MB；一场对局只写几 KB，整个比赛日不会被轮转删掉）。每次启动写一行 `deployed: commit <完整提交号> (git checkout)`（工作区有没提交的改动时注明；压缩包读它的 `COMMIT` 文件）和一行 `settings: ARENA_…`（所有设了的 `ARENA_*`，不含 key 和内部 id）；agent 重启时发现代码变了再写一行。`bundle.sh` 往包里写 `COMMIT`（完整提交号，工作区不干净时注明），`BUNDLE_README.txt` 也写上。新测试 4 个（`tests/test_deploy.py`：默认设置下整天的日志都在、git checkout 的提交号和设置、压缩包的 `COMMIT` 和运行中换了代码、压缩包写对提交号），原来的压缩包测试里也查 `COMMIT`。
- **README**（a166b42）：最上面加英文一节「Tournament entry」：是什么（宝可梦 VGC 双打选秀 + 狼人杀，红警整场回 WAIT）、比赛版本、怎么运行、影响玩法的组件（不调用任何大模型；@smogon/calc；pokemon-showdown 模拟器；`data/` 静态数据；官方 SDK）、比赛用的设置（`ARENA_*` 全部默认）、记录、没放进仓库的组件（key、VGC-Bench 权重、本地评测用的大模型 key、node_modules 和 node、日志、实验目录）。抬头改成截止时刻和官网链接；「同步官方更新」一节写明截止后不再合上游。
- **docs/platform**（74dca37）：刷新到 10-08 的实时技能文档（原文照抄；版本号还是 0.5.0，但 SKILL.md 和 9 个主题都变了，没有新增或下线的主题）。`scripts/sync_docs.sh` 注明「改内容不一定改版本号，要比文件」。
- **competition_brief**（e98ad7f）：最上面加「2026-10-08 更新」：规则要点（公开仓库、截止和冻结、记录、个人赛、Matcherino 资格含满 18 岁、人工干预要授权、运维不看观战、每局空记忆、奖金每个游戏 25%、50/30/20）、10-16 日程、Oracle 还没开放、Discord 优先、宝可梦 50 s / 85 s / 7 分钟银行、狼人杀认输、平台故障自动重打、淘汰赛等管理员、要问主办方的问题；改掉正文里过时的说法（300 秒、测试赛没有连接期限、红警闲置 10 分钟、种子高的晋级、拖太久判无结果、截止时刻的问题）。
- **deploy.md 比赛日手册**（3299593）：时间表改成绝对时间（PDT 和北京时间，从 10-13 截止到 10-16 17:30）；部署截止时 `main` 上的那个提交（不用 tag），并从它重新打包；新的一节「比赛进行中的规矩」：自动恢复可以，人动手的（重启、改 `.env` / `ARENA_*`、换 key）要主办方授权；管运维的人不看观战；规则一更新马上重新同意；换 key 后 `restart` 并通知主办方；每次手动操作记进 `logs/manual-actions.txt`。检查清单和出事处理表跟着改；切回 Self Hosted 只能在截止前；测试赛 20 分钟没到齐就取消、截止时关闭。
- **CLAUDE.md 没改**：「截止后不许往仓库推送」这条规则和 INVALID_ACTION 那条里过时的「300 s」写成了补丁，等主人确认（这个文件管以后所有会话的规矩，要主人自己决定）；同样的规则已写在 HANDOFF 最上面。
- **检查**（树 3299593，`agent` 从工作区导入；输出在 `~/wt/_compliance_out`）：
  - 全量 pytest **1966 个全过**（659 s；比 v4.2.4 的 1962 多 4 个新测试）。
  - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 1682 ms、最慢 2024 ms；`--no-calc` 同样 0 失败、0 修补，p99 1845 ms、最慢 2122 ms（同机另一个会话的狼人杀评测没走重活锁，一直占着两个核）。
  - `selfcheck.py`：ready（只有没配 key 的 1 个警告）。
- `agent/`、`altruagent/`、`champion/` 一行没动，所以没重新冻结冠军，也没跑对局对比。

### 10-08 文档和公开准备（分支 `docs/1008`，从 main 7a59a65 开，先快进到 `compliance/rules-1008` bc35a85，所以上一节的改动都在里面；`agent/` 不变）

为什么：规则第 13 节不许在公开仓库里放保密的第三方材料，第 7 节要求疑似漏洞私下报告；10-08 的公开前扫描建议方案 C（这个仓库保持私有，另建一个只放比赛代码的公开仓库，截止前同步过去）。

- **私人邮件**（454afac）：简报 10-03 节 B「主办方回信」的摘要、由它来的人名和计划（写信给谁、托管平台的猜测）都删了，只留一句「主办方通过邮件回答过我们的问题，内容不在仓库里转述」。`status_1003.md` 的同类计划也改成「先在 Discord 问主办方」。
- **疑似平台问题**：文档里描述它的地方（简报 5 处、`deploy.md` 3 处、本文件 1 处、`status_1003.md`、`intel.md`、一个测试的说明文字）都换成一句「由主人私下报告主办方，仓库里不写细节」，没有技术细节。狼人杀私信默认关闭的理由改成本来就成立的那条（两匹狼按同一份公开记录算出同一个目标，私信对胜率没影响）。**还剩**：`agent/reasoning.py` 和 `agent/werewolf/policy.py` 的 5 处代码注释（`champion/`、`versions/v4_engine_search/` 里各有一份），这个分支不改 `agent/`；git 历史里的旧文档和一条旧提交说明也还在（方案 C 不公开历史，方案 A 会公开）。
- **简报 10-08 节**：加了「推送后要在截止前更新登记的提交号」「不许看实时排行榜」「真诚比赛」和方案 C 的做法；`docs/platform/` 在 16:08 UTC 和 21:21 UTC 又各拉了一次，10 个主题和 SKILL.md 与上午刷新的逐字节相同。
- **README**（4864dac）：第 8 节那段写成红警由规则型 bot 打（`agent/redalert/`，`ARENA_RA=on`，不设 `ARENA_RA_*`）。bot 在 `ra/bot-v0` 上，**截止时 main 上必须真有它**，否则这句要改回「整场回 WAIT」（README 中文部分写了这条）。另写明：用单独的公开仓库时比赛版本是那个仓库的最新提交；`local/` 里只有 `local/js/package*.json` 在运行时有用；没放进仓库的和仓库里不用的组件都不给 agent 送信息、也不让人控制它。
- **deploy.md**：截止那一行和上线前检查清单加了方案 C：比赛机从公开仓库的全新 clone 部署，核对它的树哈希等于 `publish_sync.sh` 打印的。「一次性准备」里的 `git clone` 原来写死了私有开发仓库的地址（这个文件会导出到公开仓库，公开读者打不开；方案 C 时比赛机也不该从开发仓库部署），改成 `<参赛仓库的地址>`。
- **方案 C 工具**（a3ccbef、d3399df、fa25840、f55443b）：`scripts/publish_sync.sh --commit <提交> --out <目录>` 用 `git archive` 只从提交里导出比赛代码（`agent/`、`altruagent/`、`data/`、`deploy/`、运行 / 自检 / 打包脚本、`local/js/package*.json`、`docs/deploy.md` 和 `docs/restart_load.md`、打包文件、一局样例录像、能独立通过的测试；`PUBLIC_README.md` 变成 `README.md`，加 `NOTICE`），然后：扫描（密钥文件、像密钥的字符串、邮箱、私人通信里的人名、那个疑似问题的说法、本机路径；已知的占位符和测试里的假 key 不算）；检查 README 提到的路径都在；打印树哈希（公开仓库提交这些文件后 `git rev-parse HEAD^{tree}` 必须等于它），并核对每个导出的路径和提交里的一模一样；最后在一份拷贝里建干净的 venv、`npm ci`、跑 `selfcheck.py` 和导出的测试。结尾 `READY` 才能拿去提交；它从不推送、不碰 GitHub。`--replace` 可以直接同步进公开仓库的 clone（保留 `.git`）。`PUBLIC_README.md` 是英文的第 8 节说明（全部权利保留、注明上游 SDK 来源），`NOTICE` 列第三方许可（Showdown 数据和模拟器 MIT、@smogon/calc MIT、Node.js MIT、官方 starter 没有许可证文件）。新测试 4 个（`tests/test_publish_sync.py`）。
- **CLAUDE.md 没改**（这个文件管以后所有会话，只能主人自己改）：建议加的三处写成了补丁给主人：截止时刻和规则全文的位置；冻结规则（10-13 23:59 PDT 起到成绩公布后 30 天，参赛仓库什么都不推；方案 C 时冻结的是公开仓库，开发仓库也不能再出新的比赛版本；截止后的笔记放在参赛仓库以外；截止后合上游 SDK 要主办方书面授权）；把「300 s」改成宝可梦每步 50 s、预览 85 s、选秀每手 15 s、7 分钟银行时间，狼人杀 120 s。
- **检查**：
  - `publish_sync.sh` 演练（提交 f02bf9b 导出到 `/tmp/publish-dry`，3 分 15 秒）：103 个文件、4,140,598 字节（3.9 MB；`agent/` 31、`altruagent/` 17、`data/` 9、测试 21 个文件加 conftest 和 1 个样例数据、样例录像 2 个、其余是脚本和打包文件）；树哈希 47d55cdd049fa120f394de15cea14efefc6bd096，每个导出的路径和提交里的一模一样（同样的内容和可执行位）。干净 venv + `npm ci`：`selfcheck: ready`（计算器、引擎搜索、样例录像 60 个决策带 / 不带计算器各 0 拒绝，只有「没配 key」1 个警告），导出的测试 496 个全过（23.7 s）。结论 **NOT READY**，原因正是上面说的两件：`agent/` 里 5 处注释（`ISSUE`），以及 README 写了 `agent/redalert/` 但这个提交里还没有。其余扫描（密钥、邮箱、人名、本机路径）干净。第一次演练查出两处导出测试的问题（引用 `examples.smoke_agent`、引用没导出的 `test_tournament.py`），已改成自动排除这类测试。
  - 全量 pytest（树 f02bf9b，`agent` 从工作区导入，重活锁下）：**1970 个全过**（653 s；比上一节的 1966 多 4 个新测试）。`agent/`、`altruagent/`、`champion/` 一行没动，所以没重新冻结冠军，也没重跑 replay 和对局对比（上一节的数字照样成立）。

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

### v4.3（10-08，分支 `ra/bot-v0`，已合 main 的 v4.2.4）：红警规则型 bot（宝可梦、狼人杀不变）

以前我们被分到红警时整场回答 `WAIT`（等于认输）。这一版加了一个**不调用任何大模型**的规则型 bot，按 `docs/redalert_design.md` 实现（和设计稿不同的地方记在它的第 15 节）。主人已同意做；**报不报名红警比赛、正式部署时开不开，仍由主人决定**（`ARENA_RA=off` 一键回到以前的整场 `WAIT`）。分支从 111d14b（v4.2.3）开始，10-08 合进了 main 的 v4.2.4（上游 PR #7 运行时），所以下面的数字分「合并后」（最终）和「合并前」两组。

- **红警怎么比**（给第一次看的人）：两个 AI 实时 1 对 1（OpenRA 引擎，每秒 25 个 tick，谁也不等谁）。各有一辆基地车（MCV），展开成建造厂后造电厂、矿厂（采矿车把矿运回来变成钱）、兵营、战车工厂，再造兵去拆对方的家。拆光对方所有建筑和 MCV 就赢；20 分钟到了还没分出胜负，先比「我方打掉的敌方价值 − 我方损失的价值」（`kills_cost − deaths_cost`），再比现存单位和建筑的总价值（`assets_value`）。UCLA 比赛：10-16 下午，4 轮瑞士轮再前 8 名三局两胜，每局 20 分钟，分配后 4.5 分钟内必须连上。
- **打法一句话**：稳定的经济开局 + 生产不停 + 永远不缺电 + 侦察兵早看对方 + 看到速攻（步兵海）就先造步兵和防御塔 + 攻击部队攒到敌方强度的 1.3 倍才沿路径推进 + 见到防御塔和工厂集火 + 打不过就撤 + 最后 150 秒按平局规则保分或拼分。
- **代码**（全部在 `agent/redalert/`，`agent/agent.py` 只加了路由）：`world`（规则、地图网格、两基地之间的 BFS 路径和航点、矿区、每个画面的解析，残骸和 husk 全部滤掉）、`ledger`（「已发未见」账本：同一个 build/train 在状态里出现之前绝不重发；判决跟踪：`play_action` 的答复、`last_orders`、`recent_order_problems`；同一命令因同一原因被拒 3 次、原因还成立时拉黑 30 秒）、`orders`（本地校验：只用已知字段、当前是我方的 id、bounds 内的格子、可造列表里的东西；每批 ≤ 20 条、≤ 180 条引擎命令）、`economy`（开局 A / B、动态电力余量、矿厂放矿边和防御塔朝威胁方向的放置提示、放置失败处理、修理、集结点、闲置采矿车）、`production`（采矿车优先、建造厂没了造 MCV、主战坦克按 3tnk / 2tnk / 1tnk 偏好、步兵按需要和敌方兵种）、`army`（守家 / 主力 / 增援小队、侦察兵、威胁和速攻判断、进攻状态机 GATHER → ADVANCE → ENGAGE / SIEGE → HUNT、撤退、终局）、`safe`（安全模式）、`params`（`ARENA_RA_*` 环境变量可调的参数）。
- **路由和安全**：游戏类型是 `red_alert`（任何拼写），或者没有已知游戏类型但状态长得像红警，就交给 bot；别的未知实时游戏照旧整场 `WAIT`。新的 `Contestant.on_action_result` 把每次 `play_action` 的答复交给 bot（其他游戏什么都不做），永不抛异常。bot 任何异常只按种类记一次日志并回答 `WAIT`；30 秒内 5 次异常 → 60 秒安全模式。运营方的 **5 秒窗口模式**（runner 把它当回合制：`WAIT` 或整批无效都会杀掉进程）下每次都交出有效命令。只含「可能被世界变化作废」的命令的批次（以及窗口模式的每一批）附加一条「锚」：给一座没关电的建筑发 `power_toggle on=true`（平台答 `ok already_on`，不占引擎命令），这一批就不可能整批被拒。公开理由只写阶段标签（`opening`、`gathering`、`attacking`、`defending`、`endgame-hold` 等），`ARENA_REASONING=off` 时不写。录像是稀疏的：第一个画面、之后约每 10 游戏秒一个、有异常或新拒绝原因的画面（不再每秒 5 个）。`[match-summary]` 行带红警指标（第一个电厂 / 矿厂 / 战车工厂 / 维修厂的时间、最大军队、K / D / 资产、批次、按原因分的拒绝、INVALID_ACTION、决策耗时 p50 / p99、异常、安全模式、缺电秒数、进攻波数）。
- **合并 main 的 v4.2.4**（5950284）：4 个冲突。`agent/agent.py` 只有模块说明冲突（两条都留：PR #7 之后「狼人杀发言没送到不计数」和红警路由）；代码自动合并，两边互不相干：`_settle_pending` 只在狼人杀发言里跑，红警分支在 `choose_action` 最前面，`on_action_result` 只管红警。`docs/progress.md` 两节都留（v4.2.4 在前）；`champion/` 合并后重新冻结。上游带来的测试 `tests/test_realtime.py` 里有一个检查「我们的 Contestant 在红警整场 `WAIT`」，改成在 `ARENA_RA=off` 下跑（开着时的同一条路径由 `tests/redalert/test_routing.py` 测）。
  - **配合 PR #7 改了一处**（3c33a9a）：PR #7 之后 runner 遇到暂时故障（断线、5xx、429、工具故障）不结束这局，而是停一下、重读状态、再问我们，**从不重发**，因为那一批可能已经送达；带错误码的故障会通过 `on_action_result` 以 `{"error": 码}` 告诉我们。以前账本把任何整批错误都当成「这一批什么都没执行」，马上允许重发；现在只有明确什么都没执行的码（`INVALID_ACTION`、`STALE_STATE`、`NOT_YOUR_TURN`、`WRONG_PHASE`、`GAME_ALREADY_COMPLETE`、`RUNTIME_TEMPORARILY_UNAVAILABLE`、`PLAYER_ELIMINATED`）才这样；别的码保留这一批的「已发未见」记录（状态里出现就清掉，没出现就按原来的时限过期后再发），不会把同一个建造 / 训练排两次。新测试 1 个。
  - 模拟器新开关（bc8f054 之前）：`--fault-rate`（一部分调用像网络不稳一样失败：读状态 502；出招在送达前 502、送达后 502、送达后 429 `rate_limited`）测上面这条路径；`--a-side allies|soviet` 固定我方阵营。新测试 1 个（5% 故障下打 4 分钟 rusher：0 报错、0 整批被拒）。
  - 测试稳定性（911c671）：`tests/strategy/test_ra_sim.py` 里在模拟平台上打 rusher 的测试，等平台收尾的时间从 10 s 放宽到 40 s（全量 pytest 和别的会话的重活一起跑时超过过一次 10 s；单独连跑 8 次都是 7–17 s 过）。
- **本地模拟器测量（合并后，最终代码 bc8f054，调参时没用过的新种子 61–69）**（`local/ra_platform.py`，每个座位都走官方 runner `run_game` 的真实实时路径；锁步时钟，可复现；对手：idle 什么都不做、turtle 只守家狂造塔、rusher 2 分钟步兵海、llm 每 5–10 秒才动一次还常犯错；3 张地图轮换、座位轮换；括号里是 95% 置信区间）：

  | 场景 | 局数 | 结果 | 说明 |
  |---|---|---|---|
  | 主联赛：种子 61（我方固定盟军）、62（固定苏军）、63（阵营随机），对 idle / rusher / turtle / llm 各 36 局，3 张地图各 48 局 | 144 | **141 胜 3 负**，97.9%（94.1–99.3%）；140 胜是拆光，1 胜按平局规则 | idle、rusher、llm 都是 36/36（90.4–100%）；turtle 33/36（91.7%，78.2–97.1%），3 负都在 shadowfiend-ii、20 分钟到时按击杀差（K−D）判负；盟军 82/83（98.8%），苏军 59/61（96.7%） |
  | 自对弈（种子 64） | 24 | A 方 14 胜 10 负（58%，39–76%） | 两边是同一个 bot，差别是座位 / 种子噪声；两边都 0 报错、0 整批被拒 |
  | 网络不稳（`--fault-rate 0.05`，种子 65） | 24 | 24 胜（86–100%） | 我方共注入 3167 次故障：读状态 502 2946 次；出招送达前 502 70 次、送达后 502 78 次、送达后 429 73 次。runner（PR #7）全部扛住：0 报错、0 整批被拒 |
  | 1% 命令丢失（`--lost-rate 0.01`，种子 66） | 24 | 24 胜（86–100%） | 68 条 `lost` 的命令重发 |
  | 5 秒窗口模式（种子 67） | 12 | 11 胜 1 负 | 我方 0 报错、0 整批被拒（每个窗口 1 批）；对手里的 llm 脚本有 2 局在窗口模式下整批无效被结束（不是我们）；输的一局在 trapped 上被 rusher 拆了电厂，之后一直缺电 |
  | 开局资金 10000（种子 68） | 24 | 20 胜 4 负（83%，64–93%） | turtle 3/6（钱多时防御塔更多，3 负都是 20 分钟按 K−D）、rusher 5/6（trapped 上被步兵海打垮）、idle / llm 12/12；第一个战车工厂 104 s |
  | 实时时钟（`--clock realtime --speed 8`，思考时间也耗游戏时间，种子 69） | 4 | 4 胜 | 每秒 5.4 次决策、0.40 批，p99 1.0 ms |

  主联赛 144 局汇总：发出 37872 条命令，被拒 14 条（**0.037%**），**0 个整批被拒（INVALID_ACTION）**，0 次 runner 报错 / 卡死；每秒 5.36 次决策（每 0.2 s 一个画面答一次）、约 0.36 批（大多数画面没有新命令，回答 `WAIT`）；决策耗时 p99 中位 1.7 ms、最高 3.4 ms，最慢一帧 38.5 ms（每局第一次决策要做一次地图 BFS）；第一个矿厂中位 55 s，第一个战车工厂中位 179 s（先防后攻时最晚 326 s）；拆光中位 6.2 分钟、最长 13.3 分钟；做好的建筑最多 0.4 s 内放下，结束时没有没放下的建筑；缺电最多 63 s（电厂被打掉以后）。连同压力场景共 232 局（不算自对弈）：62422 条命令，被拒 0.030%，0 个整批被拒，我方 0 次 runner 报错；窗口模式下一次决策要处理 5 秒的变化，p99 31 ms。
  **合并没有改变打法**：种子 63 的 48 局，合并前的代码（3bec597）和合并后逐局相同（胜负和对局时长 48/48 一样；同一份代码重跑也 48/48 一样）。所以这次 3 局输给 turtle 是新种子上的真实水平，不是合并带来的。
  合并前（94476c7，种子 41–48）：主联赛 144/144，自对弈 A 方 9–15，1% 丢失 24/24，资金 10000 24/24，窗口模式 11/12，实时时钟 4/4（144 局 36514 条命令、被拒 0.03%、0 个整批被拒）。开发中跑的压力场景（种子 21–29）：命令延迟 0.4 s / 1 s + 2% 丢失 24/24；对手单位默认 `attack_anything` 11/12；「放 8 座建筑后再也放不下」11/12（整批被拒从 74 次修到 0，被拒率 17% → 4.7%，都是没地方放的 `invalid`）。
- **用模拟器修掉的问题**（详见设计稿第 15 节）：建造厂被毁后新 MCV 一直在没地方的位置展开（现在先找空地）；对步兵海（rusher）一开始只赢 67–79%（侦察兵太晚、钱闲置、守家两组人被各个击破、建筑视野看不见射程外的步兵，逐个修掉后 144/144）；对龟缩的对手一波一波送（敌军记忆太短，现在用「见过的最大一支敌军」慢慢衰减 + 防御塔）；终局全力进攻和撤退每帧来回切换；`cancel` 刚发出、还没生效时又发 `place` 导致整批被拒。
- **检查（合并 main v4.2.4 之后，最终）**（树 911c671，`agent/` 和冠军 bc8f054 逐字节相同；`agent` 从工作区导入；输出在 `~/wt/_raf_out`）：
  - 全量 pytest **2046 个全过**（751 s；比 main v4.2.4 的 1962 多 84 个，都是本分支的红警测试）。第一次全量（49599a0）2045 过、1 个失败：模拟平台上 `ARENA_RA=off` 打 rusher 的那个测试只等平台收尾 10 s，机器忙时平台还没写完结束统计（`opponent_military` 还没有），911c671 放宽到 40 s 并在超时时直接说明原因，之后全量全过。
  - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 1885 ms、最慢 2371 ms；`--no-calc` 同样 0 失败、0 修补，p99 1904 ms、最慢 2298 ms。`selfcheck.py`：ready（只有没配 key 的 1 个警告）。`python -m agent --help` 有 `--tournament` 和 `--match`、没有 `--claim`。
  - 新压缩包 `arena-agent-911c671`（46 MB / 427 个文件，密钥扫描干净；`agent/`、`altruagent/`、`data/`、`deploy/`、`run_agent.sh`（755）、`selfcheck.py` 和工作区相同；包里有 v4.2.4 的 `_settle_pending`、9 个 `agent/redalert/` 模块和 `data/red_alert_config.json`）：只有 `/usr/bin:/bin` 的干净环境里 selfcheck ready，包里重放 60 个决策 0 被拒，格式不对的假 key `--check-tournament` 退出 1。
  - `rehearsal_official.py`（从这个包跑，含认证接口 503 故障）：**16 项全过**，4.0 分钟；宝可梦 5 场（全胜）、狼人杀 3 桌，79 个决策，0 非法、0 计时器代打、0 竞争错误；kill worker 后 12.5 s 起新 worker、15.5 s 后下一步；kill 运行时 3.4 s / 8.4 s，0 次 seat_busy、0 次新 grant；认证接口 503 期间运行时没换、0 个 worker 被停；热备接管 36.9 s，那一回合没被代打；正式赛 4 个座位连上用时中位 24.1 s、最慢 40.0 s（窗口 240 s），Testing 4 个座位最慢 29.4 s，0 次 no-show；非租约持有者 0 步，key 没进日志，停止后 0 残留。
  - **红警端到端，这次是 bot 在打**（同一个包的整棵进程树：`run_agent.sh` → `python -m agent --tournament --match` → 每个座位一个 worker，本地模拟平台的官方控制面 + 红警模拟器，对手 rusher，对局 300 游戏秒）：2 场红警 Testing 各打满 300 s，**112 / 86 个命令批（150 / 114 条命令）、0 个不合法动作、0 条被拒、0 个异常、0 次安全模式**，决策耗时 p99 0.46 / 0.79 ms，到时按平局规则都赢（K / D 6300 / 6100、7200 / 100）；同时宝可梦赢、狼人杀照常；4 个座位 4 次 worker 启动（没有崩溃循环）、运行时 0 次重启、0 次 no-show、0 个 Traceback、key 没进日志、0 残留。日志里每场一行 `red_alert: the rule-based bot plays this match`，没有以前的「整场 WAIT」那一行。
  - 本地对局不变：对 main 的冠军打 20 局（seed 171，确定性设置），main 7a59a65 和本分支（冠军换成 main 的）**逐局相同**（20/20，比了 280 个决策），两边都是 11 胜，搜索用上 260/260，0 退回，0 计时器代打，最慢决策 2.05 / 2.08 s。
  - 狼人杀官方 runner 路径（`local.werewolf_platform`，新措辞，种子 1003，40 局，听关）：本分支 **0 报错**、0 卡死、0 闲置代打、0 被拒，最慢决策 5.2 ms，我方赢 67/112，216 张票 0 弃票、232 条聊天（120 次无害的 `WRONG_PHASE_NOOP`）；main 用它自己的代码同样 0 报错、赢 69/112（这个模拟器的发言顺序随线程调度变，每次不完全一样，见 v4.2.4 节）。
- **检查（合并 main v4.2.4 之前）**（树 94476c7，基于 111d14b；`agent` 从工作区导入；输出在 `~/wt/_ra_out`）：
  - 全量 pytest **1868 个全过**（655 s；新增 `tests/redalert/` 35 个；`tests/strategy/test_realtime_safety.py` 原来「红警什么都不发」的断言改在 `ARENA_RA=off` 下跑；`tests/strategy/test_ra_sim.py` 里用我们 agent 的几个测试两种开关都跑：模拟平台 HTTP / MCP 上打 rusher、窗口模式不死、1 分钟对局）。
  - `replay_real.py`：2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 1838 ms、最慢 2075 ms；`--no-calc` 同样 0 失败、0 修补，p99 1815 ms、最慢 1976 ms。`selfcheck.py`：ready（只有没配 key 的 1 个警告）。
  - 本地对局不变：对 111d14b 的冠军打 20 局（seed 171，确定性设置），111d14b 和本分支（agent 是 94476c7 的，冠军换成 111d14b 的）逐局相同（20/20，比了 280 个决策），两边都是 11 胜，搜索用上 260/260，0 退回。
  - 狼人杀官方 runner 路径（`local.werewolf_platform`，种子 1003，40 局）：本分支 0 报错、0 卡死、0 闲置代打、0 被拒，最慢决策 4.7 ms，我方赢 67/112，219 张票 0 弃票、231 条聊天（121 次 `WRONG_PHASE_NOOP` 是讨论窗口里的无害竞态）；111d14b 同样 0 报错、赢 67/112。
- **只能在真实平台上确认的**（模拟器做不到）：真实的寻路（模拟器是直线移动、没有地形和碰撞）、战斗平衡（模拟器没有溅射伤害和坦克碾压步兵，所以步兵海在模拟器里比真实游戏强）、真实的采矿收入、`place` 的 `at` 提示会不会被采纳、真实的网络延迟和节奏、事件里到底带哪些字段（`under_attack` 有没有坐标）、`units[].act` 的取值和默认 `stance`、对手的真实行为（LLM 选手、别的队的脚本）、开局资金是 5000 还是 10000、比赛是不是就用这 3 张地图、平台真的会不会切到 5 秒窗口模式、暂时故障时平台真实回的错误码（模拟器里是 502 / 429）。建议主人合进 main 之后在 Testing 页面开红警对局：两个座位都选「Mine (self-hosted)」自对弈，三张地图各打几局；有别的参赛者时再打几局对外。看日志里的 `[match-summary] … game=red_alert` 行（`invalid_action=0`、`rejected` 很少、`exceptions=0`）。录像（稀疏）会进 `logs/`，拿到后用 `scripts/replay_real.py` 回放、对照上面这些问题调整。
- **合进 main 就等于上线**：主人的正式 agent 从 main 的 checkout 跑（`scripts/run_agent.sh`），每个座位的 worker 是新起的 Python 进程（spawn），开局时从磁盘导入 `agent/`。所以本分支合进 main 的 checkout 以后，**下一局红警就由 bot 来打，不用重启**（运行时本身的代码 `agent/__main__.py`、`altruagent/` 和 main 逐字节相同，旧运行时配新 worker 没有问题；宝可梦 / 狼人杀的决策代码也相同）。想先合进去、暂时不让 bot 打：先在 `.env` 里加 `ARENA_RA=off` 并 `bash scripts/run_agent.sh restart`（`.env` 重启才生效），再合。用压缩包部署的话，包在 `~/wt/_raf_out/dist/arena-agent-911c671.tar.gz`（之后只改了这份文档）。
- **没做 / 已知局限**：
  - **对龟缩的对手（turtle）是弱点**：打不下来时一波一波进攻，损失比击杀多，20 分钟按 K−D 判负（开局 5000：33/36；10000：3/6；有一局我方资产还多，但 K−D 落后，平局规则先比 K−D）。改进方向（没做：属于「变强」，要按 `docs/v5_standard.md` 测）：进攻的交换比为负、对方防御塔又多时停止进攻、守住 K−D；终局落后时再拼。模拟器没有溅射伤害和坦克碾压，真实游戏里打塔、打步兵的结果会不一样，最好先看真实对局再调。
  - 5 秒窗口模式（运营方的备用开关）下每 5 秒才能下一次命令，被步兵海拆了电厂后恢复不过来（11/12）。
  - worker 中途重启时不接上 bot 的记忆（已发未见账本、侦察结果、进攻状态），从当前画面重新开始；不会出错，只是那一会儿少一点信息。
- **冻结为冠军**（49599a0）：`champion/` = bc8f054 的 `agent/`（之后只改了文档和一个测试）。宝可梦、狼人杀的代码和 main v4.2.4 逐字节相同（`git diff 7a59a65 -- agent/` 只有 `agent/agent.py` 的红警路由和新的 `agent/redalert/`），宝可梦本地对局逐局相同（见上），以前的擂台 / panel 数字可以接着比。

### v4.3.1（10-08，分支 `ra/bot-v0`）：红警 bot 复审修复（宝可梦、狼人杀不变）

10-08 下午对 v4.3 做了一轮对抗性复审（另一个会话，写了更强的脚本对手和「打掉我方一部分」的场景），结论「修完再合」。这一版把确认的问题都修了，每条先在修之前的代码（9820679，加上新对手）上复现，再修、再测。改动只在 `agent/redalert/`（加 `local/` 的新对手和测试），`agent/` 其余部分、`altruagent/` 和 main 逐字节相同，所以宝可梦 / 狼人杀的决策不可能变。

- **采矿车全灭后经济永久死锁**（中）：复审时把我方 4 辆采矿车全打掉，旧版从此 0 收入，在造的坦克和战车工厂把剩下的钱吃光（生产是边造边扣钱），一直到结束。现在（`economy.harv_emergency` + `production._harvester_first`）：有矿厂、0 辆采矿车持续 5 s（剩余 > 90 s）就进「采矿车优先」：先排一辆采矿车（没有战车工厂就造一座矿厂，矿厂自带一辆），把排在它前面的单位、其它队列在造的东西取消（取消会全额退回已付的钱），不开新建筑、不造步兵（家被打时除外）、停修理；钱还不够、采矿车停在半路时才卖一座用不着的建筑（剩余 > 300 s，一次一座、最多 3 座；先卖没存矿的 silo、雷达、多余的兵营、家里安静时的防御塔、多余的战车工厂 / 矿厂 / 电厂，从不卖建造厂、最后一座矿厂 / 电厂 / 兵营 / 战车工厂、维修厂）。车辆队列里已有单位时，采矿车少于矿厂数也照样排上。
- **搜索（HUNT）找不到不在出生点的基地**（中）：复审的 hider 对手把 MCV 开到离两个出生点都远的角落，旧版 15 分钟在固定的十几个点之间转圈，有一局只丢了一个 100 块的步兵就按平局输了（资产 56950 对 9500）。现在：先追最近 90 s 在我方基地 20 格外见过的敌方单位（采矿车优先，它会开回矿厂）；然后去「还没有我方单位到过 4 格内」的点（2:00 起算，建筑算 7 格），由近到远：先是原来的搜索点，再是覆盖全图的网格（`world.Static.coverage`：每 10 格一个点，只取从我方出生点走得到的格子，边和角都有）；一组朝一个点走 45 s 还到不了就算看过；全看过还没找到就从头再来。≥ 8 个单位分 2 组、≥ 18 个分 3 组。
- **建造厂在维修厂之前被毁后整局闲着**（低）：没有建造厂、也造不出 MCV 时，旧版步兵到上限后钱堆在存储上限，军队不动，最后按平局输。现在这种「回不了家」的状态下步兵上限 60、一次排 5 个，主力 ≥ 15 个单位、价值 ≥ 1500、过了强度比较就进攻（平局的第二项 `assets_value` 只算单位和建筑，不算现金）。
- **电厂被毁时建造队列在造别的**（低）：旧版要等那一项在三分之一速度下造完，缺电 100–220 s。现在缺电而建造队列里的东西在低电下还要超过 20 s 才完，就取消它先造电厂（并给电厂留最多 300）；造不了电厂（没得造、被挡、基地满了）时不取消。
- **修理每 2 秒开关一次**（低）：开始看现金 + 矿石，停止只看现金。现在都看现金 + 矿石。
- **一帧超时就跳过军队步骤**（低，复审没复现，按建议改）：机器很忙时可能每帧都超时。现在最多连续跳过 2 帧。
- **测试不稳定**（低）：模拟平台上 `ARENA_RA=off` 打 rusher 的测试，等的是 `matches_done`，而比赛线程要在世界线程停下（最多 5 s）以后才写 `opponent_military`；911c671 只把等待时间放宽了。现在等这一行真的有 `opponent_military`。
- **窗口模式的两个边角**（低，只改文档）：`ARENA_RA=off` 只在实时模式下等于「整场 WAIT、不出事」；运营方切到 5 秒窗口模式时它发不合法的 `{"type":"pass"}`、worker 反复退出（和 main 的 v4.2.4 一样）。`docs/deploy.md` 写明：宣布窗口模式时不要用 `ARENA_RA=off`。
- **新的脚本对手**：复审写的 5 个更强的对手收进 `local/ra_bots_strong.py`，`local.ra_platform --b tanks,swarm,pro,raider,hider` 直接用：tanks（快速战车工厂、坦克一波波来）、swarm（2–3 个兵营的步兵海，先打电厂）、pro（像 OpenRA 普通 AI：双矿厂、每矿 2 车、战车工厂 + 维修厂、2 座塔、混合部队，攒到 5000 价值 8 个单位才打，家被打时全军回防）、raider（吉普 / 防空车专打采矿车）、hider（基地藏在角落）。每个都有一个通过 runner 打 4 分钟的测试。
- **测试**：新增 12 个（`tests/redalert/test_policy.py` 7 个：采矿车全灭且没钱时 90 s 内重新有采矿车；缺电时取消慢的在造建筑、30 s 内有电厂；修理在固定钱数下 30 s 只发 1 次；三张地图上每个走得到的格子离网格点都不超过 11 格；复审那局 hider（种子 81009）14 分钟内拆掉至少 5 座建筑；回不了家时步兵超过 20；慢帧最多连跳 2 次。`tests/strategy/test_ra_sim.py` 5 个新对手各 1 个）。

**本地模拟器测量**（最终代码 6019355；同一批种子上和修之前的代码配对比；锁步时钟可复现；括号里是 95% 置信区间）：

| 场景 | 局数 | 修之前 → 修之后 | 说明 |
|---|---|---|---|
| 主联赛（种子 61–63，对 idle / rusher / turtle / llm） | 144 | 141 → **141** | 胜负逐局相同（0 局翻转），122 局整局一模一样；输的还是那 3 局 turtle（shadowfiend-ii，20 分钟按 K−D） |
| 强对手联赛，复审用过的种子 81（tanks / swarm / pro / raider / hider 各 12 局） | 60 | 55 → **56**（93.3%，84.1–97.4%） | 翻转 1 局：hider 81009 从平局判负变成 7.0 分钟拆光；pro 还是 9/12 |
| 强对手联赛，新种子 191 | 60 | 59 → **59**（98.3%，91.1–99.7%） | 对 hider 修之前有 4 局只靠平局规则赢（没找到基地），修之后 12 局全部拆光；pro 11/12 |
| 采矿车全灭（300 s 打掉全部采矿车；一半局同时把钱清零），新种子 7101–7148，4 种对手 × 3 张图 × 2 个座位 | 48 | 29 → **39** | 重新有采矿车：修之前只有 2 局（37 局之后又打了 90 s 以上也没有）；修之后 44 局，中位 32 s、最慢 39 s（另 3 局 90 s 内就结束了，1 局没有） |
| 电厂全灭（300 s；一半局清零钱），新种子 7201–7248 | 48 | 40 → **45** | 恢复供电（有电厂且电力不为负）：修之前 37 局，中位 68 s、90% 分位 136 s、最慢 220 s，3 局一直没恢复；修之后 41 局，中位 45 s、90% 分位 82 s、最慢 109 s，0 局没恢复（其余是 90 s 内就结束的局）。1 局对 turtle 从平局赢变成平局输（这局供电恢复得更快，之后是对 turtle 的老问题：一波波撞塔） |
| 建造厂 120 s 被毁（维修厂之前），新种子 7301–7324 | 24 | 5 → **20** | hider 0 → 6/6、pro 1 → 6/6、turtle 1 → 5/6、rusher 3 → 3/6 |
| 复审的场景（种子 901–924，对 turtle / pro） | 24 | 20 → **24** | 4 局「建造厂 120 s 被毁」全部翻成拆光 |
| 复审的场景（种子 951–962，trapped 上对 hider） | 12 | 0 → **11** | 修之前 12 局都没找到 hider |
| 自对弈（种子 64） / 网络不稳 5%（65） / 5 秒窗口（67） / 开局 10000（68） / 放置上限 8（72） / 实时时钟（69） | 24 / 24 / 12 / 24 / 12 / 4 | 14–10 / 24 / 11 / 20 / 12 / 4 → **14–10 / 24 / 11 / 20 / 12 / 4** | 前 5 组胜负和修之前逐局相同（实时时钟不可复现，只比胜场）；窗口模式 12 局整局一模一样（对手 llm 脚本 2 次报错，我方 0）；放置上限 8 时被拒的 143 条都是没地方放的 `invalid`，修之前也是 143 |

合计（主联赛 + 强对手 + 压力场景）我方 0 个整批被拒（INVALID_ACTION）、0 次 runner 报错 / 卡死，命令被拒率 0.01–0.07%（放置上限场景除外）；决策耗时 p99 中位 1.7–1.8 ms、最高 3.3 ms（同一批强对手局修之前最高 2.6 ms，多出来的是每秒一次的「哪些搜索点看过了」），最慢一帧 72 ms（同一批强对手局修之前也有 67 ms：开局的地图计算加上 8 个进程并行）。

**pro 对手（复审第 3 条，中）没修，原因找到了一部分**：输的局过程都一样：侦察兵 87 s 死在对方基地里 → 判成「对方已有部队」→ 步兵上限 36、先防后攻 → 150–240 s 军队停在 3600（钱都去了矿厂、采矿车和建筑）→ 打退 pro 的第一波（击杀 5100）以后，365–409 s 用一支以步兵为主的部队（32–34 个单位、价值 4700–5900）去打已经换成坦克的 pro → 打不过撤退，步兵跑不过坦克，几乎全灭 → 家被拆。试过「侦察兵死在对方基地 12 格内不算」（复审的建议之一）：调参种子 151 上对 pro 12 局 11 → 11（各翻 1 局），没有信号，已撤回；复审试过把步兵上限降到 20 也没用。真正的问题是进攻前不知道对方的兵种（侦察兵早死了），用价值比较时步兵和坦克等价。这属于「变强」，要按 `docs/v5_standard.md` 的办法做（先原型、调参种子和测量种子分开、配对比较），最好先看真实对局。两个强对手联赛合计对 pro 20/24（83%）。

**检查**（树 05cab19，`agent/` 和冠军逐字节相同，`agent` 和 `altruagent` 都从工作区导入；输出在 `~/wt/_rafix_out`）：

- 全量 pytest **2058 个全过**（717 s；比 v4.3 的 2046 多 12 个新测试）。
- `replay_real.py`：2349 个真实决策 **0 失败**、0 修补，搜索用上 1296/1296，p99 1979 ms、最慢 2378 ms；`--no-calc` 同样 **0 失败**、0 修补，p99 1691 ms、最慢 2047 ms。`selfcheck.py`：ready（只有没配 key 的 1 个警告）。`python -m agent --help` 有 `--tournament` 和 `--match`、没有 `--claim`。
- 新压缩包 `arena-agent-05cab19`（46 MB / 427 个文件，密钥扫描干净；`agent/`、`altruagent/`、`data/`、`deploy/`、`run_agent.sh`（755）、`selfcheck.py` 和工作区相同；包里有新的 `harv_emergency` 和 `coverage`）：只有 `/usr/bin:/bin` 的干净环境里 selfcheck ready，包里重放 60 个决策 0 被拒，格式不对的假 key `--check-tournament` 退出 1。
- `rehearsal_official.py`（从这个包跑，含认证接口 503 故障）：**16 项全过**，4.4 分钟；9 场（宝可梦 6 场全胜 + 狼人杀 3 桌），91 个决策，0 非法、0 计时器代打、0 竞争错误；热备接管 2 次，接管那一回合没被代打，非租约持有者 0 步；座位连上用时中位 5.1 s、最慢 29.4 s（窗口 240 s），0 次 no-show；key 没进日志，停止后 0 残留。
- **红警端到端**（同一个包的整棵进程树：`run_agent.sh` → `python -m agent --tournament --match` → 每个座位一个 worker，本地模拟平台的官方控制面 + 红警模拟器，对手 rusher，每场 300 游戏秒，实时时钟；自己的名字 `ra-e2e`、日志目录、锁目录和假 key，不碰正式 agent）：2 场红警 Testing 都打满 300 s，**134 / 86 个命令批（174 / 114 条命令）、0 个不合法动作**、被拒 1 / 0 条（`not_owned`：单位刚死），决策耗时 p99 1.03 / 1.51 ms；到时按平局规则 1 输 1 赢（K / D 6300 / 6900、7200 / 100；实时时钟每次不一样，上一轮是 2 赢）；同时宝可梦赢、狼人杀照常；4 个座位 4 次 worker 启动（没有崩溃循环）、0 个 Traceback、0 次 no-show、key 没进日志、0 残留。结束行带上了新的 `harv_emergencies` / `sales` / `power_cancels` / `hunt_seen`。
- 没重跑的：宝可梦对冠军的逐局核对和狼人杀官方 runner 40 局。这次 `agent/` 只改了 `agent/redalert/`（`git diff 9820679 -- agent/ ':!agent/redalert'` 是 0 行），宝可梦 / 狼人杀的决策代码逐字节没变，上面的真实决策重放也全部一致。

- **冻结为冠军**（05cab19）：`champion/` = 6019355 的 `agent/`（VERSION 写的是冻结时的 HEAD c95d906，之后只改了文档）。
- **仍然只有真实 Testing 对局能确认的**，见 v4.3 节；另外这次的修复里有两处依赖平台行为：`cancel` 退回的是已付的钱（OpenRA 是这样，模拟器照此实现）、卖建筑大约 1 s 后退一半价值。

### v4.3 狼人杀红队加固（10-08，分支 `ww/redteam`，宝可梦不变）

背景：10-08 让四个**不看我们代码、也不看我们文档**的挑战者（各是一个独立的 AI 子代理）各写一个专门打我们狼人杀 agent 的对手 bot，只能靠和我们对局、看我们说什么和怎么投票来找漏洞。这四个 bot 现在是 `local/eval_werewolf.py` 的固定评测背景（`--bg rt_wolfpack,rt_talker,rt_detective,rt_chaos`，代码在 `local/redteam_*.py`），以后改狼人杀都要对它们测。

名词（一行一个）：
- **红队**：专门扮演对手、找自己漏洞的一方。
- **身份破绽（tell）**：别人从你的说话方式或投票就能看出你是什么身份。
- **跳预言家 / 假跳**：公开说「我是预言家，X 是狼」；不是预言家却这么说叫假跳。
- **栽赃**：假预言家说某个好人是狼。**对跳**：同一天两个人都说自己是预言家。
- **卖队友（bus）**：狼投自己的狼队友，装成好人。
- **单边配对**：同一批发牌，只把我方的狼换成新版（`agent/champion`），或只把我方的村民和预言家换成新版（`champion/agent`），另一边保持冠军（= main 的 v4.2.4），比胜率差。一桌有我们好几个座位时，新版的狼更强会让我们自己的村民输得更多（自己打自己，此消彼长），所以「整桌都换新版」在我方 3 座、5 座时看不出谁变好了，要看单边配对；正式赛一桌只有我们 1 座（k = 1），这时整桌对比就是真实情况。
- **rt / rounds 节奏**：模拟器里讨论的两种叫醒方式。rt 照官方 runner 的真实节奏（有新消息就叫醒我们），rounds 是旧的「每 20 s 一轮、每轮问一次」。

**四个挑战者**：`rt_wolfpack`（两匹狼配合，一匹假跳栽赃好人，被点名就反跳；村民是 llmq 替身）；`rt_talker`（像大模型那样能说会道、会诈身份的各身份座位）；`rt_detective`（村民在全部 105 种身份分配上做贝叶斯推理，还专门读我们的固定措辞）；`rt_chaos`（对跳、第一天喊票、各种乱写法：粗体、连字符连词、箭头、错别字、「player three」）。

**挑战者找到的问题，以及这次怎么改**（括号里是挑战者的证据）：

| 问题 | 改法 |
|---|---|
| 第一天还没投过票，狼就说「My read from the vote record」（狼 430 次，村民 / 预言家 0 次） | 所有身份同一套话：有了投票记录才说这句，否则都说「No hard info yet…」 |
| 狼从不投、也从不跟预言家点名的队友（狼 0/160，村民 772/782）；喊票时总跳过队友 | 队友反正要被投出去时（可信的预言家点了它且没点我、它假跳被拆穿、或当天 3 个以上座位在推它），狼也投它，并用和村民**一字不差**的模板说出来（`chat_guard` 只放行这几句村民原话） |
| 狼的队友和别人对跳时，狼投对方（真预言家），揭晓后连带暴露 | 狼投自己的队友，用村民的对跳模板说 |
| 被假预言家栽赃时我方村民一句不回（1007 次栽赃 0 次回应，当天被投出 56%） | 村民说「PlayerX 在撒谎：我不是狼，所以他是假预言家，投他」 |
| 官方 runner 的节奏下我们只在 t = 0 说一句，后来谁跳、谁推我们都回不了 | `ARENA_WW_LISTEN` 默认改成**开**（v4.2.3 时默认关）：开场发言后继续读讨论，有值得回应的话就补一句（每天最多 1 句）；`ARENA_WW_LISTEN=off` 关 |
| 两人对跳时按死规则判断（谁被对方说成狼就投谁，否则投座位号小的），假预言家只要反咬真预言家就行 | 看证据：谁点名的狼已经翻出来是狼，谁就更可信；一方说另一方是狼不算证据（谁都能这么说） |
| 预言家查过某人是好人，那人假跳，预言家却投了他（chaos 种子 2403 第 461 局等） | 预言家永远不投自己查验为好人的座位 |
| 已经确定两匹狼（死去的真预言家点的那个 + 被拆穿的假跳者），桌上投假跳者，我们投另一个，票分了（wolfpack：44 天里 36 天分票） | 几个确定的狼里投桌上正在推的那个（村民：多 1 个座位在推就跟；预言家的规则见下面「第 3 次尝试」） |
| 常见的验人说法读不懂（「As the seer I cleared X」「X is on the village team」「checked X -> villager」、粗体、连字符、「player three」「Player03」等，58 种里漏读 19 种） | 全部能读（`agent/werewolf/state.py`；`tests/strategy/test_werewolf_redteam.py` 把挑战者的每种写法都钉成测试） |
| 模拟器没检查平台的 1024 字符上限（`MESSAGE_TOO_LONG`） | `local/werewolf_env.py` 补上（我们自己从没超过） |

**没改的（以及为什么）**：
- 一个人单独跳预言家时我们照样跟，还号召大家跟：测过的替代（贝叶斯村民、对单独跳更怀疑）在普通替身桌上村民 −4.2 个百分点，不划算。被栽赃的是我们自己时我们知道他在撒谎（上面的「村民回应」）。
- 第一天默认喊最小座位号：可预测，但这是全桌集中投票的约定。
- 固定模板让对手认得出我们（狼会先刀我们）：换成随机措辞会破坏我方几个座位之间的协调，没做。
- 狼被点名就反跳（村民不会）：真预言家被假跳点名时也会反跳；改成只否认测过 −0.2 个百分点，没采用。夜刀可预测（总刀跳预言家的人）：挑战者自己建模后也没测出收益。
- 筛选时测过、没采用的开关（实验开关都已删掉）：狼不再说话的 nosk / nosk2（大模型背景 1 座时变差）、soft（没效果）、村民 pad / vclaim（llmq 变差 / 预言家变差）、killheat / wbayes / denyw（变差）。

**第 3 次尝试新发现并修好的**：
- 只换村民那边对 main，在「启发式背景、一桌我方 3 座」上小幅显著变差（种子 2602–2605 共 4000 局：村民 −0.13 [−0.24, −0.02]、预言家 −0.30 [−0.57, −0.04] 个百分点）。逐局看：我方预言家喊「Everyone vote Player2」，但我方另一个座位已经跟着前一天的结果喊过 Player6，预言家按「多 1 个座位在推」改投 Player6，票分了。修正：预言家喊票时选座位号最小的狼，除非桌上已经多 2 个以上座位在推另一匹；投票时投自己喊的那匹，除非桌上明显（多 2 个以上座位）转向另一匹（`SEER_SWITCH = 2`；喊过谁记在对局记忆里，worker 重启后也记得）；跟随活着的可信预言家的村民，投预言家当天喊的那匹，没喊才按桌上在推的。修正后这 4000 局和 main 逐局相同；新的开发种子 2608（10 种背景 × 1/3/5 座，30,000 局）和修正前没有任何显著差别（红队背景上的收益保留）。
- 上一次留下的未提交实验（村民也要「多 2 个座位」才跟的 `CONVERGE = 2`、按投票意向数人的 `_INTENT`）丢掉：种子 2606 上「多 1 个」对 rt_chaos / rt_wolfpack 的村民好 +0.70 / +0.52 个百分点（都显著），其他背景不变。

**代码里的说法**：按比赛规则（`docs/official_rules_2026-10-06.md` 第 7、8、13 节；提交的仓库是公开的），`agent/werewolf/policy.py`、`agent/reasoning.py`、`tests/strategy/test_werewolf_chat.py` 里关于观战和私信的注释改成中性说法（「私信默认关闭」「reasoning 的 detail 模式是可选的，比赛不用」），行为不变：私信仍默认关闭。`scripts/publish_sync.sh`（`docs/1008` 分支）的检查规则在 `agent/`、`tests/`、`local/`、`scripts/` 里 0 处命中。

**最终测量**（`python -m local.eval_werewolf --games 1000 --seeds 2610 2611 --pairs agent:champion,agent/champion:champion,champion/agent:champion`，冻结前的 20ac2c6 对 main 的 v4.2.4；2600–2608 在开发和排查时用过，2610 / 2611 是新的；两种节奏都测；单位：百分点，[95% 置信区间]）：

| | rt 节奏（官方 runner 的真实节奏） | rounds 节奏 |
|---|---|---|
| 红队背景（4 种 × 1/3 座，每对 16,000 局）只换狼 | **+25.0** [+24.0, +26.1] | **+25.8** [+24.7, +26.8] |
| 红队背景 只换村民 / 预言家 | **+2.9** [+2.4, +3.4] / **+7.6** [+6.6, +8.5] | **+1.6** [+1.2, +2.0] / **+2.3** [+1.7, +2.9] |
| 红队背景 整桌、我方 1 座（每座） | **+9.8** [+9.0, +10.5] | **+9.0** [+8.2, +9.7] |
| 普通背景（llm、llmx、llmq、heuristic、first、mixed × 1/3/5 座，每对 36,000 局）只换狼 | **+10.4** [+10.0, +10.9] | **+9.5** [+9.1, +9.9] |
| 普通背景 只换村民 / 预言家 | **+0.13** [+0.07, +0.20] / **+0.20** [+0.09, +0.30] | −0.04 [−0.07, +0.00] / −0.06 [−0.13, +0.00] |
| 普通背景 整桌、我方 1 座（每座） | **+0.33** [+0.18, +0.49] | −0.07 [−0.15, +0.01]（不显著） |

- 普通背景（6 种 × 1/3/5 座 × 两种节奏 = 36 个条件）里，两个单边配对**没有一个条件显著变差**，按背景合并的分片也没有；整桌配对在我方 1 座时也没有一个条件显著变差。
- 分背景（rt 节奏，只换狼）：rt_talker +39.5、rt_detective +29.5、rt_chaos +25.8、rt_wolfpack +5.1；llm +14.8、llmq +14.4、llmx +12.2、mixed +9.1、heuristic +8.0、first +3.9。只换村民 / 预言家：rt_wolfpack +9.2 / +17.9、rt_chaos +2.2 / +8.8、rt_talker +0.6（不显著）/ +3.7、rt_detective −0.3（不显著）/ 0.0。
- 唯一的分片退步在红队背景上：rounds 节奏下 rt_detective 的村民 −0.36 [−0.61, −0.11]；rt 节奏下合并不显著（−0.29），但我方 3 座的两个格子（两个种子）都是约 −3.4（筛选时就看到过，来自「多听一会儿」）。
- 整桌配对在我方 3 座、5 座时是负的（普通背景每座 rt −0.70 / −6.01，rounds −0.84 / −6.04）：这些桌上我方新的狼更强，赢的是我方的狼、输的是我方的村民（只换村民的配对是 0 或正的）。eval 工具的总 SHIP 按整桌合并算，所以普通背景两次都报 `SHIP: False`（红队背景 rt 节奏报 `True`）；正式赛一桌只有我们 1 座，碰不到这种桌（Testing 局里一个 agent 占几个座位时会这样，不计分）。这一条是判断，不是工具的结论，主人可以否决。
- 稳定性：四次测量、四个 agent 写法合计 416,000 局：0 崩溃 / 0 非法 / 0 被拒聊天 / 0 超时 / 0 结束发言后再发，最慢决策 146 ms，0 私信。`window_close`（窗口关闭时我方还在听）只出现在 rounds 节奏 + 红队背景（新版 4984 局、只换狼 1470、只换村民 3333，每对 16,000 局）：rounds 每 20 s 才问一次，红队 bot 话多、24 s 安静规则不触发，最后一次问时离关闭还剩 15 s 以上就继续听；rt 节奏和真 runner 下听的循环最多 5 s 看一次倒计时、到 15 s 一定结束，rt 节奏这项是 0。不算失败（模拟器里它只是结束发言）；真 runner 替身里也没发生（见下面「检查」）。

**检查**：
- 全量 pytest（20ac2c6，冻结前）**2086 个全过**（709 s）；冻结后用到冠军的 6 个测试文件 + 红队、对局记忆测试共 325 个全过。
- `scripts/replay_real.py` 2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 2192 ms、最慢 2703 ms；`--no-calc` 0 失败、0 修补，p99 1976 ms、最慢 2312 ms（宝可梦没改）。
- 官方 runner 本地路径（`local.werewolf_platform`，每个座位都跑 `altruagent.runner.run_game`，120 s 窗口，听默认开，种子 1004，40 局）：我方 **0 报错**、0 卡死、0 闲置代打、0 被拒，最慢决策 7.7 ms，我方赢 62/119，219 张票 0 弃票，250 条聊天；听：174 次 hold、31 条补发、最长听 24.65 s，0 错误、0 次找不到 runner 会话；另有 80 次无害的 `WRONG_PHASE_NOOP`（runner 在窗口关闭那一刻多发的 terminate）。上一次尝试的 9cb815f（种子 1003）同样 0 报错、赢 63/112。
- 冻结为冠军：cf76409（`champion/VERSION` = 20ac2c6）。
- **合并时要一起改的**：`docs/1008` 分支的 `README.md`「Settings used in competition」和 `PUBLIC_README.md` 写的是 `ARENA_WW_LISTEN=off`；v4.3 起默认是 `on`，合并后那两处要改成 `on`（或者主人决定比赛用 off，就在 `.env` 里设 `ARENA_WW_LISTEN=off`，但截止后改配置要主人确认规则允许）。

### v4.3 复审修复（10-08 晚，分支 `ww/redteam`，模拟对局不变）

复审（独立的审查代理，种子 2695–2697）确认了 v4.3 的数字能逐格复现，另外找到 2 个中等问题和 3 个小问题。这次的处理：

名词：
- **正则表达式回溯爆炸（ReDoS）**：读聊天用的是正则表达式（一种文本匹配规则）。规则写得不好时，遇到特制的输入，匹配器要试的组合数会随输入长度的平方、立方增长，几 KB 的文字就要算好几秒。
- **看门狗（watchdog）**：每步决策有时间预算（狼人杀 20 s + 2 s 宽限），超时就交一个保底动作（聊天交 terminate，投票交保底票）。

**1. 一条塞满空格的聊天要读 2 秒（中等，已修）**
- 问题：v4.3 为了读懂「My result: "Player3 is a wolf"」加的规则 `_OWN_QUOTE`（`agent/werewolf/state.py`）里有三段连在一起的「可有可无的空白」。一条「result」后面跟 1018 个空格的聊天（1024 个字符、1 个词，在平台的 50 词 / 1024 字符限制之内）读一次要 2.18 s（空格 400 个 141 ms、800 个 1.1 s，按立方增长；main 的 v4.2.4 是 0.3 ms）。两个座位各发 5 条，我们那一步的聊天和投票都要算 23 s 以上，超过预算：聊天交 terminate（这天不说话），投票交保底票（座位号最小的人）。模拟器存聊天时会把连续空白合成一个空格，所以所有本地评测都看不到这个问题；真实服务器会不会保留连续空白不知道。
- 修法（edacce7）：读聊天之前先把连续空白合成一个字符（里面有换行就留一个换行，因为「单引号引用不跨行」这条规则要看换行）；`_OWN_QUOTE` 改写成每个可选的词 / 符号自己带后面的空白，匹配结果和原来完全相同，但不再有相邻的可选空白。
- 测量：同一条输入 2.18 s → 0.02 ms。扫了 960 种平台允许的输入（40 种关键词 × 空格 / 制表符 / 换行 / 全角空格 / 不换行空格 / 回车换行 / 混合 × 3 种结尾，再加 30 种标点和符号各重复到 1024 字符），最慢 2.0 ms；另外 1520 种不管 50 词限制、去掉粗体符号或引号之后才出现长空白的输入（300–500 个词），最慢 63 ms（平方增长，可以接受）。部署用的 `Contestant`（看门狗开着）读 10 条这样的聊天：聊天和投票都 0.0 s 答完，0 超时（修之前 23.5 s、超时、交保底）。
- 模拟对局不变：旧版和新版读同一批 4403 条聊天（真实大模型聊天样本、挑战者的全部写法、本地 bot 的句子、4000 条随机拼接）得到的事实 0 处不同；把这些句子里的单个空格换成多个空格或制表符，读出来也和原句 0 处不同（换成带换行的空白时有 95 处不同，都是「单引号引用不跨行」这条原有规则，和这次修改无关）。用 2610 / 2611 两个种子、和最终测量完全相同的参数重跑（新版对 main 的 v4.2.4，rt 节奏，红队背景 1/3 座和普通背景 1/3/5 座，每对 16,000 / 36,000 局，四个写法合计 208,000 局）：和上一节的最终测量**逐格相同**（红队 448/448 格、普通 1008/1008 格，0 格不同），所以上一节的数字原样适用于修好后的版本。
- 新测试：`tests/strategy/test_werewolf_redteam.py` 加了 2 个（9 种塞空白的聊天每条 0.1 s 内读完；塞空白不改变读出来的内容）。

**2. 导出的 `docs/deploy.md` 里私信两条还是旧说法（中等，已修）**
- 问题：本分支的 `docs/deploy.md` 私信那两条还是 main 上的旧说法（按比赛规则第 13 节不该出现在公开仓库里的那种）；这个文件在 `scripts/publish_sync.sh`（`docs/1008` 分支）的公开导出清单里，`docs/1008` 已经改成中性说法，但本分支改了旁边几行，合并时可能把旧文字带回来。
- 修法（b0fc6ee、bcfe451）：把 `docs/1008` 改过的三行（reasoning 的 `detail` 一条、私信两条）原样复制过来，测试 `test_werewolf_chat.py` 里那条私信测试的说明也用 `docs/1008` 的原文，两边改动一字不差，合并时不会冲突。用 `git merge-tree` 试合并 `ww/redteam` 和 `docs/1008`（不动任何分支和工作区）：只剩 `docs/progress.md` 冲突（两边都加了新的小节，合并时两段都留）；合并结果的导出文件按 publish_sync 的检查规则 0 处命中；合并后 `deploy.md` 的「多听一会儿」一条是 v4.3 的「默认打开」。合并后仍要手改的只有 `README.md` 第 74 行和 `PUBLIC_README.md` 第 94 行的 `ARENA_WW_LISTEN=off`（上一节最后一条）。

**3. 认得出我们固定模板的对手，在一桌只有我们 1 座时也能看出身份（小，没改，记录在案）**
复审在种子 2695 上（我方 1 座，7 种背景各 400 局）数出：
- 第一天「No hard info yet ... I'll vote PlayerN」：村民（0/1574）和预言家（0/240）从不跳过座位号最小的其他座位；狼跳过的 149/806 局（18.5%）里，被跳过的全是它的队友。所以「跳过了」就等于「是狼，而且队友是那一位」。main 的 v4.2.4 一模一样。
- 「I am the Seer. PlayerA is a WOLF and is lying about being Seer. Vote PlayerA.」这句只有狼说（126 次，全是狼；真预言家的句子是「...is lying about being Seer, so PlayerA is a WOLF.」）。main 只说过 16 次，「多听一会儿」打开后多了约 8 倍。
- 可信的预言家点了一个既不是我们也不是队友的座位时，村民 92%（352/383）会发「Following the Seer...」，狼 0/70（main 是 48% 对 0%），所以「不说话」更像狼了。
- 「Two Seer claims...」狼说 146 次、村民 125 次（这句话出自狼的概率 0.54，全桌狼的比例约 0.29；main 的狼从不说）。
没改的原因：这些只对「知道我们模板原文」的对手有用。真实的大模型对手不知道我们的模板；仓库要到 10-13 提交截止才公开，那时所有参赛仓库都冻结了。复审给的改法（狼也发「Following the Seer」；狼的反跳用预言家的原句；狼第一天也喊座位号最小的人，即使那是队友）每一条都会改变狼的打法，要先测狼的胜率，离内部截止（10-09）太近，没做。上一节「没改的」里「固定模板让对手认得出我们」「狼被点名就反跳」两条包括这些具体破绽。

**4. 换一种说法测试：v4.3 的收益有多少来自专门读懂红队的写法（小，没改，记录在案）**
复审把四个挑战者的话改写后重测（种子 2695，我方 1 座，rt 节奏，每种 4000 局，对 main）：
- 原样：每座 +10.6 [+9.5, +11.7]（狼 +28.7、村民 +2.5、预言家 +7.2）。
- 同样的打法、去掉花样的普通写法：每座 +10.9 [+9.8, +11.9]（狼 +25.5、村民 +4.5、预言家 +6.5）——收益来自打法，不是来自读懂花样。
- 换成新旧两版都读不懂的自然说法（「I hold the seer role」「PlayerN belongs to the wolves」「checked out innocent」「let's eliminate PlayerN」）：每座 +2.5 [+1.9, +3.1]（狼 +8.5、村民 0.00、预言家 +0.9 不显著）；其中「改写后的 wolfpack」上狼 −1.7 [−3.3, −0.2]（287 局），种子 2696 / 2697 再测 −0.8 [−1.8, +0.1]（608 局），合起来约 −1.1，可能是真的、很小。
- 30 种红队写法以外的自然说法里，新旧两版都还有 9 种读错；52 个用例新版对 41 个、main 对 32 个；21 个「不该读成跳预言家」的陷阱没有一个变差。另有两个小的新误读：Player2 自己说「I think Player2 is the seer」会被当成跳预言家；「. Result:」后面引号里的别人的话会被当成发言者自己的指认。
以后的改进：把这种改写版挑战者加进固定评测背景，读法往「part of / belongs to the wolves」「checked out innocent」「I hold / have the seer role」这类通用说法扩，而不是继续加红队专用的写法。

**5. 「多听一会儿」从没在真实服务器上跑过（小，复审未确认，上线后检查）**
- 只在模拟器和本地 runner 替身里跑过；真实的狼人杀局一次都没录到过。它在讨论中调用 runner 会话的 `wait_for_update`，等待时间是 0.5–5 s 的小数，服务器文档只写了「默认 20、最多 25」，接不接受小数没验证过。出错时是安全的：任何异常都只算一次 `listen_errors`，然后结束发言。
- 上线 v4.3 后，看头几局 Testing 狼人杀的 `[match-summary]` 行：应当是 `listen=on`、`holds` 大于 0、`listen_errors=0`、`no_game=0`。如果有错误，就在 `.env` 里设 `ARENA_WW_LISTEN=off`（提交截止后改配置要主办方同意）。

**检查**：
- 全量 pytest（bcfe451）**2096 个全过**（713 s，比上次多的 10 个是新测试）。
- `scripts/replay_real.py` 2349 个真实决策 0 失败、0 修补，搜索用上 1296/1296，p99 2006 ms、最慢 2705 ms；`--no-calc` 0 失败、0 修补，p99 1890 ms、最慢 2840 ms（宝可梦没改）。
- 官方 runner 本地路径（`local.werewolf_platform`，种子 1006，12 局，听默认开）：我方 **0 报错**、0 卡死、0 闲置代打、0 被拒，最慢决策 5.5 ms，我方赢 14/33，57 张票 0 弃票，63 条聊天；听：51 次 hold、6 条补发、最长 24.65 s，0 错误、0 次找不到 runner 会话；另有 22 次无害的 `WRONG_PHASE_NOOP`。
- 冻结为冠军：eaa490f（`champion/VERSION` = edacce7）；agent/ 和 champion/agent 相同。
- 正在跑的真实 agent（main 检出、`run_agent.sh` name=main）没碰；main 仍是 7a59a65；没合并、没推送。

### 狼人杀改进（09-28，宝可梦不变）
- 合入 `candidates/r2-ww-seer-and-villager`，加上模拟得出的规则（村民不弃票、集中投票、不假跳；预言家查到狼或快被投出时跳；对跳时投两个跳的人之一；狼只在被点名时对跳、先刀已跳的预言家），聊天只读有限事实并识别注入，狼的私信默认关闭（`ARENA_WW_DM=on` 可开）。
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

### v4.3 合并（10-08 合并，10-10 收尾；分支 `integrate/v43`）
把四个分支按顺序合到 main（7a59a65，v4.2.4）上：`pk/tracker-1008`（v4.2.5，宝可梦没落地的出招不算用过）、`ra/bot-v0`（v4.3.1，红警规则型 bot）、`ww/redteam`（v4.3，狼人杀红队加固，默认继续听讨论）、`docs/1008`（规则合规、比赛日手册、公开仓库准备）。冲突只在 `agent/agent.py`（两个 `on_action_result` 合成一个：先红警，后宝可梦）、`champion/VERSION` 和文档；各游戏的决策代码和各自分支逐字相同。

检查（树 5eb830a，输出在 `~/wt/_v43_out`）：
- 全量 pytest 2235 个全过（= 1962 + 宝可梦 35 + 红警 96 + 狼人杀 134 + 文档 8）。
- `replay_real.py` 2349 个真实决策 0 失败、0 修补，搜索 1296/1296，p99 1700 ms；`--no-calc` 同样 0 失败，p99 1671 ms。
- 宝可梦逐局对比：20 局对冠军（种子 171、确定性设置），胜负、回合数、非法动作、计时器代打和 main 逐局相同（20/20）。
- 狼人杀官方运行器本地路径 40 局：0 错误（63/112，默认继续听讨论；main 是 67/112）。
- 红警模拟器配对联赛 54 局：和 `ra/bot-v0` 结果、局长逐局相同（51 胜），0 次报错。
- selfcheck ready；新包 arena-agent-5eb830a（46 MB，428 个文件，带 COMMIT 文件）在干净环境 selfcheck ready、重放 60 个决策 0 拒收、坏 key 退出 1。
- `rehearsal_official.py` 16/16（含 auth503、热备接管 31.9 s、0 次缺席）；红警假平台端到端两场都真的在打（0 个不合法动作）。
- `publish_sync.sh` 预演：扫描干净、README 合格，112 个文件 / 4.3 MB，READY。
- 冠军已冻结（5eb830a）。只有真实 Testing 对局才能确认的：红警的真实寻路、战斗平衡、采矿收入；狼人杀「继续听讨论」在真实服务器上的表现。

### v4.4-draft（10-11，分支 `pk/draft-1011`）：选秀按真实对手校正（对手模型 + v4 打法残差），对战和首发不变

**一句话**：v4.3 的选秀有两个假设：对手和我们用同一个模型选牌；卡牌强弱就是 v3（经典打法）自对弈拟合出来的值。现在（1）每当对手选了一张牌，就判断它更像「真实参赛者那样选标准环境核心」（Flutter Mane、Incineroar、Dragonite……）还是更像「我们自己的模型」，像前者就按真实参赛者的偏好预测它接下来拿什么；（2）卡牌强度加上一个「v4 引擎搜索打法下的修正」（残差）。**擂台种子 7 对 v4.3：232/400 = 58.0%（95% CI 53.1–62.7），下限 > 50%；种子 11 复查 212/400 = 53.0%（CI 48.1–57.8）；两个种子和 v4.3 镜像配对合计 +5.5 个百分点（p = 0.016）。** 回退：`ARENA_DRAFT_OPP=off` 加 `ARENA_DRAFT_RESID=0` = v4.3 的选秀（逐手相同，有测试）。

名词：**真实对局** = 平台上和别的参赛者打的 22 局（10-08/09；LeCharmander 19 局 12 胜 7 负，Steve Nash Equilibrium 3 局 3 胜）；**meta 选秀者** = 照真实参赛者的选牌习惯拟合出来的本地评测对手；**对数几率** = 选秀模型给「我方 6 张牌打赢对方 6 张」打的分，0 = 五五开，+1 ≈ 73%；**残差** = 在模型的分上，每个宝可梦再加的一个小修正；**擂台** = 对冠军（v4.3）打；**配对** = 同种子、同座位、同卡池的两批局逐局比；**McNemar p** = 配对差的显著性，< 0.05 算显著。

#### 1. 真实对局里看到的（22 局的选秀全部重建）

- 卡池（每局 18 张，带完整配置）、12 手顺序都从我们的录像里重建出来，对方在我们最后一手之后的那一手从队伍预览补上（主 checkout 的 `logs/<session>.jsonl`，只读）。
- 真实对手很早就拿标准核心：Incineroar（在池里的 18 局拿了 15 次，平均是它的第 1.3 手）、Flutter Mane（13 局拿 12 次，第 1.2 手）、Dragonite（12 局拿 11 次）、Landorus-T（10 局拿 9 次）、Rillaboom（18 局拿 11 次）；公开理由是模板句（"Fake Out pressure, Intimidate"、"spread damage, beats Gyarados"）。我们几乎每局第一手拿 Archaludon（在池里 13 局拿 13 次，平均第 1.2 手），再拿 Baxcalibur（13 局拿 11 次）、Arcanine、Iron Bundle。输的 7 局里对方有 Flutter Mane 5 次、Landorus-T 4 次、Incineroar 6 次。
- 模型给这些牌的分：Archaludon +0.52（全场最高），Flutter Mane +0.07，Incineroar −0.03，Iron Hands −0.16，Dragonite −0.18，Landorus-T −0.20，Ting-Lu −0.29。
- 用我们的模型猜真实对手的下一手，只猜中 18.9%（132 手）；按真实选牌拟合的「条件 logit」（每个宝可梦一个偏好值 + 「克制我方已选牌」一项，`scripts/fit_meta_drafter.py`）留一局交叉验证能猜中 57.6%。
- 模型对真实结果太乐观：22 局模型平均预测我们赢 77.6%，实际 68.2%。
- 在这 132 个真实选秀状态上重跑 v4.3 的选秀，和当时录像里的选择 118 次相同。

#### 2. 新的本地评测对手：meta 选秀者（`local/meta_opp.py`，数据 `data/meta_drafter.json`）

- `meta`：每一手拿「偏好值 + 3.5 × 这张牌对我方已选牌的平均单挑优势」最高的合法牌；首发和对战用冠军（v4.3）自己的代码，所以和 v4.3 打时只有选秀不一样。`meta_soft` 按拟合的概率抽牌，`meta_v3` 用 v3 的经典打法（快得多）。只能按名字点（`local.panel --opps meta`），默认面板不变；结果每行多记两边的 6 只（`six_cand` / `six_opp`）。
- **它和真实对手很像**：v4.3 打 meta 200 局（种子 103）赢 134 局 = **67.0%**（CI 60.2–73.1）；真实平台对这些对手是 68.2%；模型自己预测 76.7%。本地重现了「模型高估我们对标准核心阵容的优势」。

#### 3. 改了什么（只有 `agent/pokemon/drafter.py` 和两个数据文件）

- **对手模型**（`ARENA_DRAFT_OPP=meta`，默认开；`OPP_PRIOR` 0.2，`OPP_TEMP` 0.15）：对手每选一张牌，比较「按真实参赛者偏好会选它的概率」和「按我们模型的收益（温度 0.15）会选它的概率」，更新「它是 meta 型对手」的概率 w（选秀开始前 0.2）。搜索里，对手那一步的值 = (1 − w) × v4.3 原来的「对我们最坏」+ w × 按真实偏好前 3 张牌的加权平均；后面的贪心推演里 w ≥ 0.5 时对手按偏好拿牌。我们自己对牌的估值不变。偏好数据 `agent/pokemon/draft_meta.json`（22 局拟合、不带克制项）；数据缺失、或者它认识的宝可梦少于 6 个时自动退回 v4.3。温度 0.15 是 v4.3 自己选牌时似然最大的值（145 次模拟选秀）；对一个按模型选牌的对手，看到它 3 手以后 w 平均 0.16，对 meta 对手平均 0.99。
- **v4 打法残差**（`ARENA_DRAFT_RESID=1`，默认开）：`scripts/fit_draft_resid.py` 用 1694 局「双方都是引擎搜索」的本地对局（~/exp14–21 面板的逐局日志，450 个不同卡池，**排除全部门槛种子 7、11、31、45、73、75、83**），在模型的对数几率上再拟合每个宝可梦一个修正（L2，按卡池分组 5 折交叉验证选 λ = 300：留出对数似然每局 −0.6519 → −0.6497）。修正最大的：Incineroar +0.093、Amoonguss +0.073、Flutter Mane +0.060、Landorus-T +0.046；Tornadus −0.107、Dragonite −0.080、Kingambit −0.066、Dondozo −0.060。两边同样加。（另试：把对 meta 的 400 局也加进去，交叉验证的好处几乎没了（+0.0007），所以上线的残差没用它们。）
- **成本**：在 132 个真实选秀状态上，节点数中位 10.9k、最多 55.5k（预算 120k，0 次碰到），每手最慢 193 ms（平台每手 15 s，选秀预算 10 s）。

#### 4. 选秀模拟（不打对战，只用模型给最后两边的 6 只打分；种子 101，每格 200 次选秀）

| 配置 | 对 meta（v4.3 模型打分 / 加残差后打分） | 对冠军（同上） |
|---|---|---|
| 只开对手模型（先验 0.2） | +1.85 / +1.69 个百分点 | −0.35 / −0.21 |
| 只开残差 | −1.02 / +0.36 | −2.13 / +1.64 |
| 两个都开（上线配置） | +0.72 / +1.94 | −2.35 / +1.31 |

先验 0.5 / 0.8、固定 w = 0.5 / 1 都更差（固定 w = 1 对冠军 −14.4：完全相信对手是 meta 型很危险，所以要按它的选牌更新 w）。**留出检查**：偏好数据只用一半真实对局拟合、meta 对手用另一半时，对手模型对 meta 的好处只剩 +0.3 / +0.4（全部数据时 +1.85），说明这部分在模拟里的好处大半来自「评测对手和偏好数据出自同一批对局」。选秀模拟只说明在我们自己的估值下规划得好不好；能不能多赢要看对战。

#### 5. 对战检查（全部 0 非法动作、0 计时器代打、0 判负、0 出错；`ARENA_PLOG=1`，5 个并发；候选用 `--env` 开两个开关，v4.3 用同一份代码关掉开关）

| 检查 | 候选 | 95% CI | v4.3（同一批局） | 配对差 [95% CI] | 只候选赢 / 只 v4.3 赢 | McNemar p |
|---|---|---|---|---|---|---|
| 开发筛选：对 meta，种子 103，200 局 | 144/200 = 72.0% | 65.4–77.8 | 134/200 = 67.0% | +5.0 [−2.7, +12.7] | 36 / 26 | 0.25 |
| **擂台种子 7（门槛），400 局** | **232/400 = 58.0%** | **53.1–62.7** | 镜像 187/400 = 46.8%（见下） | **+11.2 [+5.1, +17.4]** | 103 / 58 | **0.0005** |
| 擂台种子 11（复查），400 局 | 212/400 = 53.0% | 48.1–57.8 | 镜像 213/400 = 53.2% | −0.2 [−6.4, +5.9] | 79 / 80 | 1.0 |
| 擂台两个种子合计，800 局 | 444/800 = 55.5% | 52.0–58.9 | 400/800 = 50.0% | **+5.5 [+1.1, +9.9]** | 182 / 138 | **0.016** |
| 对 meta 正式测量：种子 109，400 局 | 278/400 = 69.5% | 64.8–73.8 | 272/400 = 68.0% | +1.5 [−4.8, +7.8] | 86 / 80 | 0.70 |
| 对 meta 合计（种子 103 + 109），600 局 | 422/600 = 70.3% | 66.6–73.8 | 406/600 = 67.7% | +2.7 [−2.3, +7.6] | 122 / 106 | 0.32 |
| VGC-Bench SP argmax：种子 75，200 局 | 118/200 = 59.0% | 52.1–65.6 | 116/200 = 58.0% | +1.0 [−7.5, +9.5] | 39 / 37 | 0.91 |
| simbot 同队：种子 83，100 局，`SIMBOT_BUDGET_MS=100000` | 47/100 | 37.5–56.7 | 47/100 | 0（逐局相同：同队模式没有选秀差别） | 0 / 0 | 1.0 |
| 回归面板种子 45（basic、smoke、llm_timeout、greedy、protect_spam 各 60） | 300/300 | — | 298/300 | +0.7 | 2 / 0 | 0.5 |

- 「镜像」= v4.3 自己打冠军（v4.3），同种子、同座位、同卡池，两边只差选秀开关。种子 11 的镜像是本分支冻结前的同一份代码关掉开关跑的；种子 7 的镜像借用了 `pk/protect-1011` 分支同时跑的 `v43-L7`（那个分支的守住开关关着 = v4.3 打法，提交 d5df0d6，只读取了它的结果文件）；那个分支同样设置的种子 11 镜像 `v43-L11` 和本分支自己的种子 11 镜像前 126 局胜负、回合数逐局相同（126/126），所以借用的种子 7 镜像就是 v4.3。种子 7 的镜像只有 46.8%，说明这个种子本身对「候选座位」偏低；候选在种子 7 的 58% 有一部分是运气，种子 11 上和 v4.3 一样。两个种子合起来 +5.5 个百分点，显著（p = 0.016）。
- 外部对手和回归面板都没有变差：VGC-Bench +1.0（不显著），simbot 同队逐局相同，回归面板 300/300（v4.3 298/300）。三项外部 / 回归的配对差合计 ≥ 0。
- 对 meta 的提升（+1.5 / 合计 +2.7）不显著；按任务第二条路线（「擂台 ≥ 50% 且对 meta 显著更好」）不成立，这次是按第一条路线（擂台种子 7 的 CI 下限 > 50%）过的。
- 擂台每局约 31–34 s，所有候选运行最慢一步 2.8–3.6 s（限制 55 s），0 次计时器代打。开发筛选里候选和 v4.3 的 6 只在 200 局里只有 13 局相同（种子 109：400 局里 21 局）。和 v4.3 比，候选对 meta 多拿 Amoonguss、Rillaboom、Whimsicott、Landorus-T、Incineroar，少拿 Tornadus（种子 109 少 87 次）、Garchomp、Iron Bundle、Arcanine；meta 因此少拿到 Rillaboom、Landorus-T、Incineroar。
- 全量 pytest 2247 个全过（2235 + 新的 12 个）。新测试 `tests/strategy/test_draft_meta.py`：两个开关关掉时和 v4 冻结版（`versions/v4_engine_search`，选秀代码和 v4.3 相同）在 8 个随机状态上选择、深度、节点数、分值完全相同；w 跟着对手风格走；模型 / 残差 / 默认值 / 策略读开关；meta 选秀者在本地环境合法选牌并修正驾驶员的记忆。改了两个旧测试：`test_platform_1005.py` 的 `steady` 固定用 v4.3 的选秀（它重放录像，要和录像里的选择对得上），`test_harden_v3.py` 的「节点数 < 预算的 1/3」放宽到 1/2（47.6k）。
- `replay_real.py`：2349 个真实决策 0 失败、0 修补，p99 1862 ms；`--no-calc` 同样 0 失败，p99 1822 ms。另外把主 checkout 里 10-08 以后的 26 个真实录像（含这 22 局宝可梦，600 个决策）也重放了：两种模式都是 0 失败。
- 冠军已冻结成这一版（`champion/VERSION` = 8160ba0）。冻结之后的检查（对 meta 种子 109、VGC-Bench、simbot、回归面板的 v4.3 一侧、种子 11 和它的镜像）都在冻结前的同一份代码（20b6a1f，单独的 checkout `~/wt/draft-eval`，冠军仍是 v4.3）上跑。

#### 6. 结论和风险

- 按这次任务定的上线标准（擂台种子 7 的 95% CI 下限 > 50%，pytest 全过，`replay_real` 两种模式 0 失败）**通过**；外部对手、回归面板没有显著变差。
- **但没有达到 `docs/v5_standard.md` 的完整 v5 门槛**：种子 11 的 CI 下限是 48.1%（不到 50%），和同一批局的 v4.3 镜像比是 −0.2；也没有真实平台 A/B 和独立审查。所以它是按这次限时标准上的「v4.4-draft」，不是 v5。两个种子合计对 v4.3 镜像 +5.5 个百分点（p = 0.016）是目前最可靠的估计；种子 7 的 +11.2 有一部分是运气。
- 对手模型的偏好数据只有 22 局，而且 19 局是同一个对手；正式赛里遇到「不按标准核心选」的对手时 w 会降下来，退回 v4.3 的假设。残差的修正值都很小（最大 0.11），主要来自本地自对弈，不是真实对局。
- 首发 / 对战一行没改；另一个分支 `pk/protect-1011`（对战的守住）不碰这些文件。
### 宝可梦「守住」用得太多：诊断和三种修正（10-11，分支 `pk/protect-1011` / `pk/protect-1011-chain`，**没有上位**，v4.3 仍是冠军）

名词：**守住** = Protect 一类招式（这一回合挡下攻击，优先度 +4）；**连续守住** = 上回合刚守住、这回合又守，Gen 9 里第二次只有 1/3 成功、第三次 1/9（「计数器」= 3、9……）；**搜索** = v4 的引擎搜索（每个局面把我方约 8 种双槽组合和对手的应对在 Showdown 克隆上真打，每格抽样 2 遍）；**blend** = 搜索选招时假设对手 50% 按均衡出招、50% 贪心（每格打当下伤害最高的那一下）；**擂台 / 配对 / McNemar p** 同 `docs/v5_standard.md`；**镜像** = v4.3 打 v4.3，用来和候选在同一批局上配对。

**起因**：22 局真实对外比赛（15 胜 7 负），输的局平均每局守住 4.1 次（赢的 1.9 次），连续守住 0.57 次（赢的 0.2 次）；例子 4cc3eac8 第 10 回合龙卷云连续守住失败被打倒。两个假设：(1) 每格只抽样 2 遍，碰巧「连续守住成功」的那一行被高估（抽样噪声）；(2) blend 让守住对「只会打最痛那一下」的贪心对手显得特别值钱。

**诊断**（163 局真实录像：22 局对外 + 1 局自对弈 + `logs/yoriko`、`logs/apocalypse` 140 局；用当前代码重放、截下 1569 次搜索请求，再用不截断的搜索按不同设置重算；1285 个出招回合）：
- v4.3 在 24% 的出招回合里有一格守住。能连续守住的局面 173 个，搜索选了 29 次（17%）。7 局输局里搜索的守住一共 29 格：连续 2、「没有击杀威胁」3、「有击杀威胁」（两只对手最强一击加起来能打倒）24 —— 输局里的守住主要是被威胁时的守住。重放的选择和当时真实发出的几乎完全一样。
- 只换随机种子，47%（741/1569）的搜索答案就会变：搜索本身噪声很大；连续守住的 29 次换种子后只留下 16 次。
- **假设 (1) 不成立**：把连续守住的成败按真实概率精确加权（同一组随机数下「成功」「失败」各打一遍，按 1/计数器 合成），连续守住不减反增（29 → 36，换种子也是 36）。4cc3eac8 第 10 回合那次用 24 遍抽样 + 精确概率仍然选连续守住（那时已是残局输面，别的选择更差）。
- **假设 (2) 成立**：首次守住比最好的「不守住」选项多出来的价值，主要来自贪心那一列（没有威胁的守住 114/146 次、有威胁的 138/158 次），很多时候这一行在我方自己的均衡里概率是 0 —— 守住其实是在赌「对手会把最痛的一下打在它身上」。
- 自对弈也一样（v4 镜像，种子 91）：输的一方每局连续守住 0.85 次、赢的 0.13 次；守住 3.85 次对 1.96 次。所以「输局守住多」大部分是落后、被威胁的结果，不一定是输的原因。

**改动**（都在开关后面，默认关 = v4.3 逐字不变；`tests/strategy/test_search_protect.py` 9 个测试）：
- `ARENA_SEARCH_PROTECT=1`（提交 d5df0d6，下称 p1）：双方的连续守住都按真实成功率算（上面的精确加权）。
- `ARENA_SEARCH_PROTECT=2`：在 1 之上，「没有击杀威胁的首次守住」那一行，贪心列的权重乘 `ARENA_SEARCH_PROTECT_W`（默认 0）；`ARENA_SEARCH_IDLE_THR=99` = 所有首次守住都这样。
- `ARENA_SEARCH_CHAIN_DELTA`（分支 `pk/protect-1011-chain`，b5de59e）：连续守住必须比最好的「不连续守住」选项多出这么多价值才选（0.5 = 只在明显更好的绝境里赌；99 = 基本从不连续守住）。

**测量**（全部对冠军 v4.3；本地模拟是确定性的：今天的 v4.3 和 09-29 的 v4 参照在 P1 / P2 / P3 上 119/120、100/100、60/60 局逐局相同，所以配对很干净；开发种子 113，开发代理 91–93，门槛 7 / 11 / 75 / 83 / 45）：

| 设置 | 局数 | 候选 | v4.3（同一批局） | 配对差（百分点）[95% CI] | 只候选赢 / 只 v4.3 赢 | McNemar p |
|---|---|---|---|---|---|---|
| p2（没威胁的守住不吃贪心列），种子 113 | 120 | 66 | 62 | +3.3 [−4.3, +11.0] | 13 / 9 | 0.52 |
| p2 + 所有首次守住，种子 113 | 120 | 59 | 62 | −2.5 [−11.9, +6.9] | 15 / 18 | 0.73 |
| **p1**，种子 113 | 120 | 68 | 62 | +5.0 [−1.5, +11.5] | 11 / 5 | 0.21 |
| p1，P1 擂台 种子 91 | 120 | 53 | 53 | 0.0 [−7.0, +7.0] | 9 / 9 | 1 |
| p1，P2 VGC-Bench argmax 种子 92 | 100 | 53 | 49 | +4.0 [−0.8, +8.8] | 5 / 1 | 0.22 |
| p1，P3 simbot 同队 种子 93 | 60 | 31 | 25 | +10.0 [−0.1, +20.1] | 8 / 2 | 0.11 |
| **p1，擂台 种子 7** | 400 | **183（45.8%，CI 40.9–50.6）** | 镜像 187 | −1.0 [−4.3, +2.3] | 20 / 24 | 0.65 |
| p1，擂台 种子 11 | 200 | 113（56.5%，CI 49.6–63.2） | 镜像 113 | 0.0 [−4.8, +4.8] | 12 / 12 | 1 |
| p1，VGC-Bench argmax 种子 75 | 200 | 116 | 116（09-29 参照） | 0.0 [−3.9, +3.9] | 8 / 8 | 1 |
| p1，simbot 同队 种子 83 | 100 | 45 | 47 | −2.0 [−10.8, +6.8] | 9 / 11 | 0.82 |
| p1，回归面板 种子 45（5 × 60） | 300 | 298 | 298 | 0.0 | 0 / 0 | 1 |
| chain_delta 0.5（+ p1），种子 113 / 91 | 120 / 120 | 68 / 53 | 62 / 53 | +5.0 / 0.0 | 10 / 4、9 / 9 | 0.18 / 1 |
| chain_delta 99（+ p1，从不连续守住），种子 113 / 91 | 120 / 120 | 67 / 52 | 62 / 53 | +4.2 / −0.8 | 10 / 5、8 / 9 | 0.30 / 1 |
| chain_delta 99（+ p1），擂台 种子 7（只作参考） | 400 | 190（47.5%，CI 42.7–52.4） | 镜像 187 | +0.8 [−2.7, +4.2] | 26 / 23 | 0.78 |

- 全部对局：0 非法、0 次被计时器代打、0 判负、0 出错、0 次搜索退回。p1 每次搜索平均 1432 ms（v4.3 1227 ms，种子 7），每局最慢一步 p99 3.80 s、最慢 3.91 s（安全网约 3.9 s，没有超过 5 s 的）。
- 行为确实变了，但胜负没变：p1 让我方**更常**连续守住（种子 7 每局 0.71 次，v4.3 0.65；种子 91 0.78 对 0.53），失败也更多；chain_delta 99 把连续守住几乎降到 0（每局 0.02–0.07 次；种子 7 上 0.04 次，v4.3 0.65 次；每局守住 2.56 次，v4.3 3.12 次），胜率仍和 v4.3 一样（种子 7 配对 +0.8）。也就是说连续守住多半出现在已经落后的局面，去掉它不会把输局翻成赢局。
- 7 局真实输局的反事实：v4.3 的 29 格守住里，p1 / p2 仍选 23 格「有威胁的守住」；p1 的连续守住 5 次，chain_delta 0.5 留 3 次（都是残局赌命），chain_delta 99 留 0 次。

**结论**：没有一个变体过本次的上线标准（种子 7 的 95% CI 下限 > 50%；外部对手合计配对差 ≥ 0）。p1 在种子 7 是 45.8%，而 v4.3 自己在同一批局上是 46.8%（这批局本身对候选座位不利），配对 −1.0、不显著；种子 11 / VGC-Bench / 回归面板完全持平，simbot −2.0（外部合计 −0.7，不显著但 < 0）。所以三种修正对冠军类对手都**没有可测的强弱变化**，按规则**不合入**；开关、诊断表（`diag`）和测试只留在分支上。以后如果想要「少连续守住」这种更像人的行为，可以直接用 `ARENA_SEARCH_PROTECT=1 ARENA_SEARCH_CHAIN_DELTA=99`（实测不变弱），但它不会让 agent 变强。真正的弱点在「落后局面怎么打」和搜索本身的噪声（同一局面换随机种子 47% 的答案会变），不是守住本身。

运行记录：`/home/linmengfdm/protect_runs/`（每局 jsonl + 协议日志 + 配对统计），诊断数据 `/home/linmengfdm/protect_diag/`。用过的种子：开发 113（新登记）；开发代理 91–93；门槛 7、11、75、83、45。

### v4.5（红警经济优先，10-11 到 10-12，分支 `ra/econ-1011`，冠军 = bb70a54；宝可梦、狼人杀不变）

**为什么改**：10-11 主人的正式 agent（v4.4-draft = main b95a20f）在 Testing 打了 4 局真实红警：自对弈 1 局（833b841a），对 "Steve Nash Equilibrium" 1 胜（52a77fb1，16 分钟，战车工厂 235 s，最大军队 21,700，K / D 64,500 / 14,700）1 负，对 "Minion" 1 胜（e310c9e1，6 分钟）。输的那局（cb4bfc36，sidestep，双方苏军，541 秒基地被拆光，K / D 9,150 / 31,700）在真实平台上重现了 v4.3.1 复审时没修的弱点（对 pro 的「先防后攻」）。名词：**速攻**（rush）= 对方一两分钟就带一大群步兵打过来；**哨兵**（picket）= 放在路上看守的少量部队；**K / D** = 我方打掉的敌方价值 / 我方损失的价值；**配对比较** = 同一批种子（同样的地图、座位、对手随机数）上新旧两版各打一遍，只看结果不同的局（「新版好 / 差」），p 是这些局上的精确符号检验；**调参种子** = 改代码时看过的种子，**新种子** = 改完以后才第一次用的种子。

**诊断**（哪条规则；证据：录像 54 个画面 + 模拟器复现）：
- 开局默认一直造步兵到 20 个（`production._infantry` 的 `max_infantry`），见到敌人之前就开始：62 秒已经 13 个（1,300 块），第一个矿厂刚好。
- 61 秒侦察兵死在 (56, 50)，离对方出生点 (74, 53) 18 格（对方路上放了 1 个火箭兵 + 1 个步兵）。`army.refresh` 两条规则同时触发：「离对方出生点 12 格以外被打死 → 速攻要来了」（`rush`）和「150 秒前被打死 → 对方家里已有部队」（`scout_lost_early`）。
- `rush` → 2 座火焰塔、步兵上限 22、不给关键建筑留钱、建造队列只放行电厂 / 兵营 / 矿厂（钱 < 1500 时）。`rush` 60 秒没威胁就解除，但 `scout_lost_early` 让「先防后攻」（`defense_first`）一直开到 240 秒（步兵不到 36 个就一直开），战车工厂继续被挡、步兵上限 36。结果现金 90–330 秒一直是 0，第二个矿厂 184 秒才好（之前只有 1 辆采矿车），战车工厂 240 秒才开工、281.6 秒好，第一辆重坦约 400 秒。
- 195 秒「我方 ≥ 3 倍见过的敌军」（只见过 2 个单位）让 19 个步兵出击，在对方哨兵处丢了 11 个；387 秒第二波（以步兵为主）撞上重坦 + 火箭兵；428 秒对方 3 辆重坦 + 6 火箭兵 + 9 步兵推进，我方全灭，440–470 秒 5 辆采矿车全被打掉。
- 模拟器里对 pro 输的局过程相同（侦察兵 87 秒死在 pro 家里 → 36 个步兵、战车工厂约 180 秒）。
- 把录像自己的画面喂给新旧两版（`unit_lost` 事件带侦察兵死的位置）：v4.4 在 62 秒打开 `rush` 和 `defense_first` 并继续排步兵；v4.5 判为「对方半场的哨兵」，不开 `defense_first`，战车工厂之前不再加步兵。

**改了什么**（全部在 `agent/redalert/`，每条都有 `ARENA_RA_*` 开关，`ARENA_RA_ECON_FIRST=0` 回到 v4.4 的开局；`ARENA_RA=off` 仍是整场 `WAIT`；细节见 `docs/redalert_design.md` 第 16 节）：
1. **经济优先开局**：侦察兵死在对方半场不算速攻；战车工厂之前步兵最多 6 个（真被打——速攻信号或家里威胁 ≥ 800——时解除）；「早期敌军」（侦察兵 120 秒前死在对方家里 12 格内、在对方家里看到 ≥ 700 的部队、或 150 秒前看到 2 座兵营）只换来 +6 个步兵和 2 座塔（塔先拿钱），开局顺序不变；战车工厂一好每个矿厂 2 辆采矿车；第三个矿厂 6:00 前要钱 > 2500；钱 > 1200 时提高步兵上限；安全塔（150 s 1 座、210 s 2 座）；打掉的敌军从「见过的最大敌军」里减掉。
2. **盲区先验**：150 秒没看过对方的家就算「看不见」，出击要打得过 25 ×（秒 − 90）、最多 9000 的假设敌军（没看到的部分按 30% 步兵 / 15% 火箭兵 / 55% 主战坦克算）；看不见时每 90 秒派一个便宜单位去看；「3 倍见过的敌军就出击」要求攻击组 ≥ 2000。
3. **采矿车保护**：被打的采矿车开回矿厂（矿厂在敌人那边就往反方向开），家里没被打时附近的空闲部队过去。
4. **按兵种克制比较军队**（新文件 `agent/redalert/counters.py`：兰彻斯特平方律 + 引擎武器表），用在出击判断和交战撤退判断里。
5. **两个 bug**：（a）采矿车全灭时（`harv_emergency`）家里正被打，继续排的步兵 / 塔和新采矿车分最后的钱，两样都卡在半截、一直没有收入——经济优先时正在造的步兵也取消退钱，钱不够造采矿车时不再排新步兵和塔；恢复测试（种子块 7100）「一直没有采矿车」6 局 → 0 局。（b）**原来就有的整批被拒竞态**：一批里全是 3 个以上单位的 `attack_move`、平台检查时这些单位都死了，这批没带「锚」→ `INVALID_ACTION`；现在每批都带锚。命令延迟 1 秒的压力测试（`--latency-ticks 25`，种子 3325，48 局）：v4.4 2 局有整批被拒，v4.5 修之前 3 局，修之后 0 局；锁步对局里带不带锚逐局相同（main311 48 / 48、strong314 60 / 60、pro317 120 / 120）。
6. 新的模拟器对手 `nash`（`local/ra_bots_strong.py`，按这局真实输局写的脚本），对局总结行新增 `rescouts`、`harv_pulls`、`harv_helps`、`early_army`；新测试 9 个（`tests/redalert/test_policy.py`），`tests/redalert/test_routing.py` 放行锚命令。

**过程**（调参只用调参种子 501–507；每轮测量都和 v4.4 配对）：
- 调参种子：501 对 pro 120 局、502 强对手联赛 60、503 主联赛 48、504 对 tanks 60、505 对 swarm 60、506 对 rusher 60、507 对 nash 60（v4.4：101 / 58 / 47 / 57 / 60 / 60 / 56）。逐步加改动后 501 从 104 → 111（只算「看过对方的家」才算看见）→ 116（每矿厂 2 采矿车、没看到的敌军按典型兵种）→ 118；先验上限 6000 时 507 只有 47 / 60，9000 时 55 / 60。试过没采用：先验减去最近 3 分钟打掉的价值（501 116 → 107、507 47 → 34）；早期敌军时先造 2 辆坦克（501 116 → 111、507 55 → 51）。
- 第一轮测量（f1，提交 d34b034，新种子 61–63 / 71–73 主联赛、81 / 191 / 82–84 强对手、85–87）：主联赛 282 → 280，强对手 289 → 296，pro 210 → 227，nash87 103 → 102。main62 的两局输局（62043 对 llm、62005 对 rusher）引出 b3a85ce 的两处小改动，所以 f1 的种子从此算「看过的」。
- 第二轮（f2，提交 b3a85ce，新种子 91–99）：强对手 3 组 170 → 180、pro97 / 98 110 → 119、112 → 119，但 **nash99 107 → 95（p 0.012，显著变差）**，恢复测试「采矿车全灭」39 → 35、6 局一直没有采矿车 → 停下。原因：nash 路上的 4 人哨兵（800）触发了「早期敌军」，多出的塔让原本能赢的局输掉（f1 的代码在这批输局里 5 局赢 4 局）；另一个是上面的 bug（a）。改成只算对方家里的部队、修 bug（a）→ 提交 09daea2，种子 99 上 105 / 120。然后在第三批新种子上重测。
- 第三轮（f3，提交 09daea2 的决策 = bb70a54，因为锚不改变锁步对局）：下表。

**第三轮新种子（311–325，改代码时都没看过）**（括号里是 95% Wilson 区间；「配对差」括号里是 95% 区间半宽）：

| 场景 | 局数 | v4.4（main b95a20f） | v4.5 | 配对差（百分点） | 新版好 / 差 | p | 第一个战车工厂中位 s |
|---|---|---|---|---|---|---|---|
| 主联赛 311（我方盟军） | 48 | 47（97.9%，89.1–99.6） | 47（97.9%，89.1–99.6） | +0.0 | 0 / 0 | 1 | 178.8 → 152.0 |
| 主联赛 312（我方苏军） | 48 | 46（95.8%，86.0–98.8） | 46（95.8%，86.0–98.8） | +0.0（±5.8） | 1 / 1 | 1 | 179.1 → 153.4 |
| 主联赛 313（阵营随机） | 48 | 47（97.9%，89.1–99.6） | 47（97.9%，89.1–99.6） | +0.0（±5.8） | 1 / 1 | 1 | 179.3 → 151.8 |
| **主联赛合计** | 144 | 140（97.2%，93.1–98.9） | 140（97.2%，93.1–98.9） | +0.0（±2.7） | 2 / 2 | 1 | 179.1 → 152.4 |
| 强对手 314 | 60 | 58（96.7%） | 59（98.3%） | +1.7（±3.3） | 1 / 0 | 1 | 180.9 → 153.3 |
| 强对手 315 | 60 | 57（95.0%） | 59（98.3%） | +3.3（±6.5） | 3 / 1 | 0.63 | 180.1 → 152.5 |
| 强对手 316 | 60 | 57（95.0%） | 60（100%） | +5.0（±5.6） | 3 / 0 | 0.25 | 182.1 → 151.7 |
| **强对手合计** | 180 | 172（95.6%，91.5–97.7） | 178（98.9%，96.0–99.7） | +3.3（±3.0） | 7 / 1 | 0.070 | 180.8 → 152.5 |
| pro 317 | 120 | 106（88.3%，81.4–92.9） | 117（97.5%，92.9–99.1） | +9.2（±6.1） | 13 / 2 | **0.0074** | 181.7 → 157.3 |
| pro 318 | 120 | 108（90.0%，83.3–94.2） | 115（95.8%，90.6–98.2） | +5.8（±6.3） | 11 / 4 | 0.12 | 182.1 → 161.5 |
| **pro 合计** | 240 | 214（89.2%，84.6–92.5） | 232（96.7%，93.6–98.3） | **+7.5（±4.4）** | 24 / 6 | **0.0014** | 181.9 → 157.9 |
| nash 319 | 120 | 107（89.2%，82.3–93.6） | 100（83.3%，75.7–88.9） | −5.8（±6.7） | 5 / 12 | 0.14 | 221.3 → 146.4 |
| nash 320 | 120 | 104（86.7%，79.4–91.6） | 99（82.5%，74.7–88.3） | −4.2（±7.5） | 8 / 13 | 0.38 | 221.0 → 146.0 |
| **nash 合计** | 240 | 211（87.9%，83.2–91.5） | 199（82.9%，77.6–87.2） | **−5.0（±5.0）** | 13 / 25 | 0.073（不显著） | 221.1 → 146.2 |
| 网络不稳 5%（321） | 24 | 24 | 22 | −8.3（±11.3） | 0 / 2 | 0.5 | |
| 1% 命令丢失（322） | 24 | 24 | 23 | −4.2（±8.2） | 0 / 1 | 1 | |
| 5 秒窗口模式（323） | 12 | 12 | 12 | +0.0 | 0 / 0 | 1 | |
| 开局资金 10000（324） | 24 | 23 | 24 | +4.2（±8.2） | 1 / 0 | 1 | 104.0 → 103.9 |
| 放置上限 8（325） | 12 | 12 | 11 | −8.3（±16.3） | 0 / 1 | 1 | |
| **压力场景合计** | 96 | 95（99.0%） | 92（95.8%） | −3.1（±4.5） | 1 / 4 | 0.38 | |

按对手合计（新种子，不含压力场景）：pro 276 局 242 → 266（+8.7，p 0.0001）；nash −5.0（见上）；hider、idle、llm、raider、swarm、tanks 各 36 局都是满分不变；turtle 33 → 34；rusher 35 → 34。压力场景里输的 4 局（321 两局、322、325）都是对 rusher。

**参照种子（以前用过的 61–63、81、191，以及前两轮测量看过的 85–87、97–99；最终代码重跑，v4.4 结果和前两轮同一份）**：主联赛 144 局 141 → 140（2 / 3）；强对手 120 局 115 → 118（5 / 2）；pro 480 局 432 → 468（+7.5 个百分点，46 / 10，p 1.3 × 10⁻⁶；85 / 86 / 97 / 98 分别 +5.0、+11.7、+7.5、+5.8）；nash 240 局 210 → 211（nash87 103 → 106、nash99 107 → 105）；主联赛里对 rusher 36 → 33（−8.3，0 / 3，p 0.25）。把主联赛各组（新种子 + 压力 + 参照）里对 rusher 的 96 局放在一起：95 → 88（1 / 8，按对手切片后 p 0.039）——经济优先在 2:30–3:00 有 36 个步兵冲进来时只有 12 个步兵 + 2 座塔，模拟器里挡不住；这是已知弱点，记在设计稿第 16 节第 9 条（想法：早期敌军时步兵上限 6 → 12，或早放哨兵；没做）。

**其余检查**（新种子）：
- 自对弈（种子 326，24 局）：v4.5 对 v4.5 A 方 13–11；v4.4 对 v4.4 10–14。**v4.5 对 v4.4 直接对打**（种子 327，48 局）：30–18（62.5%，95% CI 48–75%）。实时时钟（种子 328，speed 8，各 4 局）：都 4 胜，0 整批被拒。
- 恢复测试（10-08 复审的写法：300 秒打掉全部采矿车 / 电厂，120 秒打掉建造厂；一半局同时清零钱；4 种对手 × 3 张图 × 2 个座位）。新种子块 8100 / 8200 / 8300：采矿车 39 → 37（1 / 3，p 0.63；重新有采矿车 43 → 46 局，中位都是 32 s，一直没有 2 → 2 局）、电厂 45 → 45（2 / 2；恢复供电 41 → 44 局，中位 45 → 57 s，一直没恢复 1 → 0）、建造厂 23 → 19（1 / 5，p 0.22，不显著）。复审用过的块 7100 / 7200 / 7300：39 → 36（一直没有采矿车 1 → 0）、45 → 47、20 → 20。
- 健康：所有联赛、压力、恢复场景我方 **0 个整批被拒**、0 次 runner 报错 / 卡死、0 个异常；命令被拒 0.03–0.08%（放置上限场景是设计上的 `invalid`：v4.4 150 条，v4.5 269 条）；锁步决策耗时 p99 中位 1.8–2.1 ms、最高 2.9 ms（v4.4 2.3–3.8 ms），最慢一帧 80 ms（v4.4 同批 76 ms）；5 秒窗口模式 p99 最高 27.4 ms（v4.4 同批 10.7 ms；v4.3 的记录是 31 ms），一次决策要处理 5 秒的变化。

**检查（bb70a54，`~/wt/_ra2_gate4`）**：全量 pytest **2256 个全过**（837 s）；冻结冠军后再跑一遍也是 **2256 个全过**（839 s）。`replay_real.py`：2349 个真实决策 **0 失败**、0 修补，搜索用上 1296 / 1296，p99 1712.5 ms、最慢 2056 ms；`--no-calc` 同样 0 失败、0 修补，p99 1663.0 ms、最慢 1988 ms。`selfcheck.py` ready（只有没配 key 的 1 个警告）。压缩包 `arena-agent-bb70a54`（46 MB / 432 个文件，密钥扫描干净；`agent/`、`altruagent/`、`data/`、`deploy/`、`run_agent.sh`、`selfcheck.py` 和工作区相同）：干净环境 selfcheck ready，包里重放 60 个决策 0 被拒，格式不对的假 key `--check-tournament` 退出 1。**红警端到端**（同一个包的整棵进程树：`run_agent.sh` → `python -m agent --tournament --match` → 每座位一个 worker，本地模拟平台 + 红警模拟器，对手 rusher，每场 300 游戏秒，实时时钟；自己的名字 `ra-e2e`、日志目录、锁目录和假 key）跑了两次：2 + 2 场红警都打满 300 s 并赢（平局规则），76 / 107、74 / 106 个命令批，**0 个不合法动作**，被拒 0 / 0、1 / 0 条，`exceptions=0`，新总结字段都在（`early_army=1`、`harv_pulls=3` 等）；宝可梦、狼人杀照常；0 次 no-show、key 没进日志、0 残留。两次日志里各有 5 个 Traceback，都是宝可梦选秀的已知回退（`agent/pokemon/drafter.py` 第 660 行 `StopIteration`，模拟平台的选秀顺序和蛇形顺序对不上时 `_opp_weight` 没接住，打出 "drafter failed; keeping the card_score pick" 后用卡牌分数选）；`agent/pokemon/` 和 main b95a20f 逐字节相同，真实对局日志里一次都没有，和本分支无关（另记一个小 bug）。更早的 be55dab 包上端到端出现过 1 次整批被拒，就是上面 bug（b）。

**结论**：按事先定的标准上线——对 pro 显著变强（新种子 +7.5 个百分点，p 0.0014；参照种子 p 1.3 × 10⁻⁶），强对手联赛 +3.3，主联赛持平，没有哪个场景显著变差；nash（模拟器里的真实输局脚本）新种子 −5.0、不显著（v4.4 在模拟器里靠 180 秒 30–36 个步兵出击赢它 87%，而真实对局里同样的早期出击输了，所以模拟器复现不了真实输局；真实输局的开局问题由录像回放确认已改掉：战车工厂 282 s / 模拟器 180 s → 约 150–160 s，战车工厂前步兵 ≤ 6）。已知弱点：纯步兵速攻（rusher）。**只有真实对局能确认的**：碾压 / 溅射下步兵对坦克到底多吃亏；经济优先在 3–5 分钟家里兵少，真实对手会不会抓这个空当；盲区先验对真实对手是否太保守。上线后请看 `[match-summary] … game=red_alert` 行的 `first_weap_s`（应在 150–200 s）、`early_army`、`rescouts`、`invalid_action=0`。
- **冻结为冠军**：`champion/` = bb70a54 的 `agent/`。
- 用过的种子：调参 501–507、99（第二轮后的修正）、3325（延迟压力）；测量 f1：61–68、71–73、81–87、89、191；f2：91–99、165；f3：311–328（新）+ 上面的参照；恢复测试 7100 / 7200 / 7300（复审）、8100 / 8200 / 8300（新）。运行记录在本机 `~/wt/_ra2_out`（每组 JSON + 日志）、冻结的代码快照在 `~/wt/_ra2_snap`。

### 宝可梦真实对局复盘（10-12，分支 `pk/real-1012`，**没有上线的改动**，v4.5 不变）

数据：25 局对其他参赛者，18 胜 7 负（LeCharmander 12-7、Steve Nash Equilibrium 3-0、omarcanoc23 2-0、Ace Trainer Claude 1-0），自对弈不算。分析工具在分支的 `scripts/real_opp*`（把 `REAL_LOGS` 指向 main 的 `logs/`）。

- **输在开打之前**：7 场输局全是 10-08 的 v4.3 选秀。首发模型估的阵容价值（−1 到 +1）赢局平均 +0.29、输局 +0.03；我方选的首发对上对方实际首发，赢局 +0.68、输局 +0.05；输局里 5/7 先倒下的是我方（赢局 8/18）。现在的 v4.4 选秀在这 7 场里每一场都会选得不一样（从第 2 到第 8 手开始），而且能逐手复现 v4.4 上线后的 3 场。
- **队伍预览已经是最好的简单规则**：对上对方实际首发，我们的均衡选法 +0.50，最大最小 +0.36，对均匀对手的最优应对 +0.42，「对方首发前两张选的牌」先验 +0.36 ~ +0.40。LeCharmander 的首发不可预测（19 场里只有 4 场首发前两张）；弱 bot 爱首发前两张（4/6），但我们本来就 6-0。
- **对手习惯**（LeCharmander 310 个动作）：攻击 65.5%、守住 15.8%、换人 11%、变化招 7.7%；80% 打属性上伤害最高的目标；被打后马上守住 20/49 次，连续两次守住只有 2/49；真实对手有击掌奇袭时首回合只用了 10/21 次；弱 bot 几乎不换人（1/48）。本地冠军和 VGC-Bench 的动作分布和它差不多，所以本地面板能代表真实对手。
- **对战里的对手模型**：重放真实对局能复现 233/234 个决策。搜索里的「均衡对手」按动作类型和真实对手相符；「贪心对手」99% 在攻击，所以现在 50/50 混合预计对方守住 8.6%，真实是 16%。最能预测 354 个真实对手动作的混合权重是 0.35（LeCharmander 0.30）。押注「对方贪心」的打法有迹可循：49 次主动换人里 15 次换上来就掉了 40% 以上的血、5 次直接倒下；突袭（Sucker Punch）落空 6 次；每局约 2 次打在守住上。
- **搜索的随机性**：换一个随机种子，约 50% 的回答会变（2 次和 4 次抽样都一样，232 个决策）；分歧主要来自早期剪枝（33 个分歧里只有 9 个候选行相同）。
- **其他失误**：第 2 回合两只同时换人吃了月亮之力（5e0dea7d）；1% 血的古简蜗换上来吃地震（fa7c1a36）；对结果没影响的位置做无用动作（满血自我再生、对睡着后又醒来的目标击掌奇袭）。

**筛选**（只用开发种子 91 / 92 / 143，每个候选和同一工作树里开关关掉的基线配对；全部 0 非法、0 计时器代打、0 判负，最慢一步 4.2 s；候选那几组跑时机器负载约 10，因为红警在跑模拟）：

| 候选 | 对手 | 局数 | 候选 / 基线 胜 | 只有候选赢 / 只有基线赢 | 配对差 [95% CI]，p |
|---|---|---|---|---|---|
| blend 权重 0.35 | 冠军 种子 91 | 120 | 60 / 60 | 8 / 8 | 0.0 [−6.6, +6.6]，1.0 |
| blend 权重 0.35 | meta 选秀者 种子 143 | 120 | 83 / 81 | 9 / 7 | +1.7 [−4.9, +8.2]，0.80 |
| blend 权重 0.35 | VGC-Bench argmax 种子 92 | 100 | 60 / 59 | 4 / 3 | +1.0 [−4.2, +6.2]，1.0 |
| blend 权重 0.2 | 冠军 种子 91 | 120 | 58 / 60 | 12 / 14 | −1.7 [−10.0, +6.7]，0.85 |
| blend 权重 0.2 | meta 选秀者 种子 143 | 120 | 84 / 81 | 11 / 8 | +2.5 [−4.6, +9.6]，0.65 |
| 每格抽样 4 次 | 冠军 种子 91 | 40（提前停） | 18 / 24 | 2 / 8 | −15 [−30, 0]，0.11 |

**结论**：没有一个候选在本地测出提升，按门槛（种子 7 / 11 下限 > 50%）大概率不过，截止前不跑门槛，v4.5 保持不变。权重 0.35 最贴合真实数据，但本地是持平；另外两个新开关（`ARENA_SEARCH_OPP_SWITCH_W`、`ARENA_PREVIEW_ROSTER_W`）在拟合上就没有收益，没筛。赛后可以做：稳定早期剪枝；继续从选秀下手（输局都从选秀开始）。
### v4.6（红警采矿车优先，10-12，分支 `ra/harv-1012`，基于 main fd4cf58 = v4.5；宝可梦、狼人杀不变）

**为什么改**：真实红警对局（10-08 到 10-11，6 局全部在 sidestep，开局资金 5000）里我们 2:00 以后现金一直是 0，5–6 分钟以前只有 2 辆采矿车（一局到 10 分钟）。研究报告（引擎数据 + 人类打法）建议「先补采矿车」：一辆 1100，按真实录像一分钟运回 1500–1700，不到一分钟回本。名词：**采矿车**把矿运回**矿厂**（`proc`，自带一辆）变成钱；**排队**（serialization）= 钱不够时先让采矿车造完，步兵和塔先不开工；**配对比较 / p**：同一批种子上新旧两版各打一遍，只看结果不同的局（新版好 / 差），p 是这些局上的精确符号检验；**逐局相同** = 结果和对局时长都一样。

**先校准模拟器的采矿**（`calib.py`，只读真实录像 `logs/<session>-*.jsonl`）：用同一个办法（「已投入的财富」= 现金 + 存矿 + 建筑和单位造价 + 生产队列里已付的部分 + 损失 − 每座矿厂白送的采矿车；两帧之差 = 这段时间的收入）算真实录像 644b0063、cb4bfc36、52a77fb1 和模拟器里同一张图（sidestep）的对局：
- 2 辆采矿车、矿区刚开采：真实每辆每分钟 1500–1730，模拟器 1400–1900（一趟 17–20 秒 = 20 包 × 25）；3–5 辆时真实 1100–1300，模拟器 1000–1500。**一致**。
- 家门口矿区总量：真实约 3.3–3.5 万（含再生）以后收入下降（670–740 秒，2–5 辆车），模拟器约 2.8 万（430–490 秒，6 辆车）。引擎（OpenRA `ResourceLayer`，红警打开了 `RecalculateResourceDensity`）初始密度 max(12 × 相邻矿格 / 9, 1)，内部 10；模拟器统一 8；矿井再生两边相同（每 75 tick 一格）。**量级一致**。
- 不一致：矿采完后模拟器的采矿车挤在同一个再生格上（每分钟 0–500），真实还有 1200–2700。模拟器对「很多采矿车」在后期偏苛刻，对这次改动是保守的。**没有改模拟器**。

**改了什么**（新文件 `agent/redalert/harvest.py`；细节见 `docs/redalert_design.md` 第 17 节；每项都有开关，`ARENA_RA_HARV_FIRST=0 ARENA_RA_IDLE_HARV_FIX=0 ARENA_RA_DEPLOY_PENDING_TICKS=0` = v4.5）：
1. **采矿车优先**（`harv_first`）：战车工厂建成后采矿车目标 4（3:00）→ 6（4:30）→ 8（6:30），上限按家门口的矿（sidestep 8、trapped 7、shadowfiend-ii 10）；目标超过每座矿厂 2.5 辆时加矿厂（第三座在维修厂之前、第四座在之后），放在每座矿厂分到矿最多的矿田边；钱不够时排队（步兵 ≤ 6、不造威胁以外的塔），家里有威胁（≥ 300）、采矿车 / 建筑挨打、我方半场看到 ≥ 600 的敌军就停止排队。
2. **只在路远的地图上**（`harv_min_path` = 70 格）：两个出生点之间的地面路径 sidestep 55、trapped 57、shadowfiend-ii 92。**短图上和 v4.5 逐局相同**。理由：敌人走得远，贪经济才安全；调参种子上 sidestep 新版好 1 / 差 5（v4.5 在 sidestep 上常常 6–8 分钟一波打赢 nash，采矿车优先把这一波推迟了），shadowfiend-ii 9 / 4。
3. **早期敌军 / 速攻迹象**（v4.5 的 `rush`、`early_army`、`two_barracks`）7:00 前出现过一次，这一局就关掉，v4.5 逐局接管（有测试）。
4. **两个 bug 修复**：（a）`idle_harv_fix`：建造厂没了以后，被采矿车保护拉回来的空闲采矿车永远不再去采矿（调参种子 602053：8 辆采矿车、390 块钱停了 7 分钟），v4.5 就有；（b）`deploy_pending_ticks`：平台已接受的展开基地车命令 15 秒内不再重发（v4.5 是 4 秒）。全量 pytest 里的假平台测试（25 倍速、机器忙时）出现过 1 个整批被拒（`INVALID_ACTION`）：画面落后，4 秒后还看到基地车，就单独重发了展开命令（还没有建筑可以带「锚」），而基地车已经变成建造厂 → `not_owned`。v4.5 的代码在同样负载下也会失败（2 次里 1 次）。没被接受 / 被拒的展开命令仍按 4 秒。
5. 对局总结行新增 `harv_180` / `harv_270` / `harv_390`（3:00 / 4:30 / 6:30 时的采矿车数）、`serial_s`（排队秒数）。
6. 写了、默认关的：采矿车分配（`harv_spread`：空闲车去每辆分到矿最多的矿田、一座矿厂边超过 3 辆调走一辆；444 局 13 / 10，和不开 13 / 11 一样）、钱多时第二座战车工厂（`harv_weap2`；shadowfiend-ii 384 局 26 / 10，不开 25 / 9）。

**过程**（调参种子：601–608 三图联赛、611–616 只有 shadowfiend-ii、901–906 校准；最终测量的新种子 711–734 和恢复测试种子块 9100 / 9200 / 9300 在测量前没看过）：
- h2（三张图都用、不看早期敌军）：444 局新版好 16 / 差 11；差的里面 6 局是「看到早期敌军还先造采矿车和第三座矿厂，4 分钟对方进攻时只有步兵、没有坦克」（逐局回放 601006、604015、605005、606038）→ 第 3 条。
- h5：13 / 11，按地图 shadowfiend-ii 9 / 4、trapped 3 / 2、sidestep 1 / 5 → 第 2 条。
- h9（shadowfiend-ii 384 局）：26 / 10（p 0.011），nash 90 局 66.7% → 85.6%（24 / 7）。回放 614034 发现关掉以后 v4.5 的第三座矿厂规则也被关了 → 修好（第 3 条的「v4.5 逐局接管」）。之后不再调参。

**最终测量**（提交 3972c67 的决策 = df0681e，见下面「逐局相同」；对 v4.5 = main fd4cf58 配对；括号里是胜率）：

| 场景 | 局数 | v4.5 | v4.6 | 配对差（百分点） | 新版好 / 差 | p | 逐局相同 |
|---|---|---|---|---|---|---|---|
| 主联赛 711 / 712 / 713（盟 / 苏 / 随机） | 144 | 139（96.5%） | 139（96.5%） | 0 | 0 / 0 | 1 | 132 |
| 强对手 714 / 715 / 716 | 180 | 180（100%） | 180（100%） | 0 | 0 / 0 | 1 | 167 |
| pro 717 / 718 | 240 | 234（97.5%） | 234（97.5%） | 0 | 0 / 0 | 1 | 237 |
| nash 719 | 120 | 101（84.2%） | 108（90.0%） | +5.8 | 9 / 2 | 0.065 | 80 |
| nash 720 | 120 | 104（86.7%） | 110（91.7%） | +5.0 | 10 / 4 | 0.18 | 80 |
| rusher 721 | 60 | 59（98.3%） | 59（98.3%） | 0 | 0 / 0 | 1 | 60 |
| **三图合计** | 864 | 817（94.6%） | 830（96.1%） | **+1.5** | **19 / 6** | **0.015** | 756 |
| 　其中 shadowfiend-ii | 288 | 87.5% | 92.0% | +4.5 | 19 / 6 | 0.015 | |
| 　其中 sidestep / trapped | 576 | 98.1% | 98.1% | 0 | 0 / 0 | 1 | |
| 只 shadowfiend-ii：nash 730 | 120 | 90（75.0%） | 104（86.7%） | **+11.7** | 23 / 9 | **0.020** | 0 |
| 只 shadowfiend-ii：pro 731 | 120 | 118（98.3%） | 117（97.5%） | −0.8 | 0 / 1 | 1 | 119 |
| 只 shadowfiend-ii：强对手 732 / 主联赛 733 | 108 | 107 | 107 | 0 | 0 / 0 | 1 | 84 |
| **按对手：nash（三图 + 只 shadowfiend-ii）** | 360 | 295（81.9%） | 322（89.4%） | **+7.5** | **42 / 15** | **0.0005** | |
| **全部（不含压力）** | 1212 | 93.4% | 95.5% | **+2.1** | **42 / 16** | **0.0009** | |
| 压力：网络不稳 5% / 1% 命令丢失 / 5 秒窗口 / 资金 10000（主联赛、强对手）/ 放置上限 8 | 126 | 119 | 119 | 0 | 0 / 0 | 1 | 114 |

其余对手（hider、idle、llm、raider、swarm、tanks、turtle）合计逐局结果都一样。对 rusher（已知弱点）132 局 0 / 0；对 pro 414 局 0 / 1（不显著）。
- **v4.6 对 v4.5 直接对打**（种子 728，48 局）：23–25（48%，95% CI 34–62%；sidestep 6–10、trapped 9–7 是同一个 bot 互打，shadowfiend-ii 8–8）。实时时钟（种子 729，speed 8，各 4 局）：都 4 胜，0 整批被拒。
- **恢复测试**（10-08 复审的写法，新种子块）：采矿车全灭 9100 39 → 39、电厂全灭 9200 46 → 46、建造厂 9300 17 → 17，结果逐局一样；0 异常、0 整批被拒。
- **v4.6 两个修复不改变锁步对局**（df0681e 对 3972c67）：df0681e 对 3972c67 在最终测量的 4 组种子上逐局相同（主联赛 711 48 / 48、pro 717 120 / 120、nash 719 120 / 120、只 shadowfiend-ii 的 nash 730 120 / 120，共 408 局）；命令延迟 1 秒的压力（`--latency-ticks 25`，种子 734，48 局）v4.5 和 v4.6 都 48 胜、双方 0 个整批被拒（44 局逐局相同）。
- 健康：所有测量我方 **0 个整批被拒**、0 次 runner 报错 / 卡死、0 个异常、0 个 Traceback；决策耗时 p99 最高 2.9 ms（压力场景 19.5 ms，v4.5 同批 26.3 ms）。
- 模拟器里 shadowfiend-ii 对 nash 的经济（`calib.py`，调参种子 905 / 906）：430 秒时累计收入 v4.5 约 2.0 万，v4.6 约 3.0–3.2 万；250–310 秒平均采矿车 4.0 → 6.1–6.4 辆；这两局 v4.6 在 9–11 分钟拆光对方，v4.5 要 17 分钟。

**检查**（df0681e，`~/wt/_ra3_gate2`）：全量 pytest **2265 个全过**（899 s）；冻结冠军后再跑一遍：**2265 个全过**（844 s）。`replay_real.py`：2349 个真实决策 **0 失败**、0 修补，搜索用上 1296 / 1296，p99 2730.9 ms、最慢 3317 ms（检查时机器上同时在跑联赛，v4.5 检查时是 1712 ms）；`--no-calc` 同样 0 失败、0 修补，p99 2659.6 ms。`selfcheck.py` ready（只有没配 key 的 1 个警告）。压缩包 `arena-agent-df0681e`（46 MB / 433 个文件，密钥扫描干净；`agent/`、`altruagent/`、`data/`、`deploy/`、`run_agent.sh`、`selfcheck.py` 和工作区相同）：干净环境 selfcheck ready、包里重放 0 失败、格式不对的假 key `--check-tournament` 退出 1。**红警端到端**（同一个包的整棵进程树：`run_agent.sh` → `python -m agent --tournament --match` → 每座位一个 worker，假官方平台 + 红警模拟器，对手 rusher，300 游戏秒，实时时钟）：2 场红警都打满 300 s 并赢（平局规则），75 / 103 个命令批，**0 个不合法动作**，被拒 2 / 0 条，`exceptions=0`，新字段都在（对 rusher 早期敌军迹象让采矿车优先关掉：`harv_180=2`、`serial_s=0.0`）；Traceback 5 个都是已知的宝可梦选秀回退（`drafter.py` 第 660 行），其他 0 个；0 个残留进程。第一次检查（3972c67）的全量 pytest 有 1 个失败（`test_fake_platform_red_alert_match_against_a_rusher_over_mcp[on]`，`invalid_actions` 1），就是上面修复 (b) 的展开基地车竞态，v4.5 的代码在同样负载下也会失败。

**结论**：按事先定的标准可以上线——对经济型对手 nash 显著变强（新种子三图 +5.4，只 shadowfiend-ii +11.7，合计 360 局 +7.5 个百分点，p 0.0005），全部新种子 +2.1（p 0.0009），没有哪个对手变差（rusher 逐局相同，pro 差 1 局不显著）；sidestep 和 trapped 上逐局等于 v4.5。**局限**：真实对局到现在都在 sidestep，那里 v4.6 = v4.5（加两个修复），改动只在 shadowfiend-ii 这种路远的图上起作用；模拟器的 bot 常常触发「早期敌军」（对 pro 几乎每局），这时 v4.6 也等于 v4.5；真实对手会不会在 3–5 分钟抓我们兵少的空当，只能在真实对局里看 `[match-summary]` 的 `harv_180` / `harv_270` / `harv_390` 和 `serial_s`。
- 用过的种子：调参 601–609、611–616、901–906（校准、回放）；最终测量 711–734、恢复测试 9100 / 9200 / 9300。运行记录在本机 `~/wt/_ra3_out`，冻结的代码快照在 `~/wt/_ra3_snap`（v45 = main fd4cf58、fin1 = 3972c67、fin2 = df0681e）。
- 后续可做的（没做）：C3「打对方采矿车」（研究报告的第 3 项：4–6 分钟起派快车去对方矿区，见到塔或吃亏就撤），时间不够，没有实现；采矿车优先在短图上的较温和版本（目标晚一些、只在 nash 型对手时）；修改模拟器让矿采完后的采矿车像引擎一样在更大范围找矿。

### 狼人杀研究候选（10-12，分支 `strategy/werewolf-evidence-search`，未晋级）

基于 main `cda2dd9`；冻结测量源码 `cc95bb8`；[草稿 PR #1](https://github.com/Astro-wen/ucla-agent-tournament/pull/1)。细节、复现命令、淘汰方案、完整统计见 [`werewolf_evidence_strategy.md`](werewolf_evidence_strategy.md)、[`werewolf_benchmark.md`](werewolf_benchmark.md)、[`werewolf_confirmation.json`](werewolf_confirmation.json)。

- 修复聊天证据误读、已死亡但未发言的真预言家被忽略，以及预言家先报好人后无法回应晚来的假跳。新增配对评测器，检查源码不变、逐局配对、内部异常和被合法动作掩盖的兜底，并保存带校验和的合成日志。
- 新策略 `ARENA_WW_TACTICS=public_vote`：首日无人跳预言家时，狼的发言和实际投票都模仿村民，即使会投队友；用牺牲队友的风险换掉“绕开队友”的破绽。**默认关闭**。另保留 `no_skip` 中性开场实验，也默认关闭；它有可识别的新句式，不建议晋级。
- 预先保留种子 28101/28102，11 种对手，k=1/3，两种讨论时序，共 369,600 局本地模拟。主要 k=1/rt：public_vote 对冠军总体 **+0.927 个百分点**，当狼 **+2.947**；当狼对 chaos / detective / talker 分别 **+15.35 / +13.54 / +10.61**，但对 llm / llmx / llmq **-2.42 / -5.37 / -3.13**。两组种子方向一致；rounds 当狼 +2.186。全部配对、源码检查通过，0 协议错误、超时、内部异常和兜底；p99≤0.5 ms，最大75.8 ms。
- **没有通过完整晋级门槛**：总体提升不足事先要求的2个百分点，且有对手回归。默认正确性修复也未通过实力门槛（含 chaos/k3 狼人回归）。因此没有更新 champion、合并 main 或切换比赛配置；这是有实证收益和明确代价的可选策略，不是全面变强的结论。
- 本地全量测试 **2418通过、41个平台相关跳过**；冻结实现的 [Linux CI](https://github.com/Astro-wen/ucla-agent-tournament/actions/runs/38344969208) **2458通过、1跳过**；2349个真实决策重放0失败/0修补；自检ready；62局官方runner本地平台检查0错误/卡死/非法动作。真实录像语料没有狼人杀，不能据此宣称真实比赛获胜。大型原始记录在 `logs/werewolf_benchmarks/`，不提交。

### 狼人杀防诱导保护（10-13，同一研究分支，仍未晋级）

生产代码冻结在 `cde8bc2`，测试补充到 `a0bc5ad`；[完整报告](werewolf_guard_strategy.md)和[机器可读结果](werewolf_guard_confirmation.json)。本轮专门检查重复喊票、假跳预言家、矛盾证言、引文/否定句、伪装名字，以及沉默、弃票、固定投票等简单打法。

- 同一个人对同一个目标的重复指控只算一次；矛盾的查验结果不再变成可信信息。已被公开身份推翻的假预言家不能强迫狼对跳、抢占夜间目标或关闭可选策略。保留真正的紧凑查验格式和正常名字，不用隐藏身份或对手标签。
- 新增 9 种简单对手和 15 种「普通桌混入一个攻击者」场景，与原来 11 种对手一起测。先登记新种子 29301/29302，再冻结代码，默认/可选策略、两种时序、新旧代码共 **336,000 局**。独立复核全部原始记录和 **4,688 个统计切片**，逐局身份、座位、冠军结果及源码校验通过；0 协议/策略错误、超时、异常和兜底。决策 p99≤1 ms，当前策略最慢44.571 ms。
- 防护后，指控一次和重复五次在 first/heuristic 桌上逐局结果相同。对「heuristic + 一个重复喊票者」，村民胜率 **+6.88 个百分点**（实时，465 对，保守95%区间 [+2.66,+10.95]），轮次模式 **+8.85**；均通过560次比较的保守校正。**代价：同一实时场景当狼 −4.26 个百分点**（235 对，区间 [−8.29,−0.05]），不隐去该回归。
- 24 个防护场景总体实时 **+0.307 个百分点**，原来11种对手总体 **−0.034**、区间含0，只有 chaos 改变少量结果。沉默/弃票/固定投票/复读桌保持原结果；LLMQ 同桌的其他人仍会受重复信息影响，假跳和矛盾发言桌的村民胜率仍低。**不是免疫所有骗术，也不是普遍战胜简单对手的证明**。另试39,000局的队友投票保护候选无收益或变差，未采用。
- 本地全量 **2598通过、41跳过**；[Linux CI（a0bc5ad）](https://github.com/Astro-wen/ucla-agent-tournament/actions/runs/38365601607) **2638通过、1跳过**；全库语法检查通过；两种计算器模式各重放2349个真实决策，0失败/修补；官方SDK在本地平台跑62局，覆盖静默和120秒窗口，0错误/卡死/代打/非法动作；自检ready。日志、校验和和独立审计在忽略的 `logs/werewolf_benchmarks/guard-*` 下。
- 总体提升仍未达到事先规定的2个百分点，部分对手回归仍在；`public_vote` 默认关闭，草稿PR保留，没有冻结新冠军、合并main或更改比赛配置。29301/29302已消耗，不可再用于挑选下一版。

### 选秀：对「meta 型」程序对手 Dragonfly 的复盘和对手模型 1013（10-13，分支 `pk/meta-1013`，这个分支里默认开；冠军、main、在线 agent 都没动）

**一句话**：10-13 对新对手 Dragonfly（一个程序，每局约 1 分钟，专拿标准环境核心、开局集火）打了 10 局 4 胜 6 负。把这 10 局和 10-12 输给 Steve Nash Equilibrium 的 915ba9e1 的每一个选秀状态重新喂给选秀器，发现：（1）v4.4 的对手模型认不出它是「真实参赛者式」选手（偏好数据只有 22 局、大多是 LeCharmander，里面 Chien-Pao、Iron Valiant 是负分），11 局里 8 局 w 很快掉到 0.3 以下；（2）它会针对我们已经拿的牌选克制牌（我们先拿 Archaludon，它就拿 Gyarados + Landorus-T、Great Tusk、Iron Treads 这类地面系），而 v4.4 只按「受欢迎程度」预测 meta 型对手。新开关 `ARENA_DRAFT_METASET=1013`（这个分支里默认开）：偏好数据用到 10-13 为止全部 41 局真实选秀重新拟合，加上「克制我方已选牌」一项，先验 0.5。**新种子确认（种子 173，对按 Dragonfly 选牌拟合的本地替身，160 局）：121/160 = 75.6% 对 v4.4 的 106/160 = 66.2%，配对差 +9.4 个百分点 [+1.1, +17.7]，McNemar p = 0.040**；开发筛选（种子 151）+2.5（不显著），两次合计 +5.9（p = 0.067）；镜像对冠军（种子 157，100 局）43/100 对 47/100，−4.0 [−13.2, +5.2]，p = 0.52（不显著）。 回退：`ARENA_DRAFT_METASET=v44` = v4.4 的选秀（逐手相同，有测试）。

名词：**w** = 选秀器估计「对手按真实参赛者习惯选牌」的概率（v4.4-draft 节）；**克制项** = 一张牌对我方已选牌的平均单挑优势（伤害计算的单挑胜率 − 0.5），乘上 gamma 加进它被对手选中的打分；**metaDF** = 按 Dragonfly + Steve Nash Equilibrium 的真实选牌拟合的本地替身（`local/meta_opp.py` 的 `meta`，`META_DRAFTER_FILE` 指向这次的拟合文件；首发和对战用冠军自己的代码，所以它只模仿 Dragonfly 的选秀，不模仿它的打法）；**选秀模拟** = `scripts/draft_sim.py`，只选牌不对战，用我们自己的模型给两边 6 只打分。

#### 1. 复盘：11 局（Dragonfly 10 局 + 915ba9e1）的每一手重放

- 方法：从录像重建卡池和 12 手顺序（`scripts/fit_meta_drafter.py` 的 `load_games`），在我们每一手的状态上重新跑选秀器，打印全部合法牌的搜索值、对手接下来拿了什么、w。11 局 66 手全部和录像里的选择相同。
- **我们模型给 Dragonfly 爱拿的牌的分**（含 v4 残差；同一池里通常 18 张中排 8–17 位）：Chien-Pao +0.04，Chi-Yu −0.03，Iron Valiant −0.02，Iron Hands −0.20，Landorus-T −0.15，Thundurus −0.16，Dragonite −0.27；Flutter Mane +0.13。我们自己最爱的是 Archaludon +0.53（全场最高）、Amoonguss +0.25、Indeedee-F +0.22、Gyarados +0.15。这些牌模型都认识（没有「没见过的牌」问题）。
- **关键几手**：0eeebe40 先手第 1 手 Gyarados 0.398 对 Chi-Yu 0.324（很接近），Dragonfly 随后拿 Chi-Yu + Iron Hands；10d7979b 第 1 手 Iron Hands 0.596（明显最高），它拿 Chien-Pao + Dragonite；a63d5d93 它先拿 Iron Hands，我们 Chi-Yu + Archaludon，它再拿 Iron Valiant + Landorus-T；Archaludon 当第 1 手的 5 局（0a516fe0 负、0ad479eb 负、915ba9e1 负、061815f1 胜、d8ac206a 胜）领先第二名 0.4–1.3 个对数几率，Dragonfly 随后拿地面系 / 克制牌（Gyarados + Landorus-T、Great Tusk、Iron Treads）。
- **对手模型认不出它**：v4.4 的偏好数据里 Chien-Pao −0.31、Iron Valiant −0.28，Dragonfly 开局拿 Chien-Pao 后 w 掉到 0.015（b8ae3a53）、0.041（10d7979b）；11 局里 8 局在我们第一次能利用对手模型时 w < 0.3，也就是选秀器把它当成「和我们一样按模型选」的对手。
- **我们是不是低估了这些牌？证据不足**：（a）36 局有结果的真实选秀（22 胜 14 负）里，Archaludon 在我方 12/20 胜，和总胜率一样；第 1 手 Archaludon 8/15，其他第 1 手 14/21，样本太小。（b）本地 1560 局对 meta 选秀者的对局（种子 103 / 109 / 143，双方都是引擎搜索）上拟合「对方手里的牌比模型说的更强」的修正项，交叉验证只好 0.0003 nats/局，Iron Hands 只有 +0.015：在我们自己的引擎打法下，模型对这些牌的估值没有可测的偏差。模型整体偏乐观（预测 76.5%，实际 69.0%），但那只是一个常数，不改变选择。所以这次不动我们自己的估值，只修对手模型。
- 新的真实数据重新拟合（41 局真实选秀，246 手对手选牌）：带克制项的条件 logit 留一局交叉验证猜中 41.1%（不带 36.6%，我们的模型 18.7%），gamma = 2.41；只用 Dragonfly + Steve Nash Equilibrium 的 11 局拟合 gamma = 2.31，猜中 36–39%（Dragonfly 的选牌比 LeCharmander 分散）。偏好最高：Flutter Mane、Chien-Pao、Incineroar、Dragonite、Iron Hands、Sneasler、Landorus-T。

#### 2. 改了什么（`agent/pokemon/drafter.py`，新数据文件 `agent/pokemon/draft_meta_1013.json`）

- `ARENA_DRAFT_METASET`：`v44`（原样，开关关时逐手和冠军的选秀器相同：11 局真实状态 + 随机状态共 246 个，选择、全部根节点值、节点数完全相同）或 `1013`。`1013` = 偏好文件换成 41 局拟合的 `draft_meta_1013.json`、先验 0.5、打开克制项。单独的旋钮 `ARENA_DRAFT_META` / `OPPPRIOR` / `OPPCTR` 仍然可以覆盖各部分。
- 克制项（`OPP_CTR`）：meta 型对手在搜索的某个节点上对牌 x 的偏好 = u[x] + gamma × x 对「那个节点上我方已选牌」的平均单挑优势（和 `fit_meta_drafter.py` 的模型一样，单挑用 `matchup.py` 的伤害计算表）。用在三处：更新 w 时算对手每一手的概率、对手节点上的期望选牌（前 3 张）、后面的贪心推演。只在 w > 0 且文件里有 gamma 时走新的搜索函数，旧函数一行没改。
- 在 11 局真实状态上，`1013` 让 w 对 Dragonfly 保持在 0.36–0.99（8 局 ≥ 0.7），在录像里的 66 个我方状态上有 17 手选得不一样（每个状态都按录像里的历史算，不往后推演；例如 e1a99255 第 2 手拿 Flutter Mane 而不是 Baxcalibur，b8ae3a53 第 2 手 Iron Hands 而不是 Chi-Yu，a63d5d93 第 3 手 Amoonguss 而不是 Archaludon）；Archaludon 当第 1 手的局都不变（它自己的分太高）。节点数最多 53k（预算 120k）。
- 另一个候选 C1（只换偏好文件、不带克制项、先验 0.5）在选秀模拟里对 metaDF −1.6 个百分点（标准误 0.8），没上对战就放弃了。

#### 3. 测量（对战；全部 0 非法动作、0 计时器代打、0 判负、0 出错；候选和基线是同一份代码，候选用 `--env` 开开关；对战本身不是确定性的：同一种子、两边 6 只相同，胜负也会变，所以只有大差别测得出来）

| 检查 | 候选（1013） | 基线（v4.4） | 配对差 [95% CI] | 只候选赢 / 只基线赢 | McNemar p |
|---|---|---|---|---|---|
| 开发筛选：对 metaDF（6 局拟合），种子 151，160 局；候选用 36 局拟合的偏好文件 | 117/160 = 73.1% | 113/160 = 70.6% | +2.5 [−6.2, +11.2] | 27 / 23 | 0.67 |
| **新种子确认：对 metaDF（11 局拟合），种子 173（之前没看过），160 局；候选 = 最终的 `METASET=1013`（41 局文件）** | **121/160 = 75.6%** | 106/160 = 66.2% | **+9.4 [+1.1, +17.7]** | 31 / 16 | **0.040** |
| 两次合计（320 局） | 238/320 = 74.4% | 219/320 = 68.4% | +5.9 [−0.1, +11.9] | 58 / 39 | 0.067 |
| 镜像：对冠军（v4.4），种子 157，100 局 | 43/100 = 43.0% | 47/100 = 47.0% | −4.0 [−13.2, +5.2] | 9 / 13 | 0.52 |

- 两边 6 只完全相同的局（每批约 21 局）胜负基本一样（15/15、15/14），差别都来自选秀不一样的约 139 局。
- 选秀模拟（不对战，我们的模型打分，种子 151，200 次选秀）：对 metaDF（11 局）+2.65 个百分点（标准误 0.44），对冠军 +0.48（0.57），对旧的 meta 选秀者（22 局，LeCharmander 式）+0.56（0.57），meta_soft +0.76（0.47）。
- `replay_real.py`（候选开关打开）：已提交的 142 个录像 2349 个决策，两种模式都 0 失败、0 修补，p99 2.8 s / 2.4 s；主 checkout 里最近的 65 个录像（含这 11 局）1923 / 1993 个决策，两种模式都 0 失败，p99 2.5 s。
- 全量 pytest：默认开（最终提交）**2642 全过**；之前默认关时 2641 过、1 个红警模拟确定性测试（`test_ra_sim.py::test_matches_are_deterministic_in_lockstep`）在机器满载时偶发失败，单独重跑通过（这个分支没改红警）。新测试在 `tests/strategy/test_draft_meta.py`：克制项关 / gamma 为 0 时和 v4.4 对手模型逐手相同；开时合法、不超节点预算、确实改变搜索；`METASET` 同时切换文件、先验、克制项，单独旋钮能覆盖；默认是 1013，`v44` 退回。

#### 4. 结论和风险

- 按这次任务定的标准（新种子确认显著、别处没有显著变差）**通过**，所以在分支里默认打开（`META_SET = "1013"`）。但证据是边缘的：只有一次确认显著（p = 0.04），筛选只有 +2.5，镜像点估计 −4.0（100 局，区间很宽）。
- 改动只影响选秀（首发、对战一行没改），开关一行就能退回：`ARENA_DRAFT_METASET=v44` 在 276 个真实 + 随机状态上和冠军的选秀器逐手相同（选择、根节点值、节点数）。每手选秀最慢 0.66 s（平台每手 15 s），节点数最多 53k（预算 120k）。
- Archaludon：它是模型分最高的牌，`1013` 也不会把它从第 1 手拿掉；真实战绩里没有它被高估的显著证据（我方 12/20，和总胜率一样），本地自对弈的残差也是 0。Dragonfly 的地面系反制是对手模型现在能预料到的（克制项），但我们的估值仍然认为先拿 Archaludon 值得。要改它得先有更多真实对局。
- **样本内**：本地替身 metaDF 和候选的偏好文件用了同一批 Dragonfly 对局（替身用 11 局，候选用全部 41 局），而且替身的选牌公式（偏好 + gamma × 克制）正是候选里加的那一项，所以 +9.4 有一部分是「替身和对手模型同源」带来的；它能说明「对手真按这个方式选牌时，1013 选得更好」，不能证明对真实 Dragonfly 也多赢这么多。替身用我们自己的引擎打对战，真实 Dragonfly 的打法（开局集火）不在里面：本地基线对替身赢 66–71%，真实只有 4/10。
- 开发筛选（替身 6 局、候选文件 36 局）只有 +2.5，确认（替身 11 局、候选文件 41 局）+9.4，差别在两次的噪声范围内（对战本身不是确定性的）；只有一次确认显著，p = 0.04 刚过线。
- 对战没测（时间不够）：旧的 meta 选秀者（LeCharmander 式）、VGC-Bench、simbot、门槛种子 7 / 11。只有选秀模拟：对旧 meta +0.56、meta_soft +0.76、冠军 +0.48（都不显著，没有变差的迹象）。
- 种子：开发 151、157，确认 173（之前没用过），另用 143 的前 8 局查确定性；都记进 `docs/v5_standard.md` 的「已经用过」。
- 没改冠军（`champion/` 仍是 v4.4 的选秀），没合 main、没推送，没动在线 agent。

### v4.7（红警进攻判断，10-14，分支 `ra/attack-1014`，基于 main b0f22c6 = v4.6；宝可梦、狼人杀不变）

**为什么改**：10-13 晚 Testing 里真实红警 7 胜 2 负，两局输局（都在 sidestep、开局资金 5000）是同一个样子：8:30–9:00 一大波出击吃了大亏，对方反推把家拆了。名词：**波次** = 主力一次出击；**盲区先验** = 很久没看到对方的家时假设的敌军（v4.5，25 ×（秒 − 90），以前封顶 9000）；**交换** = 一段时间里我方损失的价值对打掉的敌方价值；**配对比较 / p**：同一批种子上新旧两版各打一遍，只看结果不同的局（新版好 / 差），p 是这些局上的精确符号检验；**逐局相同** = 结果和对局时长都一样；**开发种子** = 调参时看过的种子，**新种子** = 测量前没用过的种子；**录像回放** = 把真实录像自己的画面（约 10 秒一帧）喂给 bot，看它会在哪一秒出击 / 撤退（脚本在 scratchpad：`recplay.py` 单局、`recall.py` 全部 23 局；之后的画面还是真实对局里发生的，所以只看「第一次不一样的决定」）。

**诊断**（录像 `logs/<session>-*.jsonl` 只读）：
- f5ce0e92（对 Steve Nash Equilibrium）：整局没看到一座敌方建筑（侦察兵 61 秒死在哨兵处，之后的重新侦察也都死在路上）。524 秒按先验 9000 出击（36 个单位、约 1.4 万：19 步兵、11 火箭兵、4 掷弹兵、8 重坦）——录像回放时 v4.6 在同一秒发出同样的波次。545–555 秒半路遇到 24 步兵 + 10 火箭兵 + 3–5 重坦：10 秒里损失 5550、打掉 1800（步兵 20 → 9）。586–608 秒对方反推，家里 11 秒损失 7700、打掉 300，703 秒结束。
- d527c9fa（对 Riffo）：侦察兵 71 秒死在对方家里，没看到建筑；505 秒同样按先验 9000 出击（约 1.3 万：21 步兵、9 火箭兵、13 坦克）。525–536 秒先撞上 5 中坦 + 2 门火炮（射程 12）+ 步兵：11 秒损失 5150、打掉 3100（步兵 21 → 8），然后继续打进有 2 座碉堡和 1 座炮塔的基地，到 586 秒双方各损失 1 万多。之后 4 分钟 6 辆采矿车收入只有每分钟约 1900（家门口的矿采空了），军队 1.1 万 → 1.3 万；对方 14:00 带 7–8 辆中坦和火炮推进，14:51 结束。
- 结论：(1) 两次都是盲目出击（从没看到对方建筑，先验封顶 9000，真实对手 8–9 分钟时约 1.2 万以上，还有塔和火炮）；(2) 先死的是步兵；(3) d527c9fa 第一回合明显吃亏后还继续打；(4) 失败后没有先守再攻（d527c9fa 后半段主要是经济，这次没改）。

**改了什么**（只有 `agent/redalert/army.py`、`params.py`、总结行；细节见 `docs/redalert_design.md` 第 18 节；`ARENA_RA_ATTACK_GATE=0 ARENA_RA_TRADE_RETREAT=0 ARENA_RA_RECOVER=0` = v4.6）：
1. **进攻门槛**（`attack_gate`）：没看到过敌方建筑时要 1.6 倍（不是 1.3 倍）；盲区先验封顶从 9000 提到 13000；看到过的塔和火炮按 1.5 倍算。
2. **交换止损**（`trade_retreat`）：最近 12 秒里主力损失 ≥ 1.5 倍击杀、≥ 1500、≥ 这一波的 33%，**而且这一波出发以来的累计损失 ≥ 累计击杀**，就撤（算失败的一波）。
3. **失败后先守再攻**（`recover`）：失败后 4 分钟内下一波要 1.6 倍（原来 1.3 倍）。补塔（`recover_towers`）写了、默认 0。
4. **出击编成**（`attack_comp`，**默认关**）：步兵留家，只带车辆 + 火箭兵 + 每车 0.5 个步兵。模拟器没有碾压 / 溅射，判断不了；人类高手的军队约 99% 是步兵 + 火箭兵 2:1–3:1 配坦克，所以不默认开。
5. 新的模拟对手 `riffo`、`nashball`（`local/ra_bots_strong.py`，按两局真实输局写）；总结行新增 `base_seen`、`trade_retreats`、`comp_kept`；新测试 8 个。

**录像回放**（23 局真实红警录像，v4.6 对 v4.7 第一次不同的决定）：两局输局都按设想变了——f5ce0e92 524 秒不再出击；d527c9fa 536 秒撤退（损失 5150、击杀 3100、波次约 1.4 万）。21 局赢局里 20 局前几波的出击 / 撤退和 v4.6 一样，1 局（1da6311e，对 Quicksilver）门槛把第一波从 509 秒推迟到 623 秒（那局我方 2.5 万军队、最后赢了）。

**过程**（开发种子 1401–1406；每个候选都和「开关全关」配对）：
- 先确认开关全关 = v4.6：对 main b0f22c6 的快照在 1401–1404 上 228 / 228 局逐局相同（最终测量又核对了 336 局，见下）。
- v4.6 在开发种子上：nash 59 / 60、strong 60 / 60、pro 59 / 60、main 47 / 48（模拟器里几乎打满），riffo 116 / 120、nashball 89 / 120（trapped 上只有 35–40%：nashball 经济压过我们、推进拆家，和进攻判断无关）。
- 单项（各 228–348 局）：门槛 +1.1 个百分点（5 / 1，175 局逐局相同）；交换止损（20% 版本）−0.3（2 / 3）；补塔版的先守再攻 +0.4（2 / 1）；第一版编成在 riffo / nashball 上 −0.8（5 / 6）。四项全开（第一版）+0.6（6 / 4）。
- 追查变差的局：第一版编成在 310 秒、只有 3 辆车的小波次上把 14 个步兵留家（种子 1404002、1405014）→ 改成 ≥ 5 辆车、6:00 以后；先守再攻的 3 座塔把钱吃光、军队 2 分钟没长（1405014）→ 塔默认 0；交换止损 20% 在 nashball 前哨刚接触的 15 秒常常是「死 1700、打死 0–200」，叫停了本来能赢的冲锋（1405099、1405111，v4.6 7–9 分钟赢）→ 33%（真实两局都是 40%）。
- 组合（408–468 局）：门槛 + 止损 33% +1.0（8 / 4）；四项全开（改后）+0.6（8 / 5）；含门槛的组合在 riffo / nashball 1405 上都是 +3.3（5 / 1）。之后停止调参。

**第一次最终测量**（提交 9a15e61，新种子 1451–1462，基线 = 同一快照关掉三个开关）：核心 756 局 93.5% → 94.4%（+0.9，10 / 3，p 0.092）；riffo / nashball 240 局 +1.2（6 / 3）、nash +0.8、pro +0.8、strong +1.7、main 和 rusher 不变；压力 114 局 +0.9（2 / 1）；0 非法、0 异常；基线对 main 快照 336 / 336 局逐局相同。**直接对打 v4.6**（48 局）16–32，看着很差，但同一批种子上 v4.6 对 v4.6 也只有 19–29（这个对打的 A 方在这些种子上本来就吃亏），按配对算 −6.2（5 / 8，p 0.58，不显著）；拆开看主要来自门槛（单开门槛 −8.3，3 / 7；单开止损 0，2 / 2）：v4.6 在 12–13 分钟一波打赢的局，v4.7 更谨慎、拖到时间到。
- 然后用 23 局真实录像回放检查 9a15e61，发现 2 局**真实赢局**里止损会在一次好的交换之后叫停（fffc7ced 381 秒：前面 340–371 秒打掉 5400、损失约 3000，之后 12 秒损失 2600、打掉 400；971ec5dc 920 秒）→ 加了「出发以来累计也在吃亏」（`trade_cum_ratio` = 1.0），这两局回放后和 v4.6 一样，两局输局照样触发。因为这是在最终测量之后改的，**在另一批新种子上重新测了一遍**（下表），第一次测量只作参考。

**最终测量**（提交 4d4852f = 快照 `fin2`；新种子 1471–1482，测量前没用过；基线 = 同一快照关掉三个开关，等于 v4.6）：

| 场景（新种子） | 局数 | v4.6（开关全关） | v4.7 | 配对差（百分点） | 新版好 / 差 | p |
|---|---|---|---|---|---|---|
| riffo + nashball 1471（三图） | 240 | 206（85.8%） | 212（88.3%） | +2.5（±2.3） | 7 / 1 | 0.070 |
| 　其中 nashball | 120 | 87（72.5%） | 93（77.5%） | +5.0（±4.6） | 7 / 1 | 0.070 |
| 　其中 riffo | 120 | 119（99.2%） | 119（99.2%） | 0 | 0 / 0 | 1 |
| nash 1472 | 120 | 112（93.3%） | 113（94.2%） | +0.8 | 1 / 0 | 1 |
| pro 1473 | 120 | 118（98.3%） | 116（96.7%） | −1.7（±3.3） | 1 / 3 | 0.63 |
| 强对手 1474（tanks、swarm、pro、raider、hider） | 120 | 118（98.3%） | 119（99.2%） | +0.8 | 1 / 0 | 1 |
| 主联赛 1475（idle、rusher、turtle、llm） | 96 | 93（96.9%） | 95（99.0%） | +2.1 | 2 / 0 | 0.50 |
| rusher 1476 | 60 | 57（95.0%） | 57（95.0%） | 0 | 0 / 0 | 1 |
| **核心合计** | 756 | 704（93.1%） | 712（94.2%） | **+1.1（±1.0）** | **12 / 4** | **0.077** |
| 　按地图：sidestep | 252 | 243（96.4%） | 250（99.2%） | +2.8 | 8 / 1 | 0.039 |
| 　按地图：trapped | 252 | 221（87.7%） | 220（87.3%） | −0.4 | 2 / 3 | 1 |
| 　按地图：shadowfiend-ii | 252 | 240（95.2%） | 242（96.0%） | +0.8 | 2 / 0 | 0.50 |
| 压力：网络不稳 5%（1477）/ 1% 命令丢失（1478）/ 5 秒窗口（1479）/ 资金 10000（1480，主联赛 + 强对手） | 24 / 24 / 12 / 54 | 24 / 23 / 12 / 47 | 24 / 23 / 12 / 46 | −0.9（合计 114 局） | 1 / 2 | 1 |
| 直接对打 v4.6（1481；基线 = v4.6 对 v4.6，同一批种子） | 96 | 46（47.9%） | 50（52.1%） | +4.2（±9.6） | 13 / 9 | 0.52 |
| 实时时钟（1482，speed 8，riffo / nashball / rusher / pro 各 1 局） | 4 | 3 胜 | 3 胜 | — | — | — |

按对手合计（核心）：nashball +5.0（7 / 1）；pro 144 局 −0.7（2 / 3：3 局变差都在 trapped，v4.6 11–16 分钟一波打赢，v4.7 更谨慎、拖到时间到）；turtle +8.3（2 / 0）；nash +0.8；riffo、rusher、hider、idle、llm、raider、swarm、tanks 结果不变。基线（开关全关）对 main b0f22c6 的快照：第一次测量的 1452 / 1454 / 1455 上 336 / 336 局逐局相同，开发种子上 228 / 228。全部测量我方 **0 个非法动作**、0 次 runner 报错 / 卡死、0 个异常；决策耗时 p99 最高 4.4 ms（压力场景 34.3 ms，v4.6 同批 31.3 ms）。

**模拟器能判断 / 不能判断的**：能判断门槛（敌方强度估计、什么时候出击）、交换止损触发的时机（按造价和兵种克制表）、失败后的门槛，以及「守家 + 反推」型对手（riffo、nashball）的输赢。不能判断碾压、溅射、卧倒：模拟器里步兵很强，真实引擎里步兵冲坦克吃亏得多，所以真实对局里交换止损可能触发得更多、价值更大，`attack_comp` 也只能在真实对局里评估。两局真实输局的改变是录像回放看到的（见上），不是模拟器复现的。

**检查**（提交 4d4852f，`~/wt/_ra4_gate`，机器上同时在跑最终测量）：全量 pytest **2650 个全过**（1544 s）；`replay_real.py` 2349 个真实决策 **0 失败**、0 修补，搜索用上 1296 / 1296，p99 3494.5 ms（机器忙）；`--no-calc` 同样 0 失败、0 修补，p99 1619.4 ms。`selfcheck.py` ready（只有没配 key 的 1 个警告）。压缩包 `arena-agent-4d4852f`（46 MB / 434 个文件，密钥扫描干净；`agent/`、`altruagent/`、`data/`、`deploy/`、`run_agent.sh`、`selfcheck.py` 和工作区相同）：干净环境 selfcheck ready，包里重放 60 个决策 0 被拒，格式不对的假 key `--check-tournament` 退出 1。**红警端到端**（同一个包的整棵进程树：`run_agent.sh` → `python -m agent --tournament --match` → 每座位一个 worker，假官方平台 + 红警模拟器，对手 rusher，300 游戏秒，实时时钟）：2 场红警都打满并赢（平局规则），75 / 112 个命令批，**0 个不合法动作**，被拒 0 / 0，`exceptions=0`，新字段都在（`base_seen=1 trade_retreats=0 comp_kept=0`）；5 个 Traceback 都是已知的宝可梦选秀回退（`drafter.py` 第 761 / 765 行），其他 0 个；0 次 no-show、key 没进日志、0 个残留进程。冻结冠军后（f79ed59）再跑一遍全量 pytest：**2650 个全过**（856 s）。

**结论**：按事先定的标准，**没有哪个对手或压力场景显著变差**（pro −1.7、压力 −0.9、直接对打 v4.6 +4.2，都不显著），0 非法、0 异常、检查全过；**变好的幅度小**：核心 +1.1 个百分点（p 0.077），照真实输局写的对手 +2.5（p 0.070，其中 nashball +5.0），sidestep（真实对局都在这张图）+2.8（p 0.039）；两次独立的最终测量方向一致（第一次 +0.9，p 0.092）。最直接的证据是录像回放：两局输局里的两个错误决定（盲目出击、吃亏后继续打）都会改掉，21 局赢局里 20 局前几波的决定不变。**建议合并**：风险小，也容易退回（`git revert` 合并提交，下一局就用回 v4.6，不用重启；或者 `ARENA_RA_ATTACK_GATE=0 ARENA_RA_TRADE_RETREAT=0 ARENA_RA_RECOVER=0` 再重启）。合并后在 Testing 里看 `[match-summary]` 的 `waves`、`base_seen`、`trade_retreats` 和日志里的 `losing trade`：如果对弱对手 10 分钟还没出击（`waves=0`），或者止损在赢局里频繁触发，就退回。**局限**：模拟器没有碾压 / 溅射，步兵相关的效果判断不了；门槛在模拟器的镜像对打里略偏谨慎（第一次 −6.2、第二次 +4.2，都不显著）；对 pro 在 trapped 上有 3 局拖到时间到；d527c9fa 后半段的经济问题没有改。
- **冻结为冠军**（本分支）：`champion/` = 4d4852f 的 `agent/`（提交 f79ed59）。没合 main、没推送、没动在线 agent。
- 用过的种子：开发 1401–1406、1499（冒烟）；第一次最终测量 1451–1462（9a15e61，作参考）；最终测量 1471–1482（4d4852f）。运行记录在本机 `~/wt/_ra4_out`（每组 JSON + 日志），快照 `~/wt/_ra4_snap`（v46 = main b0f22c6、c0 / c1 = 开发、fin = 9a15e61、fin2 = 4d4852f）。
- 后续可做的（没做）：d527c9fa 后半段的经济（家门口的矿采空后去第二块矿田建矿厂 / 扩张）；f5ce0e92 的侦察（重新侦察的步兵都死在同一个哨兵处，可以换路或用快车）；门槛在模拟器的镜像对打里偏谨慎（看对方是否会进攻再定）；`attack_comp` 的温和版（只在看到对方以坦克为主时）；交换止损改用伤害（血量变化）而不是死亡来算，就不怕击杀滞后。

### v4.8 候选（红警对坦克海，10-15，分支 `ra/tanks-1015`，基于 main 4dc4457 = v4.7；宝可梦、狼人杀不变；**没有合并、没有推送、没动在线 agent**）

**为什么改**：10-14 / 10-15 在 Testing 里连输 Tom_2026 三局（10-13 前我们 3 : 0 赢过他，之后他换了打法）：6d0c7844（shadowfiend-ii，653 秒）、f1cd3ba4（shadowfiend-ii，1116 秒）、edf99e95（sidestep，597 秒）。名词：**坦克海** = 坦克为骨干、配大量步枪兵的军队；**分火** = 我们指定坦克 / 火箭兵打敌方车辆（默认是单位自己打最近的敌人）；**配对比较 / p**：同一批种子上两版各打一遍，只看结果不同的局（新版好 / 差），p 是这些局上的精确符号检验；**逐局相同** = 结果和对局时长都一样；**开发种子** = 调参时看过的种子（1501–1510），**新种子** = 最终测量前没用过的（1551–1567）；**录像回放** = 把真实录像自己的画面（约 10 秒一帧）喂给 bot 看它的决定。

**录像里看到的**（scratchpad `ra5\` 的 `tl2.py`、`pos.py`、`calib.py`、`recall5.py`；只读 `logs/`；细节见 `docs/redalert_design.md` 第 19 节）：
- 两局 shadowfiend-ii 都是侦察兵 112 秒死在对方基地里 → 「早期敌军」：多造 6 个步枪兵和 2 座打步兵的塔（碉堡 / 火焰塔，共 1200），采矿车优先整局关掉（3:00 / 4:30 / 6:30 采矿车 2 / 4 / 4），现金 2–3 分钟起一直是 0；edf99e95（sidestep）也是 2 座碉堡 + 12 个步兵。4:00 时三局都有 20–24 个步枪兵，都是没看到敌人之前造的。
- Tom 4:56–5:47 第一次出现（2 辆坦克 + 步枪兵 + 火箭兵试探），6:30–7:40 进攻（有赚有亏），然后一次推进拆家：6d0c7844 9:20（15–19 步兵、7–8 中坦、3 火箭兵）我方损失约 1.15 万、打掉约 2300；edf99e95 7:00–7:30 在前沿损失 7800、打掉 2650；f1cd3ba4 17:09（19 步兵、7 火箭兵、5 中坦、3 吉普）损失约 1.8 万、打掉约 7000。f1cd3ba4 这一仗我方 10 重坦、10 火箭兵、10 步兵挤在一起，10 秒里火箭兵 10 → 1、步兵 10 → 5，对方只掉 3 辆坦克：他的步枪兵团先打死我们的步兵和火箭兵，我们的坦克自动打最近的步枪兵（对步兵约四分之一伤害）。
- f1cd3ba4 15:17 那次出击：盲区先验到 900 秒就不再算，敌方强度从 13000 掉到 4908，出击撞上火炮 + 坦克（损失约 9000、打掉 700），交换止损撤回。
- 6d0c7844、f1cd3ba4 各一次 `invalid_action=1`，都在基地正被拆的最后几十秒（598 秒、1096 秒，同一秒第一次出现 `not_owned` 拒绝）：锚所在的建筑也在平台检查前被打掉，整批被拒。对胜负没影响。
- **v4.6 对 v4.7 的录像回放**（假设「门槛让我们太被动」）：6d0c7844、edf99e95 两版都整局不出击（决定一样）；f1cd3ba4 v4.6 会在 693 秒出击、v4.7 917 秒（真实）。模拟器里对 `tom` 关掉门槛 120 局 2 好 / 4 差，整个 v4.6 对 v4.7 3 / 2：门槛不是这三局的原因。全部 30 局真实录像回放：v4.7、v4.8 默认、分火打开三版的出击 / 撤退决定完全一样，四个版本（加 v4.6）0 个异常。

**新的模拟对手 `tom`**（`local/ra_bots_strong.py`）：战车工厂先于第二座矿厂，钱多时第三座矿厂和第二座战车工厂，坦克不停，步兵以步枪兵为主（约 3 : 1 配火箭兵），2 辆吉普 / 防空车 4:30 起抓采矿车，7:00 后每 4 辆坦克 1 门火炮；全军站在去我方 30% 路程处；4:50 两坦克 + 4 步兵试探；军队 ≥ 它看到的我方最大军队 1.1 倍（≥ 5000，6:30 起）或 9:30 起全军推进。v4.7 对它约 89–92%：模拟器里它的军队峰值只有我们的一半到三分之二；真实的 Tom 强得多（9:30 时约 1 万，之前还被我们打掉约 8700；我们约 6900）。

**写了什么**（每项一个 `ARENA_RA_*` 开关；**默认只开第 1 项**；`ARENA_RA_ANCHOR2=0` = v4.7）：
1. **第二个锚**（`anchor2`，默认开）：每批命令再给离看得见的敌人最远的另一座建筑发一条 `power_toggle on=true`（`ok already_on`，不占引擎命令），两座都在检查前被打掉才会整批被拒。世界不变。
2. **分火**（`split_fire`，默认关）：坦克和火箭兵每 1.5 秒去打射程 + 2 格内最近的敌方车辆。
3. **反坦克塔**（`at_towers`，默认关）：看到坦克海、坦克占对方一半以上、对方看到的军队 ≥ 我方 0.6 倍时在基地里造最多 2 座 `gun` / `tsla`（先补电）。
4. **反坦克兵种**（`at_comp`，默认关）：看到坦克海后火箭兵 ≥ 60%、步枪兵 ≤ 12。
5. **长图上早期敌军不关采矿车优先**（`hf_keep`，默认关）：只有速攻迹象和两座兵营才关。
6. 总结行新增 `tank_army_s`、`split_orders`、`at_towers`；新测试 8 个（`tests/redalert/test_policy.py`），v4.6 的早期敌军测试改为关掉 `hf_keep` 跑。

**开发种子上的筛选**（1501–1510；每项都和同一快照「v4.8 开关全关」配对，好 / 差局数；v4.7 对 `tom`：1501 111 / 120，1507 107 / 120）：

| 项 | 对手（种子） | 结果（好 / 差） |
|---|---|---|
| 反坦克塔 3 个版本 | tom 1501 | 1 / 5、0 / 3、0 / 3（第一版低电时间翻倍；塔在 6 格外时对方火炮 / 试探打塔，主力被拉去打不利的仗；钱和电从军队里拿走） |
| 反坦克兵种 | tom 1501；分火之上 | 0 / 0（38 局逐局相同）；0 / 1 |
| 采矿车优先不关 | shadowfiend-ii 混合 1508（pro、nash、nashball、riffo、tanks、swarm、raider、rusher）；分火之上 | 1 / 3；0 / 3（tanks / pro 9–10 分钟推进打穿更贪的经济，正是 v4.6 整局关掉的原因） |
| 门槛关掉（= v4.6 的出击判断） | tom 1501 | 2 / 4 |
| 分火（留下的参数：坦克 / 火箭兵走 2 格去打任何车辆） | tom 1501 + 1507；nash 1504；riffo / nashball 1502；pro；strong；main；shadowfiend-ii 混合 | 19 / 5；**0 / 5**；6 / 3；2 / 2；0 / 0；0 / 0；4 / 1 |
| 分火的 6 个变体 | tom 1501 / 1507、nash 1504 / 1509 | 能护住 nash 的变体都丢了对 tom 的好处：只打射程内的坦克 tom 16 / 34、nash 4 / 1；目标死后回战场 tom 8 / 4、nash 0 / 4；火箭兵不走 tom 19 / 8、nash 0 / 4；只走向火箭兵少的坦克 nash 0 / 4；只走向孤立的坦克 tom 18 / 16、nash 3 / 5；只在对方坦克不到一半时走 tom 10 / 15、nash 3 / 5 |

追查（`tomtrace.py`、`ordtrace.py`）：nash 1504014 分火时我方 7 个火箭兵和 4 辆坦克走出步兵线去追 nash 的坦克推进，先死光；不分火时它们都活下来。结论：四个变强开关都默认关；分火的参数留成对 tom 最好的那一版（打开开关就是它），在最终测量里作为参考一起测。

**最终测量**（提交 20b12ce = 快照 `fin`，新种子 1551–1567，测量前没用过；三组同一批种子：基线 = 开关全关 = v4.7，候选 = 默认（只开第二个锚），参考 = 再打开分火）：

| 场景（新种子） | 局数 | v4.7（基线） | 候选：第二个锚 | 逐局相同 | 参考：分火 | 分火好 / 差 | p |
|---|---|---|---|---|---|---|---|
| tom 1551（三图） | 240 | 216（90.0%） | 216 | 240 | 226（94.2%） | 16 / 6 | 0.052 |
| tom 1552（只 shadowfiend-ii） | 120 | 103（85.8%） | 103 | 120 | 108（90.0%） | 10 / 5 | 0.30 |
| riffo + nashball 1553 | 240 | 206（85.8%） | 206 | 240 | 208（86.7%） | 8 / 6 | 0.79 |
| nash 1554 | 120 | 106（88.3%） | 106 | 120 | **91（75.8%）** | **3 / 18** | **0.0015** |
| pro 1555 | 120 | 119（99.2%） | 119 | 120 | 118 | 0 / 1 | 1 |
| 强对手 1556（tanks、swarm、pro、raider、hider） | 120 | 119（99.2%） | 119 | 120 | 120 | 1 / 0 | 1 |
| 主联赛 1557（idle、rusher、turtle、llm） | 96 | 96（100%） | 96 | 96 | 96 | 0 / 0 | 1 |
| rusher 1558 | 60 | 58（96.7%） | 58 | 60 | 58 | 0 / 0 | 1 |
| shadowfiend-ii 混合 1559 | 96 | 89（92.7%） | 89 | 96 | 88 | 0 / 1 | 1 |
| **核心合计** | **1212** | **1112（91.7%）** | **1112** | **1212** | 1113（91.8%） | 38 / 37 | 1 |
| 　按对手：tom（三组合计） | 360 | 319（88.6%） | 319 | 360 | 334（92.8%） | 26 / 11 | **0.020** |
| 　按对手：nash | 132 | 114（86.4%） | 114 | 132 | 99（75.0%） | 3 / 18 | **0.001** |
| 　按地图：sidestep / trapped / shadowfiend-ii | 332 / 332 / 548 | 97.0% / 88.9% / 90.3% | 相同 | 全部 | 98.8% / 85.5% / 91.4% | 9 / 3、9 / 20、20 / 14 | 0.15、0.061、0.39 |
| 资金 10000 1563 | 60 | 52 | 52 | 60 | — | — | — |
| 命令延迟 1 秒 1564（`--latency-ticks 25`） | 60 | 60 | 60 | 60 | — | — | — |
| 命令延迟 2 秒 1567（tom、nash、nashball，`--latency-ticks 50`） | 60 | 60 | 60 | 60 | — | — | — |
| 网络不稳 5% 1560 / 1% 命令丢失 1561 / 5 秒窗口 1562（主联赛 + tom） | 30 / 30 / 15 | 30 / 30 / 15 | 30 / 30 / 15 | 30 / 27 / 15 | — | — | — |
| 直接对打 v4.7 冠军 1565（基线 = v4.7 对 v4.7，同一批种子） | 96 | 56 | 56 | 96 | — | — | — |
| 实时时钟 1566（speed 8，tom / riffo / nashball / rusher 各 1 局） | 4 | 3 胜（输 tom） | 3 胜（输 tom） | — | — | — | — |

基线（开关全关）对 main 4dc4457 的快照：strong 1556、main 1557、nash 1554 共 336 / 336 局逐局相同。全部测量我方 **0 个非法动作**、0 次 runner 报错 / 卡死；决策耗时 p99 最高 18.4 ms（资金 10000 场景；v4.7 同批 17.8 ms）。命令延迟 2 秒时双方都有很多单条拒绝（1068 条，两组一样），但没有一批整批被拒——锁步模拟里第二个锚没有用武之地，它要防的是真实平台上「锚所在建筑恰好在 0.2–0.4 秒里被打掉」。

**检查**（20b12ce，`~/wt/_ra5_gate`，机器上同时在跑最终测量，负载 15–35）：全量 pytest **2657 个通过、1 个失败**：`tests/strategy/test_ra_sim.py::test_matches_are_deterministic_in_lockstep`——两个脚本 bot（llm 对 rusher）把同一局打两遍比较总结行，只差 `b.p99_ms`（决策耗时；测试没把这一项去掉），负载约 16 时出现；它不跑我们的 bot，用到的代码这次没改；负载降下来后单独重跑 3 / 3 通过，main 的代码同样通过。（第一次跑时工作区没有 `node_modules` 链接——ra4 有——2 个选秀测试失败、122 个跳过、重放没用上搜索；加了指向 main 的链接（不进 git）后重跑，下面是重跑的数。）`replay_real.py` 2349 个真实决策 **0 失败**、0 修补，搜索用上 1296 / 1296，p99 4012 ms（机器忙）；`--no-calc` 同样 0 失败、0 修补，p99 3863 ms。`selfcheck.py` ready（只有没配 key 的 1 个警告）。压缩包 `arena-agent-20b12ce-dirty`（46 MB / 434 个文件，密钥扫描干净；名字里的 dirty 是因为那时文档还没提交；`agent/`、`altruagent/`、`data/`、`deploy/`、`run_agent.sh`、`selfcheck.py` 和工作区相同）：干净环境 selfcheck ready，包里重放 60 个决策 0 被拒，格式不对的假 key `--check-tournament` 退出 1。**红警端到端**（同一个包的整棵进程树：`run_agent.sh` → `python -m agent --tournament --match` → 每座位一个 worker，假官方平台 + 红警模拟器，对手 rusher，300 游戏秒，实时时钟）：2 场红警都打满并赢（平局规则），73 / 111 个命令批，**0 个不合法动作**，被拒 0 / 0，`exceptions=0`，新字段都在（`tank_army_s=None split_orders=0 at_towers=0`）；5 个 Traceback 都是已知的宝可梦选秀回退，其他 0 个；0 次 no-show、key 没进日志、0 个残留进程。冻结冠军后（8e50910）再跑一遍全量 pytest：**2658 个全过**（880 s，机器空闲，包括上面那个计时测试）。
- **冻结为冠军**（本分支）：`champion/` = 20b12ce 的 `agent/`（提交 8e50910）。没合 main、没推送、没动在线 agent。

**结论**：按事先定的上线标准（对 tom 明显变好、没有哪个对手显著变差），**这个分支没有可以上线的变强改动**：反坦克塔、反坦克兵种、长图上不关采矿车优先在开发种子上都是净负或零；分火在新种子上证实了开发种子上的两面性——对 tom +4.2 个百分点（360 局 26 / 11，p 0.020），对 nash −11.4（132 局 3 / 18，p 0.001），trapped −3.3（p 0.061），核心合计 +0.1——所以默认关。唯一默认开的是第二个锚：核心 1212 局、资金 10000、两档命令延迟、网络不稳、5 秒窗口、直接对打 v4.7 全部和 v4.7 结果相同、逐局相同（只有 1% 命令丢失的 30 局里 3 局时长不同、结果相同：多一条命令改变了模拟器抽「丢失」的顺序），0 非法、0 异常；检查除了一个与本分支无关、只在高负载下出现的计时测试以外全过。**建议：不作为变强版本合并。** 第二个锚是低风险的健壮性修复（真实对局里 2 次整批被拒都发生在已经输掉的最后几十秒，模拟器里复现不了），合不合由主会话决定：要合就整支合（四个变强开关默认关 = v4.7 + 第二个锚），出问题 `ARENA_RA_ANCHOR2=0` 或 `git revert`。真实对局里如果再遇到坦克海，`[match-summary]` 的 `tank_army_s` 会记下第一次看到坦克海的秒数；分火可以 `ARENA_RA_SPLIT_FIRE=1` 打开（模拟器里对 Tom 式「少量坦克躲在步兵后」好、对 nash 式坦克推进差；截止后改配置要主办方同意）。**模拟器判断不了的**：碾压 / 溅射 / 卧倒和真实的集火，所以「步兵团对坦克多吃亏」「多造火箭兵好不好」「分火在真实引擎里好不好」都只能看真实对局；模拟器里的 `tom` 也比真实的 Tom 弱得多（经济差一截），真实输局的经济差距没有复现。
- 用过的种子：开发 1501–1510；最终 1551–1567。运行记录在本机 `~/wt/_ra5_out`，快照 `~/wt/_ra5_snap`（v47 = main 4dc4457，d0 / c1–c11 = 开发，fin = 20b12ce）。
- 后续可做的（没做）：盲区先验 900 秒的断崖（改成慢慢减小）；更像真实 Tom 的经济（需要更多他的录像）；分火只在「对方坦克少、藏在步兵后面」时走过去打（模拟器里 nash 那样的坦克推进会被它害死），需要每个对手 300 局以上才分得清。
