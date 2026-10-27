-- 002: artifacts keyed by (host, path), file change log, run user and snapshot columns.

-- The same path on two hosts is not the same file (CLAUDE.md section 5.4).
CREATE TABLE artifacts_v2 (
  id   TEXT PRIMARY KEY,
  host TEXT NOT NULL,
  path TEXT NOT NULL,
  kind TEXT,  -- data|figure|config|script|log|other
  UNIQUE (host, path)
);
INSERT INTO artifacts_v2 (id, host, path, kind) SELECT id, '', path, kind FROM artifacts;
DROP TABLE artifacts;
ALTER TABLE artifacts_v2 RENAME TO artifacts;

ALTER TABLE artifact_versions ADD COLUMN fp TEXT NOT NULL DEFAULT 'full';  -- full|fast
ALTER TABLE artifact_versions ADD COLUMN mtime TEXT;
CREATE INDEX artifact_versions_sha256 ON artifact_versions (sha256);

-- Every observed change, in time order: what the window linker reads.
CREATE TABLE file_events (
  event_id    TEXT PRIMARY KEY REFERENCES events(id),
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  version_id  TEXT REFERENCES artifact_versions(id),  -- NULL for deletions
  op          TEXT NOT NULL,  -- created|modified|deleted|moved|scanned
  ts          TEXT NOT NULL,
  mtime       TEXT
);
CREATE INDEX file_events_ts ON file_events (ts);
CREATE INDEX file_events_artifact ON file_events (artifact_id, ts);

ALTER TABLE runs ADD COLUMN user TEXT;
ALTER TABLE runs ADD COLUMN git_branch TEXT;
ALTER TABLE runs ADD COLUMN git_diffstat TEXT;
ALTER TABLE runs ADD COLUMN env_name TEXT;
UPDATE runs SET user = (SELECT json_extract(e.payload, '$.user') FROM events e WHERE e.id = runs.id);
