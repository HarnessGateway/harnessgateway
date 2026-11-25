#!/bin/sh
# Extensions as a chain, in a real browser: the checkout's server, run with the sample edition and a second
# extension after it (tests/integration/edition.py, second.py), a stand-in sandbox, and a database the tests may
# empty (HG_TEST_DATABASE_URL). Then tests/e2e/chain.cjs opens its console. The core's image carries no extension,
# so this is where the console's side of the chain is shown.
set -eu
cd "$(dirname "$0")/../.."
[ -n "${HG_TEST_DATABASE_URL:-}" ] || { echo "FAIL  HG_TEST_DATABASE_URL is not set: this check needs a database it may empty" >&2; exit 1; }
port=${HG_CHAIN_PORT:-8791}
data=$(mktemp -d)
export PYTHONPATH=. HG_DATABASE_URL="$HG_TEST_DATABASE_URL" HG_PROXY_KEY=chain HG_DATA="$data" HG_PORT="$port" HG_INTERNAL_PORT=$((port + 1)) \
  HG_SECRET_KEY="$(uv run harnessgateway secrets | sed -n 's/^HG_SECRET_KEY=//p')" \
  HG_EXTENSIONS=tests.integration.edition:Edition,tests.integration.second:Second HG_DRIVER=sample HG_BLOBS=memory://chain
uv run python - <<'EMPTY'
import asyncio
import os

from harnessgateway.testing import emptied


async def main() -> None:
    await (await emptied(os.environ["HG_DATABASE_URL"])).close()

asyncio.run(main())
EMPTY
key=$(uv run harnessgateway init | awk 'END {print $NF}')
uv run harnessgateway serve > "$data/serve.log" 2>&1 &
server=$!
stopped() { code=$?; kill "$server" 2>/dev/null || true; wait "$server" 2>/dev/null || true; [ "$code" = 0 ] || tail -30 "$data/serve.log"; rm -rf "$data"; exit "$code"; }
trap stopped EXIT INT TERM
for _ in $(seq 60); do curl -fsS -o /dev/null "http://127.0.0.1:$port/readyz" 2>/dev/null && break; sleep 0.5; done
HG_URL="http://127.0.0.1:$port" HG_KEY="$key" node tests/e2e/chain.cjs
