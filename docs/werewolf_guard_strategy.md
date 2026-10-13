# Werewolf defensive guards — 2026-10-13

Base candidate: `1ff9c4c` on `strategy/werewolf-evidence-search` (PR #1).
The user's follow-up asks for defenses against cheap manipulation, ordinary
strategies, and opponents that do little or nothing.

## Scope and evidence rules

Protect decisions against repeat-message pressure, fabricated or contradictory
seer evidence, misleading quotations and player names, and needless sacrifices
at weak or silent tables. Use only the seat's permitted observation. A plausible
false claim cannot always be distinguished from a real claim in a hidden-role
game; tests must not promise immunity to every opponent.

Development seeds: **161–169**. Preregistered confirmation seeds: **29301 and
29302**, both required by `--seed-registry guards --split heldout`. The earlier
28101/28102 confirmation has already been consumed and will not be reused to
select this revision. All synthetic output stays in
`logs/werewolf_benchmarks/guard-*`, with recording/state persistence disabled.

Before changing production code, captured 6,600 paired single-seat scenarios
(13,200 games) on development seeds 161/162, all 11 established opponent families,
with `ARENA_WW_TACTICS=public_vote`. The existing audited benchmark is unchanged
apart from the explicitly named new seed registry. The development comparison verified
matching deals and champion outcomes before measuring this pre-guard run.

Use an independent panel of simple legal opponents, including one-attacker tables
among ordinary seats. Test protocol correctness and the attacker's information
boundary; do not give attackers the hidden deal or our controlled-seat identity.
Report silent/fixed/random/heuristic and established strong opponents alongside
attack cases, including any regressions. The existing promotion gates remain
unchanged. Production deployment and champion promotion are outside this change.

## Implemented defenses

- Count an unverified accusation at most once per speaker and target across the
  transcript. Different wording, five messages, a later day or a repeated vote
  appeal cannot manufacture extra witnesses. Distinct speakers remain distinct
  weak evidence. Repeated injection attempts also receive one bounded penalty.
- Reject contradictory seer reports: a seat cannot be both wolf and clear; a
  speaker cannot consistently claim both seer and wolf, in either message order.
  Preserve these facts through the existing transcript/restart memory. A revealed
  seer's identity still invalidates rivals, but does not authenticate that seer's
  contradictory results. Private investigation results retain priority.
- An obviously invalid claimant cannot bait a wolf into a counterclaim, displace
  a credible night target, or turn off the public-vote profile by merely claiming.
- Do not convert copied speaker lines, Markdown quotations, hypothetical or
  negated vote calls into the sender's own evidence. Apply injection detection to
  the same normalized surface that the claim reader sees.
- Protect canonical seat labels before resolving display names. Duplicate,
  ambiguous and grammar-like names (including punctuation variants) cannot erase
  negation or steal a verdict's subject. Ordinary unique names, including
  multiword/hyphenated names, and genuine compact results remain supported.

These guards apply to both default and optional tactics. They do not enable
`public_vote` or the known-tell `no_skip` experiment by default. No simulator
internals, opponent-type labels, hidden deal or other player's private state are
read by production strategy code.

## Development evidence and rejected guards

The independent panel reproduced the attack: on ordinary heuristic tables with
one attacker, changing one accusation into five identical legal messages reduced
the pre-guard candidate's win rate by 4.25 points across 400 paired tables. Repetition
increased one unsupported accusation from 0.25 to 1.25 suspicion, enough to override
an actual vote-record score of 1.0. The guard keeps it at 0.25.

Across 9,600 paired guard-panel development scenarios, guards versus pre-guard
`public_vote` improved overall win rate by 0.354 points (conservative 95% CI
[0.086, 0.622], exact p=4.45e-5), mostly through villagers (+0.703 points). Seer
outcomes were identical. Wolf delta was −0.149 points (p=.503). The one-versus-five
accusation contrast became outcome-identical on all 400 first-action tables and
all 400 heuristic tables. It remains harmful in LLMQ tables because those other
scripted players can still be persuaded; our guards cannot protect their choices.
Silent, abstaining, fixed-vote and echo-table outcomes were unchanged.

A separate search tried 39,000 development simulations of tactical safeguards.
Requiring independent supporters before an unclaimed ally ballot changed no
winners. Extending a two-supporter rule to claim fights reduced wolf wins
by 2.90 points; delaying only ally-targeting openings did not fix the weak-table
tradeoff. None was landed. The input defenses close demonstrated failure modes
without claiming that these rejected tactics improve playing strength.

These are development results, not fresh confirmation. An intermediate 11-opponent
screen changed only 2 of 6,600 outcomes (both losses against chaos); all first,
random, heuristic, LLM-style, mixed and other strong-opponent outcomes matched.
Subsequent review fixes are covered by the frozen confirmation below.

## Independent review

Reviewers reproduced and fixed genuine compact-result regressions introduced by
the first guard draft (`Player5: wolf`, arrows and compact own-role labels),
inconsistent reports from a publicly revealed seer, cross-message self-role
contradictions, and punctuation variants of grammar-like aliases. Positive tests
preserve genuine own quoted results and unique normal display names.

The 555-message existing real/scripted/chaos corpus retained every claim, wolf and
clear result; only 2 false endorsements from negated vote language were removed.
Mean parser time was 0.102 ms per message in the final audit under concurrent
benchmark load (pre-guard: 0.085 ms in that audit). Independent review found no
hidden-information access, illegal-action path or unbounded-runtime issue. The
parser remains a bounded English-pattern reader, not a proof of arbitrary-language
understanding or immunity to plausible deception.

## Final confirmation design (registered before reserved-seed results)

Freeze implementation at `cde8bc2`. Run seeds 29301/29302 together, one controlled
seat per table, realistic `rt` timing with 400 deals per opponent/seed and `rounds`
timing with 200. Run both `ARENA_WW_TACTICS=off` and `public_vote`.

The established panel contains all 11 original backgrounds. The guard panel has
9 homogeneous probes (silent, abstain, fixed, echo, single accusation, repeated
accusation, fake seer, contradictory seer, quotation/conditional bait), plus
15 one-attacker conditions: each of first/heuristic/llmq with one accuse/repeat/
fake/contradict/bait seat. All 35 conditions are reported, including negative ones.

For each condition, measure both the guarded implementation and a pre-guard
`1ff9c4c` source archive, each against the unchanged champion on identical deals.
The archive receives only the same evaluator registration, guard-bot module and
benchmark seed-registry files; its production agent remains byte-identical to
`1ff9c4c`. All source files are hashed. Direct pre/post comparisons require exact
matching deals and champion outcomes. Both sources, both profiles, both timings
produce **336,000 simulated games**. Repeated deals are not extra independent
samples. A strategy revision after seeing these results would require fresh
confirmation seeds.

Primary defense question: do the demonstrated cheap attacks lose their influence
without a material regression against ordinary/strong opponents? Require unit-level
invariants, no protocol/strategy failures, and report the direct paired effect of
the guards with uncertainty for each role/opponent. Also retain the benchmark's
unchanged 2-point overall promotion gate; robustness fixes are not permission to
promote the experimental public-vote profile. The current one-seat confirmation
does not extend the earlier multi-seat strength claims to this revision.

## Final confirmation results

Completed all **16 runs / 336,000 games**. The reserved seeds are now consumed;
do not use them to select a further revision. Production source stayed frozen at
`cde8bc2`; subsequent `a0bc5ad` only repaired an obsolete test fixture and added
its complementary negative case. The aggregate
[`werewolf_guard_confirmation.json`](werewolf_guard_confirmation.json) includes
commands, configurations, source manifests, raw-log hashes, every opponent/role
slice, both confidence intervals, and the unchanged promotion gates.

The comparisons below isolate **guards versus the same pre-guard profile**, using
identical deals and champion outcomes. All changes are percentage points. CIs are
conservative paired 95% intervals, and p-values are exact paired McNemar tests.
The two timings and two profiles are analyzed separately, not pooled. Overall
panel means weight each scripted background equally; role slices pool the
matching-role scenarios. Neither estimates the real tournament opponent
distribution.

| Profile / timing | Established panel, all roles | Guard panel, all roles | Guard panel, villager | Guard panel, wolf |
|---|---:|---:|---:|---:|
| Default / realtime | −0.034 [−0.144, +0.076] | +0.307 [+0.103, +0.511] | +0.692 [+0.404, +0.980] | −0.321 [−0.688, +0.047] |
| Public vote / realtime | −0.034 [−0.144, +0.076] | +0.307 [+0.104, +0.510] | +0.692 [+0.404, +0.980] | −0.321 [−0.679, +0.038] |
| Default / rounds | −0.068 [−0.261, +0.125] | +0.365 [+0.059, +0.670] | +0.747 [+0.291, +1.202] | −0.248 [−0.761, +0.265] |
| Public vote / rounds | −0.045 [−0.227, +0.136] | +0.365 [+0.056, +0.673] | +0.747 [+0.291, +1.202] | −0.248 [−0.785, +0.289] |

The established panel has 8,800 paired scenarios per realtime comparison and
4,400 per rounds comparison. All changed outcomes there are against chaos; the
other ten backgrounds match exactly. The guard panel has 19,200 and 9,600 paired
scenarios respectively. Realtime guard-panel overall p-values are 2.84e-6
(default) and 2.44e-6 (public vote). Realtime wolf p-values are .00792 and .00643,
although their more conservative intervals include zero. Both are reported rather
than choosing the more favorable uncertainty measure.

The strongest targeted result is `heuristic+1guard_repeat`: ordinary heuristic
players with one seat repeating an accusation. Both profiles give the same direct
guard effect:

| Role / timing | Pairs | Before → after | Change [95% CI] | Better / worse | Exact p |
|---|---:|---:|---:|---:|---:|
| Villager / realtime | 465 | 32.26% → 39.14% | **+6.882 [+2.663, +10.953]** | 38 / 6 | 9.43e-7 |
| Wolf / realtime | 235 | 63.83% → 59.57% | **−4.255 [−8.286, −0.047]** | 0 / 10 | .001953 |
| Villager / rounds | 226 | 28.32% → 37.17% | **+8.850 [+2.123, +15.191]** | 23 / 3 | 8.80e-5 |
| Wolf / rounds | 122 | 64.75% → 59.84% | **−4.918 [−11.357, +1.910]** | 0 / 6 | .03125 |

The villager gains survive a Bonferroni correction across all 560 direct
opponent/role comparisons (adjusted p=.000528 realtime, .04927 rounds). The wolf
losses do not survive that correction, but they remain a practical tradeoff and
are not dismissed. All 38 negative direct opponent/role slices are retained.

After guarding, one accusation and five repetitions produce **identical final
outcomes** on every paired first-action and heuristic table: 800 realtime and
400 rounds scenarios per ordinary background, in both profiles. This verifies
the specific repetition defense. It does not make all attacks harmless: other
LLMQ seats remain persuadable, with repetition still reducing our realtime wolf
wins by 14.29 points in default and 12.24 points in public vote at those tables.

Silent, abstaining, fixed-vote and echo opponents are explicitly tested, with no
pre/post outcome changes. Default realtime win rates are 48.75%, 50.88%, 52.13%
and 74.00% respectively. Fake-seer and contradictory-seer homogeneous tables also
remain unchanged and difficult: 36.13% / 33.88% overall and 16.92% / 14.32% as
villager. Correctly rejecting an impossible claim does not reveal which remaining
player is a wolf or make the other seats vote well. There is no claim of beating
every simple strategy, and silence is not treated as confirmed guilt.

Against the frozen champion on the established realtime panel, the guarded
default is +0.057 points and optional public vote is +0.852 points. Neither
passes the preregistered 2-point overall improvement gate; public vote also has
opponent regressions. **No promotion, merge, or tournament reconfiguration is
justified by this evaluation.** Public vote remains opt-in. The defenses close
specific input-manipulation failures; broad playing-strength improvement remains
unproven.

## Tests, runtime checks and audit trail

- Full local suite: **2,598 passed, 41 platform-related skips**, 434.65 seconds,
  on Python 3.13 / Node 25. No failures. This adds 178 guard/panel tests plus a
  seed-registry test and the complementary seer-target fixture case. Coverage
  includes legal actions, limited information, repetition/order/restart invariants,
  contradictory claims, quotes, names, empty tables and positive parsing cases.
- [Linux CI at `a0bc5ad`](https://github.com/Astro-wen/ucla-agent-tournament/actions/runs/38365601607)
  passes on Python 3.12 / Node 22: **2,638 passed, 1 skipped**, 1,043.54 seconds,
  with syntax/undefined-name lint also passing. Later report-only commits do not
  change the tested implementation.
- Repository syntax/undefined-name lint passes. The obsolete seer-target fixture
  was split into a credible claim and a claim disproved by a publicly dead seer;
  the final full-suite result above includes both cases.
- All 336,000 simulated games have **zero protocol, strategy, actual timeout,
  internal exception, fallback or logged-error failures**. The 8,792 ordinary
  discussion-window closures in rounds mode are counted separately. Decision
  p99 is at most 1 ms; current-candidate maximum is 44.571 ms (47.984 ms across
  all measured candidate/champion sources).
- Replayed all **2,349 recorded decisions from 142 files**, both with and without
  the calculator: zero failures or joint repairs; all 1,296 engine decisions use
  search, with zero classic fallbacks. Replays contain no real Werewolf matches
  and are cross-game regression checks, not evidence of live Werewolf strength.
- **62 official-SDK runner games against the local platform** cover default and
  public vote, no-messaging play, and the full 120-second discussion window.
  Zero errors, hangs, idle auto-actions or rejected actions. Expected
  `WRONG_PHASE_NOOP` race responses are separately recorded. Self-check is ready;
  its sole warning is the intentionally absent live key in this checkout.
- An independent implementation rechecked every raw record and **4,688 statistic
  slices**, exact integer-binomial p-values, both intervals, pairing, source
  hashes, counter sums and promotion gates. It confirmed all reported gains and
  regressions. Separate multiplicity families contain 560 guard-effect, 96
  repetition-diagnostic and 280 profile-effect comparisons.

Large raw and scratch outputs remain ignored. Reproducible commands and SHA-256
hashes are committed in the aggregate; local records are under
`logs/werewolf_benchmarks/guard-final-*`, review scripts and audit records under
`guard-development/`, and final test/replay/runner logs plus their inventory under
`guard-validation-cde8bc2/` in that same directory. Synthetic bounded traces use
no production key and do not enable recording or persistent match state.

For a small **development** recheck (not a fresh confirmation):

```sh
ARENA_RECORD=0 ARENA_STATE=0 ARENA_SUMMARY=0 .venv/bin/python -m local.werewolf_benchmark \
  --seed-registry guards --split development --seeds 169 --games 100 --ks 1 --jobs 2 \
  --backgrounds first,random,heuristic,guard_silent,guard_abstain,heuristic+1guard_repeat \
  --candidate-env ARENA_WW_TACTICS=off --trace-games 2 \
  --out logs/werewolf_benchmarks/guard-recheck-169
```

Use `ARENA_WW_TACTICS=public_vote` in the candidate environment to examine the
optional tactic with the same guards. Follow the seed registry and promotion
requirements in [`werewolf_benchmark.md`](werewolf_benchmark.md) for any further
research; consumed confirmation seeds are not a new selection set.
