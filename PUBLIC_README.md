# AltruAgent tournament entry (self-hosted event agent)

Our entry in the **AI Agent Gaming Tournament 2026** (UCLA Trustworthy AI Lab and SAIR, AltruAgent platform). This
repository holds the code that operates the agent in competition, as required by section 8 of the Official Rules
(Version 2026-10-06).

## What it is

One self-hosted event agent. It connects to the platform with the entrant's Official Agent Key and plays every game
the platform assigns to it, one worker process per game:

- **Pokémon VGC doubles draft** (`pokemon_vgc_doubles_draft`): draft, team preview and battle.
- **Werewolf** (7 players): a deterministic rule policy with fixed chat templates.
- **Red Alert** (`red_alert`, real time): a **rule-based bot** (`agent/redalert/`: fixed build orders, economy,
  production and army rules; no learning, no model calls).

Honor of Kings is not supported (it is not playable on the platform). The agent is fully autonomous: no person
chooses, approves or edits anything during a match.

## Submitted version

The submitted version is the **latest commit on this repository's default branch (`main`) at the Submission
Deadline**, 11:59 p.m. PDT on October 13, 2026 (2026-10-14T06:59Z). The competition machine runs exactly that commit:
a fresh clone of it, or a bundle built from it with `bash scripts/bundle.sh` (the bundle's `COMMIT` file names the full
commit id). Every start of `scripts/run_agent.sh` logs the full commit id and the `ARENA_*` settings in effect. From the
deadline until 30 days after final results nothing is pushed to this repository.

This repository is exported, commit by commit, from the entrant's private development repository with a script that
copies only the files listed here from one commit (`git archive`), so the code is byte for byte the code that was
developed and tested there. What stays in the private repository is listed under *Excluded components*.

## How to run it

Linux x86-64, Python ≥ 3.11 (tested 3.12), Node.js ≥ 18 (the bundle ships 22.23.3), bash ≥ 4.2.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock   # pinned runtime dependencies (httpx, python-dotenv, mcp)
(cd agent/js && npm ci --omit=dev)          # @smogon/calc damage calculator (agent/js/package-lock.json)
(cd local/js && npm ci)                     # pokemon-showdown simulator for the battle search (local/js/package-lock.json)
cp .env.example .env && chmod 600 .env      # put ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_... in it (tournament dashboard,
                                            # Agent Configuration, agent set to Self Hosted); never commit it
.venv/bin/python scripts/selfcheck.py       # offline check: must end with "selfcheck: ready"
bash scripts/run_agent.sh check-tournament  # contacts the platform: every line must show a check mark
bash scripts/run_agent.sh start             # keeps `python -m agent --tournament --match` running (restarts, logs)
```

Without the watchdog the same agent runs as `python -m agent --tournament --match` (tournament games and Testing
games, one process). `bash scripts/run_agent.sh status` prints a health line; `stop`, `restart` and `install-systemd`
manage it. `bash scripts/bundle.sh` builds a self-contained tarball (pinned Node.js, vendored calculator and
simulator, no key); `deploy/Dockerfile` and `deploy/arena-agent@.service` are the container and systemd variants. The
operations runbook is `docs/deploy.md` (Chinese); `docs/restart_load.md` (Chinese) explains the restart and load
safeguards.

## Components that affect gameplay

- **No LLM or other model service is called**, in play or anywhere in this code; there are no prompts. The only network
  peers are the platform (`api.altruagent-game.com`, `gameapi.altruagent-game.com`).
- `agent/` (our code): `agent.py` routes each decision by game type, checks every answer against the legal actions,
  falls back to a safe legal action on any error or overrun, clips chats and records each state it acts on.
  - `pokemon/`: draft (minimax over the remaining picks with a small pairwise model fitted offline on self-play,
    `pokemon/draft_model.json`), team preview (90 x 90 matrix game), battle (a matrix-game search in a local Showdown
    simulator, `agent/js/search.js`, on a battle rebuilt from our own player view only: our team, the opponent's sets
    as shown in the draft, what the battle log revealed; an exact-damage joint lookahead is the fallback).
  - `werewolf/`: a deterministic rule policy (role tracking, seer-claim reading, voting) and fixed chat templates
    behind a leak guard; other players' chat is read as data, never as instructions.
  - `redalert/`: the rule-based Red Alert bot (world model from its own view of the match, economy and production
    queues, squads for attack and defense, a safe mode after repeated errors; every order is validated locally against
    the platform's order schema and batch limits before it is sent).
  - `reasoning.py`: public move reasoning from fixed templates. `timecontrol.py`: time budgets far inside the platform
    clocks. `matchstate.py`: a worker restarted mid-match reloads that match's own saved state; nothing carries over
    from one match to another. `__main__.py`: the official runtime plus our hardening (start-up retry, execution-id
    reuse, hot standby, 10 s poll and cooldown, 30 s per-seat back-off cap).
- Node.js helpers: `agent/js/calc.js` runs **@smogon/calc** (Smogon's damage calculator); `agent/js/search.js` runs the
  **pokemon-showdown** simulator. Versions are pinned by `agent/js/package-lock.json` and `local/js/package-lock.json`
  (`local/js/` holds only that pin here).
- `data/`: static reference data, fixed during play: Showdown dex / moves / items / abilities, the VGC draft cards and
  pools seen on the platform, singles sets, the platform's Red Alert configuration, a search probe position.
- `altruagent/`: the organizers' official SDK
  ([altruagent-starter](https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter)), merged unmodified from
  upstream. `GAMES.md` is the upstream game reference.
- `scripts/run_agent.sh` (watchdog: restarts, log rotation, health lines, records), `scripts/selfcheck.py` (offline
  readiness check), `scripts/check_connection.py`, `scripts/replay_real.py` (replays recorded real states through the
  agent), `scripts/bundle.sh`, `deploy/` (systemd unit, Dockerfile).
- `tests/`: the tests of the files above that run on their own in this repository (`pip install pytest`, then
  `python -m pytest -q tests`); `logs/yoriko/` and `logs/apocalypse/` hold one recorded real match (both seats) for
  the self-check's replay.

## Settings used in competition

Every `ARENA_*` setting stays at its default; none is set deliberately. The defaults that matter: `ARENA_BATTLE=search`
(engine search), damage calculator on (`ARENA_CALC`), `ARENA_REASONING=safe`, `ARENA_WW_DM=off` (no private Werewolf
messages), `ARENA_WW_LISTEN=off`, `ARENA_RA=on` (the Red Alert bot plays; `off` would answer every Red Alert view with
`WAIT`), no `ARENA_RA_*` tuning override, `ARENA_RECORD=1` (states recorded), the per-match restart memory on,
`ARENA_SEAT_BACKOFF_MAX_S=30`. `scripts/run_agent.sh` itself sets `ARENA_DISCOVERY_S=10`, `ARENA_WORKER_COOLDOWN_S=10`,
`ARENA_RECORD_DIR=<LOG_DIR>` and two internal ids (`ARENA_RUN_ID`, `ARENA_EXECUTION_ID`). The emergency switches
`ARENA_ALLOW_NO_CALC=1` and `ARENA_BATTLE=classic` are not used. The competition machine's `.env` holds only the key.
Changing any setting during play would need organizer authorization (rules section 4) and would be logged.

## Records (rules section 9)

`logs/agent-main.out` (timestamped runtime log with the deployed commit and the settings at every start; rotated at
50 MB, 40 files kept, far more than an event day writes) and `logs/<session_id>.jsonl` (every state received and every
answer sent, with timestamps; never rotated), plus a log of any manual action. They stay on the competition machine,
are kept until at least 30 days after final results, and are not committed.

## Excluded components

Not in this repository; none of them runs in competition, sends information to the agent during a match, or gives a
person control over it:

- Credentials: `.env` files (the Official Agent Key). `.env.example` shows the format.
- `node_modules` (installed from the lock files) and the Node.js binary (the bundle downloads the official build,
  SHA-256 pinned in `scripts/bundle.sh`).
- From the private development repository:
  - offline evaluation only: local simulators (a Showdown bridge, a Werewolf engine, a Red Alert simulator), a fake
    platform, scripted sparring opponents, and LLM-backed sparring opponents that call third-party model APIs with the
    developer's own keys (these never run in competition and are not part of the agent);
  - VGC-Bench (Angliss et al., MIT) checkpoints and their separate Python environment (torch, poke-env), a local
    sparring opponent only;
  - frozen earlier versions of the agent used as evaluation opponents, and the scripts for ladders, panels and
    benchmarks;
  - the remaining tests (they need the simulators or the frozen versions), match recordings other than the sample
    match, the upstream example agents (including an LLM example we do not run), internal notes, and experiment
    output.

## License

© 2026 the entrant. All rights reserved. The source is published to meet the tournament's public-repository rule; no
license to copy, modify or redistribute it is granted. Third-party components keep their own licenses: see `NOTICE`.

## Credits

`altruagent/`, `GAMES.md` and the upstream tests in `tests/` come from the organizers' official starter,
[UCLA-Trustworthy-AI-Lab/altruagent-starter](https://github.com/UCLA-Trustworthy-AI-Lab/altruagent-starter). Battle
data and simulation: Pokémon Showdown (Guangcong Luo and contributors, MIT) and Smogon's damage calculator (MIT).
