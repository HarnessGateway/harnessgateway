/** What a new harness starts as: the smallest package that works. Its agent is the shell, so it runs before
 *  anything is changed; each file says what a package for a real agent CLI puts in its place. */
export const template = name => new Map([
  ["harness.json", { executable: false, text: JSON.stringify({ contract: 1, name, version: "0.1", description: "", apis: [] }, null, 2) + "\n" }],
  ["install", { executable: true, text: `#!/bin/sh
# install: runs once for each environment image, before the first turn on it.
#
# A package for a real agent CLI installs that CLI here, at a pinned version, into $HG_HARNESS. This one has
# nothing to install: it checks the image has what its run needs.
set -eu
command -v python3 >/dev/null 2>&1 || { echo "this image has no python3, which this package's run needs" >&2; exit 1; }
` }],
  ["run", { executable: true, text: `#!/usr/bin/env python3
"""run: one turn. Called in /work, with the request under /turn."""
import os
import sys

sys.path.insert(0, os.path.join(os.environ["HG_HARNESS"], "kit"))

import hgkit  # noqa: E402 - the kit is found through the path set above

# 1. Read the request.
request, emit = hgkit.Request(), hgkit.Emitter()
if not request.prompt.strip():
    emit.failed("no_command", "The input is empty: there is no command to run.")
    sys.exit(0)

# 2. Start the CLI the way it runs headless. Here: the shell, with errors joined to the output.
cli = hgkit.Cli(["sh", "-c", "exec 2>&1\\n" + request.prompt])

# 3. Print what it does as Responses events. Here: every line it prints is a piece of the answer.
for line in cli.lines():
    emit.say(line + "\\n")

# 4. Print one terminal line, and exit 0.
if cli.exit_code == 0:
    emit.done()
else:
    emit.failed("command_failed", f"The command exited with code {cli.exit_code}.")
` }],
]);
