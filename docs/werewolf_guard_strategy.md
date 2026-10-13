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
