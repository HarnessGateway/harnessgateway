-- What the sample edition adds to the schema: somewhere to keep what it hears.
CREATE TABLE edition_audit (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id  text NOT NULL,
    name    text NOT NULL,
    target  text
);
