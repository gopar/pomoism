# Pomodoro sync server (home-base) — container image.
#
# Only the SERVER is containerized. The agent and CLI are host processes by
# design (they fire OS-native hooks and write to ~/.config, ~/.cache).
#
# Build & run:
#   docker build -t pomo-server .
#   docker run -d --name pomo-server -p 8787:8787 -v pomo-data:/data pomo-server
#
# The DB (and its WAL sidecars) live in the /data volume so they survive
# restarts. Set POMO_TOKEN to require bearer auth.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Stdlib only — no pip install. Copy the pomo package.
WORKDIR /app
COPY src/pomo/ ./pomo/

ENV POMO_DB_PATH=/data/pomo.db \
    POMO_HOST=0.0.0.0 \
    POMO_PORT=8787

EXPOSE 8787
VOLUME ["/data"]

# Run as a non-root user; ensure it can write the data volume.
RUN useradd --create-home --uid 10001 pomo \
    && mkdir -p /data \
    && chown -R pomo:pomo /data /app
USER pomo

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import os,urllib.request; \
urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('POMO_PORT','8787'), timeout=3)" \
    || exit 1

CMD ["python3", "-m", "pomo.server"]
