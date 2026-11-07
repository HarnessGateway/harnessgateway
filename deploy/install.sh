#!/usr/bin/env bash
# HarnessGateway, self-hosted on one Linux host (SPEC §16). Run as root; safe to run again (it keeps .env, the data
# and the admin key already issued).
#
#   curl -fsSL https://raw.githubusercontent.com/harnessgateway/harnessgateway/v3.0.1/deploy/install.sh | sudo bash
#   sudo ./deploy/install.sh                  # from a checkout: builds the images locally if they cannot be pulled
#
# It checks Docker, installs gVisor (runsc) by its official method if absent and registers it with Docker, writes
# $HG_HOME/.env with fresh secrets, pulls (or builds) the images, starts the compose project (Postgres, Redis, api,
# worker, door, console) and creates the organization with its first admin (HG_ADMIN_EMAIL; asked for when unset),
# printing the admin's invitation link and an admin key once.
#
# Settings (environment): HG_HOME (default /opt/harnessgateway), HG_ADMIN_EMAIL, HG_BIND (127.0.0.1), HG_HTTP_PORT
# (8000), HG_CONSOLE_PORT (8800), HG_CONSOLE_URL (http://localhost:8800: where people open the console; invitation
# links are built on it), HG_BOX_DIR (/var/lib/harnessgateway/boxes), HG_DOCKER_POOL (10.200.0.0/16: room for one
# network per box), HG_SANDBOX_RUNTIME (runsc; runc skips gVisor and runs boxes unisolated).
set -euo pipefail

# Everything runs from main(), called on the last line: bash reads the whole script before executing any of it, so a
# run piped from curl is not cut short by a command that reads stdin.
main() {
VERSION=3.0.1
RELEASE=${HG_RELEASE_URL:-https://raw.githubusercontent.com/harnessgateway/harnessgateway/v$VERSION/deploy}
HOME_DIR=${HG_HOME:-/opt/harnessgateway}
PLATFORM_IMAGE=${HG_PLATFORM_IMAGE:-ghcr.io/harnessgateway/harnessgateway:$VERSION}
SANDBOX_IMAGE=${HG_SANDBOX_IMAGE:-ghcr.io/harnessgateway/sandbox:$VERSION}
RUNTIME=${HG_SANDBOX_RUNTIME:-runsc}
SOURCE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run as root (sudo)"
[ "$(uname -s)" = Linux ] || fail "boxes need Linux (gVisor runs on Linux only)"
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
    docker build -q -t "$image" -f "$SOURCE/../sandbox/Dockerfile" "$SOURCE/.." >/dev/null
  fi
done

# ── gVisor ─────────────────────────────────────────────────────────────────────────────────────────────────────
restart_docker=0
if [ "$RUNTIME" = runsc ] && ! docker info --format '{{json .Runtimes}}' | grep -q '"runsc"'; then
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
# One internal network per box: Docker's default pools hold about 30; /28 subnets of one /16 hold 4096.
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
if [ "$RUNTIME" = runsc ]; then
  docker run --rm --runtime=runsc --network none --entrypoint true "$SANDBOX_IMAGE" >/dev/null \
    || fail "Docker cannot run containers with gVisor (runsc); set HG_SANDBOX_RUNTIME=runc to run boxes unisolated"
fi

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
  sed -e '/^HG_SECRET_KEY=$/d' -e '/^HG_OPERATOR_KEY=$/d' -e '/^HG_DOOR_KEY=$/d' \
      -e "s/^POSTGRES_ROOT_PASSWORD=$/POSTGRES_ROOT_PASSWORD=$(password)/" \
      -e "s/^HG_DB_PASSWORD=$/HG_DB_PASSWORD=$(password)/" \
      -e "s|^HG_BIND=.*|HG_BIND=${HG_BIND:-127.0.0.1}|" -e "s|^HG_HTTP_PORT=.*|HG_HTTP_PORT=${HG_HTTP_PORT:-8000}|" -e "s|^HG_CONSOLE_PORT=.*|HG_CONSOLE_PORT=${HG_CONSOLE_PORT:-8800}|" \
      -e "s|^HG_ADMIN_EMAIL=.*|HG_ADMIN_EMAIL=${HG_ADMIN_EMAIL:-}|" \
      -e "s|^HG_CONSOLE_URL=.*|HG_CONSOLE_URL=${HG_CONSOLE_URL:-http://localhost:${HG_CONSOLE_PORT:-8800}}|" \
      -e "s|^HG_BOX_DIR=.*|HG_BOX_DIR=${HG_BOX_DIR:-/var/lib/harnessgateway/boxes}|" \
      -e "s|^HG_SANDBOX_RUNTIME=.*|HG_SANDBOX_RUNTIME=$RUNTIME|" .env.example > .env
  docker run --rm "$PLATFORM_IMAGE" keygen >> .env
  printf 'HG_PLATFORM_IMAGE=%s\nHG_SANDBOX_IMAGE=%s\n' "$PLATFORM_IMAGE" "$SANDBOX_IMAGE" >> .env
fi
mkdir -p "$(grep -E '^HG_BOX_DIR=' .env | cut -d= -f2-)"

# The first admin's email: the login the invitation activates. Asked for once; kept in .env for a re-run.
email=$(grep -E '^HG_ADMIN_EMAIL=' .env | cut -d= -f2-)
if [ -z "$email" ] && docker compose ps --status running --services 2>/dev/null | grep -qx postgres && \
   docker compose exec -T postgres psql -U postgres -d harnessgateway -tAc 'SELECT count(*) FROM orgs' 2>/dev/null | grep -qvx 0; then
  :                                              # the organization exists already: init needs no email
elif [ -z "$email" ]; then
  if [ -t 0 ] || [ -r /dev/tty ]; then
    printf 'Email of the first admin (the login that activates the console): '
    read -r email < /dev/tty
  fi
  [ -n "$email" ] || fail "set HG_ADMIN_EMAIL (the first admin's email) and run again"
  sed -i "s|^HG_ADMIN_EMAIL=.*|HG_ADMIN_EMAIL=$email|" .env
fi

say "starting HarnessGateway"
docker compose up -d --wait
init=$(docker compose exec -T -e HG_ADMIN_EMAIL="$email" api harnessgateway init </dev/null)
key=$(printf '%s' "$init" | sed -n 's/.*"admin_key": "\(hg_[^"]*\)".*/\1/p')
invitation=$(printf '%s' "$init" | sed -n 's/.*"invitation": "\([^"]*\)".*/\1/p')
port=$(grep -E '^HG_HTTP_PORT=' .env | cut -d= -f2-)
cport=$(grep -E '^HG_CONSOLE_PORT=' .env | cut -d= -f2-)
echo
say "HarnessGateway is running: API http://${HG_BIND:-127.0.0.1}:${port:-8000}, console http://${HG_BIND:-127.0.0.1}:${cport:-8800}  (project: $HOME_DIR)"
if [ -n "$invitation" ]; then
  echo "Activate your admin account ($email) by choosing a password at this link (shown once, valid 7 days):"
  echo "  $invitation"
  echo "Your admin API key for automation (shown once; keep it):  $key"
else
  echo "The organization already exists; sign in to the console with your account (an admin can reset a password)."
fi
cat <<EOF

Next:
  # open the console: http://localhost:${cport:-8800} — add a provider under Setup → Providers, then a harness
  export OPENAI_BASE_URL=http://localhost:${port:-8000}/v1 OPENAI_API_KEY=<the admin key>
  # or over the API, a provider for the harnesses that need a model:
  curl -X PUT \$OPENAI_BASE_URL/providers/anthropic -H "authorization: Bearer \$OPENAI_API_KEY" \\
       -H 'content-type: application/json' -d '{"api": "anthropic", "base_url": "https://api.anthropic.com", "key": "<your Anthropic key>"}'
  # a first turn:
  curl \$OPENAI_BASE_URL/responses -H "authorization: Bearer \$OPENAI_API_KEY" -H 'content-type: application/json' \\
       -d '{"model": "shell", "input": "echo hello"}'
EOF
}

main "$@"
