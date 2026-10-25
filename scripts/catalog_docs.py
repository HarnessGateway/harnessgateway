"""Write one documentation page per catalog package, and the index, from the packages themselves.

    python scripts/catalog_docs.py          # docs/catalog/<name>.md and docs/catalog/index.md

Each page is built from the package's catalog.json (title, intro, notes, the verification record)
and harness.json (the CLI, the defaults, egress, what it accepts), so the documentation cannot
drift from the package. Run it after changing a package; CI builds the site strictly.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG, DOCS = ROOT / "catalog", ROOT / "docs" / "catalog"
CHECKS = {"make": "a first turn writes a file and answers", "recall": "a second turn, continued with previous_response_id, recalls it",
          "stream": "a streamed turn delivers events as they happen"}


def page(name: str, meta: dict, cfg: dict) -> str:
    v = meta.get("verified") or {}
    checks = v.get("checks") or {}
    d, cli = cfg["defaults"], cfg["cli"]
    lines = [f"# {meta['title']}", "", meta["intro"], ""]
    if meta.get("docs"):
        lines += [f"The CLI's documentation: <{meta['docs']}>.", ""]
    lines += ["## The package", "", "| | |", "|---|---|",
              f"| CLI | `{cli['name']}` {cli.get('version', '')} |",
              f"| Default provider | `{d.get('provider') or '(none: name one in the model path)'}` |",
              f"| Default model | `{d.get('model') or '(the provider decides)'}` |",
              f"| Provider shape | `{meta.get('api', '')}` |" if meta.get("api") else None,
              f"| Accepts | {', '.join(f'`{a}`' for a in cfg.get('accepts', []))} |",
              f"| Egress, install | {', '.join(f'`{e}`' for e in cfg['install']['egress']) or 'none'} |",
              f"| Egress, run | {', '.join(f'`{e}`' for e in cfg['run']['egress']) or 'none'} |", ""]
    lines = [x for x in lines if x is not None]
    if v:
        passed = [c for c in CHECKS if checks.get(c)]
        failed = [c for c in CHECKS if not checks.get(c)]
        lines += ["## Verified", "", f"On {v.get('on', '?')}, contract {v.get('contract', '?')}, provider `{v.get('provider', '?')}`, model `{v.get('model', '?')}`: "
                  f"**{len(passed)} of {len(CHECKS)}** checks passed. {v.get('how', '')}".rstrip(), "",
                  "| Check | Means | Result |", "|---|---|---|"]
        lines += [f"| {c} | {CHECKS[c]} | {'passes' if checks.get(c) else 'does not pass'} |" for c in CHECKS]
        lines.append("")
        if failed:
            lines += [f"Not passing: {', '.join(failed)}. See the notes.", ""]
    if meta.get("notes"):
        lines += ["## Notes", ""] + [f"- {n}" for n in meta["notes"]] + [""]
    lines += ["## Use it", "", "```bash",
              f"hg harness new {name} --catalog {name}        # fork it into your organization",
              f"hg ask {name} \"Create hello.txt containing hello, then say done.\"",
              f"hg ask {name}/myprovider/some-model \"…\"       # another provider and model", "```", "",
              "The fork's first commit is the catalog's package; pull it, change it and push it as your own from there.", ""]
    return "\n".join(lines)


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    rows = []
    for folder in sorted(p for p in CATALOG.iterdir() if (p / "catalog.json").is_file()):
        meta = json.loads((folder / "catalog.json").read_text())
        cfg = json.loads((folder / "harness.json").read_text())
        (DOCS / f"{folder.name}.md").write_text(page(folder.name, meta, cfg))
        checks = (meta.get("verified") or {}).get("checks") or {}
        marks = " ".join(f"{'✓' if checks.get(c) else '✗'} {c}" for c in CHECKS) if checks else "not verified"
        rows.append(f"| [{meta['title']}]({folder.name}.md) | `{cfg['cli']['name']}` {cfg['cli'].get('version', '')} | `{cfg['defaults'].get('provider') or '—'}` | {marks} |")
    index = ["# The catalog", "",
             "Twelve packages ship with the platform, one per CLI (and one for any CLI), each built from that CLI's own help and documentation "
             "(the research is in [the Responses wire](../responses-wire.md)). Fork one into your organization from the "
             "app or with `hg harness new NAME --catalog PACKAGE`; the fork is a git repository of your own whose first "
             "commit is the catalog's package.", "",
             "Each package was run against a live provider on the day it shipped: a first turn that writes a file, a second "
             "turn that recalls it through `previous_response_id`, and a streamed turn. The results are recorded in the "
             "package's `catalog.json` and shown on its card in the console. A check that does not pass is a limit of the CLI "
             "at that version, named on the package's page; the package still ships so you can start from it.", "",
             "| Package | CLI | Default provider | Checks |", "|---|---|---|---|", *rows, ""]
    (DOCS / "index.md").write_text("\n".join(index))
    print(f"wrote {len(rows)} pages")


if __name__ == "__main__":
    main()
