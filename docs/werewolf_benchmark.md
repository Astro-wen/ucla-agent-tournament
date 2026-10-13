# 狼人杀：可复现的配对评测

`local.werewolf_benchmark` 比较当前候选和冻结的 `champion`，以**我们只占一个座位的桌子（k=1）**为主要成绩。每一对使用相同身份、座位、对手种类、随机种子和讨论顺序。一次评测只代表本地模拟；不能据此宣称已经击败真实参赛者，也不会自动晋级冠军、改动部署或重启服务。

## 推荐流程

1. 调试和挑选方案只用开发种子 **151–159**。先完成小样本筛选，再冻结候选代码和参数。
2. 最后确认使用预先保留的 **28101、28102**，两组一起跑。看过成绩后又改了策略，这两组就不再是新样本，必须事先登记新的确认种子；不能反复挑中最好看的结果。工具拒绝把保留种子作为开发种子，也拒绝只跑其中一组。
3. 最终运行覆盖下列八种对手、两种讨论时序，并完成完整测试、真实决策重放和平台流程检查。
4. 真实平台对局仍是独立的一道关卡，需要按仓库规则处理，不能用本地模拟替代。

开发筛选（每种对手 100 对桌子，只有一个我方座位）：

```bash
.venv/bin/python -m local.werewolf_benchmark \
  --split development --seeds 151 --games 100 --ks 1 --jobs 3 \
  --out logs/werewolf_benchmarks/ww-dev-151
```

候选开关只作用于候选座位，不会影响同一桌上的冻结冠军座位：

```bash
.venv/bin/python -m local.werewolf_benchmark \
  --split development --seeds 152 --games 200 --ks 1,3 --jobs 3 \
  --candidate-env ARENA_WW_EXAMPLE=on --trace-games 3 \
  --out logs/werewolf_benchmarks/ww-dev-152-example
```

上面的 `ARENA_WW_EXAMPLE` 是格式示例，须替换为实际策略开关。其他工厂可用 `--candidate module:factory`、`--reference module:factory` 指定，但模块必须位于源码清单覆盖的 `agent`、`champion`、`local`、`versions`、`altruagent` 或 `examples` 下；其他包会被拒绝。清单同时包含这些目录的 Python 源码、依赖声明/锁文件和冠军版本。策略开关应在构造或决策时读取；若两个工厂用相同模块但依赖导入时固定的开关，须改为不同模块或分别运行，因为 Python 会缓存导入。

冻结后的确认评测（两次运行使用同一批身份分配，不把两种时序重复算作独立样本）：

```bash
.venv/bin/python -m local.werewolf_benchmark \
  --split heldout --games 400 --ks 1,3 --jobs 3 --timing rt \
  --trace-games 3 --out logs/werewolf_benchmarks/ww-heldout-rt

.venv/bin/python -m local.werewolf_benchmark \
  --split heldout --games 400 --ks 1,3 --jobs 3 --timing rounds \
  --out logs/werewolf_benchmarks/ww-heldout-rounds
```

每组默认对手：`first`（官方示例首个合法动作）、`random`、`heuristic`、`llmq`、`rt_wolfpack`、`rt_talker`、`rt_detective`、`rt_chaos`。`llmq` 是固定规则的语言模型替身，不是真实大模型。不会调用付费模型服务。默认 k=1、3；k=3 是多席位压力测试，不能替代 k=1。k=7 的背景没有对手意义，因此只计算一次。

## 输出和如何判断

每次必须提供新的输出目录，旧结果不会被覆盖。仓库内使用 `logs/werewolf_benchmarks/<run>`；工具拒绝 `logs/<run>`，因为真实重放脚本会把那一层的 `*.jsonl` 当成真实录制，混入合成数据会破坏重放。

| 文件 | 内容 |
|---|---|
| `manifest.json` | 代码提交、源码文件 SHA-256、Python 版本、种子、对手、参数、实际继承的非秘密策略开关、预计局数 |
| `games.jsonl` | 每局独立结果：身份/座位配对信息、胜负、回合数、错误计数、延迟直方图、发言次数 |
| `summary.json` | 总体、每种身份、每个对手、每个种子、每种席位数及其组合的配对统计和检查结果 |

主要指标是 k=1 的候选胜率减去冠军胜率。例如 +0.05 就是 **+5 个百分点**，不是“相对提高 5%”。k>1 的狼人指标只替换我方狼人，村民和预言家指标只替换我方好人，避免“自己的狼人变强导致自己的村民看起来变弱”的混淆。同一桌相同身份有多个座位时，只算一个样本；整桌平均胜率也只算一个样本。

二元胜负给出候选独赢、冠军独赢、双方相同的数量，以及精确 McNemar 检验的 p 值。p<0.05 表示零差异假设下这种配对差较少见，但不代表真实平台必然提升。`ci95` 是配对均值的正态近似区间；`conservative_ci95` 由改善/退步概率的 97.5% Wilson 区间相减得到（Bonferroni 构造，覆盖率仍是近似的），在小样本完全相同的情况下也保留不确定性。控制台优先展示这个较保守区间。多席位平均值不是二元结果，不对它滥用 McNemar；只显示按桌计算的配对均值区间。

`local_evidence_pass` 同时要求：

- 全部预定配对齐全，没有重复、身份错配、构造异常，运行过程中相关源码没有变化。
- k=1 至少 200 对，提升至少 2 个百分点，较保守区间下界 >0，精确 p<0.05。
- k=1 和合并样本中每种身份至少 200 个样本；较保守区间下界高于 -3 个百分点（容许的非劣界限）。
- 没有某一对手/身份显著退步；小于 20 个样本的切片仅作描述。切片检查未做多重比较校正，刻意保守地阻止可疑回归；没有检出退步不等于证明每个切片都无退步。
- 引擎和策略内部的所有错误计数均为零，包括策略异常后被合法动作掩盖的兜底、超时、聊天错误、监听错误、内部 ERROR 日志。模拟对手错误也阻止通过。
- 最慢决策 ≤10 秒，p99（99% 决策的延迟上界）≤5 秒。

这只是本地证据检查。完整 pytest、重放、打包、自检和真实平台验证仍需另外完成。默认门槛可以在开发时通过参数调整，但更改确认评测门槛必须事先记录理由，不能看到结果后放宽标准。应单独检查每个保留种子的结果，不能只报告合并数字。

## 日志边界

模块和每个模拟任务强制 `ARENA_RECORD=0`、`ARENA_STATE=0`；不读取或写入真实比赛记录，也不保存状态恢复文件。默认没有详细动作轨迹。`--trace-games N` 只为前 N 对桌子保存合成轨迹，最多 10 对；每局最多 100 条合成聊天、每个我方座位最多 80 个决策，不写原始 observation、内部记忆或推理内容。所有数据来自本地随机产生的游戏。

生产策略会把异常转成合法兜底，单看“非法动作=0”不够。本工具额外读取 `fallbacks`、`overruns`、`listen_stats.errors`、`last_decision.path`，并捕获策略调用期间的 ERROR 日志。还包装 `WerewolfPolicy` 的辅助方法和 `fallback`，记录其 `act`/`chat` 静默捕获的辅助方法异常（`policy_errors`）和策略内部兜底（`policy_fallbacks`）；运行时重建策略后会重新包装，生成器内部异常也会计入。任一出现就阻止通过。计数可能描述同一个事件，不能把它们相加当成独立事故数。包装无法识别任意新辅助函数在自己内部完全吞掉且不触发兜底的错误；完整单元测试仍然必要。

## 已知模拟器差异

独立检查发现以下限制；本工具没有改动公共模拟器，旧评测仍可比较。

- `local/werewolf_env.py:Game.state` 在投票和夜晚仍返回当日的 `new_messages`；官方文档规定阶段切换时清空。`local.werewolf_platform.Server.state` 已正确清空。因此快模拟可能让提前停止监听的策略在投票时补读遗漏的聊天，夸大发言/监听收益。候选必须另通过真实 runner 的平台替身测试，且最好用真实平台确认。
- `rt` 把我方 `ContestantSeat` 当成快速规则策略，在其他席位前发言，并被每个事件唤醒；`rounds` 每轮随机排列所有人。都不能复现任意真实模型延迟和网络调度；两种时序都要看。
- 对手是脚本，语言理解/误读/相信声明的概率大多是设定值。对抗脚本的提升是开发证据，不等于对真实参赛者提升。
- 本地超时检查在函数返回后计数，不能中止死循环；生产 `Contestant` 自身的看门狗仍工作。需要外部进程超时来防范一个完全卡死的自定义工厂。
- 本工具没有测量真实部署并发内存、网络错误恢复或消息送达；这些由完整测试、自检和平台替身覆盖。

相关文件：`local/eval_werewolf.py` 保留既有混合桌评测接口；`local/werewolf_platform.py` 跑真实 runner；`tests/strategy/test_werewolf_benchmark.py` 检查统计、配对完整性、候选配置隔离、合成记录限量及被兜底掩盖的异常。
