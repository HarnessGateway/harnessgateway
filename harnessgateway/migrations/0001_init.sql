-- HarnessGateway schema, version 1.
--
-- Tenant isolation is enforced twice: by the services (every query names the organization) and here, by row-level
-- security. A transaction states its scope with set_config: `hg.org` for one organization, or `hg.scope = platform`
-- for the platform's own work (authentication, scheduling, metering). FORCE makes the policies apply to the table
-- owner too; the application role must not be a superuser or have BYPASSRLS (the platform refuses to start if it is).

CREATE TABLE orgs (
    id          text PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    limits      jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE members (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    email       text NOT NULL,
    role        text NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, email)
);

CREATE TABLE api_keys (
    id            text PRIMARY KEY,
    org_id        text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    member_id     text NOT NULL REFERENCES members (id) ON DELETE CASCADE,
    name          text NOT NULL,
    role          text NOT NULL CHECK (role IN ('admin', 'member')),
    digest        text NOT NULL UNIQUE,
    hint          text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    expires_at    timestamptz,
    revoked_at    timestamptz
);

CREATE TABLE invites (
    id          text PRIMARY KEY,
    digest      text NOT NULL UNIQUE,
    label       text NOT NULL DEFAULT '',
    uses_left   integer NOT NULL CHECK (uses_left >= 0),
    limits      jsonb NOT NULL DEFAULT '{}',
    expires_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE providers (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name        text NOT NULL,
    api         text NOT NULL CHECK (api IN ('anthropic', 'openai')),
    base_url    text NOT NULL,
    sealed_key  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

CREATE TABLE package_blobs (
    digest      text PRIMARY KEY,
    name        text NOT NULL,
    version     text NOT NULL,
    manifest    jsonb NOT NULL,
    object_key  text NOT NULL,
    bytes       bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE installs (
    id                text PRIMARY KEY,
    org_id            text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name              text NOT NULL,
    digest            text NOT NULL REFERENCES package_blobs (digest),
    source            jsonb NOT NULL,
    settings          jsonb NOT NULL DEFAULT '{}',
    default_provider  text NOT NULL DEFAULT '',
    default_model     text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

CREATE TABLE node_join_tokens (
    id          text PRIMARY KEY,
    digest      text NOT NULL UNIQUE,
    org_id      text REFERENCES orgs (id) ON DELETE CASCADE,
    label       text NOT NULL DEFAULT '',
    uses_left   integer NOT NULL CHECK (uses_left >= 0),
    expires_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE nodes (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    org_id        text REFERENCES orgs (id) ON DELETE CASCADE,
    digest        text NOT NULL UNIQUE,
    slots         integer NOT NULL DEFAULT 0,
    version       text NOT NULL DEFAULT '',
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'draining', 'removed')),
    last_seen_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    id               text PRIMARY KEY,
    org_id           text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    install_name     text NOT NULL,
    digest           text NOT NULL REFERENCES package_blobs (digest),
    provider_id      text,
    store            boolean NOT NULL,
    status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'deleted')),
    node_id          text,
    running_turn_id  text,
    turns            integer NOT NULL DEFAULT 0,
    snapshot_key     text,
    snapshot_bytes   bigint NOT NULL DEFAULT 0,
    manifest_key     text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_active_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_org ON sessions (org_id, created_at DESC);
CREATE INDEX sessions_node ON sessions (node_id) WHERE node_id IS NOT NULL;

CREATE TABLE turns (
    id           text PRIMARY KEY,
    org_id       text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    session_id   text NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq          integer NOT NULL,
    status       text NOT NULL CHECK (status IN ('queued', 'in_progress', 'completed', 'incomplete', 'failed', 'cancelled')),
    model        text NOT NULL,
    provider_model text NOT NULL DEFAULT '',
    stream       boolean NOT NULL,
    background   boolean NOT NULL,
    request_key  text NOT NULL,
    previous_id  text,
    node_id      text,
    cancel       text,
    hg_reason    text,
    exit_code    integer,
    hg_message   text,
    response     jsonb,
    terminal     jsonb,
    events       integer NOT NULL DEFAULT 0,
    last_batch   integer NOT NULL DEFAULT -1,
    log_tail     text NOT NULL DEFAULT '',
    stderr_tail  text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    ended_at     timestamptz,
    UNIQUE (session_id, seq)
);
CREATE INDEX turns_queued ON turns (created_at) WHERE status = 'queued';
CREATE INDEX turns_running ON turns (node_id) WHERE status = 'in_progress';
CREATE INDEX turns_org_created ON turns (org_id, created_at);

CREATE TABLE turn_events (
    turn_id  text NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    seq      integer NOT NULL,
    org_id   text NOT NULL,
    data     jsonb NOT NULL,
    PRIMARY KEY (turn_id, seq)
);

CREATE TABLE turn_tokens (
    digest       text PRIMARY KEY,
    turn_id      text NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    org_id       text NOT NULL,
    session_id   text NOT NULL,
    provider_id  text NOT NULL,
    capture      boolean NOT NULL DEFAULT false,
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz
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

CREATE TABLE objects (
    key         text PRIMARY KEY,
    org_id      text,
    kind        text NOT NULL,
    bytes       bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE INDEX objects_org_live ON objects (org_id) WHERE deleted_at IS NULL;

CREATE TABLE usage_events (
    id          bigserial PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    meter       text NOT NULL,
    quantity    numeric NOT NULL CHECK (quantity >= 0),
    session_id  text,
    turn_id     text,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX usage_org_at ON usage_events (org_id, at);

CREATE TABLE storage_accruals (
    org_id         text PRIMARY KEY REFERENCES orgs (id) ON DELETE CASCADE,
    accrued_until  timestamptz NOT NULL
);

CREATE TABLE idempotency (
    org_id          text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    key             text NOT NULL,
    request_digest  text NOT NULL,
    turn_id         text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, key)
);

CREATE TABLE audit_log (
    id      bigserial PRIMARY KEY,
    org_id  text,
    actor   text NOT NULL,
    action  text NOT NULL,
    target  text NOT NULL DEFAULT '',
    detail  jsonb NOT NULL DEFAULT '{}',
    at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_org_at ON audit_log (org_id, at);

-- Row-level security: one policy per tenant table.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['members', 'api_keys', 'providers', 'installs', 'sessions', 'turns', 'turn_events',
                             'files', 'container_ops', 'usage_events', 'storage_accruals', 'idempotency', 'audit_log'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant ON %I USING (
            current_setting('hg.scope', true) = 'platform' OR org_id = current_setting('hg.org', true))
            WITH CHECK (current_setting('hg.scope', true) = 'platform' OR org_id = current_setting('hg.org', true))$p$, t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['orgs'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant ON %I USING (
            current_setting('hg.scope', true) = 'platform' OR id = current_setting('hg.org', true))
            WITH CHECK (current_setting('hg.scope', true) = 'platform' OR id = current_setting('hg.org', true))$p$, t);
    END LOOP;
    -- platform-only tables: no organization may read them; only platform-scoped transactions.
    FOREACH t IN ARRAY ARRAY['invites', 'node_join_tokens', 'nodes', 'turn_tokens', 'objects', 'package_blobs'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY platform ON %I USING (current_setting('hg.scope', true) = 'platform')
            WITH CHECK (current_setting('hg.scope', true) = 'platform')$p$, t);
    END LOOP;
    -- package blobs are content-addressed and shared: anyone may read a manifest, only the platform writes.
    CREATE POLICY read ON package_blobs FOR SELECT USING (true);
END $$;
