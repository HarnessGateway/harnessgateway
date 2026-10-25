#!/usr/bin/env bash
# Hermes Agent, one turn: `hermes chat -q -Q` (quiet chat: the answer on stdout, then a session_id
# line), mapped by map.py. Hermes 0.19.0 has no JSON event stream (`chat --format stream-json` arrived
# after it), and its one-shot mode (-z) does not carry a resumed session's history, so chat mode it is:
# the response is the final text; the CLI reports no token usage this way.
#   input text -> the query; input_file parts -> the workspace
#   instructions -> AGENTS.md section (Hermes reads the workspace's AGENTS.md as its project context)
#   previous_response_id -> --resume <session id from the session_id line, kept in $HG_STATE/session>
set -uo pipefail
cd "$HG_WORKSPACE"
q() { jq -r "$1" "$HG_REQUEST"; }
prompt=$(q 'if (.input|type)=="string" then .input else [.input[] | (if .type=="input_text" then .text elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end) else empty end)] | join("\n") end')
instructions=$(q 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end')
for f in "$HG_FILES"/*; do [ -f "$f" ] && cp -f "$f" "$HG_WORKSPACE/"; done
python3 "$HG_HARNESS_DIR/instructions.py" "$instructions"

export HERMES_HOME="$HOME/.hermes" HERMES_YOLO_MODE=1 HERMES_ACCEPT_HOOKS=1 HERMES_INFERENCE_PROVIDER=anthropic
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL%/v1}"
mkdir -p "$HERMES_HOME"
cat > "$HERMES_HOME/config.yaml" <<CFG
model:
  provider: anthropic
  base_url: ${ANTHROPIC_BASE_URL}
  default: ${HG_MODEL}
  context_length: 128000
security:
  tirith_enabled: false
memory:
  nudge_interval: 0
skills:
  creation_nudge_interval: 0
agent:
  verify_on_stop: false
CFG

run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

turn() {
  local args=(chat -q "$1" -Q --yolo --provider anthropic -m "$HG_MODEL" --no-restore-cwd)
  [ -s "$HG_STATE/session" ] && args+=(--resume "$(cat "$HG_STATE/session")")
  hermes "${args[@]}" 2> "$HG_DIR/hermes.err" | python3 "$HG_HARNESS_DIR/map.py" "$HG_DIR/hermes.err"
  local rc=${PIPESTATUS[0]}
  cat "$HG_DIR/hermes.err" >&2
  return $rc
}
run_wait turn "$prompt"; rc=$?
exit $rc
