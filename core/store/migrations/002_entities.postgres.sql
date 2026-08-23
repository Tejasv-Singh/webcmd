-- 002_entities -- Postgres dialect. Mirrors 002_entities.sqlite.sql.

CREATE TABLE IF NOT EXISTS entities (
  entity_id    TEXT PRIMARY KEY,
  isin         TEXT UNIQUE,
  primary_name TEXT NOT NULL,
  entity_type  TEXT NOT NULL DEFAULT 'equity',
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TIMESTAMPTZ NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entities_isin ON entities(isin);

CREATE TABLE IF NOT EXISTS entity_aliases (
  alias_id   BIGSERIAL PRIMARY KEY,
  entity_id  TEXT NOT NULL REFERENCES entities(entity_id),
  alias_kind TEXT NOT NULL,
  alias_norm TEXT NOT NULL,
  alias_raw  TEXT NOT NULL,
  valid_from DATE NOT NULL DEFAULT DATE '1900-01-01',
  valid_to   DATE,
  source     TEXT,
  confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (alias_kind, alias_norm, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_alias_lookup ON entity_aliases(alias_kind, alias_norm);
CREATE INDEX IF NOT EXISTS ix_alias_entity ON entity_aliases(entity_id);

CREATE TABLE IF NOT EXISTS identity_events (
  event_id          TEXT PRIMARY KEY,
  event_type        TEXT NOT NULL,
  entity_id         TEXT NOT NULL,
  related_entity_id TEXT,
  effective_date    DATE NOT NULL,
  details_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_url        TEXT,
  created_at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_identity_entity ON identity_events(entity_id, effective_date);

DROP TRIGGER IF EXISTS identity_events_append_only ON identity_events;
CREATE TRIGGER identity_events_append_only
  BEFORE UPDATE OR DELETE ON identity_events
  FOR EACH ROW EXECUTE FUNCTION kosh_append_only();

CREATE TABLE IF NOT EXISTS alias_review_queue (
  queue_id           BIGSERIAL PRIMARY KEY,
  alias_kind         TEXT NOT NULL,
  alias_norm         TEXT NOT NULL,
  alias_raw          TEXT NOT NULL,
  source_slug        TEXT,
  context_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurrences        INTEGER NOT NULL DEFAULT 1,
  first_seen_at      TIMESTAMPTZ NOT NULL,
  last_seen_at       TIMESTAMPTZ NOT NULL,
  status             TEXT NOT NULL DEFAULT 'open',
  resolved_entity_id TEXT,
  UNIQUE (alias_kind, alias_norm)
);
CREATE INDEX IF NOT EXISTS ix_queue_status ON alias_review_queue(status, occurrences);
