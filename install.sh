#!/usr/bin/env bash
# Installs HarnessGateway on this host, or updates it, and starts it.
#
#   ./install.sh [--port N] [--bind ADDRESS] [--namespace NAME]
#
#   --port N           the port the API is served on (default 8000)
#   --bind ADDRESS     the address it listens on (default 127.0.0.1: this host only)
#   --namespace NAME   names everything it makes on this host (default hg): give another to run a second one
#
# The first run writes .env, with the installation's secrets, and prints the first API key, once.
set -euo pipefail
cd "$(dirname "$0")"
fail() { echo "install: $*" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || fail "Docker is not installed."
docker info >/dev/null 2>&1 || fail "Docker is installed but not running, or this user may not use it."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is needed (the 'docker compose' command)."

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
HG_NAMESPACE=hg
ENV
fi
while [ $# -gt 0 ]; do
  case "$1" in
    --port) put HG_PORT "$2"; shift 2 ;;
    --bind) put HG_BIND "$2"; shift 2 ;;
    --namespace) put HG_NAMESPACE "$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

docker compose build --quiet
docker compose up --detach --wait db
# The first key is made before the platform starts, and printed here, not into a log. For an update, the old
# version is stopped first, so that it never runs on the new version's database.
docker compose stop serve proxy 2>/dev/null || true
echo
docker compose run --rm -T serve harnessgateway init </dev/null
docker compose up --detach serve proxy
port=$(kept HG_PORT)
printf 'Waiting for HarnessGateway '
for _ in $(seq 120); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${port}/readyz" 2>/dev/null; then ready=1; break; fi
  printf '.'; sleep 1
done
echo
[ "${ready:-}" = 1 ] || { docker compose logs --tail 40 serve >&2; fail "it did not become ready; its last log lines are above."; }
cat <<DONE
HarnessGateway is running.
  API:       http://$(kept HG_BIND):${port}
  Settings:  $(pwd)/.env  (it holds the installation's secrets; keep it)
Next: add a provider, a harness and an environment through the API, and send a request. README.md walks through it.
Lost the key: docker compose exec serve harnessgateway init --new-key
To update it: run this installer again.     To stop it: docker compose down
DONE
