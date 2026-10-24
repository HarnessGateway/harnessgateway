#!/usr/bin/env bash
# Runs once per package commit, unprivileged, in the platform's base image (Debian, Python 3.12,
# Node 22, git, curl, jq, ripgrep), with egress to install.egress only. Put the CLI under $HG_TOOLS;
# $HG_TOOLS/bin is on PATH at run time. Pin the version.
set -euo pipefail
mkdir -p "$HG_TOOLS/bin" "$HG_TOOLS/lib"
# npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --strict-allow-scripts --allow-scripts=some-cli "some-cli@1.2.3"
#   (npm runs no install script it was not told to: name the packages whose scripts you allow;
#    --strict-allow-scripts fails the install when a version brings a new one, instead of a broken CLI.
#    npm 12 looks for $HG_TOOLS/lib before it would create it: make it with bin above.)
# python3 -m venv "$HG_TOOLS/venv" && "$HG_TOOLS/venv/bin/pip" install -q "some-cli==1.2.3" && ln -sf ../venv/bin/some-cli "$HG_TOOLS/bin/some-cli"
cat > "$HG_TOOLS/bin/echo-cli" <<'SH'
#!/bin/sh
# A stand-in for a coding agent: it "runs" a command, "writes" a file, answers, and reports usage,
# as JSON lines, the way a real CLI's JSON mode does.
task="$1"
printf '{"kind":"tool","name":"shell","command":"echo %s","output":"%s"}\n' "$(echo "$task" | wc -w) words" "$(echo "$task" | wc -w) words"
printf 'hello from echo-cli\n' > NOTES.txt
printf '{"kind":"tool","name":"write","path":"NOTES.txt","content":"hello from echo-cli\\n"}\n'
printf '{"kind":"text","text":"You said: %s"}\n' "$(printf '%s' "$task" | tr -d '"\\' | head -c 200)"
printf '{"kind":"done","input_tokens":12,"output_tokens":7}\n'
SH
chmod +x "$HG_TOOLS/bin/echo-cli"
