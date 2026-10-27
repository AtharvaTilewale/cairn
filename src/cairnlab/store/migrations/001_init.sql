-- 001_init: core PROV-style schema (CLAUDE.md section 6) plus ingest bookkeeping.

CREATE TABLE events (
  id      TEXT PRIMARY KEY,
  type    TEXT NOT NULL,
  ts      TEXT NOT NULL,
  host    TEXT,
  payload TEXT NOT NULL
);
-- The hook's own event id lives in the payload; storing it twice is a bug.
CREATE UNIQUE INDEX events_source_id ON events (json_extract(payload, '$.id'));
CREATE INDEX events_host_ts ON events (host, ts);

CREATE TABLE runs (
  id        TEXT PRIMARY KEY,
  session   TEXT,
  host      TEXT,
  shell     TEXT,
  cwd       TEXT,
  cmd       TEXT,
  ts_start  TEXT,
  ts_end    TEXT,
  exit      INTEGER,
  git_commit TEXT,
  git_dirty INTEGER,
  env_hash  TEXT,
  phase     TEXT
);
CREATE INDEX runs_ts_start ON runs (ts_start);

CREATE TABLE artifacts (
  id   TEXT PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  kind TEXT  -- data|figure|config|script|log|other
);

CREATE TABLE artifact_versions (
  id          TEXT PRIMARY KEY,
  artifact_id TEXT REFERENCES artifacts(id),
  sha256      TEXT NOT NULL,
  size        INTEGER,
  ts          TEXT,
  stored      INTEGER DEFAULT 0,
  UNIQUE (artifact_id, sha256)
);

CREATE TABLE edges (
  run_id     TEXT REFERENCES runs(id),
  version_id TEXT REFERENCES artifact_versions(id),
  role       TEXT CHECK (role IN ('used', 'generated')),
  method     TEXT,  -- argpath|window|wrapper|readset
  confidence REAL,
  PRIMARY KEY (run_id, version_id, role)
);

CREATE TABLE notes (
  id         TEXT PRIMARY KEY,
  ts         TEXT,
  run_id     TEXT REFERENCES runs(id),
  text       TEXT,
  source     TEXT,  -- voice|typed
  audio_path TEXT
);

CREATE TABLE entries (
  id       TEXT PRIMARY KEY,
  day      TEXT UNIQUE,
  created  TEXT,
  model    TEXT,
  json     TEXT,
  markdown TEXT
);

-- Byte offset reached in each events/*.jsonl file, so re-running ingest adds nothing twice.
CREATE TABLE ingest_state (
  file    TEXT PRIMARY KEY,
  offset  INTEGER NOT NULL,
  updated TEXT
);
