-- HarnessGateway schema (SPEC §7.1). Every tenant row carries its organization, and row-level security confines a
-- transaction to the organization it declared (`hg.org`) unless it declared the platform scope (`hg.scope`), which
-- boxes, doors, metering and authentication use. FORCE applies the policies to the table owner too: the application
-- role must be neither a superuser nor BYPASSRLS.

-- ── tenancy and identity (SPEC §7.4, §10) ───────────────────────────────────────────────────────────────────────────
CREATE TABLE orgs (
    id          text PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    limits      jsonb NOT NULL DEFAULT '{}',
    egress      jsonb NOT NULL DEFAULT '[]',
    data_key    text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- A person: the email is the login; the password is an scrypt hash, absent until the invitation is accepted or SSO
-- provisioned the account.
CREATE TABLE accounts (
    id             text PRIMARY KEY,
    email          text NOT NULL UNIQUE,
    name           text NOT NULL DEFAULT '',
    password_hash  text,
    status         text NOT NULL DEFAULT 'invited' CHECK (status IN ('invited', 'active', 'disabled')),
    platform_admin boolean NOT NULL DEFAULT false,
    sso_subject    text,
    failures       integer NOT NULL DEFAULT 0,
    locked_until   timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    signed_in_at   timestamptz
);

CREATE TABLE memberships (
    account_id  text NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    role        text NOT NULL CHECK (role IN ('admin', 'developer')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, org_id)
);

-- One-time tokens, kept as digests: an invited account's activation, a password reset.
CREATE TABLE invitations (
    digest      text PRIMARY KEY,
    account_id  text NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sign_ins (
    id            text PRIMARY KEY,
    account_id    text NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    digest        text NOT NULL UNIQUE,
    client        text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

-- One identity provider per organization (SPEC §10.3); the client secret is sealed.
CREATE TABLE identity_providers (
    org_id          text PRIMARY KEY REFERENCES orgs (id) ON DELETE CASCADE,
    name            text NOT NULL DEFAULT '',
    issuer          text NOT NULL,
    client_id       text NOT NULL,
    sealed_secret   text NOT NULL,
    domains         jsonb NOT NULL DEFAULT '[]',
    auto_provision  boolean NOT NULL DEFAULT true,
    default_role    text NOT NULL DEFAULT 'developer' CHECK (default_role IN ('admin', 'developer')),
    role_claim      text NOT NULL DEFAULT '',
    admin_values    jsonb NOT NULL DEFAULT '[]',
    required        boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- A key is what an application is built around: named after it, with a role, and run-only when it only runs.
CREATE TABLE api_keys (
    id            text PRIMARY KEY,
    org_id        text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    account_id    text REFERENCES accounts (id) ON DELETE SET NULL,
    digest        text NOT NULL UNIQUE,
    role          text NOT NULL CHECK (role IN ('admin', 'developer')),
    run_only      boolean NOT NULL DEFAULT false CHECK (NOT run_only OR role = 'developer'),
    label         text NOT NULL CHECK (label <> ''),
    hint          text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz,
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

-- Every change of standing: who did what to what, in which organization (none: the platform's own).
CREATE TABLE audit (
    id      text PRIMARY KEY,
    org_id  text REFERENCES orgs (id) ON DELETE CASCADE,
    at      timestamptz NOT NULL DEFAULT now(),
    actor   text NOT NULL,
    action  text NOT NULL,
    target  text NOT NULL DEFAULT '',
    detail  jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_org_at ON audit (org_id, at DESC);

-- ── providers, packages, harnesses, installs (SPEC §5, §9.4, §9.6) ─────────────────────────────────────────────────
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

-- A harness is one package installed under a name, with its settings and sealed secret values.
CREATE TABLE harnesses (
    id                text PRIMARY KEY,
    org_id            text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name              text NOT NULL,
    digest            text NOT NULL REFERENCES packages (digest),
    manifest          jsonb NOT NULL,
    settings          jsonb NOT NULL DEFAULT '{}',
    secrets           jsonb NOT NULL DEFAULT '{}',
    default_provider  text NOT NULL DEFAULT '',
    default_model     text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

-- The organization's own run of a package's `install`, once per package digest and sandbox image: its archive.
CREATE TABLE installs (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    digest      text NOT NULL REFERENCES packages (digest),
    image       text NOT NULL,
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'failed')),
    object_key  text,
    bytes       bigint NOT NULL DEFAULT 0,
    log_tail    text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, digest, image)
);

CREATE TABLE files (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    created_by  text NOT NULL,
    filename    text NOT NULL,
    purpose     text NOT NULL,
    bytes       bigint NOT NULL,
    object_key  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);

-- ── sessions, turns, boxes, snapshots (SPEC §9) ─────────────────────────────────────────────────────────────────────
-- A session pins the package it started on; it records who created it, the application's end user, and its current
-- snapshot. A fork names the response it continued from.
CREATE TABLE sessions (
    id               text PRIMARY KEY,
    org_id           text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    created_by       text NOT NULL,
    end_user         text,
    harness          text NOT NULL,
    digest           text NOT NULL REFERENCES packages (digest),
    manifest         jsonb NOT NULL,
    provider_id      text,
    model            text NOT NULL DEFAULT '',
    store            boolean NOT NULL,
    status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    turns            integer NOT NULL DEFAULT 0,
    snapshot_id      text,
    snapshot_seq     integer NOT NULL DEFAULT 0,
    forked_from      text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_active_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_org ON sessions (org_id, created_at DESC);

-- A turn is the response a caller reads and the job a box runs; it records its actor and the state saved after it.
CREATE TABLE turns (
    id              text PRIMARY KEY,
    org_id          text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    session_id      text NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq             integer NOT NULL,
    status          text NOT NULL CHECK (status IN ('queued', 'in_progress', 'completed', 'incomplete', 'failed',
                                                    'cancelled')),
    actor           text NOT NULL,
    end_user        text,
    model           text NOT NULL,
    provider_id     text,
    provider_model  text NOT NULL DEFAULT '',
    stream          boolean NOT NULL,
    background      boolean NOT NULL,
    request_key     text NOT NULL,
    mcp             jsonb NOT NULL DEFAULT '[]',
    previous_id     text,
    box_id          text,
    cancel          text,
    hg_reason       text,
    hg_message      text,
    exit_code       integer,
    response        jsonb,
    events          integer NOT NULL DEFAULT 0,
    last_batch      integer NOT NULL DEFAULT -1,
    log_tail        text NOT NULL DEFAULT '',
    stderr_tail     text NOT NULL DEFAULT '',
    snapshot_id     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    ended_at        timestamptz,
    exited_at       timestamptz,
    UNIQUE (session_id, seq)
);
CREATE INDEX turns_queued ON turns (session_id, seq) WHERE status = 'queued';
CREATE INDEX turns_running ON turns (org_id) WHERE status = 'in_progress';

-- The durable event log: every non-delta event. Deltas live in the Redis stream while a turn is live.
CREATE TABLE turn_events (
    turn_id  text NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
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

-- A box: one sandbox container, for one session (consecutive turns) or one install. One live box per session.
CREATE TABLE boxes (
    id              text PRIMARY KEY,
    org_id          text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    kind            text NOT NULL CHECK (kind IN ('session', 'install')),
    session_id      text REFERENCES sessions (id) ON DELETE CASCADE,
    install_id      text REFERENCES installs (id) ON DELETE CASCADE,
    token_digest    text UNIQUE,
    status          text NOT NULL DEFAULT 'starting' CHECK (status IN ('starting', 'standby', 'running', 'gone')),
    turn_id         text,
    policy_version  integer NOT NULL DEFAULT 0,
    launcher_ref    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    launched_at     timestamptz,
    ready_at        timestamptz,
    idle_since      timestamptz,
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL DEFAULT now() + interval '10 minutes',
    gone_at         timestamptz,
    gone_reason     text NOT NULL DEFAULT '',
    CHECK ((kind = 'session') = (session_id IS NOT NULL)),
    CHECK ((kind = 'install') = (install_id IS NOT NULL))
);
CREATE UNIQUE INDEX boxes_live_session ON boxes (session_id) WHERE status <> 'gone' AND session_id IS NOT NULL;
CREATE UNIQUE INDEX boxes_live_install ON boxes (install_id) WHERE status <> 'gone' AND install_id IS NOT NULL;
CREATE INDEX boxes_live ON boxes (org_id) WHERE status <> 'gone';
CREATE INDEX boxes_expiry ON boxes (expires_at) WHERE status <> 'gone';

-- A snapshot: the manifest of a session's state after a turn (SPEC §9.5). Every one is a checkpoint.
CREATE TABLE snapshots (
    id            text PRIMARY KEY,
    org_id        text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    session_id    text NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    turn_id       text,
    manifest_key  text NOT NULL,
    bytes         bigint NOT NULL,
    files         integer NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX snapshots_session ON snapshots (session_id, created_at);

-- Content, once per organization, with how many snapshots name it.
CREATE TABLE blobs (
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    hash        text NOT NULL,
    bytes       bigint NOT NULL,
    refs        integer NOT NULL DEFAULT 0 CHECK (refs >= 0),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, hash)
);
CREATE INDEX blobs_unreferenced ON blobs (updated_at) WHERE refs = 0;

-- Files staged through the Containers API, applied to the workspace before the session's next turn.
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

CREATE TABLE idempotency (
    org_id          text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    key             text NOT NULL,
    request_digest  text NOT NULL,
    turn_id         text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, key)
);

-- ── objects, metering and observability (SPEC §7.3, §12) ───────────────────────────────────────────────────────────
-- Every stored non-blob object and its size: what storage metering sums beside the blobs.
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
    kind         text NOT NULL CHECK (kind IN ('harness', 'door', 'provider')),
    object_key   text NOT NULL,
    at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX traces_response ON traces (response_id, at);

-- ── row-level security ──────────────────────────────────────────────────────────────────────────────────────────────
DO $$
DECLARE t text;
BEGIN
    -- the organization's tables: its rows, or every row on the platform scope
    FOREACH t IN ARRAY ARRAY['api_keys', 'providers', 'harnesses', 'installs', 'files', 'sessions', 'turns', 'turn_events',
                             'turn_bindings', 'boxes', 'snapshots', 'blobs', 'container_ops', 'usage', 'storage_accruals',
                             'traces', 'idempotency', 'memberships', 'audit', 'identity_providers'] LOOP
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
    -- accounts: an organization reads its members; only the platform scope (the identity service) writes
    ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
    ALTER TABLE accounts FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant ON accounts USING (
        current_setting('hg.scope', true) = 'platform' OR EXISTS (
            SELECT 1 FROM memberships m WHERE m.account_id = accounts.id AND m.org_id = current_setting('hg.org', true)))
        WITH CHECK (current_setting('hg.scope', true) = 'platform');
    -- the platform's own tables
    FOREACH t IN ARRAY ARRAY['invitations', 'sign_ins', 'objects', 'packages'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY platform ON %I USING (current_setting('hg.scope', true) = 'platform')
            WITH CHECK (current_setting('hg.scope', true) = 'platform')$p$, t);
    END LOOP;
    CREATE POLICY read ON packages FOR SELECT USING (true);      -- content-addressed manifests are not secret
END $$;
