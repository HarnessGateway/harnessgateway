#!/usr/bin/env bash
# DeepSeek Harness, one turn: `dsh --profile headless --json`, mapped by map.py.
#   input text -> the task; input_file parts -> the workspace
#   instructions -> AGENTS.md section (dsh reads AGENTS.md and CLAUDE.md from the project root to cwd)
#   previous_response_id -> --session-id <id kept in $HG_STATE/session>
#   reasoning.effort -> the llm-deepseek row's reasoningEffort (off | low | high | max), through a --patch
set -uo pipefail
cd "$HG_WORKSPACE"
q() { jq -r "$1" "$HG_REQUEST"; }
prompt=$(q 'if (.input|type)=="string" then .input else [.input[] | (if .type=="input_text" then .text elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end) else empty end)] | join("\n") end')
instructions=$(q 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end')
effort=$(q '.reasoning.effort // ""')
for f in "$HG_FILES"/*; do [ -f "$f" ] && cp -f "$f" "$HG_WORKSPACE/"; done
python3 "$HG_HARNESS_DIR/instructions.py" "$instructions"

export DSH_HOME="$HOME/.dsh" DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL%/v1}" DSH_TELEMETRY_DISABLED=1 DSH_PERMISSION_MODE=danger-full-access
mkdir -p "$DSH_HOME"
case "$effort" in minimal) effort=low ;; medium) effort=high ;; xhigh|ultra) effort=max ;; esac
{
  printf -- '- id: agent-default-model\n  config: {provider: deepseek-official, model: %s}\n' "$HG_MODEL"
  printf -- '- id: session-log-deepseek\n  disabled: true\n'
  case "$effort" in off|low|high|max) printf -- '- id: llm-deepseek\n  config: {apiKeyEnv: DEEPSEEK_API_KEY, baseURL: %s, reasoningEffort: %s}\n' "$DEEPSEEK_BASE_URL" "$effort" ;;
                   *) printf -- '- id: llm-deepseek\n  config: {apiKeyEnv: DEEPSEEK_API_KEY, baseURL: %s}\n' "$DEEPSEEK_BASE_URL" ;; esac
} > "$HG_DIR/patch.yml"

run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

turn() {
  local args=(--profile headless --patch "$HG_DIR/patch.yml" --json)
  [ -s "$HG_STATE/session" ] && args+=(--session-id "$(cat "$HG_STATE/session")")
  dsh "${args[@]}" "$1" | python3 "$HG_HARNESS_DIR/map.py"
}
run_wait turn "$prompt"; rc=$?
exit $rc
