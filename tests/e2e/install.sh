#!/bin/sh
# Installs the platform as SPEC.md section 14.2 says: from the image alone, with four commands, into the folder
# HG_E2E_DIR. Leaves the first API key there, as `key`. Run by tests/e2e/run.sh, which removes the installation.
set -eu
image=${HG_E2E_IMAGE:?} port=${HG_E2E_PORT:?}
cd "${HG_E2E_DIR:?}"
fail() { echo "FAIL  $*" >&2; exit 1; }
pass() { echo "PASS  $*"; }

docker run --rm "$image" harnessgateway compose > compose.yaml
docker run --rm "$image" harnessgateway secrets > .env
grep -q "image: \${HG_IMAGE:-$image}" compose.yaml || fail "the Compose file does not name the image it came from"
! grep -q "build:" compose.yaml || fail "the Compose file builds something"
pass "the image prints the Compose file it is run with, which names it and builds nothing"
[ "$(wc -l < .env | tr -d ' ')" = 6 ] && grep -q "^HG_SECRET_KEY=.\{40,\}" .env || fail "the secrets are not a new installation's .env"
pass "the image prints a new installation's secrets"
# A namespace and a port of its own, so that it runs beside whatever else this host has.
sed -i.bak -e "s/^HG_NAMESPACE=.*/HG_NAMESPACE=${HG_E2E_NAMESPACE:?}/" -e "s/^HG_PORT=.*/HG_PORT=$port/" .env && rm .env.bak

docker compose up --detach --wait --quiet-pull > up.log 2>&1 || { tail -5 up.log >&2; fail "the installation did not become ready"; }
curl -fsS "http://127.0.0.1:$port/readyz" | grep -q '"ready"' || fail "it is up and says it is not ready"
pass "docker compose up --wait returns when the platform can work"

said=$(docker compose exec -T serve harnessgateway init </dev/null)
key=$(printf '%s\n' "$said" | awk 'END {print $NF}')
case "$key" in hg_*) ;; *) fail "init did not print a key: $said" ;; esac
printf '%s' "$key" > key
pass "init gives the defaults and prints the first API key"
docker compose exec -T serve harnessgateway init </dev/null | grep -q "Already set up" || fail "a second init made a second key"
other=$(docker compose exec -T serve harnessgateway init --new-key </dev/null | awk 'END {print $NF}')
[ "$other" != "$key" ] && case "$other" in hg_*) ;; *) fail "init --new-key made no key" ;; esac
pass "a second init makes no second key, and --new-key is the way back in"
! docker compose logs serve 2>/dev/null | grep -q "$key" || fail "the first key is in a log"
pass "the key is in no log"

about=$(curl -fsS -H "Authorization: Bearer $key" "http://127.0.0.1:$port/v1/platform")
printf '%s' "$about" | grep -q '"extensions": *\[\]' || fail "the core's image loaded an extension: $about"
printf '%s' "$about" | grep -q "\"version\": *\"$(docker run --rm "$image" harnessgateway --version)\"" || fail "the platform is not the image's version: $about"
pass "the installation is the core, at the image's version, with no extension"
shipped=$(curl -fsS -H "Authorization: Bearer $key" "http://127.0.0.1:$port/v1/environments/default")
printf '%s' "$shipped" | grep -q '"harness-package-builder"' || fail "the default environment was not given: $shipped"
pass "a new installation starts with the default environment and the packages that ship"
