-- An uploaded file is its uploader's, as a session is whoever started it's. A file uploaded before this was kept
-- is the installation's owner's: with the core alone that is everyone, and in an edition with people it is then
-- reached only by whoever may reach everyone's.
ALTER TABLE files ADD COLUMN owner text NOT NULL DEFAULT 'owner';
CREATE INDEX files_owner ON files (org_id, owner, id DESC);
