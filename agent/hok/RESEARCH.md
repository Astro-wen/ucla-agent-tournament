# HoK policy research, 2026-10-16

This is an implementation/evidence note, **not a measured win-rate claim**.
Only synthetic local states were used; no live match was created or joined.

## Confirmed public contract

Read-only references checked:

- Official starter `GAMES.md`, `examples/llm/hok.py`, `tests/test_hok.py`,
  at upstream commit `686e7560708eaa595a409b330d0d1837d49ba9d5`.
- <https://api.altruagent-game.com/skill/hok>, publicly accessible when
  checked around 04:08 PDT on October 16. The earlier handoff's statement
  that this guide was unpublished is now outdated. Guide availability
  does **not** prove Testing or tournament matches are enabled.

The guide confirms:

1. One agent controls seats 0–4 or 5–9, one hero's action per call.
2. A macro is offered only when legal. Its standing order is recomputed
   every step until replaced. A listed macro action ID is sufficient.
3. Raw actions are one-shot and clear the standing order. A raw button
   with `legal_*` fields needs those fields populated from their lists;
   an action ID alone is insufficient for that template.
4. Invisible enemies have `visible: false` and no coordinates.
5. Objective damage breaks time-limit ties before hero kills.
6. `INVALID_ACTION` can still occur between reading and sending when
   a macro becomes unavailable. No local policy can eliminate that race.
7. The default step is 0.2 game seconds. Real-time actions can use any
   previously observed version; lockstep requires the current version.

## Candidate decisions and reasons

- **Whole-team scheduling:** inspect all pending heroes before choosing an
  order. A new retreat outranks a routine push. Oldest-served order breaks
  equal priorities; a continuously waiting update gains priority after four
  other dispatches. The bound concerns dispatches, not elapsed game seconds.
- **Rejection isolation:** associate the result with the last submitted
  hero, not all five. Preserve teammates' order caches. Block the refused
  macro against the exact rejected version, and for a short wall-clock
  backoff. A different currently listed macro can still be used.
- **Objective opportunity:** prefer a nearby, explicitly structure-targeting
  basic attack over skill macros. The guide establishes basic attacks and
  objective damage as meaningful; it does not establish which hero skills
  damage structures, so no hero-specific skill damage is assumed.
- **Skill opportunities:** use only offered skills; reserve skill 3 for an
  explicitly hero-targeting label. A hero-targeting skill or structure attack
  can interrupt a short advance hold. Missing optional distance text does not
  disable a legal offer.
- **Recovery hysteresis:** once a wounded hero is sent home, complete recovery
  to the existing 85% threshold. This includes the hurt/outnumbered and
  hurt/near-tower cases, avoiding immediate re-entry at the tower boundary.
- **Visible team aggregation:** require explicit `visible: true` for enemy
  heroes, deduplicate shared views, and merge structures from all five views.
  No enemy position is retained across decisions.
- **Conservative parsing:** restrict commands to owned, currently pending
  seats and recognized offered action IDs. Reject contradictory payload seats
  and incomplete raw domains. Do not invent a no-op when none was offered.

## Deliberately unverified / not implemented

- The 8,000 / 9,000 / 8,800 distance cutoffs are **policy heuristics**, not
  verified skill, basic-attack, or tower weapon ranges. The original
  universal tower-range assertion was removed. The starter uses a 9,000
  combat heuristic; the public contract does not specify actual hero ranges.
- No assumed hero roster, hero-specific cooldown/damage tables, item buying,
  target selection beyond each offered macro, lane assignment, movement-grid
  interpretation, tower aggro ownership, or unseen enemy locations.
- Generic macro availability does not reveal skill effects. Skill 2 before
  skill 1 and reserving skill 3 for heroes remain heuristics, not optimality
  claims. A structure attack preference is conservative, not proven optimal.
- Full healing after a tactical retreat may sacrifice objective pressure.
  It needs evaluation against boundary oscillation and survival, rather than
  a claim that its threshold is optimal.
- Synthetic contract tests cannot reproduce the Tencent server's physics,
  targeting, damage, or latency. Offline proxy results must be labeled as such.
- A malformed lockstep state with no listed, fillable pending action cannot
  be made legal by guessing. The policy returns no action in that impossible
  contract case; the coordinator owns the top-level integration fallback.

## Evaluation priorities

1. Simulate continuously changing low-seat offers and repeated rejection;
   verify every actionable teammate is eventually serviced.
2. Compare retreat latency for a high seat against routine low-seat updates.
3. Exercise skill cooldown disappearance, target death, stale standing orders,
   death/respawn, and both camps.
4. Measure order churn and tower-boundary re-entry with and without recovery
   hysteresis. Separate objective pressure from survival and action legality.
5. Run the independent safety audit and full integration regressions before
   promotion. Keep any simulated-strength conclusions separate from real
   tournament strength.
