-- 003: run status for background runs, readset cache, edge lookups.

ALTER TABLE runs ADD COLUMN status TEXT NOT NULL DEFAULT 'done';  -- done|running|lost

-- Muse Spark's reading of a script (files it reads and writes), cached by content hash.
CREATE TABLE readsets (
  sha256  TEXT PRIMARY KEY,
  model   TEXT NOT NULL,
  created TEXT NOT NULL,
  json    TEXT NOT NULL
);

CREATE INDEX edges_version ON edges (version_id, role);
CREATE INDEX runs_host_ts ON runs (host, ts_start);
