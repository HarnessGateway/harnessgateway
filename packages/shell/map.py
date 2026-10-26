"""The shell harness's whole mapping, with hgkit: Responses request in, Responses events out."""
import subprocess

from hgkit import Emitter, Request

req, out = Request.load(), Emitter()
command = req.prompt
if not command:
    out.failed("invalid_prompt", "the input text is the command to run")
    raise SystemExit(0)
proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=540)
out.shell(command, stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)
out.message(proc.stdout if proc.stdout else f"(exit {proc.returncode})")
out.completed(metadata={"exit_code": str(proc.returncode)})
