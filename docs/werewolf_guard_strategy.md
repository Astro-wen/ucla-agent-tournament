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

Development seeds: **161–169**. New reserved confirmation seeds: **29301 and
29302**, both required by `--seed-registry guards --split heldout`. The earlier
28101/28102 confirmation has already been consumed and will not be reused to
select this revision. All synthetic output stays in
`logs/werewolf_benchmarks/guard-*`, with recording/state persistence disabled.

Before changing production code, captured 6,600 paired single-seat scenarios
(13,200 games) on development seeds161/162, all11 established opponent families,
with `ARENA_WW_TACTICS=public_vote`. The existing audited benchmark is unchanged
apart from the explicitly named new seed registry. A later comparison will verify
matching deals and champion outcomes before comparing with this pre-guard run.

Add an independent panel of simple legal opponents, including one-attacker tables
among ordinary seats. Test protocol correctness and the attacker's information
boundary; do not give attackers the hidden deal or our controlled-seat identity.
Report silent/fixed/random/heuristic and established strong opponents alongside
attack cases, including any regressions. The existing promotion gates remain
unchanged. Production deployment and champion promotion are outside this change.

## Results

Implementation and validation in progress.

## Final confirmation design (registered before reserved-seed results)

Freeze implementation at `cde8bc2`. Run seeds29301/29302 together, one controlled
seat per table, realistic `rt` timing with400 deals per opponent/seed and `rounds`
timing with200. Run both `ARENA_WW_TACTICS=off` and `public_vote`.

The established panel contains all11 original backgrounds. The guard panel has
9 homogeneous probes (silent, abstain, fixed, echo, single accusation, repeated
accusation, fake seer, contradictory seer, quotation/conditional bait), plus
15 one-attacker conditions: each of first/heuristic/llmq with one accuse/repeat/
fake/contradict/bait seat. All35 conditions are reported, including negative ones.

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
unchanged2-point overall promotion gate; robustness fixes are not permission to
promote the experimental public-vote profile. The current one-seat confirmation
does not extend the earlier multi-seat strength claims to this revision.
