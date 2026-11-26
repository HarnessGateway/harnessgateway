-- A key acts as someone, and may be limited: who it acts as (the installation's owner, unless an edition has other
-- people), the patterns of actions it is limited to (none: it is not limited), and the time from which it is refused.
-- And it is accountable: who made it, and the last characters of the key itself, by which someone who finds one
-- knows which it is. Both are unknown (NULL) for a key made before they were kept.
ALTER TABLE keys
    ADD COLUMN subject     text NOT NULL DEFAULT 'owner',
    ADD COLUMN scopes      jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN expires_at  timestamptz,
    ADD COLUMN created_by  text,
    ADD COLUMN hint        text;
CREATE INDEX keys_subject ON keys (org_id, subject);

-- A turn says which API key sent it. NULL: its caller came with no key (signed in), or it was sent before this was kept.
ALTER TABLE turns ADD COLUMN key_id text;
