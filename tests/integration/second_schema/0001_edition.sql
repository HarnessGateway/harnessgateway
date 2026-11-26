-- What the second extension adds: a file of the same name as the sample edition's, recorded under its own owner.
CREATE TABLE second_notes (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id  text NOT NULL,
    said    text NOT NULL
);
