# Honor of Kings research, 2026-10-16

This folder was finalized after the user-approved 08:00 PDT research cutoff.

The deployed-code candidate is the conservative hardened five-hero controller frozen at `2132641`.
Its complete `agent/` tree is `139fcf59bfbdddb752424f726af77b2e59b0322b`. The SDK tree remains unchanged.
The source-only package was fully verified READY at README-only commit `72b4c314`; later research/test
metadata does not change any of its selected source files.

## Evidence boundaries

- The local environment is an explicitly synthetic diagnostic model, not Tencent's 5v5 engine.
- Development preserves v3/v4 assumptions separately: 2,496 unique generated records, six policies,
  208 conditions, and only two world seeds. Pilot duplicates are identified, not counted as new evidence.
- Reserved diagnostics compare current main and the final candidate: 400 generated records, two policies,
  100 matched conditions, two world seeds and mirrored camps. Each policy scores 198 wins and 2 losses.
  One condition improves in lockstep and regresses in realtime; this is not overall strategy superiority.
- Both policies win all 120 listed weak-control records. In this menu, idle and first-legal behavior coincide:
  40 records per policy are from a duplicate control arm, leaving two effective weak behaviors (idle and random).
  These are correlated synthetic records, not arbitrary human players or a real-platform win-rate estimate.
- All five speculative policy variants remain unselected. One apparent geometric advantage was a label
  rounding artifact. Their exact source is archived separately and is not part of the runtime policy.
- One completed real spectator record ends in a zero-objective-damage draw. It lacks submitted actions,
  legal lists, receipts and a deployment version, so it cannot establish this candidate's behavior or strength.

## Contents

`development-paired-results.json` and `final-results-summary.json` preserve compact paired outcomes,
conditions and source hashes. The final tarball includes the audited public source/provenance package.
The two evaluator tarballs preserve historical diagnostic implementations. The unselected-policy tarball
contains exactly four source files to reconstruct the experimental factory on an isolated `0785a42` checkout.
Never extract research snapshots over a running agent.

`independent-audit.json`, `controller-stability.json` and `verification.json` record independent numeric,
legality, bounded-state, test, replay and delivery checks. Temporary execution logs, credentials, raw private
observations and the full real spectator record are deliberately not included.

Runtime strengths established here are legal owned-seat control, fair scheduling, recovery from rejected
or cleared commands, bounded sparse recording, explicit game routing and preservation of other games.
The preserved simulation results are diagnostic evidence only.
