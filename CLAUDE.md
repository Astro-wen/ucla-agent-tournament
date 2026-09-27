# Project notes for Claude

Our entry in the UCLA Trustworthy AI Lab × SAIR AI Agent Gaming Tournament (AltruAgent platform).
Submission window closes **2026-10-09**; live competition **2026-10-16**. Start by reading `docs/HANDOFF.md`.
The owner is new to this domain: explain in plain Chinese, define terms.

## Rules
- Commits are in the owner's name only. **Never add `Co-Authored-By: Claude` or any Claude attribution** to commits or PRs.
- Never commit `.env*` (agent API keys), `logs/*.out`, or scratch logs. Only `logs/yoriko/` and `logs/apocalypse/` (real platform recordings, checked clean) are tracked.
- Never create accounts, enter passwords, or submit payment details. Registering agents via the platform's own signup API is fine; claiming them is done by the owner.
- `altruagent/` is the official SDK (remote `upstream`): don't edit it; `git fetch upstream && git merge upstream/main` to update.
- Local simulators and replays must never record decisions (`ARENA_RECORD=0` is set in `local/*_env.py` and `scripts/replay_real.py`). A ladder once wrote 7 GB to `logs/`.

## How changes get promoted
1. Change `agent/` (the deployed code). `champion/` is the frozen current best, tracked in git so worktrees can ladder against it.
2. `python -m pytest -q` green, `python scripts/replay_real.py` and `--no-calc` with 0 failures.
3. `python -m local.ladder --games 400 --seed 7`: promote only if the 95% CI lower bound > 50%. Re-check with another seed.
4. `python -m local.benchmark --games 200`: no regression vs official baseline, random, or any `versions/*`.
5. `./scripts/freeze_champion.sh`, archive under `versions/<name>` for real milestones, commit, update `docs/progress.md`.

## Facts that are easy to get wrong
- Real payload shapes are in `docs/real_payloads.md` (authoritative over `docs/platform/`): foe HP is out of 100, foe item/ability/moves hidden in battle, the opponent's full sets are only visible during the draft (the policy caches them), doubles targets: slot 0 single-target moves `[-2, 1, 2]`, slot 1 `[-1, 1, 2]`, spread/self `[0]`.
- The 18-card draft pool is sampled per match from a larger set; `data/vgc_cards.json` grows with `python -m local.pool --harvest`.
- The official runner kills a match process on any `INVALID_ACTION`; the platform then random-plays our seat (300 s Pokémon, 120 s Werewolf). Every policy path must fall back safely.
- `python -m agent` does NOT join tournaments by itself (see `docs/deploy.md`).
- Only one machine may run `python -m agent` for a given agent key at a time.
