-- A turn runs on the provider it was routed to, which a continuation may change (the session keeps its harness).
ALTER TABLE turns ADD COLUMN provider_id text;
