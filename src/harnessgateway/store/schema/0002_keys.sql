-- A key acts as someone, and may be limited: who it acts as (the installation's owner, unless an edition has other
-- people), the patterns of actions it is limited to (none: it is not limited), and the time from which it is refused.
ALTER TABLE keys
    ADD COLUMN subject     text NOT NULL DEFAULT 'owner',
    ADD COLUMN scopes      jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN expires_at  timestamptz;
CREATE INDEX keys_subject ON keys (org_id, subject);
