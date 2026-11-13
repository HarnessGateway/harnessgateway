-- The schema of HarnessGateway 4 (spec §17).
-- Every table that belongs to an organization has org_id, and every transaction on such tables sets hg.org_id.

CREATE TABLE orgs (
    id              text PRIMARY KEY,
    name            text NOT NULL,
    retention_days  integer NOT NULL DEFAULT 0 CHECK (retention_days >= 0),
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Stored chunks of file content. refs counts the snapshots that name the chunk (spec §12.8).
CREATE TABLE blobs (
    org_id           text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    hash             text NOT NULL,
    bytes            integer NOT NULL CHECK (bytes > 0),
    refs             integer NOT NULL DEFAULT 0 CHECK (refs >= 0),
    unreferenced_at  timestamptz DEFAULT now(),
    PRIMARY KEY (org_id, hash),
    CHECK ((refs = 0) = (unreferenced_at IS NOT NULL))
);
CREATE INDEX blobs_unreferenced ON blobs (org_id, unreferenced_at) WHERE refs = 0;

-- One snapshot of a session's mind and work, as a turn left them (spec §12.2).
CREATE TABLE snapshots (
    id            text PRIMARY KEY,
    org_id        text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    session_id    text NOT NULL,
    turn_id       text NOT NULL,
    manifest_key  text NOT NULL,
    bytes         bigint NOT NULL CHECK (bytes >= 0),
    files         integer NOT NULL CHECK (files >= 0),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX snapshots_session ON snapshots (org_id, session_id);

-- A model endpoint (spec §10.1). Its key is sealed.
CREATE TABLE providers (
    org_id         text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name           text NOT NULL,
    apis           jsonb NOT NULL,
    default_model  text,
    key_sealed     bytea,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, name)
);

-- Where turns run (spec §9). spec is the definition without secret values; those are sealed, by name.
CREATE TABLE environments (
    org_id          text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name            text NOT NULL,
    spec            jsonb NOT NULL,
    secrets_sealed  jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, name)
);

-- Uploaded package archives, by digest (spec §8.1).
CREATE TABLE packages (
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    digest      text NOT NULL,
    manifest    jsonb NOT NULL,
    object_key  text NOT NULL,
    bytes       integer NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, digest)
);

-- Installed harnesses: a package, and what the organization has set on it (spec §14.2).
CREATE TABLE harnesses (
    org_id          text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name            text NOT NULL,
    digest          text NOT NULL,
    provider        text,
    model           text,
    settings        jsonb NOT NULL DEFAULT '{}',
    secrets_sealed  jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, name),
    FOREIGN KEY (org_id, digest) REFERENCES packages (org_id, digest)
);

-- A package installed for one environment image (spec §8.5). Its volume is named for its id.
CREATE TABLE installs (
    id                text PRIMARY KEY,
    org_id            text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    digest            text NOT NULL,
    image_id          text NOT NULL,
    status            text NOT NULL CHECK (status IN ('pending', 'ready', 'failed')),
    log_tail          text NOT NULL DEFAULT '',
    step_token_hash   text,
    proxy_token_hash  text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, digest, image_id)
);
CREATE INDEX installs_step_token ON installs (step_token_hash) WHERE step_token_hash IS NOT NULL;

-- Uploaded files (spec §13.9).
CREATE TABLE files (
    id          text PRIMARY KEY,
    org_id      text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    filename    text NOT NULL,
    purpose     text NOT NULL,
    bytes       bigint NOT NULL,
    object_key  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX files_org ON files (org_id, id);

-- A chain of turns (spec §6). volume_snapshot_id says which snapshot the session's volumes hold (§12.5).
CREATE TABLE sessions (
    id                   text PRIMARY KEY,
    org_id               text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    head_turn_id         text,
    forked_from_turn_id  text,
    volume_snapshot_id   text,
    volume_used_at       timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    last_active_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_org ON sessions (org_id, last_active_at DESC);
CREATE INDEX sessions_volumes ON sessions (volume_used_at) WHERE volume_used_at IS NOT NULL;

-- One row per turn, with everything needed to explain it (spec §5.3).
CREATE TABLE turns (
    id                 text PRIMARY KEY,
    org_id             text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    session_id         text NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    parent_id          text,                          -- may be a turn of another session: a fork
    number             integer NOT NULL,              -- position in its chain, from 1
    status             text NOT NULL CHECK (status IN
                           ('queued', 'in_progress', 'completed', 'failed', 'cancelled', 'incomplete')),
    reason             text,
    code               text,
    message            text,
    incomplete_reason  text,
    -- what it runs with
    environment        text NOT NULL,
    harness            text NOT NULL,
    harness_version    text NOT NULL,
    digest             text NOT NULL,
    mind_dir           text,
    provider           text,
    api                text,
    model              text,
    image_id           text,
    -- what was asked and what came of it
    request            jsonb,                         -- NULL once the turn is deleted
    output             jsonb NOT NULL DEFAULT '[]',
    usage              jsonb,
    snapshot_id        text,
    handover           text NOT NULL DEFAULT '',
    background         boolean NOT NULL DEFAULT false,
    store              boolean NOT NULL DEFAULT true,
    cancel_requested   boolean NOT NULL DEFAULT false,
    key_id             text,
    -- while it runs
    step_token_hash    text,
    proxy_token_hash   text,
    placeholders       jsonb NOT NULL DEFAULT '{}',   -- placeholder -> which secret it stands for
    runner_id          text,
    heartbeat_at       timestamptz,
    log_tail           text NOT NULL DEFAULT '',
    command            text,                          -- the sandbox command, tokens masked
    -- when
    created_at         timestamptz NOT NULL DEFAULT now(),
    started_at         timestamptz,
    restored_at        timestamptz,
    ran_at             timestamptz,
    exited_at          timestamptz,
    saved_at           timestamptz,
    completed_at       timestamptz,
    deleted_at         timestamptz
);
CREATE INDEX turns_queue ON turns (created_at) WHERE status = 'queued';
CREATE INDEX turns_running ON turns (heartbeat_at) WHERE status = 'in_progress';
CREATE INDEX turns_session ON turns (org_id, session_id, number);
CREATE INDEX turns_step_token ON turns (step_token_hash) WHERE step_token_hash IS NOT NULL;

-- A turn's stream events, in order (spec §13.5).
CREATE TABLE events (
    org_id   text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    turn_id  text NOT NULL REFERENCES turns (id) ON DELETE CASCADE,
    seq      integer NOT NULL,
    data     jsonb NOT NULL,
    PRIMARY KEY (turn_id, seq)
);

-- One row per finished turn (spec §22.1). It outlives the turn.
CREATE TABLE usage (
    turn_id          text PRIMARY KEY,
    org_id           text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    at               timestamptz NOT NULL DEFAULT now(),
    status           text NOT NULL,
    harness          text NOT NULL,
    provider         text,
    model            text,
    environment      text NOT NULL,
    input_tokens     bigint NOT NULL DEFAULT 0,
    output_tokens    bigint NOT NULL DEFAULT 0,
    sandbox_seconds  double precision NOT NULL DEFAULT 0,
    bytes_up         bigint NOT NULL DEFAULT 0,
    bytes_down       bigint NOT NULL DEFAULT 0,
    snapshot_bytes   bigint NOT NULL DEFAULT 0
);
CREATE INDEX usage_org ON usage (org_id, at);

-- API keys (spec §16). Only a keyed hash of a key is stored.
CREATE TABLE keys (
    id            text PRIMARY KEY,
    org_id        text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name          text NOT NULL,
    role          text NOT NULL CHECK (role IN ('runner', 'developer', 'admin')),
    hash          text NOT NULL UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);
CREATE INDEX keys_org ON keys (org_id, created_at);
