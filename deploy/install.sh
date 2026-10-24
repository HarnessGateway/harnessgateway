#!/usr/bin/env bash
# One box, one command:
#
#   curl -fsSL https://raw.githubusercontent.com/HarnessGateway/harnessgateway/main/deploy/install.sh | bash
#
# Installs Docker when the machine has none, starts the platform as one container (every role, the
# data on the `hg` volume), waits for it, and prints the keys the first boot made. Environment:
#   HG_PORT (8000)        where the API listens on this machine; put a TLS proxy in front for the internet
#   HG_PUBLIC_URL         how browsers and workers reach it (default http://<this host>:<port>)
#   HG_SIGNUP (closed)    open: anyone may create an organization from the sign-up page
#   HG_IMAGE              the image (default ghcr.io/harnessgateway/harnessgateway:8)
set -euo pipefail
PORT="${HG_PORT:-8000}"
IMAGE="${HG_IMAGE:-ghcr.io/harnessgateway/harnessgateway:8}"
NAME="${HG_NAME:-harnessgateway}"
PUBLIC="${HG_PUBLIC_URL:-http://$(hostname -f 2>/dev/null || hostname):$PORT}"

if ! command -v docker >/dev/null 2>&1; then
  echo "installing Docker"
  curl -fsSL https://get.docker.com | sh
fi
docker info >/dev/null 2>&1 || { echo "Docker is installed but this user cannot reach it; try with sudo or join the docker group"; exit 1; }

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "a container named $NAME exists; pulling the image and restarting it"
  docker pull -q "$IMAGE"
  docker rm -f "$NAME" >/dev/null
fi
docker run -d --name "$NAME" --restart unless-stopped \
  -p "${HG_BIND:-0.0.0.0}:$PORT:8000" \
  -v /var/run/docker.sock:/var/run/docker.sock -v hg:/data \
  -e HG_PUBLIC_URL="$PUBLIC" -e HG_SIGNUP="${HG_SIGNUP:-closed}" \
  ${HG_ORG:+-e HG_ORG="$HG_ORG"} ${HG_OWNER:+-e HG_OWNER="$HG_OWNER"} \
  "$IMAGE" >/dev/null

echo -n "waiting for the platform"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then echo; break; fi
  echo -n .; sleep 1
done
echo
echo "HarnessGateway is up at $PUBLIC  (console: $PUBLIC/app, docs: $PUBLIC/docs/)"
echo
echo "the first boot's keys, shown once (also in: docker logs $NAME):"
docker logs "$NAME" 2>&1 | sed -n '/^first boot/,/API key/p' | sed 's/^/  /'
