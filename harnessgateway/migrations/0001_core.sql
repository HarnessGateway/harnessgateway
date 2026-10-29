-- HarnessGateway schema (SPEC §15). Every tenant row carries its organization, and row-level security confines a
-- transaction to the organization it declared (`hg.org`) unless it declared the platform scope (`hg.scope`), which
-- scheduling, metering and authentication use. FORCE applies the policies to the table owner too: the application
-- role must be neither a superuser nor BYPASSRLS.

CREATE TABLE orgs (
    id          text PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    capacity    jsonb NOT NULL DEFAULT '{}',
    egress      jsonb NOT NULL DEFAULT '[]',
    data_key    text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_keys (
    id            text PRIMARY KEY,
    org_id        text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    digest        text NOT NULL UNIQUE,
    scope         text NOT NULL CHECK (scope IN ('admin', 'run')),
    label         text NOT NULL DEFAULT '',
    hint          text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

CREATE TABLE providers (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name        text NOT NULL,
    api         text NOT NULL CHECK (api IN ('anthropic', 'openai', 'google')),
    base_url    text NOT NULL,
    sealed_key  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

-- Package archives, content-addressed and shared: the same files are stored once, whoever installs them.
CREATE TABLE packages (
    digest      text PRIMARY KEY,
    name        text NOT NULL,
    version     text NOT NULL,
    manifest    jsonb NOT NULL,
    object_key  text NOT NULL,
    bytes       bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE harnesses (
    id                text PRIMARY KEY,
    org_id            text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name              text NOT NULL,
    base_digest       text NOT NULL REFERENCES packages (digest),
    overlay_digest    text REFERENCES packages (digest),
    extends           text,
    manifest          jsonb NOT NULL,
    source            jsonb NOT NULL,
    settings          jsonb NOT NULL DEFAULT '{}',
    secrets           jsonb NOT NULL DEFAULT '{}',
    default_provider  text NOT NULL DEFAULT '',
    default_model     text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

CREATE TABLE files (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    filename    text NOT NULL,
    purpose     text NOT NULL,
    bytes       bigint NOT NULL,
    object_key  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);

CREATE TABLE node_tokens (
    id          text PRIMARY KEY,
    digest      text NOT NULL UNIQUE,
    pool        text NOT NULL DEFAULT '',
    label       text NOT NULL DEFAULT '',
    uses_left   integer NOT NULL CHECK (uses_left >= 0),
    expires_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE nodes (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    pool          text NOT NULL DEFAULT '',
    digest        text NOT NULL UNIQUE,
    slots         integer NOT NULL DEFAULT 0,
    version       text NOT NULL DEFAULT '',
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'draining')),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- A session pins the composed package it started on; it points at its last saved state, the node holding its warm
-- box, the turn running in it and the turn whose state is still being saved.
CREATE TABLE sessions (
    id               text PRIMARY KEY,
    org_id           text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    harness          text NOT NULL,
    base_digest      text NOT NULL REFERENCES packages (digest),
    overlay_digest   text REFERENCES packages (digest),
    manifest         jsonb NOT NULL,
    provider_id      text,
    model            text NOT NULL DEFAULT '',
    store            boolean NOT NULL,
    status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    node_id          text,
    running_turn_id  text,
    saving_turn_id   text,
    turns            integer NOT NULL DEFAULT 0,
    snapshot_seq     integer NOT NULL DEFAULT 0,
    snapshot_key     text,
    files_key        text,
    snapshot_bytes   bigint NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_active_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_org ON sessions (org_id, created_at DESC);
CREATE INDEX sessions_node ON sessions (node_id) WHERE node_id IS NOT NULL;

-- A turn is the response a caller reads and the job a node runs.
CREATE TABLE turns (
    id              text PRIMARY KEY,
    org_id          text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    session_id      text NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq             integer NOT NULL,
    status          text NOT NULL CHECK (status IN ('queued', 'in_progress', 'completed', 'incomplete', 'failed',
                                                    'cancelled')),
    model           text NOT NULL,
    provider_id     text,
    provider_model  text NOT NULL DEFAULT '',
    stream          boolean NOT NULL,
    background      boolean NOT NULL,
    request_key     text NOT NULL,
    mcp             jsonb NOT NULL DEFAULT '[]',
    previous_id     text,
    node_id         text,
    cancel          text,
    hg_reason       text,
    hg_message      text,
    exit_code       integer,
    response        jsonb,
    events          integer NOT NULL DEFAULT 0,
    last_batch      integer NOT NULL DEFAULT -1,
    log_tail        text NOT NULL DEFAULT '',
    stderr_tail     text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    ended_at        timestamptz,
    exited_at       timestamptz,
    UNIQUE (session_id, seq)
);
CREATE INDEX turns_queued ON turns (created_at) WHERE status = 'queued';
CREATE INDEX turns_running ON turns (org_id) WHERE status = 'in_progress';

CREATE TABLE turn_events (
    turn_id  text NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    seq      integer NOT NULL,
    org_id   text NOT NULL,
    data     jsonb NOT NULL,
    PRIMARY KEY (turn_id, seq)
);

-- Deltas: delivered while a turn runs, purged soon after (the items they build carry the whole content).
CREATE UNLOGGED TABLE live_events (
    turn_id  text NOT NULL,
    seq      integer NOT NULL,
    org_id   text NOT NULL,
    data     jsonb NOT NULL,
    PRIMARY KEY (turn_id, seq)
);

-- A caller's own MCP credentials, sealed, for one turn only (SPEC §8.3).
CREATE TABLE turn_bindings (
    turn_id  text PRIMARY KEY REFERENCES turns (id) ON DELETE CASCADE,
    org_id   text NOT NULL,
    sealed   text NOT NULL
);

CREATE TABLE turn_tokens (
    digest       text PRIMARY KEY,
    door_digest  text NOT NULL,
    turn_id      text NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    org_id       text NOT NULL,
    provider_id  text NOT NULL,
    node_id      text NOT NULL,
    capture      text NOT NULL,
    expires_at   timestamptz,
    revoked_at   timestamptz
);

CREATE TABLE container_ops (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    session_id  text NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    op          text NOT NULL CHECK (op IN ('put', 'delete')),
    path        text NOT NULL,
    object_key  text,
    bytes       bigint NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    applied_at  timestamptz
);
CREATE INDEX container_ops_pending ON container_ops (session_id, created_at) WHERE applied_at IS NULL;

-- Every stored object and its size: what storage metering sums.
CREATE TABLE objects (
    key         text PRIMARY KEY,
    org_id      text,
    kind        text NOT NULL,
    bytes       bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE INDEX objects_org_live ON objects (org_id) WHERE deleted_at IS NULL;

CREATE TABLE usage (
    id           text PRIMARY KEY,
    org_id       text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    meter        text NOT NULL,
    quantity     double precision NOT NULL CHECK (quantity >= 0),
    kind         text NOT NULL,
    harness      text,
    session_id   text,
    response_id  text,
    start_at     timestamptz NOT NULL,
    end_at       timestamptz NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX usage_org_end ON usage (org_id, end_at);

CREATE TABLE storage_accruals (
    org_id         text PRIMARY KEY REFERENCES orgs (id) ON DELETE CASCADE,
    accrued_until  timestamptz NOT NULL
);

CREATE TABLE traces (
    id           text PRIMARY KEY,
    org_id       text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    response_id  text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('harness', 'gateway', 'door')),
    object_key   text NOT NULL,
    at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX traces_response ON traces (response_id, at);

CREATE TABLE idempotency (
    org_id          text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    key             text NOT NULL,
    request_digest  text NOT NULL,
    turn_id         text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, key)
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['api_keys', 'providers', 'harnesses', 'files', 'sessions', 'turns', 'turn_events',
                             'live_events', 'turn_bindings', 'container_ops', 'usage', 'storage_accruals', 'traces',
                             'idempotency'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant ON %I USING (
            current_setting('hg.scope', true) = 'platform' OR org_id = current_setting('hg.org', true))
            WITH CHECK (current_setting('hg.scope', true) = 'platform' OR org_id = current_setting('hg.org', true))$p$, t);
    END LOOP;
    ALTER TABLE orgs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE orgs FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant ON orgs USING (
        current_setting('hg.scope', true) = 'platform' OR id = current_setting('hg.org', true))
        WITH CHECK (current_setting('hg.scope', true) = 'platform' OR id = current_setting('hg.org', true));
    FOREACH t IN ARRAY ARRAY['node_tokens', 'nodes', 'turn_tokens', 'objects', 'packages'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY platform ON %I USING (current_setting('hg.scope', true) = 'platform')
            WITH CHECK (current_setting('hg.scope', true) = 'platform')$p$, t);
    END LOOP;
    CREATE POLICY read ON packages FOR SELECT USING (true);      -- content-addressed manifests are not secret
END $$;
