#!/usr/bin/env bash
# Cline, one turn: `cline --json`, mapped by map.py.
#   input text -> the prompt; input_file parts -> the workspace (Cline attaches @paths named in the prompt)
#   instructions -> AGENTS.md section (Cline reads AGENTS.md and .clinerules; -s would replace its whole system prompt)
#   previous_response_id -> --id <session id kept in $HG_STATE/session>; the documentation says this resumes,
#                           the 3.0.73 source resumes only in interactive mode, so each turn may start fresh
#   reasoning.effort -> --thinking
set -uo pipefail
cd "$HG_WORKSPACE"
q() { jq -r "$1" "$HG_REQUEST"; }
prompt=$(q 'if (.input|type)=="string" then .input else [.input[] | (if .type=="input_text" then .text elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end) else empty end)] | join("\n") end')
instructions=$(q 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end')
effort=$(q '.reasoning.effort // ""')
for f in "$HG_FILES"/*; do [ -f "$f" ] && cp -f "$f" "$HG_WORKSPACE/" && prompt="$prompt @./$(basename "$f")"; done
python3 "$HG_HARNESS_DIR/instructions.py" "$instructions"

export CLINE_SESSION_BACKEND_MODE=local CLINE_NO_AUTO_UPDATE=1
mkdir -p "$HOME/.cline/data/settings"
cat > "$HOME/.cline/data/settings/providers.json" <<CFG
{"version": 1, "lastUsedProvider": "openai-compatible", "modes": {},
 "providers": {"openai-compatible": {"settings": {"provider": "openai-compatible", "apiKey": "$OPENAI_API_KEY", "model": "$HG_MODEL", "baseUrl": "${HG_PROVIDER_URL%/}"},
                                     "updatedAt": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "tokenSource": "manual"}}}
CFG

run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

turn() {
  local args=(--json -v --auto-approve true -P openai-compatible -m "$HG_MODEL")
  case "$effort" in none|low|medium|high|xhigh) args+=(--thinking "$effort") ;; esac
  [ -s "$HG_STATE/session" ] && args+=(--id "$(cat "$HG_STATE/session")")
  cline "${args[@]}" "$1" | python3 "$HG_HARNESS_DIR/map.py"
}
run_wait turn "$prompt"; rc=$?
exit $rc
