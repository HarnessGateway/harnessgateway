#!/usr/bin/env bash
# Installs HarnessGateway on this machine with Docker, and prints where it is and the first admin key.
#
#   curl -fsSL https://raw.githubusercontent.com/HarnessGateway/harnessgateway/main/deploy/install.sh | bash
#
# Options:
#   --dir PATH       where its .env and compose.yaml are kept (default ~/.harnessgateway)
#   --port N         the port the API and the console are served on (default 8000)
#   --bind ADDRESS   the address they are served on (default 127.0.0.1; 0.0.0.0 to be reachable from outside)
#   --with-gvisor    on Linux: also install gVisor, so sandboxes do not share this host's kernel
#
# Run again, it updates the installation to this version. Its secrets and settings are kept; an option given
# again takes the place of what was kept.
set -euo pipefail

VERSION=4.1.1
DIR="$HOME/.harnessgateway"
PORT=""
BIND=""
GVISOR=false

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --with-gvisor) GVISOR=true; shift ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

fail() { echo "install: $*" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Install Docker (or Docker Desktop, or OrbStack) first."
docker info >/dev/null 2>&1 || fail "Docker is installed but not running, or this user may not use it."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is needed (the 'docker compose' command)."

if [ "$GVISOR" = true ]; then
  [ "$(uname -s)" = Linux ] || fail "--with-gvisor is for Linux hosts."
  if ! docker info --format '{{json .Runtimes}}' | grep -q runsc; then
    echo "Installing gVisor (runsc), by its official method ..."
    arch=$(uname -m)
    base="https://storage.googleapis.com/gvisor/releases/release/latest/${arch}"
    tmp=$(mktemp -d)
    (cd "$tmp" && curl -fsSLO "$base/runsc" -O "$base/runsc.sha512" -O "$base/containerd-shim-runsc-v1" \
        -O "$base/containerd-shim-runsc-v1.sha512" \
      && sha512sum -c runsc.sha512 -c containerd-shim-runsc-v1.sha512 \
      && chmod a+rx runsc containerd-shim-runsc-v1 \
      && sudo mv runsc containerd-shim-runsc-v1 /usr/local/bin/)
    rm -rf "$tmp"
    sudo /usr/local/bin/runsc install
    sudo systemctl reload docker || sudo systemctl restart docker
  fi
fi

# Run from a checkout, the compose file beside this script is used; piped from the web, it is downloaded.
source_dir=""
case "${BASH_SOURCE[0]:-}" in
  *install.sh) source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) ;;
esac
mkdir -p "$DIR"
if [ -n "$source_dir" ] && [ -f "$source_dir/compose.yaml" ]; then
  [ "$source_dir" -ef "$DIR" ] || cp "$source_dir/compose.yaml" "$DIR/compose.yaml"
else
  curl -fsSL "https://raw.githubusercontent.com/HarnessGateway/harnessgateway/v${VERSION}/deploy/compose.yaml" \
    -o "$DIR/compose.yaml"
fi
cd "$DIR"

secret() { head -c "$1" /dev/urandom | base64 | tr -d '\n'; }
kept() { sed -n "s/^$1=//p" .env; }
put() { { grep -v "^$1=" .env || true; echo "$1=$2"; } > .env.next; mv .env.next .env; }
umask 077
if [ ! -f .env ]; then
  cat > .env <<ENV
HG_SECRET_KEY=$(secret 32)
HG_PROXY_KEY=$(secret 32 | tr -d '/+=')
HG_DB_PASSWORD=$(secret 24 | tr -d '/+=')
HG_PORT=8000
HG_BIND=127.0.0.1
ENV
fi
# The images are this version's on every run: that is what updates an installation. A port or an address
# given now takes the place of the one kept. Everything else in .env stays as it is.
put HG_PLATFORM_IMAGE "${HG_PLATFORM_IMAGE:-ghcr.io/harnessgateway/harnessgateway:${VERSION}}"
put HG_SANDBOX_IMAGE "${HG_SANDBOX_IMAGE:-ghcr.io/harnessgateway/sandbox:${VERSION}}"
[ -z "$PORT" ] || put HG_PORT "$PORT"
[ -z "$BIND" ] || put HG_BIND "$BIND"
PORT=$(kept HG_PORT)
BIND=$(kept HG_BIND)

docker compose pull --quiet 2>/dev/null || true               # images built on this machine are used as they are
docker compose up --detach --wait db
docker compose up --detach serve proxy

printf 'Waiting for HarnessGateway '
for _ in $(seq 120); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/readyz" 2>/dev/null; then ready=1; break; fi
  printf '.'; sleep 1
done
echo
[ "${ready:-}" = 1 ] || { docker compose logs --tail 40 serve >&2; fail "it did not become ready; its last log lines are above."; }

echo
# Piped from the web, this script is bash's standard input: a command that reads it would take the rest.
docker compose exec -T serve harnessgateway init </dev/null
isolation=$(docker info --format '{{json .Runtimes}}' | grep -q runsc && echo gvisor || echo none)
cat <<DONE

HarnessGateway ${VERSION} is running.
  Console and API: http://${BIND}:${PORT}
  Files:           ${DIR}  (.env holds its secrets; keep it)
  Isolation:       ${isolation}$( [ "$isolation" = none ] && echo "  (sandboxes share this host's kernel: for development and trusted code; --with-gvisor on Linux changes that)" )

Next: open the console, sign in with the admin key above, add a provider and a harness, and chat.
Lost the key: cd ${DIR} && docker compose exec serve harnessgateway init --new-admin-key
To update it: run this installer again.
To stop it:   cd ${DIR} && docker compose down
To remove it: cd ${DIR} && docker compose down --volumes
              docker volume ls --quiet --filter name=hg- | xargs -r docker volume rm   (sessions' volumes)
DONE
