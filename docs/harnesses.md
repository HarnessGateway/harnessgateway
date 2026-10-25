# Harnesses

A harness is a package your organization keeps on the platform: a git repository with the
contract's files ([Write a package](packages.md)) and a history. A request's model names one.
Harnesses come from three places: the catalog, a fork of another harness, or an archive you
upload. Owners add and change them; every member runs them.

## From the catalog

"Add a harness" in the app, from the catalog; or:

```bash
hg harness new claude-code --catalog claude-code
hg harness new my-harness --catalog template
```

The fork's first commit is the catalog's package as shipped, authored by the catalog. The catalog
packages are built for the providers they name as defaults; add that [provider](providers.md) and
the package works unchanged.

## From the command line

```bash
hg harness pull my-harness ./pkg                 # the files at the head
hg harness run ./pkg --input "hello"             # run it here, without the platform
hg harness push my-harness ./pkg -m "tune the mapper"   # the next commit, from the directory
hg harness log my-harness                        # the history
```

`push` on a name that does not exist creates the harness from the directory; so does the app's
"Add a harness" with an archive. `hg harness new NAME --dir DIR` does the same explicitly. An
archive uploaded to `PUT /v1/harnesses/{name}` (gzip accepted) is the same operation over the API;
the `x-commit-message` header goes with it. A commit through `POST /v1/harnesses/{name}/commits`
is made against the head you read; two people committing at once get a conflict on the second
commit, not a lost change.

## Forks

`hg harness new NAME --fork OTHER` makes a new harness whose history starts from the source's head,
with `origin` recording where it came from.

## Who may edit

Owners add, change and delete harnesses; members run them. A session runs the commit the harness
was at when the session started, so a commit never changes a session in flight; the next session
starts on the new head.

## Which commit runs

Each session records the harness's head when it was created and every turn of that session runs
that commit. Workers cache each commit's package and its installed tools layer (`install.sh` runs
once per commit per node), so a commit costs one install per node and nothing after.
