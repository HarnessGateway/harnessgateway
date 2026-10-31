#!/usr/bin/env bash
# HarnessGateway, self-hosted on one Linux host (SPEC §10.4). Run as root; safe to run again (it keeps .env, the data
# and the admin key already issued).
#
#   curl -fsSL https://raw.githubusercontent.com/harnessgateway/harnessgateway/v1.1.1/deploy/install.sh | sudo bash
#   sudo ./deploy/install.sh                  # from a checkout: builds the images locally if they cannot be pulled
#
# It checks Docker, installs gVisor (runsc) by its official method if absent and registers it with Docker, writes
# $HG_HOME/.env with fresh secrets, pulls (or builds) the images, starts the compose project and creates the
# organization, printing its admin key once.
#
# Settings (environment): HG_HOME (default /opt/harnessgateway), HG_BIND (127.0.0.1), HG_HTTP_PORT (8000),
# HG_NODE_DIR (/var/lib/harnessgateway), HG_DOCKER_POOL (10.200.0.0/16: room for one network per sandbox).
set -euo pipefail

# Everything runs from main(), called on the last line: bash reads the whole script before executing any of
# it, so a run piped from curl is not cut short by a command that reads stdin.
main() {
VERSION=1.1.1
RELEASE=${HG_RELEASE_URL:-https://raw.githubusercontent.com/harnessgateway/harnessgateway/v$VERSION/deploy}
HOME_DIR=${HG_HOME:-/opt/harnessgateway}
PLATFORM_IMAGE=${HG_PLATFORM_IMAGE:-ghcr.io/harnessgateway/harnessgateway:$VERSION}
SANDBOX_IMAGE=${HG_SANDBOX_IMAGE:-ghcr.io/harnessgateway/sandbox:$VERSION}
SOURCE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run as root (sudo)"
[ "$(uname -s)" = Linux ] || fail "a node needs Linux (gVisor runs on Linux only)"
command -v docker >/dev/null || fail "Docker is not installed: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || fail "the Docker Compose plugin is not installed: https://docs.docker.com/compose/install/"
docker info >/dev/null 2>&1 || fail "Docker is not running"

# ── images ─────────────────────────────────────────────────────────────────────────────────────────────────────
for image in "$PLATFORM_IMAGE" "$SANDBOX_IMAGE"; do
  docker image inspect "$image" >/dev/null 2>&1 && continue
  say "pulling $image"
  docker pull -q "$image" >/dev/null 2>&1 && continue
  [ -f "$SOURCE/../Dockerfile" ] || fail "cannot pull $image; run this script from a checkout to build it"
  say "building $image from $SOURCE/.."
  if [ "$image" = "$PLATFORM_IMAGE" ]; then
    docker build -q -t "$image" "$SOURCE/.." >/dev/null
  else
    docker build -q -t "$image" "$SOURCE/../sandbox" >/dev/null
  fi
done

# ── gVisor ─────────────────────────────────────────────────────────────────────────────────────────────────────
restart_docker=0
if ! docker info --format '{{json .Runtimes}}' | grep -q '"runsc"'; then
  if ! command -v runsc >/dev/null; then
    say "installing gVisor (https://gvisor.dev/docs/user_guide/install/)"
    if command -v apt-get >/dev/null; then
      apt-get update -qq && apt-get install -y -qq apt-transport-https ca-certificates curl gnupg >/dev/null
      curl -fsSL https://gvisor.dev/archive.key | gpg --batch --yes --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
        > /etc/apt/sources.list.d/gvisor.list
      apt-get update -qq && apt-get install -y -qq runsc >/dev/null
    else
      tmp=$(mktemp -d)
      url="https://storage.googleapis.com/gvisor/releases/release/latest/$(uname -m)"
      for f in runsc runsc.sha512 containerd-shim-runsc-v1 containerd-shim-runsc-v1.sha512; do
        curl -fsSL -o "$tmp/$f" "$url/$f"
      done
      (cd "$tmp" && sha512sum -c runsc.sha512 && sha512sum -c containerd-shim-runsc-v1.sha512)
      install -m 0755 "$tmp/runsc" "$tmp/containerd-shim-runsc-v1" /usr/local/bin/
      rm -rf "$tmp"
    fi
  fi
  runsc install
  restart_docker=1
fi
# One internal network per sandbox: Docker's default pools hold about 30; /28 subnets of one /16 hold 4096.
if [ ! -f /etc/docker/daemon.json ] || ! grep -q default-address-pools /etc/docker/daemon.json; then
  pool=${HG_DOCKER_POOL:-10.200.0.0/16}
  if [ -s /etc/docker/daemon.json ]; then
    docker run --rm -i --entrypoint python "$PLATFORM_IMAGE" -c "import json,sys; c=json.load(sys.stdin); \
c['default-address-pools']=[{'base':'$pool','size':28}]; print(json.dumps(c, indent=2))" \
      < /etc/docker/daemon.json > /etc/docker/daemon.json.new 2>/dev/null \
      && mv /etc/docker/daemon.json.new /etc/docker/daemon.json \
      || { rm -f /etc/docker/daemon.json.new; echo "note: add default-address-pools to /etc/docker/daemon.json by hand"; }
  else
    mkdir -p /etc/docker
    printf '{\n  "default-address-pools": [{"base": "%s", "size": 28}]\n}\n' "$pool" > /etc/docker/daemon.json
  fi
  restart_docker=1
fi
if [ "$restart_docker" = 1 ]; then
  say "restarting Docker"
  systemctl restart docker 2>/dev/null || service docker restart
fi
docker run --rm --runtime=runsc --network none "$SANDBOX_IMAGE" true >/dev/null \
  || fail "Docker cannot run containers with gVisor (runsc)"

# ── the compose project ────────────────────────────────────────────────────────────────────────────────────────
mkdir -p "$HOME_DIR"
cd "$HOME_DIR"
for f in compose.yaml postgres-init.sh .env.example; do
  if [ -f "$SOURCE/$f" ]; then cp "$SOURCE/$f" "$f"; else curl -fsSL -o "$f" "$RELEASE/$f"; fi
done
chmod 0755 postgres-init.sh
if [ ! -f .env ]; then
  say "writing $HOME_DIR/.env with fresh secrets"
  umask 077
  password() { tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32 || true; }
  sed -e '/^HG_SECRET_KEY=$/d' -e '/^HG_OPERATOR_KEY=$/d' -e '/^HG_NODE_JOIN_TOKEN=$/d' \
      -e "s/^POSTGRES_ROOT_PASSWORD=$/POSTGRES_ROOT_PASSWORD=$(password)/" \
      -e "s/^HG_DB_PASSWORD=$/HG_DB_PASSWORD=$(password)/" \
      -e "s|^HG_BIND=.*|HG_BIND=${HG_BIND:-127.0.0.1}|" -e "s|^HG_HTTP_PORT=.*|HG_HTTP_PORT=${HG_HTTP_PORT:-8000}|" -e "s|^HG_CONSOLE_PORT=.*|HG_CONSOLE_PORT=${HG_CONSOLE_PORT:-8800}|" \
      -e "s|^HG_NODE_DIR=.*|HG_NODE_DIR=${HG_NODE_DIR:-/var/lib/harnessgateway}|" .env.example > .env
  docker run --rm "$PLATFORM_IMAGE" keygen >> .env
  printf 'HG_PLATFORM_IMAGE=%s\nHG_SANDBOX_IMAGE=%s\n' "$PLATFORM_IMAGE" "$SANDBOX_IMAGE" >> .env
fi
mkdir -p "$(grep -E '^HG_NODE_DIR=' .env | cut -d= -f2-)"

say "starting HarnessGateway"
docker compose up -d --wait postgres harnessgateway node
key=$(docker compose exec -T harnessgateway harnessgateway init </dev/null | sed -n 's/.*"admin_key": "\(hg_[^"]*\)".*/\1/p')
if [ -n "$key" ]; then
  sed -i "s|^HG_CONSOLE_KEY=.*|HG_CONSOLE_KEY=$key|" .env
fi
users=""
if grep -qE '^HG_CONSOLE_KEY=hg_' .env; then
  users=$(docker compose run --rm -T console init </dev/null 2>/dev/null | sed -n 's/^ *"\(deployer\|admin\)": "\([^"]*\)".*/\1: \2/p')
  docker compose up -d --wait console
fi
port=$(grep -E '^HG_HTTP_PORT=' .env | cut -d= -f2-)
cport=$(grep -E '^HG_CONSOLE_PORT=' .env | cut -d= -f2-)
echo
say "HarnessGateway is running: API http://${HG_BIND:-127.0.0.1}:${port:-8000}, console http://${HG_BIND:-127.0.0.1}:${cport:-8800}  (project: $HOME_DIR)"
if [ -n "$key" ]; then
  echo "Your admin key (shown once; keep it):  $key"
else
  echo "The organization already exists; use the admin key printed at its first install."
fi
if [ -n "$users" ]; then
  echo "Console sign-ins (shown once; each must be changed at first sign-in):"
  echo "$users" | sed 's/^/  /'
fi
cat <<EOF

Next:
  export OPENAI_BASE_URL=http://localhost:${port:-8000}/v1 OPENAI_API_KEY=<the admin key>
  # a provider for the harnesses that need a model:
  curl -X PUT \$OPENAI_BASE_URL/providers/anthropic -H "authorization: Bearer \$OPENAI_API_KEY" \\
       -H 'content-type: application/json' -d '{"api": "anthropic", "base_url": "https://api.anthropic.com", "key": "<your Anthropic key>"}'
  # or open the console: http://localhost:${cport:-8800} (the "admin" user), http://localhost:${cport:-8800}/ops (the "deployer" user)
  # a first turn:
  curl \$OPENAI_BASE_URL/responses -H "authorization: Bearer \$OPENAI_API_KEY" -H 'content-type: application/json' \\
       -d '{"model": "claude-code", "input": "Write hello.py and run it"}'
EOF
}

main "$@"
