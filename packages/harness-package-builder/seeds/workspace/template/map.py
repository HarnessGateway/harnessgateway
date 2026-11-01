"""The shell harness, one turn: the input text runs as a bash command in the workspace; its output comes back as a
shell_call, its output and a message. Request fields other than the input are ignored and named as such."""
import subprocess

from hgkit import Emitter, Request

IGNORED = ("instructions", "reasoning", "text", "tools", "max_output_tokens", "max_tool_calls")


def main() -> None:
    req, out = Request.load(), Emitter()
    out.ignored([f for f in IGNORED if req.get(f)])
    command = req.prompt
    if not command:
        out.failed("invalid_prompt", "the input text is the command to run")
        return
    proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    out.shell(command, stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)
    out.message(proc.stdout or f"(exit {proc.returncode})")
    out.completed(metadata={"exit_code": str(proc.returncode)})


if __name__ == "__main__":
    main()
