#!/usr/bin/env bash
# One turn. The request, as the template staged it in $HG_REQUEST, becomes foobar's flags; the
# seeds' instructions and the request's instructions become its system text; a continuation
# becomes --resume with the state kept in $HG_DIR between turns. foobar's native events go to
# stdout, which the template relays live and hands to items.py.
set -euo pipefail
cd "$HG_WORKSPACE"
field() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2], ""); print("1" if v is True else "" if v is False else v)' "$HG_REQUEST" "$1"; }
system="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("instructions", ""))' "$HOME/.foobar/config.json")"
if [ -n "${HG_INSTRUCTIONS:-}" ]; then system="${system:+$system
}$HG_INSTRUCTIONS"; fi
args=(--model "$HG_MODEL" --state "$HG_DIR/foobar" --config "$HOME/.foobar/config.json" --effort "$(field reasoning_effort)")
[ -n "$system" ] && args+=(--system "$system")
[ -n "$(field resume)" ] && args+=(--resume)
exec foobar "${args[@]}" "$(cat "$HG_DIR/prompt")"
