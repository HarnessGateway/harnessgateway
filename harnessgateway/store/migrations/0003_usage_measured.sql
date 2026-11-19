-- Version 4.3 (spec §22.1): what a turn's sandbox used, as its driver measured it. Zero where a driver cannot
-- measure, and for turns that ended before this version.

ALTER TABLE usage ADD COLUMN cpu_seconds double precision NOT NULL DEFAULT 0;
ALTER TABLE usage ADD COLUMN memory_peak_bytes bigint NOT NULL DEFAULT 0;
