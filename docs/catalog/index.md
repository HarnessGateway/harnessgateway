# The catalog

Eleven packages ship with the platform, one per CLI, each built from that CLI's own help and documentation (the research is in [the Responses wire](../responses-wire.md)). Fork one into your organization from the console or with `hg harness new NAME --catalog PACKAGE`; the fork is a git repository of your own whose first commit is the catalog's package.

Each package was run against a live provider on the day it shipped: a first turn that writes a file, a second turn that recalls it through `previous_response_id`, and a streamed turn. The results are recorded in the package's `catalog.json` and shown on its card in the console. A check that does not pass is a limit of the CLI at that version, named on the package's page; the package still ships so you can start from it.

| Package | CLI | Default provider | Checks |
|---|---|---|---|
| [Claude Code](claude-code.md) | `claude` 2.1.310 | `deepseek` | ✓ make ✓ recall ✓ stream |
| [Cline](cline.md) | `cline` 3.0.73 | `deepseek-openai` | ✓ make ✗ recall ✓ stream |
| [Codex](codex.md) | `codex` 0.163.0 | `openai` | ✓ make ✓ recall ✓ stream |
| [Command Code](command-code.md) | `command-code` 1.88.0 | `deepseek` | ✗ make ✗ recall ✗ stream |
| [DeepSeek Harness](dsh.md) | `dsh` 0.2.0-rc.2 | `deepseek` | ✓ make ✓ recall ✓ stream |
| [Hermes Agent](hermes.md) | `hermes` 0.19.0 | `deepseek` | ✓ make ✓ recall ✗ stream |
| [Kilo Code](kilo.md) | `kilo` 7.8.12 | `deepseek` | ✓ make ✓ recall ✓ stream |
| [Oh My Pi](omp.md) | `omp` 18.12.0 | `deepseek` | ✓ make ✓ recall ✓ stream |
| [OpenClaw](openclaw.md) | `openclaw` 2026.10.6 | `deepseek` | ✓ make ✗ recall ✗ stream |
| [Pi](pi.md) | `pi` 1.3.0 | `deepseek` | ✓ make ✓ recall ✓ stream |
| [Template](template.md) | `echo-cli` 1.0.0 | `—` | ✓ make ✓ recall ✓ stream |
