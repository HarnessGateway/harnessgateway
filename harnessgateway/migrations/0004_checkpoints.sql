-- Every turn records the session's saved state after it (a checkpoint), and a session may be a fork: a new session
-- restored from an earlier response's checkpoint (previous_response_id names a response that is not its session's last).
ALTER TABLE turns ADD COLUMN snapshot_key text, ADD COLUMN files_key text;
ALTER TABLE sessions ADD COLUMN forked_from text;
