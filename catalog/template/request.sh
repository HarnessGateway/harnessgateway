#!/usr/bin/env bash
# One turn. The client's Responses request is at $HG_REQUEST (and on stdin), verbatim. This script
# reads what the CLI can use, runs the CLI in $HG_WORKSPACE, and pipes its output through map.py,
# which prints Responses stream events; the platform relays those to the client as they appear and
# assembles the response from them.
#
# The platform sets: HG_REQUEST, HG_WORKSPACE (cwd, kept between turns), HOME (seeded, kept per member),
# HG_STATE (kept with the session: put the CLI's resume handle here), HG_FILES (the request's
# input_file parts, decoded), HG_DIR (scratch; $HG_DIR/interrupt holds a steer's text), HG_SESSION,
# HG_RESPONSE, HG_TURN (1, 2, …), HG_PREVIOUS (the previous_response_id, or empty), HG_MODEL (the
# model segment of the request's model path, or the package default), HG_PROVIDER_URL and
# HG_PROVIDER_KEY_FILE (the gateway and the session token), plus the provider_env names from
# harness.json carrying the same URL and token. HG_TOOLS/bin is on PATH.
set -uo pipefail
cd "$HG_WORKSPACE"

# 1. the request: the input's text, and the fields the CLI can take
prompt=$(jq -r 'if (.input|type)=="string" then .input
                else [.input[] | (if .type=="input_text" then .text
                                  elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end)
                                  else empty end)] | join("\n") end' "$HG_REQUEST")
instructions=$(jq -r 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end' "$HG_REQUEST")
effort=$(jq -r '.reasoning.effort // ""' "$HG_REQUEST")

# 2. instructions, by the precedence ladder: the file the most harnesses read, as a section of
#    AGENTS.md that is replaced each turn (a CLI with a flag would take --append-system-prompt instead)
python3 "$HG_HARNESS_DIR/instructions.py" "$instructions"

# 3. a steer: the text arrives in $HG_DIR/interrupt with this signal, delivered to this script alone;
#    the usual answer is to let the running turn end and run the steer as the next turn
steer=""
trap 'steer=$(cat "$HG_DIR/interrupt" 2>/dev/null || true)' USR1
run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

# 4. the turn: the CLI, resumed from $HG_STATE on later turns, its output mapped to Responses events.
#    Point a real CLI at $HG_PROVIDER_URL with the token in $OPENAI_API_KEY (whatever provider_env names).
turn() {
  local resume=""
  [ -s "$HG_STATE/session" ] && resume=$(cat "$HG_STATE/session")
  echo "log: turn=$HG_TURN model=${HG_MODEL:-default} effort=${effort:-none} resume=${resume:-new} files=$(ls "$HG_FILES" | wc -l)"   # a plain line: kept as the log, not streamed
  echo "$HG_SESSION" > "$HG_STATE/session"        # what a real CLI prints as its session id goes here
  echo-cli "$1" | python3 "$HG_HARNESS_DIR/map.py"
}
run_wait turn "$prompt"; rc=$?
if [ -n "$steer" ]; then run_wait turn "$steer"; rc=$?; fi
exit $rc
