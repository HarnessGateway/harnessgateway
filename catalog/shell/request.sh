#!/usr/bin/env bash
# One turn: the request's input is a shell script, run in the workspace. The workspace persists across
# the session's turns, so a continuation (previous_response_id) sees what earlier turns left.
#   input          the script; a list input's text parts are joined with newlines
#   instructions   run first, their output not shown: a setup step (exports, installs within egress)
# The output: one shell_call item with the script, its shell_call_output with stdout, stderr and the
# exit code, and a message carrying stdout, so output_text is what the command printed.
set -uo pipefail
cd "$HG_WORKSPACE"

script=$(jq -r 'if (.input|type)=="string" then .input
                else [.input[] | (if .type=="input_text" then .text
                                  elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end)
                                  else empty end)] | join("\n") end' "$HG_REQUEST")
setup=$(jq -r 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end' "$HG_REQUEST")

run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

if [ -n "$setup" ]; then
  echo "log: running the instructions as setup"
  run_wait bash -c "$setup" > "$HG_DIR/setup.out" 2>&1 || echo "log: setup exited $?"
fi

turn() {
  local n="$1" body="$2" out err rc
  out="$HG_DIR/out.$n"; err="$HG_DIR/err.$n"
  run_wait bash -c "$body" > "$out" 2> "$err"; rc=$?
  python3 "$HG_HARNESS_DIR/map.py" "$n" "$rc" "$body" "$out" "$err"
  return $rc
}
turn 1 "$script"; rc=$?
python3 -c 'import json; print(json.dumps({"type": "response.completed", "response": {"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}}))'
exit 0
