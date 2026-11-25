"""The platform is agent infrastructure: it runs any harness package, and knows no agent CLI. Everything about one
is in its package. This is what keeps that true."""

import re

from harnessgateway import shipped

# Agent CLIs by the names their makers give them. An API shape (`anthropic`, `gemini`) is not one of them.
_CLIS = re.compile(r"\b(claude[ -]?code|claude|codex|opencode|open code|copilot|cursor[ -]agent|goose|aider|gemini[ -]cli|"
                   r"pi[ -]coding[ -]agent|amp code|ampcode)\b", re.I)
_READ = (".py", ".sql", ".md", ".json", ".js", ".html", ".css")


def test_the_server_the_console_and_what_the_builder_is_told_name_no_agent_cli():
    server = shipped.folder("console").parent / "src" if (shipped.folder("console").parent / "src").is_dir() else None
    assert server is not None, "this test reads a checkout"
    files = [path for folder in (server, shipped.folder("console")) for path in folder.rglob("*")
             if path.is_file() and path.suffix in _READ and "__pycache__" not in path.parts]
    builder = shipped.contract().parent                 # what the builder is told of the platform, and how it builds
    files += [builder / "AGENTS.md", *builder.glob("skills/*/SKILL.md")]
    named = {f"{path.relative_to(server.parent)}: {found[0]}" for path in files
             for found in [_CLIS.search(path.read_text(errors="ignore"))] if found}
    assert not named, f"an agent CLI is named outside a package: {sorted(named)}"
