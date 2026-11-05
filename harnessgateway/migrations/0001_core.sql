-- HarnessGateway schema (SPEC §15). Every tenant row carries its organization, and row-level security confines a
-- transaction to the organization it declared (`hg.org`) unless it declared the platform scope (`hg.scope`), which
-- scheduling, metering and authentication use. The project-scoped tables (harnesses, sessions, turns) also carry their
-- project, and a transaction that declared one (`hg.project`) sees that project's rows only. FORCE applies the policies
-- to the table owner too: the application role must be neither a superuser nor BYPASSRLS.

-- ── tenancy and identity (SPEC §9) ──────────────────────────────────────────────────────────────────────────────────
CREATE TABLE orgs (
    id          text PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    capacity    jsonb NOT NULL DEFAULT '{}',
    egress      jsonb NOT NULL DEFAULT '[]',
    data_key    text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

-- A person: the email is the login; the password is an scrypt hash, absent until the invitation is accepted.
CREATE TABLE accounts (
    id             text PRIMARY KEY,
    email          text NOT NULL UNIQUE,
    name           text NOT NULL DEFAULT '',
    password_hash  text,
    status         text NOT NULL DEFAULT 'invited' CHECK (status IN ('invited', 'active', 'disabled')),
    platform_admin boolean NOT NULL DEFAULT false,
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

CREATE TABLE project_members (
    project_id  text NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    account_id  text NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, account_id)
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

-- A key carries a role: an organization key is admin; a project key is developer, and with run_only it only runs.
CREATE TABLE api_keys (
    id            text PRIMARY KEY,
    org_id        text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    project_id    text REFERENCES projects (id) ON DELETE CASCADE,
    account_id    text REFERENCES accounts (id) ON DELETE SET NULL,
    digest        text NOT NULL UNIQUE,
    role          text NOT NULL CHECK (role IN ('admin', 'developer')),
    run_only      boolean NOT NULL DEFAULT false,
    label         text NOT NULL DEFAULT '',
    hint          text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz,
    last_used_at  timestamptz,
    revoked_at    timestamptz,
    CHECK ((role = 'admin') = (project_id IS NULL))
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

-- ── the organization's providers and the projects' harnesses (SPEC §5, §7) ──────────────────────────────────────────
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

-- A harness is one package installed in a project under a name, with its settings and sealed secret values.
CREATE TABLE harnesses (
    id                text PRIMARY KEY,
    org_id            text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    project_id        text NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    name              text NOT NULL,
    digest            text NOT NULL REFERENCES packages (digest),
    manifest          jsonb NOT NULL,
    settings          jsonb NOT NULL DEFAULT '{}',
    secrets           jsonb NOT NULL DEFAULT '{}',
    default_provider  text NOT NULL DEFAULT '',
    default_model     text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
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

-- ── nodes (SPEC §10) ────────────────────────────────────────────────────────────────────────────────────────────────
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

-- ── sessions and turns (SPEC §4, §6) ────────────────────────────────────────────────────────────────────────────────
-- A session pins the package it started on, in its harness's project; it records who created it (its first turn's
-- actor) and the application's end user, and points at its last saved state, the node holding its warm box, the turn
-- running in it and the turn whose state is still being saved. A fork names the response it continued from.
CREATE TABLE sessions (
    id               text PRIMARY KEY,
    org_id           text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    project_id       text NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    created_by       text NOT NULL,
    end_user         text,
    harness          text NOT NULL,
    digest           text NOT NULL REFERENCES packages (digest),
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
    forked_from      text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_active_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_org ON sessions (org_id, created_at DESC);
CREATE INDEX sessions_project ON sessions (project_id, created_at DESC);
CREATE INDEX sessions_node ON sessions (node_id) WHERE node_id IS NOT NULL;

-- A turn is the response a caller reads and the job a node runs; it records its actor and the state saved after it.
CREATE TABLE turns (
    id              text PRIMARY KEY,
    org_id          text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    project_id      text NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
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
    snapshot_key    text,
    files_key       text,
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

CREATE TABLE idempotency (
    org_id          text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    key             text NOT NULL,
    request_digest  text NOT NULL,
    turn_id         text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, key)
);

-- ── objects, metering and observability (SPEC §11, §12) ─────────────────────────────────────────────────────────────
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

-- ── row-level security ──────────────────────────────────────────────────────────────────────────────────────────────
DO $$
DECLARE t text;
BEGIN
    -- the organization's tables: its rows, or every row on the platform scope
    FOREACH t IN ARRAY ARRAY['api_keys', 'providers', 'files', 'turn_events', 'live_events', 'turn_bindings',
                             'container_ops', 'usage', 'storage_accruals', 'traces', 'idempotency', 'projects',
                             'project_members', 'memberships', 'audit'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant ON %I USING (
            current_setting('hg.scope', true) = 'platform' OR org_id = current_setting('hg.org', true))
            WITH CHECK (current_setting('hg.scope', true) = 'platform' OR org_id = current_setting('hg.org', true))$p$, t);
    END LOOP;
    -- the project-scoped tables: the organization's rows, confined to one project when the transaction named one
    FOREACH t IN ARRAY ARRAY['harnesses', 'sessions', 'turns'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY tenant ON %I USING (
            current_setting('hg.scope', true) = 'platform' OR (org_id = current_setting('hg.org', true) AND
            (coalesce(current_setting('hg.project', true), '') = '' OR project_id = current_setting('hg.project', true))))
            WITH CHECK (current_setting('hg.scope', true) = 'platform' OR (org_id = current_setting('hg.org', true) AND
            (coalesce(current_setting('hg.project', true), '') = '' OR project_id = current_setting('hg.project', true))))$p$, t);
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
    FOREACH t IN ARRAY ARRAY['invitations', 'sign_ins', 'node_tokens', 'nodes', 'turn_tokens', 'objects', 'packages'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$CREATE POLICY platform ON %I USING (current_setting('hg.scope', true) = 'platform')
            WITH CHECK (current_setting('hg.scope', true) = 'platform')$p$, t);
    END LOOP;
    CREATE POLICY read ON packages FOR SELECT USING (true);      -- content-addressed manifests are not secret
END $$;
