# Werewolf strategy investigation — 2026-10-12

## 2026-10-13 扩展研究：来源与策略清单

以下是主要策略家族的系统清单，不是数学上“所有可能策略”的穷举。已实际取得并核对24篇论文、8份队伍算法说明、平台规则和2页官方排名，共35条来源。作者、日期、实际URL、抓取状态/哈希、短摘录、规则差异和证据限制在 [`data/werewolf_strategy_sources.json`](../data/werewolf_strategy_sources.json)。35条不等于35次独立复现：多个AIWolf团队共享祖先，相关性已标明。新实验与最终状态见 [`werewolf_benchmark.md`](werewolf_benchmark.md) 的08:00续研段；下文原2026-10-12记录保持历史含义。

“信念”是对可能身份分配的权重，不是确定事实；“信息增益”是减少不确定性，不等同胜率；“析因实验”把各组件分别开关，测主效应和组合之间的交互。论文中的有医生、狂人、随机平票处决、不公开死亡身份、禁止谎言等条件，均不能直接套用本赛。

| ID | 策略家族 | 本地研究方式或边界 |
|---|---|---|
| T01 | 联合身份的贝叶斯推断 | 105种世界枚举；硬约束、手工软权重、离线学习分别测试 |
| T02 | 硬事实与软断言分层 | 私有查验/公开死亡优先；谎言不能成为硬事实 |
| T03 | 可信度校准与重复证据限权 | Brier、对数损失、ECE与复制不放大守护测试 |
| T04 | 角色声明和查验时间线 | 记录说话人、日期、目标；区分反跳、更正、转述 |
| T05 | 多预言家假设 | 保留村民谎报与解析不确定性，不自动认证单个声称 |
| T06 | 投票关系与反断联 | 允许狼投狼，不把曾投狼的人视为确定好人 |
| T07 | 票势预测与身份预测分离 | 预测投给谁与判断谁是狼是两个问题 |
| T08 | 夜刀反推及栽赃防御 | 多刀法失配测试；不把幸存预言家自动判假 |
| T09 | 查验期望信息增益 | 与最高狼概率查验比较，未发现稳定收益 |
| T10 | 查验的下一票价值 | 测组织者查验；不能用全知模拟夸大查验价值 |
| T11 | 预言家条件式公开 | 反跳、黑查、受压、救白人；不无条件首日跳 |
| T12 | 清白集合与发布顺序 | 真实查验与推测分开，测试对手是否真的跟票 |
| T13 | 放逐声明者进行有成本验证 | 必须先算验证前是否已经输，不能写成普适定理 |
| T14 | 死亡认证及历史报告追溯 | 身份被确认不自动保证每句报告正确 |
| T15 | 集中票与枢纽选票 | 独立活人支持、承诺兑现、噪声票势与反例 |
| T16 | 平票、弃票、错杀的状态比较 | 穷举3/4/5人合法票型；四人一狼是重要反例 |
| T17 | 狼人分票与停滞策略 | 只用合法投票；不能把报票当实际票 |
| T18 | 小残局精确搜索 | 3,408种票型检查；精确规则不等于正确对手模型 |
| T19 | 多世界保守规划 | 同一信息集必须选择同一动作，禁止全知分支选择 |
| T20 | 信息集蒙特卡洛树搜索 | 文献研究，完整语言搜索未复现；先守住信息边界 |
| T21 | CFR及有限策略混合 | 文献研究；七人隐藏阵营不自动继承双人零和保证 |
| T22 | 近似同价值动作的随机化 | 需同时保留好人集中票与狼队协调，不是均匀乱选 |
| T23 | 狼人的角色对称伪装 | 共享中性开场、延迟报票、public_vote及组合已测 |
| T24 | 主动假跳与被查杀反跳 | 新晚报对手、现有反跳与既有主动假跳失败记录 |
| T25 | 狼人真实信息与公开故事双台账 | 角色保密、前后承诺与发言额度守护测试 |
| T26 | 断联、卖队友、救队友 | 新条件卖队友/分散对手；反例禁止无条件救队友 |
| T27 | 夜刀多目标价值 | 窄范围机械投票续局搜索有小幅收益 |
| T28 | 游戏内狼队私信协调 | 显式on/off与public_vote的正交组合；只向活同伴 |
| T29 | 说话者可信度与指控证据分解 | 排除重复灌输与未经证实的多数；不冒充官方 |
| T30 | 支持与质疑动态切换 | 条件合票、基线保留、独立切换型对手 |
| T31 | 定向询问与承诺探测 | 资料支持但未实现专用问答策略，不能宣称已验证 |
| T32 | 短证据式说服 | 对照查验表述，区分脚本理解缺陷与真实模型 |
| T33 | 消息额度与终止时机 | 1/3次后续发言开关，五条配额及重试/恢复测试 |
| T34 | 本局结构化记忆 | 不跨局学习；事实、承诺、已发送消息分别记账 |
| T35 | 语言模型外置规则内核 | 官方LLM示例接免费本地模型；不让聊天决定工具调用 |
| T36 | 异质对手种群 | 28类析因面板、新8类独立对手、角色与座位分层 |
| T37 | 置信度门控与风险控制 | 学习模型低优势保留基线；零误删不保证高胜率 |
| T38 | 否定、引述、别名等语言守护 | 元数据扰动、私聊隔离、重复消息和合法动作测试 |
| T39 | 信念、语言、行动一致性 | 更准概率仍可能投错目标；动作与发言分离诊断 |
| T40 | 座位对称性与身份侧信道 | 64组合全部身份可见信息等价检查、旋转硬查验测试 |

补充接口证据（07:38完成）：另核验4篇关于选项位置/编号偏差、结构化输出和解释忠实性的论文，11条短引文与本地116条诊断重算见 [`data/werewolf_interface_sources.json`](../data/werewolf_interface_sources.json)。论文并不一致支持去掉编号或强制JSON：有任务退步，也有任务改善；理由与答案表面一致不等于解释忠实，更不等于动作正确。后续要用未参加选择的新题、平衡选项顺序和座位置换，而不是只引用本轮25/29。

### 已明确否定的迁移

- `2408.17177v2` 的honesty rule禁止公开谎言，且其部分分析在狼与好人等人数时继续投票，与这里不符。
- 2019 AIWolf冠军takeda的排名及算法均查到一手资料，但其五人单狼局、狂人/护卫规则不能复制；我们只借鉴联合角色推断与策略层分离。
- DeepRole的论文是 *Finding Friend and Foe in Multi-Agent Games*（1906.02330），不是 *Finding Friends and Flipping Frenemies*。
- 准确概率、低熵、活得久、少暴露身份、发言更流畅，都不是阵营胜率的替代指标。本轮负结果也保留。


Base: `origin/main` at `cda2dd9`; branch: `strategy/werewolf-evidence-search`.
The frozen `champion/` remains the comparison target. The production runtime and
the other two game strategies are outside this change.

Follow-up: the [defensive guard report](werewolf_guard_strategy.md) covers the
current `cde8bc2` implementation and its separate 336,000-game confirmation. The
369,600-game results below describe the earlier frozen research revision and
must not be presented as a new measurement of the guarded implementation.

## Objective and game model

Improve the probability that our single tournament seat wins, rather than its
survival probability. Seven seats contain two wolves, one seer and four villagers;
dead teammates still win with their side. Day votes are hidden until resolved,
plurality wins, ties eliminate nobody, and wolves win at parity. Night one is
peaceful. These distinctions make coalition votes, trustworthy seer information,
and preserving likely future mis-eliminations valuable.

Existing strengths are legal-action filtering, deterministic coordination,
reactive listening, role cross-checks, bounded chat templates and reproducible
mixed-table evaluation. Existing opponent families include silent first-action
and random baselines, heuristic and LLM-style seats, and four adversarial bots.
LLM-style seats in the offline benchmark are scripted; they are not live models.

## Development plan and evidence rules

1. Verify the current tests and freeze the baseline by source hash.
2. Screen role-specific hypotheses: decisive-day wolf voting, night targets,
   seer disclosure, and reading accusations in their proper context.
3. Compare on identical deals and seats. Evaluate the realistic one-seat case
   first, then several candidate seats with only one side changed at a time.
4. Reserve development seeds 151–159 for experiments. Reserve 28101 and 28102
   for final held-out evaluation after selecting and freezing the candidate.
5. Report differences by role, opponent and timing, together with uncertainty,
   complete pairing checks, crashes, illegal actions, rejected chats, timeouts
   and fallback counters. Never treat a small-sample tie as proof of parity.
6. Run the full test suite, recorded-state replays, self-check and the official
   runner against the local Werewolf platform. The recorded platform corpus
   currently contains no real Werewolf matches, so its replay cannot establish
   live Werewolf playing strength.

Local simulations use `ARENA_RECORD=0`. Diagnostic outputs are compact synthetic
benchmark records, with source hashes and settings, not production recordings or
credentials. Development experiments do not consume paid model calls.

## Promotion boundary

A favorable offline result supports a candidate branch, not an assertion of live
tournament superiority. `docs/v5_standard.md` includes additional promotion
requirements and live validation. Do not freeze `champion/`, merge `main`, or
change a running tournament agent merely because a development screen improves.
The final report must record what passed, what regressed and what remains untested.

## Candidate design

The default candidate corrects evidence interpretation: quoted, hypothetical,
uncertain and negated statements do not become asserted results; a check result
must refer to the checked player; ambiguous display-name aliases cannot rebind
canonical seats. A publicly revealed seer invalidates rival claims even if the
seer died without speaking. Causal result phrases such as "because I checked"
retain their meaning. These changes address concrete counterexamples, rather
than claiming a large average win-rate gain from parsing alone.

A seer that already disclosed clear results can now rebut a later false seer
claim once that day. Previously, the daily `claim` topic suppressed the new
information. The extra topic survives restart, obeys the existing runtime
follow-up quota, and never treats an uninvestigated rival as a verified wolf.
Targeted tests reproduce the missing response and exercise the actual runtime;
development stress tests did not establish a win-rate gain from this repair.

The optional wolf profiles are separate research choices:

| `ARENA_WW_TACTICS` | Behavior | Main tradeoff |
|---|---|---|
| unset / `off` | Established wolf tactics plus evidence fixes | Default; no experimental opening |
| `public_vote` | On day 1, before any seer claim, announce and cast the same vote an uninformed villager would choose, even against an ally | Hides partner-exclusion behavior, but can sacrifice the partner to persuadable villagers |
| `no_skip` / `on` | When that public target is the wolf's ally, use a neutral opening; preserve the established ballot | Gains against some scripts, but its unique sentence can identify the speaker as a wolf |

Neither profile inspects simulator state, the hidden deal, opponent type, seed or
other seats' private observations. The villager persona uses the wolf's allowed
public record with role/allies/investigations removed. `public_vote` uses the
established village message and action, avoiding the new exact-sentence tell
introduced by `no_skip`. Neither profile is enabled by default or approved for
production by this branch.

## Development results and rejected approaches

These are selection data, not independent confirmation. Several variants reused
development seeds, so their confidence intervals must not be interpreted as
adjusted for the entire search. The older screening harness reports paired
normal intervals; final confirmation uses the audited benchmark and also reports
the more conservative interval described in `werewolf_benchmark.md`.

| Experiment | Development evidence | Decision |
|---|---|---|
| Evidence fixes only | Seed 151, 5,500 paired single-seat tables: +0.18 pp overall; exact p=.143. All non-chaos opponent outcomes identical to champion | Correctness improvements; no broad strength claim |
| Public voting before claims | Seed 151, 5,500 pairs: wolf +3.39 pp [2.06, 4.72], overall +1.11 pp [0.69, 1.53]. Wolf gains: detective +21.88 pp, talker +10.85 pp, chaos +15.33 pp; losses: llmq -5.00 pp, llmx -4.32 pp | Keep as a selectable tradeoff; does not dominate the baselines |
| Neutral ally-protecting opening | Seed 157, 5,500 pairs: wolf +1.49 pp [0.69, 2.30], overall +0.58 pp [0.29, 0.87] | Experimental only; independent review found a new exact-sentence role tell |
| Proactive fake-seer claims | Promising with round-robin speaking, but negative under realistic fast-agent timing; unrestricted bluffing lost about 13 pp as a wolf | Reject |
| Night-kill lookahead and changing tied kill targets | Some first-action improvements, offset by losses against random, varied-language and chaos opponents; no stable aggregate gain | Reject |
| Delayed first messages, including corrected follow-up accounting | Detective gains offset by wolfpack losses; corrected wait-three overall +0.35 pp [-0.45, 1.14] on seed 157 | Reject |
| Early seer disclosure, alternate investigations, vote-pressure coalitions | No repeatable meaningful gain across development samples | Reject |
| Even-parity abstention / terminal wolf vote alignment | Only a handful of extra wins, without sufficient evidence of a useful gain | Reject |

One important proxy limitation: some scripted town opponents follow a lone seer
claim regardless of a non-claimer's truthful rebuttal, then choose randomly
between multiple claimants. Better truthful persuasion cannot change those
scripts' decisions. We did not add parser-targeting messages or new fake claims
by villagers to inflate their scores.

Development summaries and temporary prototypes are preserved locally under
`logs/werewolf_benchmarks/development_archive/`, with checksums in `inventory.json`.
They are historical exploratory snapshots; later base-policy changes mean they
are not a substitute for a frozen manifest. Large synthetic game logs remain
untracked. The benchmark documentation also records the exact champion mirror
and the independent neutral-sentence sensitivity test.

## Predeclared confirmation

After the default fixes and optional public-vote profile are committed, freeze
the source and run each profile against champion on **both** reserved seeds
28101 and 28102. Use all eleven opponent families: first, heuristic, random,
llm, llmx, llmq, mixed, rt_wolfpack, rt_talker, rt_detective and rt_chaos.
For each profile: realistic `rt` timing gets 1,000 deals per seed/opponent/seat
configuration; `rounds` robustness gets 400. Include k=1 and side-isolated k=3.
Do not pool the same deals across the two timing models as independent samples.
Keep the existing 2 pp aggregate improvement requirement and regression checks
unchanged, even if an optional profile has large gains on selected opponents.

## Frozen confirmation results

Realistic `rt` timing completed **264,000 simulated games** across the two
profiles. Each profile's primary comparison contains 22,000 paired single-seat
tables, including 6,312 wolf-seat tables. `public_vote` versus champion gained
**0.927 percentage points overall** (conservative 95% CI [0.619, 1.236], exact
p=7.23e-21) and **2.947 pp as a wolf** ([1.981, 3.908], p=5.37e-21).
Both reserved seeds support the direction: overall +0.909/+0.945 pp; wolf
+2.761/+3.128 pp. These are absolute percentage-point changes, not relative gains.

The full single-seat wolf comparison is below, including every loss:

| Opponent | Public-vote win rate | Champion win rate | Change (pp) | Paired tables |
|---|---:|---:|---:|---:|
| first | 64.30% | 64.30% | 0.00 | 577 |
| random | 92.27% | 93.13% | -0.86 | 582 |
| heuristic | 46.91% | 41.58% | +5.33 | 582 |
| llm | 95.33% | 97.75% | -2.42 | 578 |
| llmx | 89.98% | 95.35% | -5.37 | 559 |
| llmq | 92.08% | 95.21% | -3.13 | 543 |
| mixed | 83.94% | 84.84% | -0.90 | 554 |
| rt_wolfpack | 91.18% | 92.21% | -1.04 | 578 |
| rt_talker | 74.09% | 63.48% | +10.61 | 575 |
| rt_detective | 58.71% | 45.18% | +13.54 | 591 |
| rt_chaos | 64.08% | 48.74% | +15.35 | 593 |

The three large adversarial gains have conservative intervals entirely above
zero: talker [7.20, 13.84], detective [9.85, 17.00], chaos [10.45, 19.99] pp.
The llm/llmx/llmq losses are statistically significant too. Scripted LLM-style
seats are not live language models. The separate k=3 stress results cannot be
substituted for these single-seat outcomes.

**No profile passes the full promotion gate.** The public-vote profile's aggregate
gain is below the predeclared 2 pp threshold and there are opponent regressions.
The default correctness fixes gain only +0.132 pp overall (conservative interval
[-0.022, 0.285], exact p=.00815); they also have a significant k=3 wolf loss
against chaos (-1.91 pp, p=.0093). The conservative interval uses a wider
Bonferroni-Wilson construction rather than inverting the exact paired test;
the gate requires both, so a positive exact result does not override a failed
interval gate. Keep the frozen champion and deployed configuration;
the branch provides a measured optional tradeoff, not evidence of overall or
live-tournament superiority.

Both `rt` runs have complete pairs, stable source, zero protocol errors,
timeouts, internal strategy exceptions or fallbacks. p99 decision time is at
most 0.5 ms and the largest observed decision is 75.8 ms.

The `rounds` robustness comparison also completed: public-vote +0.818 pp
overall ([0.408, 1.227], p=6.09e-10), +2.186 pp as a wolf ([1.010, 3.353],
p=1.09e-8). Its promotion gate still fails. Directly comparing public-vote with
the corrected default isolates the tactic's wolf gain: +2.772 pp in `rt` and
+1.952 pp in `rounds`; both conservative intervals exclude zero. All isolated
villager/seer outcomes match exactly between the profiles, and all paired
champion outcomes match between profiles within each timing model.

Across four runs there are **369,600 simulations and 123,200 paired comparisons**.
Deals repeat across profiles and timing models; this is not 123,200 independent
draws. Every complete-pair, source-stability and runtime-failure check passes.
Round-robin timing includes 6,782/6,783 normal discussion-window closures across
the default/public-vote runs' branches, tracked separately from actual timeouts;
there are no background protocol failures. An independent agent recomputed all
672 candidate/champion metric slices and 336 direct-profile slices from the four
runs' raw records, including exact integer-binomial tests and both confidence
intervals, and matched the report. It also independently verified the 660-test
multiplicity analysis, file checksums, configurations and counters.

All slices are reported, not only winners. A secondary Bonferroni check over 660
binary opponent comparisons still supports the `rt` wolf gains against heuristic,
chaos, detective and talker, **and the loss against llmx**. In `rounds`, the wolf
gains against chaos and talker survive that conservative correction. This is
context for individual claims; the original promotion gates were never relaxed.
Full metrics, confidence intervals, gates, counters, commands, source manifests
and raw-log checksums are in [werewolf_confirmation.json](werewolf_confirmation.json).

## Validation on the frozen source

Source freeze: `cc95bb87edef32ae08afada2d443c6ff5eae9848`;
source manifest digest:
`9beb58f5a9722373edec484e7b1c92183144494963626e778bf22ced5b04fec2`.
Documentation-only updates after this revision do not change the evaluated source.

- Full local suite: **2,418 passed, 41 skipped**, 437.17 seconds. The skips are
  platform-dependent checks on macOS. The frozen implementation also passed
  [Linux CI](https://github.com/Astro-wen/ucla-agent-tournament/actions/runs/38344969208):
  **2,458 passed, 1 skipped**, 1,037.14 seconds, with syntax/undefined-name lint
  passing. Python 3.13.9 and Node 25.2.1 were used locally; CI uses Python 3.12 and
  Node 22. An earlier run set `ARENA_NODE` explicitly and bypassed two tests'
  deliberately slow PATH shim; removing that test-environment override resolved
  both failures. The clean full run above used standard PATH discovery.
- Syntax/undefined-name lint passed across the repository; `git diff --check`
  passed. The official SDK and frozen champion have no changes.
- Self-check: ready, zero failures. Both 80-decision replay passes (calculator
  enabled/disabled) had zero rejected actions; slowest 0.89/0.86 seconds. The
  isolated development checkout has no tournament key configured.
- Full recorded-state replay: **2,349 real decisions from 142 files**, zero
  failures, zero joint repairs, zero classic fallback on 1,296 engine-search
  decisions. Overall p99 795.1 ms, maximum 1,072 ms. A separate calculator-off
  pass also replayed all 2,349 decisions without failures or repairs. The corpus
  contains no real Werewolf match, so these passes check cross-game regressions,
  not the new Werewolf strategy's tournament strength.
- Official SDK runner against the local platform: **62 games**, zero seat
  errors, hung seats, idle auto-actions or rejected moves/chats. This includes
  20 ten-second-window games for each profile, two normal 120-second-window
  games with delayed scripted opponents, and 20 games with discussion disabled.
  Slowest decision 8.1 ms. Expected phase-race `WRONG_PHASE_NOOP` responses were
  40/48/7/0 respectively; there were no listening errors. These are protocol
  checks, not a statistical playing-strength comparison.

Full logs are saved under
`logs/werewolf_benchmarks/validation-cc95bb8/`. Synthetic benchmark records live
in neighboring named run directories; neither is mixed into real replay inputs.
