#!/bin/sh
# Every end-to-end check, on an installation made for them: the image is built from this tree, installed as
# SPEC.md section 14.2 says (the image and four commands, nothing from the repository), checked, and removed.
#
#   tests/e2e/run.sh
#
# It runs beside whatever else the host has: a namespace (HG_E2E_NAMESPACE, default hge2e) and a port
# (HG_E2E_PORT, default 8790) of its own. It needs Docker with Compose v2, python3, node with Playwright (NODE_PATH
# may name where), and for the last check uv and a database the tests may empty (HG_TEST_DATABASE_URL).
set -eu
cd "$(dirname "$0")/../.."
export HG_E2E_NAMESPACE="${HG_E2E_NAMESPACE:-hge2e}" HG_E2E_PORT="${HG_E2E_PORT:-8790}" HG_E2E_IMAGE=harnessgateway:e2e
HG_E2E_DIR=$(mktemp -d) && export HG_E2E_DIR
removed() {
  code=$?
  [ "$code" = 0 ] || (cd "$HG_E2E_DIR" && docker compose logs --tail 60 serve proxy 2>/dev/null) || true
  (cd "$HG_E2E_DIR" && docker compose down --volumes --remove-orphans >/dev/null 2>&1) || true
  rm -rf "$HG_E2E_DIR"
  exit "$code"
}
trap removed EXIT INT TERM

echo "== the image, built from this tree"
docker build --quiet --build-arg "HG_IMAGE=$HG_E2E_IMAGE" --tag "$HG_E2E_IMAGE" . >/dev/null
echo "== installing from the image alone"
tests/e2e/install.sh
export HG_URL="http://127.0.0.1:$HG_E2E_PORT" HG_KEY="$(cat "$HG_E2E_DIR/key")"
echo "== real turns, in real containers"
python3 tests/e2e/turns.py
echo "== every page of the console, in a real browser"
node tests/e2e/console.cjs
echo "== updating in place"
tests/e2e/update.sh
echo "== extensions as a chain, in a real browser"
tests/e2e/chain.sh
echo "every end-to-end check passed"
