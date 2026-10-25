#!/bin/bash
# One turn, as the platform runs it inside the sandbox. Mounted read-only at /opt/hg/runtime; packages never see it.
#
# request.sh runs in a process group of its own (setsid), with the Responses request on stdin and at $HG_REQUEST.
# Its stdout and stderr are the turn's; its exit code is this script's. The node stops a turn by signalling the
# group recorded in /tmp/hg-turn.pid with SIGTERM; after the package's stop grace it SIGKILLs every process in the box.
set -u
cd "$HG_WORKSPACE" || exit 120
setsid bash "$HG_PACKAGE_DIR/request.sh" < "$HG_REQUEST" &
pid=$!
echo "$pid" > /tmp/hg-turn.pid
wait "$pid"
rc=$?
rm -f /tmp/hg-turn.pid
exit "$rc"
