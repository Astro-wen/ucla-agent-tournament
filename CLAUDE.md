# Project notes for Claude

Our entry in the UCLA Trustworthy AI Lab × SAIR AI Agent Gaming Tournament (AltruAgent platform).
Submission window **2026-10-06 – 10-13** (altruagent-game.com, updated 09-27; treat 10-09 as our internal safety deadline); live competition **2026-10-16**. Start by reading `docs/HANDOFF.md`, then `docs/competition_brief.md` (rules, organizers, per-game notes, open questions).
The owner is new to this domain: explain in plain Chinese, define terms.

## Rules
- Commits are in the owner's name only. **Never add `Co-Authored-By: Claude` or any Claude attribution** to commits or PRs.
- Never commit `.env*` (agent API keys), `logs/*.out`, or scratch logs. Only `logs/yoriko/` and `logs/apocalypse/` (real platform recordings, checked clean) are tracked.
- Never create accounts, enter passwords, or submit payment details. The platform's agent signup/claim API is retired (HTTP 410 since 10-06): the owner signs in to the event site with their UCLA Google account, sets the agent to Self Hosted and generates the Official Agent Key themselves.
- `altruagent/` is the official SDK (remote `upstream`): don't edit it; `git fetch upstream && git merge upstream/main` to update.
- Local simulators and replays must never record decisions (`ARENA_RECORD=0` is set in `local/*_env.py` and `scripts/replay_real.py`). A ladder once wrote 7 GB to `logs/`.

## How changes get promoted
1. Change `agent/` (the deployed code). `champion/` is the frozen current best, tracked in git so worktrees can ladder against it.
2. `python -m pytest -q` green, `python scripts/replay_real.py` and `--no-calc` with 0 failures.
3. `python -m local.ladder --games 400 --seed 7`: promote only if the 95% CI lower bound > 50%. Re-check with another seed.
4. `python -m local.benchmark --games 200`: no regression vs official baseline, random, or any `versions/*`.
5. `./scripts/freeze_champion.sh`, archive under `versions/<name>` for real milestones, commit, update `docs/progress.md`.
6. From v5 on, the full bar (external opponents, dev-proxy funnel, seed registry, live A/B, what was already tried) is `docs/v5_standard.md`. Fixes that do not aim at strength ship as v4.x.

## Facts that are easy to get wrong
- Real payload shapes are in `docs/real_payloads.md` (authoritative over `docs/platform/`): foe HP is out of 100, foe item/ability/moves hidden in battle, the opponent's full sets are only visible during the draft (the policy caches them), doubles targets: slot 0 single-target moves `[-2, 1, 2]`, slot 1 `[-1, 1, 2]`, spread/self `[0]`.
- The 18-card draft pool is sampled per match from a larger set; `data/vgc_cards.json` grows with `python -m local.pool --harvest`.
- The official runner kills a match process on any `INVALID_ACTION`; the platform then random-plays our seat (300 s Pokémon, 120 s Werewolf). Every policy path must fall back safely.
- Every agent is an event agent with one Official Agent Key (`ALTRUAGENT_OFFICIAL_AGENT_KEY`). `python -m agent` needs a flag: `--tournament` (tournament games), `--match` (Testing games), both = one process for both; `scripts/run_agent.sh` always runs `--tournament --match`. No flag or `--claim` only prints the retirement notice. A tournament game is lost as a no-show if we do not connect within 4 minutes (Red Alert 4.5) of its creation (`docs/platform/04-tournaments.md`).
- Red Alert (`red_alert`, real time, OpenRA) is an event tournament game: rules in `docs/platform/redalert.md`, unit/map data in `data/red_alert_config.json`. The SDK has real-time mode (merged 10-06), but our agent answers every Red Alert view with WAIT (a forfeit); whether to play it is the owner's call.
- One runtime per Official Agent Key (a second one gets `seat_busy`); the only exception is our hot standby (`ARENA_ROLE=standby` on another machine), and only with the organizers' OK first.
