#!/usr/bin/env bash
# Kilo Code, one turn: `kilo run --format json --auto`, mapped by map.py.
#   input text -> the message; input_file parts -> -f attachments
#   instructions -> AGENTS.md section (Kilo reads AGENTS.md; it has no system-prompt flag)
#   previous_response_id -> -s <session id kept in $HG_STATE/session>
#   reasoning.effort -> --variant (the provider's reasoning variant name)
set -uo pipefail
cd "$HG_WORKSPACE"
q() { jq -r "$1" "$HG_REQUEST"; }
prompt=$(q 'if (.input|type)=="string" then .input else [.input[] | (if .type=="input_text" then .text elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end) else empty end)] | join("\n") end')
instructions=$(q 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end')
effort=$(q '.reasoning.effort // ""')
attach=()
for f in "$HG_FILES"/*; do [ -f "$f" ] && cp -f "$f" "$HG_WORKSPACE/" && attach+=(-f "$HG_WORKSPACE/$(basename "$f")"); done
python3 "$HG_HARNESS_DIR/instructions.py" "$instructions"

export KILO_DISABLE_MODELS_FETCH=1 KILO_DISABLE_AUTOUPDATE=1 KILO_TELEMETRY_LEVEL=off KILO_PERMISSION='{"*":"allow"}' KILO_CONFIG_DIR="$HOME/.config/kilo"
mkdir -p "$KILO_CONFIG_DIR"
export KILO_CONFIG_CONTENT=$(cat <<CFG
{"autoupdate": false, "share": "disabled", "enabled_providers": ["anthropic"], "experimental": {"openTelemetry": false},
 "provider": {"anthropic": {"options": {"baseURL": "${HG_PROVIDER_URL%/v1}/v1", "apiKey": "{env:ANTHROPIC_API_KEY}"}, "models": {"$HG_MODEL": {}}}},
 "model": "anthropic/$HG_MODEL", "small_model": "anthropic/$HG_MODEL"}
CFG
)

steer=""
trap 'steer=$(cat "$HG_DIR/interrupt" 2>/dev/null || true)' USR1
run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

turn() {
  local args=(run --format json --auto -m "anthropic/$HG_MODEL")
  [ -n "$effort" ] && args+=(--variant "$effort")
  [ -s "$HG_STATE/session" ] && args+=(-s "$(cat "$HG_STATE/session")")
  kilo "${args[@]}" "${attach[@]}" "$1" | python3 "$HG_HARNESS_DIR/map.py"
}
run_wait turn "$prompt"; rc=$?
if [ -n "$steer" ]; then run_wait turn "$steer"; rc=$?; fi
exit $rc
