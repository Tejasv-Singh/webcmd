-- 001_core -- Postgres dialect. Mirrors 001_core.sqlite.sql.
-- Keep the two in step: the SQLite one is what tests run against, the Postgres one
-- is what the corpus actually lives in, and a divergence between them is a bug that
-- only shows up in production.

CREATE TABLE IF NOT EXISTS raw_blobs (
  sha256        TEXT PRIMARY KEY,
  byte_len      BIGINT NOT NULL,
  content_type  TEXT,
  rel_path      TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS captures (
  capture_id   TEXT PRIMARY KEY,
  source_slug  TEXT NOT NULL,
  source_url   TEXT NOT NULL,
  sha256       TEXT NOT NULL REFERENCES raw_blobs(sha256),
  target_date  DATE,
  captured_at  TIMESTAMPTZ NOT NULL,
  http_status  INTEGER,
  run_id       TEXT,
  fetch_meta   JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_captures_source_date ON captures(source_slug, target_date);
CREATE INDEX IF NOT EXISTS ix_captures_sha ON captures(sha256);
CREATE INDEX IF NOT EXISTS ix_captures_captured_at ON captures(captured_at);

CREATE TABLE IF NOT EXISTS observations (
  observation_id TEXT PRIMARY KEY,
  entity_id      TEXT NOT NULL,
  metric         TEXT NOT NULL,
  value_json     JSONB NOT NULL,
  value_num      DOUBLE PRECISION,
  observed_at    TIMESTAMPTZ NOT NULL,
  captured_at    TIMESTAMPTZ NOT NULL,
  source_url     TEXT NOT NULL,
  capture_id     TEXT NOT NULL REFERENCES captures(capture_id),
  extractor_ver  TEXT NOT NULL,
  confidence     DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  inserted_at    TIMESTAMPTZ NOT NULL,
  UNIQUE (capture_id, entity_id, metric, observed_at, extractor_ver)
);
CREATE INDEX IF NOT EXISTS ix_obs_entity_metric ON observations(entity_id, metric, observed_at);
CREATE INDEX IF NOT EXISTS ix_obs_metric_observed ON observations(metric, observed_at);
CREATE INDEX IF NOT EXISTS ix_obs_capture ON observations(capture_id);
CREATE INDEX IF NOT EXISTS ix_obs_ver ON observations(extractor_ver);
CREATE INDEX IF NOT EXISTS ix_obs_captured_at ON observations(captured_at);

CREATE OR REPLACE FUNCTION kosh_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'append-only: % on % is forbidden (CLAUDE.md rule 1)',
        TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS observations_append_only ON observations;
CREATE TRIGGER observations_append_only
  BEFORE UPDATE OR DELETE ON observations
  FOR EACH ROW EXECUTE FUNCTION kosh_append_only();

DROP TRIGGER IF EXISTS captures_append_only ON captures;
CREATE TRIGGER captures_append_only
  BEFORE UPDATE OR DELETE ON captures
  FOR EACH ROW EXECUTE FUNCTION kosh_append_only();

DROP TRIGGER IF EXISTS raw_blobs_append_only ON raw_blobs;
CREATE TRIGGER raw_blobs_append_only
  BEFORE DELETE ON raw_blobs
  FOR EACH ROW EXECUTE FUNCTION kosh_append_only();

-- ---------------------------------------------------------------------------
-- Operational tables -- mutable on purpose. How we run the system, not what we
-- know about the world.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sources (
  slug               TEXT PRIMARY KEY,
  display_name       TEXT NOT NULL,
  url                TEXT NOT NULL,
  ring               INTEGER NOT NULL DEFAULT 1,
  cadence            TEXT NOT NULL DEFAULT 'daily',
  calendar           TEXT NOT NULL DEFAULT 'weekday',
  enabled            BOOLEAN NOT NULL DEFAULT FALSE,
  compliance_verdict TEXT,
  registered_at      TIMESTAMPTZ NOT NULL,
  notes              TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  source_slug   TEXT NOT NULL,
  phase         TEXT NOT NULL,
  target_date   DATE,
  status        TEXT NOT NULL,
  started_at    TIMESTAMPTZ NOT NULL,
  finished_at   TIMESTAMPTZ,
  blob_count    INTEGER NOT NULL DEFAULT 0,
  row_count     INTEGER NOT NULL DEFAULT 0,
  extractor_ver TEXT,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_source_phase_date ON runs(source_slug, phase, target_date);
CREATE INDEX IF NOT EXISTS ix_runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS canary_results (
  id          BIGSERIAL PRIMARY KEY,
  source_slug TEXT NOT NULL,
  run_id      TEXT,
  check_name  TEXT NOT NULL,
  status      TEXT NOT NULL,
  detail      TEXT,
  created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_canary_source_time ON canary_results(source_slug, created_at);
