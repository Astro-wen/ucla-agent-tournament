# Werewolf strategy investigation — 2026-10-12

Base: `origin/main` at `cda2dd9`; branch: `strategy/werewolf-evidence-search`.
The frozen `champion/` remains the comparison target. The production runtime and
the other two game strategies are outside this change.

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

Final confirmation and validation measurements will be appended after the
source freeze. The current default is not claimed to be substantially stronger
overall, and the optional public-vote strategy is explicitly a tradeoff.
