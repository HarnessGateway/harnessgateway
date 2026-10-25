#!/usr/bin/env bash
# Codex, one turn. The request is read from $HG_REQUEST; `codex exec --json` goes through map.py.
#
#   input text            -> the prompt                 input_file parts -> the workspace; input_image -> -i
#   instructions          -> AGENTS.md, as a section replaced each turn (Codex reads AGENTS.md root to cwd)
#   previous_response_id  -> codex exec resume <thread id kept in $HG_STATE/thread>
#   reasoning.effort      -> model_reasoning_effort     reasoning.summary -> model_reasoning_summary
#   text.format           -> --output-schema            text.verbosity -> model_verbosity
#   tools of type mcp     -> [mcp_servers.<label>] with the request's URL and headers
set -uo pipefail
cd "$HG_WORKSPACE"
q() { jq -r "$1" "$HG_REQUEST"; }
prompt=$(q 'if (.input|type)=="string" then .input else [.input[] | (if .type=="input_text" then .text elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end) else empty end)] | join("\n") end')
instructions=$(q 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end')
effort=$(q '.reasoning.effort // ""'); summary=$(q '.reasoning.summary // ""'); verbosity=$(q '.text.verbosity // ""')
schema=$(q '.text.format | select(.type=="json_schema") | .schema // empty | tojson')
images=()
for f in "$HG_FILES"/*; do
  [ -f "$f" ] || continue
  case "$f" in *.png|*.jpg|*.jpeg|*.gif|*.webp) images+=(-i "$f") ;; *) cp -f "$f" "$HG_WORKSPACE/" ;; esac
done
python3 "$HG_HARNESS_DIR/instructions.py" "$instructions"

# the provider and the rest of Codex's configuration, in its own file in this member's home
export CODEX_HOME="$HOME/.codex"
mkdir -p "$CODEX_HOME"
{
  cat <<TOML
model_provider = "hg"
model = "$HG_MODEL"
approval_policy = "never"
sandbox_mode = "danger-full-access"
check_for_update_on_startup = false
TOML
  [ -n "$effort" ] && echo "model_reasoning_effort = \"$effort\""
  [ -n "$summary" ] && echo "model_reasoning_summary = \"$summary\""
  [ -n "$verbosity" ] && echo "model_verbosity = \"$verbosity\""
  cat <<TOML
[model_providers.hg]
name = "hg"
base_url = "${HG_PROVIDER_URL%/}"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
[features]
plugins = false
api_key_model_discovery = false
unbounded_connection_retries = false
[analytics]
enabled = false
TOML
  q '.tools // [] | .[] | select(.type=="mcp") | "[mcp_servers.\(.server_label)]\nurl = \"\(.server_url)\"" + (if .authorization then "\nbearer_token_env_var = \"HG_MCP_\(.server_label | ascii_upcase | gsub("[^A-Z0-9]"; "_"))\"" else "" end) + (if .headers then "\nhttp_headers = \(.headers | tojson)" else "" end)'
} > "$CODEX_HOME/config.toml"
while IFS=$'\t' read -r label token; do [ -n "$label" ] && export "HG_MCP_$(echo "$label" | tr a-z A-Z | sed 's/[^A-Z0-9]/_/g')=$token"; done < <(q '.tools // [] | .[] | select(.type=="mcp" and .authorization) | "\(.server_label)\t\(.authorization)"')

run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

turn() {
  local args=(--json --skip-git-repo-check)
  [ -n "$schema" ] && { printf '%s' "$schema" > "$HG_DIR/schema.json"; args+=(--output-schema "$HG_DIR/schema.json"); }
  if [ -s "$HG_STATE/thread" ]; then
    codex exec resume "${args[@]}" "$(cat "$HG_STATE/thread")" "$1" | python3 "$HG_HARNESS_DIR/map.py"
  else
    codex exec "${args[@]}" "${images[@]}" -m "$HG_MODEL" "$1" | python3 "$HG_HARNESS_DIR/map.py"
  fi
}
run_wait turn "$prompt"; rc=$?
exit $rc
