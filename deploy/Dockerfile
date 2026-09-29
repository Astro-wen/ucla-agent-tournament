# The agent as a container: Python 3.12 + Node.js 22.23.3 (the damage calculator; the same pinned version that
# scripts/bundle.sh ships). Build from a bundle directory (scripts/bundle.sh) or from the repository root; the key
# comes from the environment at run time, never the image.
#
#   docker build -f deploy/Dockerfile -t arena-agent .
#   docker run -d --name arena-main --restart unless-stopped \
#       -e ALTRUAGENT_API_KEY=... -v arena-logs:/data arena-agent
#   docker logs -f arena-main          # same lines as /data/logs/agent-main.out
#   docker stop arena-main             # clean stop (SIGTERM -> scripts/run_agent.sh stops the agent tree)
#
# (No --init: the entrypoint already runs under tini.) The build fails when the calculator cannot answer a calc
# (selfcheck), and the container refuses to start without it unless -e ARENA_ALLOW_NO_CALC=1 (emergency only).
# Optional env: ALTRUAGENT_CONTROL_URL (default: the platform), ARENA_REASONING, HEALTH_EVERY, ARENA_RECORD=0.

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
COPY --from=calc /calc/node_modules ./agent/js/node_modules
RUN python scripts/selfcheck.py --no-replay \
 && useradd --create-home --uid 10001 arena \
 && mkdir -p /data/logs && chown -R arena /data
USER arena
VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/tini", "--", "bash", "/app/scripts/run_agent.sh", "run", "--name", "main"]
