#!/usr/bin/env bash
# Command Code, one turn: `command-code -p --output-format json --yolo`, mapped by map.py.
#   input text -> the query; input_file parts -> the workspace (named as @paths in the query)
#   instructions -> AGENTS.md section (Command Code reads AGENTS.md; it has no system-prompt flag)
#   previous_response_id -> --resume <session id kept in $HG_STATE/session>
#   reasoning.effort -> --effort;  max_tool_calls -> --max-turns;  tools of type mcp -> .mcp.json;  store: false -> --no-session
set -uo pipefail
cd "$HG_WORKSPACE"
q() { jq -r "$1" "$HG_REQUEST"; }
prompt=$(q 'if (.input|type)=="string" then .input else [.input[] | (if .type=="input_text" then .text elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end) else empty end)] | join("\n") end')
instructions=$(q 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end')
effort=$(q '.reasoning.effort // ""'); max_calls=$(q '.max_tool_calls // ""'); store=$(q '.store // true')
for f in "$HG_FILES"/*; do [ -f "$f" ] && cp -f "$f" "$HG_WORKSPACE/" && prompt="$prompt @$(basename "$f")"; done
python3 "$HG_HARNESS_DIR/instructions.py" "$instructions"
if [ "$(q '[.tools // [] | .[] | select(.type=="mcp")] | length')" != "0" ]; then
  q '{mcpServers: ([.tools[] | select(.type=="mcp") | {key: .server_label, value: ({transport: "http", url: .server_url} + (if .headers or .authorization then {headers: ((.headers // {}) + (if .authorization then {Authorization: ("Bearer " + .authorization)} else {} end))} else {} end))}] | from_entries)}' > .mcp.json
fi

# the provider: a bring-your-own entry whose wire shape follows the provider's
export CMD_LOCAL_ONLY=1 COMMANDCODE_SKIP_UPDATES=1 HG_PROVIDER_TOKEN="$ANTHROPIC_API_KEY"
mkdir -p "$HOME/.commandcode"
api="anthropic-messages"; [ "${HG_PROVIDER_API:-}" = "openai-chat" ] && api="openai-completions"; [ "${HG_PROVIDER_API:-}" = "openai-responses" ] && api="openai-responses"
cat > "$HOME/.commandcode/providers.json" <<CFG
{"provider": {"hg": {"name": "HarnessGateway", "api": "$api", "baseURL": "${HG_PROVIDER_URL%/}", "apiKey": "\$HG_PROVIDER_TOKEN",
                     "models": {"$HG_MODEL": {"name": "$HG_MODEL", "contextWindow": 128000, "maxOutput": 16384}}}}}
CFG
[ -s "$HOME/.commandcode/config.json" ] || echo '{"provider": "hg", "model": "hg/'"$HG_MODEL"'", "localOnly": true}' > "$HOME/.commandcode/config.json"

steer=""
trap 'steer=$(cat "$HG_DIR/interrupt" 2>/dev/null || true)' USR1
run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

turn() {
  local args=(-p --output-format json --yolo --local-only --skip-onboarding --trust -m "hg/$HG_MODEL")
  case "$effort" in low|medium|high|xhigh|max) args+=(--effort "$effort") ;; esac
  [ -n "$max_calls" ] && args+=(--max-turns "$max_calls")
  [ "$store" = "false" ] && args+=(--no-session)
  [ -s "$HG_STATE/session" ] && [ "$store" != "false" ] && args+=(--resume "$(cat "$HG_STATE/session")")
  command-code "${args[@]}" "$1" | python3 "$HG_HARNESS_DIR/map.py"
}
run_wait turn "$prompt"; rc=$?
if [ -n "$steer" ]; then run_wait turn "$steer"; rc=$?; fi
exit $rc
