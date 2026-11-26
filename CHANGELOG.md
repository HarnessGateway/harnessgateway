# Changelog

What changed in each version, for someone who runs the platform. A version that asks anything of them beyond the
two commands of an update is a new major version, and says what.

## 1.2.0

The core is for one person, and a key acts as them. What a caller makes is theirs, and every key is accountable.
Nothing is asked of whoever runs the platform: the two commands of an update, which add columns to `keys`,
`turns`, and `files`.

- **Every key is the owner's.** A key acts as whoever made it, and the core has one person, its owner. So the
  chats and the sessions someone has are theirs whichever key they sign in with: `hg-subject`, and a session's
  `owner`, are `owner`, where they were the key's id. A session an earlier version recorded keeps the owner it
  was given.
- **A key may be limited.** `POST /v1/keys` takes `expires_at`, a time from which the key is refused, and
  `scopes`, patterns of the actions it is limited to (`responses.*`, `*.read`). A key that is limited makes no key.
  A field of that request the platform does not know is now refused.
- **A key is accountable.** A key says who made it (`created_by`) and how the key itself ends (`hint`: its last
  four characters), so one that turns up in a log is told and revoked. A turn says which key sent it (`key`, with
  a session's turns).
- **An uploaded file is its uploader's**, as a session is whoever started it's. With the core alone every file is
  still the owner's; an edition with people now keeps a file private to whoever uploaded it. A file uploaded
  before this is the owner's.
- **A harness's package is taken by whoever may change harnesses**: `GET /v1/harnesses/{name}/package` asks for
  `harnesses.write`, where it asked for `harnesses.read`. With the core alone nothing changes.
- **`GET /v1/permissions`** answers what the caller may do, of every action this installation's routes name.
- **The console has an API keys page**, and its sidebar lists the pages the person signed in may use. Wherever it
  shows who (a session's owner, whose a key is, who made it), it shows a name, not an id.
- **For an edition:** a `Principal` carries the key a caller came with and its scopes; another's keys are asked
  about as `keys.read_all` and `keys.write_all`; `Keys.create` takes who the key acts as, and `Keys.revoke_of`
  revokes everything that acts as someone; the rule of a pattern (`permission`) and `permit`, `may`, `make_key`,
  and `OWNER` are in `harnessgateway.edition`. Another's uploaded files are asked about as `files.read_all` and
  `files.write_all`: a role's `files.*` now allows them, so a role that is to keep files private names `files.read`
  and `files.write`. `calling()` answers which request, and so which key, a change is being made in.
  `responses.created` and `turn.finished` carry the key. An extension's `edition.js` may export `names`: who is
  here, by id, for the console to show.

## 1.1.0

A session is its owner's, and each person's chats are their own. Nothing is asked of whoever runs the platform:
the two commands of an update, and no change to the schema.

- **A session names who started it** (`owner`). Its owner reaches it as before. Anyone else who reads or changes
  it is asked for as `sessions.read_all` or `sessions.write_all`, at the `authorize` seam. With API keys alone
  nothing changes: every key still reaches every session. An edition with roles now keeps a session private to
  whoever started it, unless their role allows everyone's.
- **`GET /v1/sessions` lists what the caller may read**: every session for a caller who may read all of them,
  their own otherwise.
- **Every answer to an authenticated caller names that caller** in the header `hg-subject`.
- **The console keeps each person's chats apart.** The chat's list is kept in the browser under who is signed in,
  so two people who use one browser no longer see each other's, and someone who may read every session sees only
  their own chats. The Sessions page shows who started each session. Chats a browser kept under an earlier version
  are no longer shown.

## 1.0.0

The first version whose installations are updated in place, and the base of every edition
([SPEC.md](SPEC.md) is the specification). It is installed fresh: nothing upgrades an earlier build. What the
platform does for a caller is as it was (the Responses API, the three definitions, the package contract 3, the
sandbox, sessions, handover, the console's pages). What is new is how it is installed and updated, and what an
edition is given.

- **The image is the whole distribution.** A host needs Docker and nothing from the repository: `harnessgateway
  compose` prints the Compose file a version is run with, `harnessgateway secrets` a new installation's `.env`, and
  `harnessgateway init` gives the defaults and the first API key. There is no installer script, and nothing is
  built on the host.
- **Updated in place.** The same two commands with a newer image: the schema is brought up to date before anything
  is served, and a version started on a database a later one changed refuses to start, and says so.
- **Editions are a chain.** The extensions an image names are asked in order at every seam, and what one adds never
  takes the place of another's or of the core's: a driver, a blob store, a route, or a page offered twice is refused
  at start; each extension's schema files are recorded under its own name; each one's console pages and sign-ins are
  added under `/console/x/<name>/`.
- **A change and its record commit together.** An extension is told of every change inside the transaction that
  makes it (`on_change`), with who made it: an audit log built on it has no change without a row, and no row
  without a change.
- **An extension is given more.** `harnessgateway.edition` is everything of the core it may import. It is handed
  the platform when it starts, has a turn in every housekeeping round, may narrow what a sandbox reaches, and hears
  of each connection a sandbox made or was refused.
- **What a turn's sandbox used is kept on the turn** (`used`: how long it ran, what it was allowed, what was
  measured), shown with a session's turns and told with `turn.finished`.
- **`GET /v1/platform` lists the extensions loaded.** The core's image loads none.
- **View links are signed with a key of the installation's own**, made once and kept sealed, so a key service can
  stand in for `HG_SECRET_KEY`.
