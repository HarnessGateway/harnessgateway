-- The core keeps no audit log (spec §22.2): it tells its extensions of each change, and an edition that keeps a
-- log keeps it in a table of its own.
DROP TABLE audit;
