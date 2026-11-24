/** What a new harness starts as: the smallest package that works. Its agent is the shell, so it runs before
 *  anything is changed; each file says what a package for a real agent CLI puts in its place. */
export const template = () => new Map([
  ["install", { executable: true, text: `#!/bin/sh
# install: runs once for each environment image, before the first turn on it.
#
# A package for a real agent CLI installs that CLI here, at a pinned version, into $HG_HARNESS. This one has
# nothing to install: it checks the image has what its run needs.
set -eu
command -v python3 >/dev/null 2>&1 || { echo "this image has no python3, which this package's run needs" >&2; exit 1; }
` }],
  ["run", { executable: true, text: `#!/usr/bin/env python3
"""run: one turn. It reads the Responses request and prints Responses output: there is no other schema."""
import json
import os
import signal
import subprocess


def say(line):
    print(json.dumps(line), flush=True)


# 1. Read the request: the caller's body, as it was sent. Use as much of it as the agent can.
with open(os.path.join(os.environ["HG_TURN"], "request.json"), encoding="utf-8") as file:
    request = json.load(file)
command = request["input"] if isinstance(request["input"], str) else "\\n".join(
    part["text"] for item in request["input"] if item.get("role") == "user" and isinstance(item.get("content"), list)
    for part in item["content"] if part.get("type") == "input_text")

# 2. Start the CLI the way it runs headless. Here: the shell, with errors joined to the output. A stop is sent
#    to this program and to the CLI at once: take it, and go on until the CLI has ended.
signal.signal(signal.SIGTERM, lambda *_: None)
cli = subprocess.Popen(["sh", "-c", "exec 2>&1\\n" + command], stdout=subprocess.PIPE, text=True, errors="replace")

# 3. Print what it does as Responses output. Here: every line is a piece of the answer. A step the agent takes
#    is an output item, printed whole: {"type": "shell_call", ...}, {"type": "apply_patch_call", ...}.
for line in cli.stdout:
    say({"type": "response.output_text.delta", "delta": line})

# 4. Print the stream's last event, and exit 0.
if cli.wait() == 0:
    say({"type": "response.completed"})
else:
    say({"type": "response.failed", "response": {"error": {
        "code": "command_failed", "message": f"The command exited with code {cli.returncode}."}}})
` }],
]);
