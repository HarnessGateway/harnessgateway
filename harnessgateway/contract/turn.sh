#!/usr/bin/env bash
# The turn, as the platform runs it inside the sandbox. Mounted read-only; the package never sees it.
#
#   request.sh   the request payload (the client's Responses request, verbatim JSON) on stdin and at
#                $HG_REQUEST; its stdout is relayed live and kept in $HG_STDOUT, its stderr relayed and
#                kept in $HG_STDERR; its exit code is $HG_EXIT. JSON lines on stdout whose "type" starts
#                with "response." are Responses stream events, relayed to the client as they are.
#   response.sh  runs after request.sh ends however it ended, with HG_STDOUT, HG_STDERR, HG_EXIT and
#                HG_OUTCOME (exited | cancelled); what it prints is a JSON object with the response's
#                output, usage, status, error and metadata; its exit code is the status. Without one,
#                the response is assembled from the stream events request.sh printed.
#
# A stop (SIGTERM) is passed to request.sh's whole process group; response.sh still runs, within the
# stop's grace. The steer signal ($HG_INTERRUPT) is delivered to request.sh alone, never to the CLI
# it runs, so a package handles a steer in one trap and nothing it starts has to.
set -u
cd "$HG_WORKSPACE"
export HG_STDOUT="$HG_DIR/stdout" HG_STDERR="$HG_DIR/stderr"
rm -f "$HG_DIR/.out" "$HG_DIR/.err" "$HG_DIR/response" "$HG_DIR/response.err"
: > "$HG_STDOUT"; : > "$HG_STDERR"
mkfifo "$HG_DIR/.out" "$HG_DIR/.err"
set -m
tee -a "$HG_STDOUT" < "$HG_DIR/.out" & out=$!
tee -a "$HG_STDERR" < "$HG_DIR/.err" >&2 & err=$!
outcome=exited
bash "$HG_HARNESS_DIR/request.sh" < "$HG_REQUEST" > "$HG_DIR/.out" 2> "$HG_DIR/.err" & pid=$!
trap 'outcome=cancelled; kill -TERM -- "-$pid" 2>/dev/null' TERM
trap 'kill -s "$HG_INTERRUPT" "$pid" 2>/dev/null' "$HG_INTERRUPT"
while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done
wait "$out" "$err" 2>/dev/null
trap - TERM "$HG_INTERRUPT"
rm -f "$HG_DIR/.out" "$HG_DIR/.err"
export HG_EXIT=$rc HG_OUTCOME=$outcome
if [ -f "$HG_HARNESS_DIR/response.sh" ]; then
  bash "$HG_HARNESS_DIR/response.sh" < /dev/null > "$HG_DIR/response" 2> "$HG_DIR/response.err"
  exit $?
fi
exit "$rc"
