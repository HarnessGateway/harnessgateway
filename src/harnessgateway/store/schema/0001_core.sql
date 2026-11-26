-- The core's schema. Every row that belongs to an organization carries its id, and every transaction on such rows
-- runs with the setting hg.org_id. A community installation has one organization; nothing here assumes that.

CREATE TABLE orgs (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- API keys. Only a hash of a key is kept.
CREATE TABLE keys (
    id            text PRIMARY KEY,
    org_id        text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name          text NOT NULL,
    hash          text NOT NULL UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

-- Providers, environments and harnesses: one shape. `spec` is the definition; `sealed` its secret values, by name.
CREATE TABLE resources (
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('providers', 'environments', 'harnesses')),
    name        text NOT NULL,
    spec        jsonb NOT NULL,
    sealed      jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, kind, name)
);

-- A package archive that was added, by the digest of its files. Its bytes are in the blob store.
CREATE TABLE packages (
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    digest      text NOT NULL,
    bytes       bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, digest)
);

-- A package installed for an image: once, whichever environments name that image.
CREATE TABLE installs (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    digest      text NOT NULL,
    image_id    text NOT NULL,
    status      text NOT NULL CHECK (status IN ('pending', 'ready', 'failed')),
    log         text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, digest, image_id)
);

-- Uploaded files: what a request's input may name. Their bytes are in the blob store.
CREATE TABLE files (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    filename    text NOT NULL,
    purpose     text NOT NULL,
    bytes       bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- A session is a chain of turns on the same two folders.
CREATE TABLE sessions (
    id                   text PRIMARY KEY,
    org_id               text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    head_turn_id         text,
    forked_from_turn_id  text,
    folder_snapshot_id   text,                         -- what its folders hold, where a driver keeps them; NULL: unknown
    folder_used_at       timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    last_active_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_active ON sessions (org_id, last_active_at DESC);

-- A turn is one Responses request. The waiting ones are the queue; a runner takes one and keeps saying it is alive.
CREATE TABLE turns (
    id                 text PRIMARY KEY,
    org_id             text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    session_id         text NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    parent_id          text,                           -- may be a turn of another session: a fork
    number             integer NOT NULL,               -- its place in its chain, from 1
    status             text NOT NULL CHECK (status IN
                           ('queued', 'in_progress', 'completed', 'failed', 'cancelled', 'incomplete')),
    reason             text,
    code               text,
    message            text,
    incomplete_reason  text,
    environment        text NOT NULL,
    harness            text NOT NULL,
    provider           text,
    model              text,
    request            jsonb,                          -- as it was sent; NULL once the turn is deleted
    credentials        jsonb NOT NULL DEFAULT '{}',    -- what the request carried, sealed, by its placeholder: until it ends
    output             jsonb NOT NULL DEFAULT '[]',
    usage              jsonb,                          -- the tokens its harness reported
    used               jsonb,                          -- what its sandbox used; NULL when it never started
    snapshot_id        text,
    store              boolean NOT NULL DEFAULT true,
    cancel_requested   boolean NOT NULL DEFAULT false,
    principal          text,                           -- who sent it
    runner_id          text,
    heartbeat_at       timestamptz,
    log                text NOT NULL DEFAULT '',
    created_at         timestamptz NOT NULL DEFAULT now(),
    started_at         timestamptz,
    completed_at       timestamptz,
    deleted_at         timestamptz
);
CREATE INDEX turns_queue ON turns (created_at) WHERE status = 'queued';
CREATE INDEX turns_running ON turns (heartbeat_at) WHERE status = 'in_progress';
CREATE INDEX turns_session ON turns (org_id, session_id, number);

-- A turn's stream, in order, so that it can be read again from any point.
CREATE TABLE events (
    turn_id  text NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    seq      integer NOT NULL,
    data     jsonb NOT NULL,
    PRIMARY KEY (turn_id, seq)
);

-- A chunk of a file, kept once for an organization under its hash. It counts the snapshots that name it.
CREATE TABLE chunks (
    org_id           text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    hash             text NOT NULL,
    bytes            integer NOT NULL,
    refs             integer NOT NULL DEFAULT 0 CHECK (refs >= 0),
    unreferenced_at  timestamptz DEFAULT now(),        -- since when nothing names it; NULL while something does
    PRIMARY KEY (org_id, hash)
);

CREATE TABLE snapshots (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    session_id  text NOT NULL,
    turn_id     text NOT NULL,
    bytes       bigint NOT NULL,
    files       integer NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX snapshots_session ON snapshots (org_id, session_id);

-- What an organization used, a row a day: its lifetime is their sum, and any period a range of them. Processor
-- time and memory are its sandboxes', as their driver measured them; the bytes are those through the egress
-- proxy; storage is what it held in the blob store, times how long.
CREATE TABLE usage (
    org_id                text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    day                   date NOT NULL,
    cpu_seconds           double precision NOT NULL DEFAULT 0,
    memory_byte_seconds   double precision NOT NULL DEFAULT 0,
    network_in_bytes      bigint NOT NULL DEFAULT 0,
    network_out_bytes     bigint NOT NULL DEFAULT 0,
    storage_byte_seconds  double precision NOT NULL DEFAULT 0,
    PRIMARY KEY (org_id, day)
);

-- What one running sandbox (a turn's, or an install's) may reach, for as long as it runs. The policy is sealed:
-- it holds the secrets the egress proxy puts in. The row is deleted when the sandbox is over.
CREATE TABLE grants (
    subject_id  text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    token_hash  text NOT NULL,
    policy      text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- What the installation makes once and keeps sealed: its certificate authority, the key view links are signed with.
CREATE TABLE platform (
    name   text PRIMARY KEY,
    value  jsonb NOT NULL
);

-- Work one process does on a timer though several may run: when each kind was last done.
CREATE TABLE rounds (
    name  text PRIMARY KEY,
    at    timestamptz NOT NULL
);
