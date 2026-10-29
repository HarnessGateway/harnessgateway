"""map.py run as the sandbox runs it (the request at $HG_REQUEST, the workspace as the working directory); every line
it prints is checked with the platform's own reader."""
import json
import os
import subprocess
import sys
from pathlib import Path

from harnessgateway.domain.contract import check_output

MAP = Path(__file__).resolve().parents[1] / "map.py"
HGKIT = Path(__file__).resolve().parents[3] / "sandbox"


def turn(tmp_path: Path, body: dict) -> tuple[list[dict], object]:
    request = tmp_path / "request.json"
    request.write_text(json.dumps(body))
    env = {**os.environ, "HG_REQUEST": str(request), "PYTHONPATH": str(HGKIT)}
    proc = subprocess.run([sys.executable, str(MAP)], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    return check_output(proc.stdout)


def test_the_input_runs_as_a_command(tmp_path):
    events, outcome = turn(tmp_path, {"model": "shell", "input": "echo hi > f.txt; cat f.txt; exit 3"})
    assert outcome.status == "completed" and outcome.metadata == {"exit_code": "3"}
    call, result, message = outcome.output
    assert call["type"] == "shell_call" and call["action"]["commands"] == ["echo hi > f.txt; cat f.txt; exit 3"]
    assert result["output"][0] == {"stdout": "hi\n", "stderr": "", "outcome": {"type": "exit", "exit_code": 3}}
    assert message["content"][0]["text"] == "hi\n" and (tmp_path / "f.txt").exists()


def test_fields_it_cannot_honour_are_named(tmp_path):
    _, outcome = turn(tmp_path, {"input": [{"role": "user", "content": [{"type": "input_text", "text": "true"}]}],
                                 "instructions": "be brief", "tools": [{"type": "web_search"}]})
    assert outcome.metadata["hg_ignored_fields"] == "instructions,tools"
    assert outcome.output[-1]["content"][0]["text"] == "(exit 0)"


def test_no_command_fails_the_turn(tmp_path):
    _, outcome = turn(tmp_path, {"input": ""})
    assert outcome.status == "failed" and outcome.error["code"] == "invalid_prompt"
