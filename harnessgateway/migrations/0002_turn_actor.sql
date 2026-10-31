-- Who created each turn (the API key, or what an extension's principal names): the client a session belongs to.
ALTER TABLE turns ADD COLUMN actor text;
