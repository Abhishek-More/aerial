#!/bin/sh
# Build and (re)start the bot in Docker on this machine.
#
#   ./deploy.sh                    # rebuild from the current checkout
#   AERIAL_PORT=5051 ./deploy.sh   # publish somewhere other than 5050
#
# Credentials live in api/.env (gitignored) and are never touched here.
set -eu
cd "$(dirname "$0")"

docker info >/dev/null 2>&1 || {
  echo "docker is not running (on macOS: open -a Docker, wait for it, rerun)" >&2
  exit 1
}

[ -f api/.env ] || { echo "no api/.env: MindBody and SMTP credentials go there" >&2; exit 1; }

# A container created by `docker run` owns the name compose wants. Adopt it once
# rather than failing forever on "name is already in use".
if docker inspect aerial >/dev/null 2>&1 &&
   [ "$(docker inspect aerial --format '{{index .Config.Labels "com.docker.compose.project"}}')" = "" ]; then
  echo "removing the pre-compose 'aerial' container so compose can own the name"
  docker rm -f aerial >/dev/null
fi

docker compose up -d --build

port=${AERIAL_PORT:-5050}
# Compose returns as soon as the container starts, which is before gunicorn
# binds. Poll instead of sleeping on a guess. The bot boots a session thread and
# a scheduler at import time, so give it longer than a web app would need.
i=0
while [ "$i" -lt 60 ]; do
  if curl -fsS "http://127.0.0.1:$port/api/boot-log" >/dev/null 2>&1; then
    echo "aerial is up on http://127.0.0.1:$port"
    exit 0
  fi
  i=$((i + 1))
  sleep 1
done

echo "no answer on port $port after 60s:" >&2
docker compose logs --tail 30 aerial >&2
exit 1
