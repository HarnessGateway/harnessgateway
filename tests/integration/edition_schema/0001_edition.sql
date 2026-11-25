-- What the sample edition adds to the schema: somewhere to keep, with each change, the record of it.
CREATE TABLE sample_audit (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id  text NOT NULL,
    name    text NOT NULL,
    target  text NOT NULL,
    by      text NOT NULL
);
