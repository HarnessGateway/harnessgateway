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
