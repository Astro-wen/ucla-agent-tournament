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

## Results

In progress. Commands, sample sizes and final measurements will be added after
the candidate and independent validation are complete.
