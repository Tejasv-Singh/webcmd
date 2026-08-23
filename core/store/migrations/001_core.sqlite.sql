-- 001_core -- SQLite dialect.
-- Append-only is enforced here, in the schema, not in application code. Rule 1 of
-- CLAUDE.md is the whole thesis of the project; a rule that lives only in a code
-- review comment is a rule that gets broken at 2am during an incident.

CREATE TABLE IF NOT EXISTS raw_blobs (
  sha256        TEXT PRIMARY KEY,
  byte_len      INTEGER NOT NULL,
  content_type  TEXT,
  rel_path      TEXT NOT NULL,
  first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS captures (
  capture_id   TEXT PRIMARY KEY,
  source_slug  TEXT NOT NULL,
  source_url   TEXT NOT NULL,
  sha256       TEXT NOT NULL REFERENCES raw_blobs(sha256),
  target_date  TEXT,
  captured_at  TEXT NOT NULL,
  http_status  INTEGER,
  run_id       TEXT,
  fetch_meta   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_captures_source_date ON captures(source_slug, target_date);
CREATE INDEX IF NOT EXISTS ix_captures_sha ON captures(sha256);
CREATE INDEX IF NOT EXISTS ix_captures_captured_at ON captures(captured_at);

CREATE TABLE IF NOT EXISTS observations (
  observation_id TEXT PRIMARY KEY,
  entity_id      TEXT NOT NULL,
  metric         TEXT NOT NULL,
  value_json     TEXT NOT NULL,
  value_num      REAL,
  observed_at    TEXT NOT NULL,
  captured_at    TEXT NOT NULL,
  source_url     TEXT NOT NULL,
  capture_id     TEXT NOT NULL REFERENCES captures(capture_id),
  extractor_ver  TEXT NOT NULL,
  confidence     REAL NOT NULL DEFAULT 1.0,
  inserted_at    TEXT NOT NULL,
  UNIQUE (capture_id, entity_id, metric, observed_at, extractor_ver)
);
CREATE INDEX IF NOT EXISTS ix_obs_entity_metric ON observations(entity_id, metric, observed_at);
CREATE INDEX IF NOT EXISTS ix_obs_metric_observed ON observations(metric, observed_at);
CREATE INDEX IF NOT EXISTS ix_obs_capture ON observations(capture_id);
CREATE INDEX IF NOT EXISTS ix_obs_ver ON observations(extractor_ver);
-- Point-in-time reconstruction: "what did Kosh know on date X" is a captured_at filter.
CREATE INDEX IF NOT EXISTS ix_obs_captured_at ON observations(captured_at);

-- Append-only triggers. Corrections are new rows with a later captured_at.
CREATE TRIGGER IF NOT EXISTS observations_no_update BEFORE UPDATE ON observations
BEGIN
  SELECT RAISE(ABORT, 'append-only: UPDATE on observations is forbidden (CLAUDE.md rule 1)');
END;

CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON observations
BEGIN
  SELECT RAISE(ABORT, 'append-only: DELETE on observations is forbidden (CLAUDE.md rule 1)');
END;

CREATE TRIGGER IF NOT EXISTS captures_no_update BEFORE UPDATE ON captures
BEGIN
  SELECT RAISE(ABORT, 'append-only: UPDATE on captures is forbidden (CLAUDE.md rule 1)');
END;

CREATE TRIGGER IF NOT EXISTS captures_no_delete BEFORE DELETE ON captures
BEGIN
  SELECT RAISE(ABORT, 'append-only: DELETE on captures is forbidden (CLAUDE.md rule 1)');
END;

CREATE TRIGGER IF NOT EXISTS raw_blobs_no_delete BEFORE DELETE ON raw_blobs
BEGIN
  SELECT RAISE(ABORT, 'append-only: DELETE on raw_blobs is forbidden (CLAUDE.md rule 1)');
END;

-- ---------------------------------------------------------------------------
-- Operational tables. These are mutable on purpose: they are how we run the
-- system, not what we know about the world. Nothing here is an Observation.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sources (
  slug               TEXT PRIMARY KEY,
  display_name       TEXT NOT NULL,
  url                TEXT NOT NULL,
  ring               INTEGER NOT NULL DEFAULT 1,
  cadence            TEXT NOT NULL DEFAULT 'daily',
  calendar           TEXT NOT NULL DEFAULT 'weekday',
  enabled            INTEGER NOT NULL DEFAULT 0,
  compliance_verdict TEXT,
  registered_at      TEXT NOT NULL,
  notes              TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  source_slug   TEXT NOT NULL,
  phase         TEXT NOT NULL,
  target_date   TEXT,
  status        TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  blob_count    INTEGER NOT NULL DEFAULT 0,
  row_count     INTEGER NOT NULL DEFAULT 0,
  extractor_ver TEXT,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_source_phase_date ON runs(source_slug, phase, target_date);
CREATE INDEX IF NOT EXISTS ix_runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS canary_results (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source_slug TEXT NOT NULL,
  run_id      TEXT,
  check_name  TEXT NOT NULL,
  status      TEXT NOT NULL,
  detail      TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_canary_source_time ON canary_results(source_slug, created_at);
