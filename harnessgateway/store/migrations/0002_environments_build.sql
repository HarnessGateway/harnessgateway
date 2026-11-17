-- Version 4.2 (spec §9, §10, §17): a request names environment/harness/provider/model in full, so a harness
-- has no default provider or model; a provider lists the models it offers; an environment carries a list of
-- harnesses (in its spec) and may be set up by a script, whose result is a build.

ALTER TABLE harnesses DROP COLUMN provider, DROP COLUMN model;

ALTER TABLE providers DROP COLUMN default_model;
ALTER TABLE providers ADD COLUMN models jsonb NOT NULL DEFAULT '[]';

-- What the environment was last prepared as: the image its turns run on, and the build that made it (none
-- when it has no setup script). NULL until it is first prepared, or while its build has failed.
ALTER TABLE environments ADD COLUMN image_id text;
ALTER TABLE environments ADD COLUMN build_id text;

-- An environment's setup script run on a base image (spec §9.4). One per organization, base image and script.
CREATE TABLE builds (
    id                text PRIMARY KEY,
    org_id            text NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    base_image_id     text NOT NULL,
    script_digest     text NOT NULL,
    status            text NOT NULL CHECK (status IN ('pending', 'ready', 'failed')),
    image_id          text,                          -- the image it made, once ready
    environment       text,                          -- the environment the current attempt runs for
    log_tail          text NOT NULL DEFAULT '',
    proxy_token_hash  text,
    placeholders      jsonb NOT NULL DEFAULT '{}',   -- placeholder -> the environment secret it stands for
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, base_image_id, script_digest)
);

-- An installation that is updated keeps working: its default environment carries every harness it has.
UPDATE environments e SET spec = jsonb_set(spec, '{harnesses}', COALESCE(
    (SELECT jsonb_agg(h.name ORDER BY h.name) FROM harnesses h WHERE h.org_id = e.org_id), '[]'))
WHERE name = 'default';
