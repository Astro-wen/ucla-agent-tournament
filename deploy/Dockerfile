# The agent as a container: Python 3.12 + Node.js 22.23.3 (the damage calculator and the engine search; the same
# pinned version that scripts/bundle.sh ships). Build from an extracted bundle directory (scripts/bundle.sh): the
# bundle's agent/js/node_modules carries the engine search's simulator (a trimmed pokemon-showdown), which the
# repository root does not (.dockerignore drops node_modules and local/), so a build from the repository root stops at
# the selfcheck step (ARENA_BATTLE=search, the default battle policy, needs the simulator). The bundle brings its own
# .dockerignore (keys, .venv, logs and its node/ build stay out of the build context). The key comes from the
# environment at run time, never the image: use --env-file with a mode-600 file, not -e KEY=... (shell history,
# docker inspect).
#
#   bash scripts/bundle.sh && tar xzf dist/arena-agent-*.tar.gz -C /tmp && cd /tmp/arena-agent-*
#   docker build -f deploy/Dockerfile -t arena-agent .
#   # the official tournament: arena.keys holds ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_... (mode 600)
#   docker run --rm --env-file arena.keys arena-agent check-tournament      # once: contacts the platform
#   docker run -d --name arena-main --restart unless-stopped --env-file arena.keys \
#       --log-opt max-size=20m --log-opt max-file=3 -v arena-logs:/data arena-agent
#   docker logs -f arena-main          # same lines as /data/logs/agent-main.out (bounded by --log-opt)
#   docker ps                          # STATUS shows (healthy) only while the agent process is up
#   docker stop arena-main             # clean stop (SIGTERM -> scripts/run_agent.sh stops the agent tree)
#
# (No --init: the entrypoint already runs under tini.) With ALTRUAGENT_OFFICIAL_AGENT_KEY set the container runs
# `python -m agent --tournament`; with only ALTRUAGENT_API_KEY the discovery mode (ARENA_MODE forces one). The build
# fails when the calculator cannot answer a calc or the engine search cannot load its simulator (selfcheck), and the
# container refuses to start without them unless -e ARENA_ALLOW_NO_CALC=1 (emergency only; covers the search too when
# node itself cannot run) / -e ARENA_BATTLE=classic (the classic policy on purpose).
# Optional env: ALTRUAGENT_CONTROL_URL (default: the platform), ARENA_REASONING, HEALTH_EVERY, ARENA_RECORD=0.
# Never built on the development machine (no docker there): build it once on the deployment host and check that
# `node --version` prints v22.23.3 and selfcheck ends with "selfcheck: ready".

# Node.js (official build, pinned by version) + the calculator's node_modules, installed exactly from
# agent/js/package-lock.json (pure JavaScript)
FROM node:22.23.3-bookworm-slim AS calc
WORKDIR /calc
COPY agent/js/package.json agent/js/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    LOG_DIR=/data/logs ARENA_RECORD_DIR=/data/logs LOG_ECHO=1
# procps gives run_agent.sh its `ps`; tini reaps orphans as PID 1
RUN apt-get update \
 && apt-get install -y --no-install-recommends procps tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=calc /usr/local/bin/node /usr/local/bin/node
RUN node --version | grep -qx 'v22.23.3'
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml .env.example ./
COPY altruagent ./altruagent
COPY agent ./agent
COPY data ./data
COPY scripts/run_agent.sh scripts/selfcheck.py scripts/check_connection.py ./scripts/
# (merged over the bundle's agent/js/node_modules, which keeps its vendored pokemon-showdown for the engine search)
COPY --from=calc /calc/node_modules ./agent/js/node_modules
RUN python scripts/selfcheck.py --no-replay \
 && useradd --create-home --uid 10001 arena \
 && mkdir -p /data/logs && chown -R arena /data
USER arena
VOLUME ["/data"]
# healthy = the run loop's agent process is up (run_agent.sh health: agent=up); the start takes a few seconds
HEALTHCHECK --interval=60s --timeout=20s --start-period=120s --retries=3 \
  CMD bash /app/scripts/run_agent.sh health --name main | grep -q ' agent=up '
ENTRYPOINT ["/usr/bin/tini", "--", "bash", "/app/scripts/run_agent.sh"]
CMD ["run", "--name", "main"]
