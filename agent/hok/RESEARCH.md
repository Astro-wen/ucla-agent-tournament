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

- **Whole-team scheduling:** inspect all owned legal heroes in real time;
  `your_pending_seats` means "no fresh action this step", not exclusive
  permission to replace a standing order. Lockstep respects that pending list.
  A new retreat outranks a routine push. Oldest-served order breaks
  equal priorities; a continuously waiting update gains priority after four
  other dispatches. The bound concerns dispatches, not elapsed game seconds.
- **Rejection isolation:** associate the result with the last submitted
  hero, not all five. Preserve teammates' order caches. Block the refused
  macro against the exact rejected version, and for a short wall-clock
  backoff. A different currently listed macro can still be used.
- **Objective opportunity:** prefer an offered, explicitly structure-targeting
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
  seats in lockstep (all owned legal seats in real time) and recognized offered
  action IDs. Normalize bounded numeric-string seats/camps; reject contradictory
  ownership or payload seats and incomplete raw domains. Do not invent a no-op
  when none was offered.
- **Pure exception fallback:** `safe_fallback_action(raw)` is exported from
  `agent.hok`. It reads no telemetry, clock, or policy state. It uses owned,
  pending-seat offers only, preferring a playable button 0 across the team,
  then a fully populated raw button, then a complete macro. A raw button 0
  with `legal_*` fields also requires a completed action object, not its ID.
- **Standing-order authority:** tolerate a short acknowledgement delay
  (2 seconds for a never-acknowledged missing entry, 0.6 seconds for an explicit
  clear), but never preserve an optimistic local order indefinitely. An observed
  death, including during an empty legal-action view, invalidates it. A changed
  death counter also handles a missed dead frame.
- **Basic attacks:** the live guide explicitly says a basic-attack macro is
  offered only with a target in range. The historical 9,000 label cutoff is
  therefore removed; it is not a verified universal weapon range. Missing,
  malformed, or large distance labels cannot by themselves disable an offered
  basic attack. Recovery and short order hysteresis still take precedence.

## Deliberately unverified / not implemented

- The remaining 8,000 skill / 8,800 tower-proximity cutoffs are **policy
  heuristics**, not verified weapon ranges. The original universal tower-range
  assertion was removed. The starter's historical 9,000 combat heuristic is
  no longer used to reject a currently offered basic attack.
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
- Parser bounds are defensive: at most 1,024 action entries, 64 seat/order
  metadata entries, 256 actors per summary, 64 raw fields, 256 choices per
  domain, and 512 label characters. Oversized or contradictory metadata is
  not used as authority to control another camp.

## Legacy fixture compatibility

The original `tests/hok/test_policy.py` fixture simultaneously offers
`macro_attack` and labels it 12,000 units away, while many tests assert that
the policy chooses advance. Its direct rule test similarly asserts advance
for an offered attack at 20,000. Those tactical expectations conflict with
trusting the live legal-attack contract. They are left unchanged for the
coordinator to reconcile; neither these nor the independent integration test
assertions were edited. The owned strategy fixture now omits attacks by default
and adds them explicitly when testing an actual attack opportunity.

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
