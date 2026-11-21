#!/bin/sh
# Runs the tests in a container of the platform's image, against a test database on the installation's database
# server (the installation must be up: ./install.sh). Arguments are passed to pytest.
set -eu
cd "$(dirname "$0")/.."
ns=$(sed -n 's/^HG_NAMESPACE=//p' .env); password=$(sed -n 's/^HG_DB_PASSWORD=//p' .env)
docker compose exec -T db sh -c "psql -U hg -d hg -tc \"SELECT 1 FROM pg_database WHERE datname='hg_test'\" | grep -q 1 || createdb -U hg hg_test"
exec docker run --rm --network "${ns:-hg}-control" --volume "$PWD":/repo:ro --workdir /repo \
  --env HG_TEST_DATABASE_URL="postgresql://hg:${password}@db:5432/hg_test" --env PYTHONDONTWRITEBYTECODE=1 \
  --env PYTHONPATH=/repo/src "$(docker compose config --images | grep harnessgateway | head -1)" \
  sh -c 'uv pip install --system --quiet pytest pytest-asyncio && python -m pytest -p no:cacheprovider "$@"' -- "$@"
