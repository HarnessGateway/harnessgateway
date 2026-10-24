# Harnesses

A harness is a package your organization keeps on the platform: a git repository with the
contract's files ([Write a package](packages.md)) and a history. A request's model names one.
Harnesses come from three places: the catalog, a fork of another harness, or an archive you
upload.

## From the catalog

Catalog in the console, Fork on a card; or:

```bash
hg harness new claude-code --catalog claude-code
hg harness new my-harness --catalog template
```

The fork's first commit is the catalog's package as shipped, authored by the catalog. The catalog
packages are built for the providers they name as defaults; add that [provider](providers.md) and
the package works unchanged.

## The editor

Harnesses, then the harness: the file tree, an editor, a commit message and Commit. A commit is
made of the changed files against the head you opened; two people editing at once get a conflict
on the second commit, not a lost change. New file adds a path; "remove file" commits a deletion.
History lists every commit with its author. Export downloads the package as a tar archive.

<figure class="shot" markdown>
![The package editor](assets/console/editor.png)
</figure>

## From the command line

```bash
hg harness pull my-harness ./pkg                 # the files at the head
hg harness run ./pkg --input "hello"             # run it here, without the platform
hg harness push my-harness ./pkg -m "tune the mapper"   # the next commit, from the directory
hg harness log my-harness                        # the history
```

`push` on a name that does not exist creates the harness from the directory (`--team` puts it in
a team). `hg harness new NAME --dir DIR` does the same explicitly. An archive uploaded to
`PUT /v1/harnesses/{name}` with `content-type: application/x-tar` (gzip accepted) is the same
operation over the API; `?team=` and the `x-commit-message` header go with it.

## Forks

Fork on a harness page makes a new harness whose history starts from the source's head, with
`origin` recording where it came from. Fork a teammate's harness into your team to change it
without touching theirs.

## Who may edit

A harness belongs to the whole organization or to one team (the `team` field; the fork dialog
and `--team` set it, `PATCH /v1/harnesses/{name}` moves it). Owners edit everything. Members edit
the harnesses of their teams and use those plus the organization's. A session runs the commit
the harness was at when the session started, so a commit never changes a conversation in flight;
the next conversation starts on the new head.

## Which commit runs

Each session records the harness's head when it was created and every turn of that session runs
that commit. Workers cache each commit's package and its installed tools layer (`install.sh` runs
once per commit per node), so a commit costs one install per node and nothing after.
