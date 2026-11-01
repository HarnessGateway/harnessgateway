-- A harness is one package: no overlays, and no record of where a package came from (every package is an upload).
ALTER TABLE harnesses DROP COLUMN overlay_digest, DROP COLUMN extends, DROP COLUMN source;
ALTER TABLE harnesses RENAME COLUMN base_digest TO digest;
ALTER TABLE sessions DROP COLUMN overlay_digest;
ALTER TABLE sessions RENAME COLUMN base_digest TO digest;
