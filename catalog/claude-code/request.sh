#!/usr/bin/env bash
# Claude Code, one turn. The request is read from $HG_REQUEST; the CLI's stream-json goes through
# map.py, which prints Responses stream events and keeps the CLI's session id in $HG_STATE.
#
# Request fields and where they go (the precedence ladder in docs/responses-wire.md):
#   input text            -> the prompt (-p)            input_file parts -> copied into the workspace
#   instructions          -> --append-system-prompt     (Claude Code reads AGENTS.md only when no CLAUDE.md
#                                                        exists, so the flag is the reliable way here)
#   previous_response_id  -> --resume <session id kept in $HG_STATE/session>
#   reasoning.effort      -> --effort                   tools of type mcp -> --mcp-config (a file built here)
#   max_tool_calls        -> --max-turns                text.format json_schema -> --json-schema (print mode)
set -uo pipefail
cd "$HG_WORKSPACE"
q() { jq -r "$1" "$HG_REQUEST"; }
prompt=$(q 'if (.input|type)=="string" then .input else [.input[] | (if .type=="input_text" then .text elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end) else empty end)] | join("\n") end')
instructions=$(q 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end')
effort=$(q '.reasoning.effort // ""')
max_calls=$(q '.max_tool_calls // ""')
schema=$(q '.text.format | select(.type=="json_schema") | .schema // empty | tojson')
for f in "$HG_FILES"/*; do [ -f "$f" ] && cp -f "$f" "$HG_WORKSPACE/"; done

# the provider: the gateway's URL and the session token, through the variables Claude Code reads
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-$HG_PROVIDER_URL}"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$HG_MODEL" ANTHROPIC_DEFAULT_SONNET_MODEL="$HG_MODEL" ANTHROPIC_DEFAULT_OPUS_MODEL="$HG_MODEL" ANTHROPIC_SMALL_FAST_MODEL="$HG_MODEL"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 IS_SANDBOX=1 DISABLE_AUTOUPDATER=1 CLAUDE_CONFIG_DIR="$HOME/.claude"
mkdir -p "$CLAUDE_CONFIG_DIR"

# MCP servers named in the request: Claude Code's own file shape, passed with --mcp-config
mcp=""
if [ "$(q '[.tools // [] | .[] | select(.type=="mcp")] | length')" != "0" ]; then
  q '{mcpServers: ([.tools[] | select(.type=="mcp") | {key: .server_label, value: ({type: "http", url: .server_url} + (if .headers then {headers: .headers} else {} end) + (if .authorization then {headers: ((.headers // {}) + {Authorization: ("Bearer " + .authorization)})} else {} end))}] | from_entries)}' > "$HG_DIR/mcp.json"
  mcp="$HG_DIR/mcp.json"
fi

run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

turn() {
  local args=(-p "$1" --output-format stream-json --verbose --dangerously-skip-permissions --model "$HG_MODEL")
  [ -n "$instructions" ] && args+=(--append-system-prompt "$instructions")
  case "$effort" in low|medium|high|xhigh|max) args+=(--effort "$effort") ;; esac
  [ -n "$max_calls" ] && args+=(--max-turns "$max_calls")
  [ -n "$schema" ] && args+=(--json-schema "$schema")
  [ -n "$mcp" ] && args+=(--mcp-config "$mcp" --strict-mcp-config)
  [ -s "$HG_STATE/session" ] && args+=(--resume "$(cat "$HG_STATE/session")")
  claude "${args[@]}" | python3 "$HG_HARNESS_DIR/map.py"
}
run_wait turn "$prompt"; rc=$?
exit $rc
