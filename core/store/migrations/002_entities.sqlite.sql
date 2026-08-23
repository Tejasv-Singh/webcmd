-- 002_entities -- the ISIN-keyed identity graph (CLAUDE.md rule 3).
-- Never join on a display name. Aliases are date-scoped because "TATAMOTORS" did
-- not always mean what it means today, and a corpus that forgets that produces
-- backtests that quietly lie.

CREATE TABLE IF NOT EXISTS entities (
  entity_id    TEXT PRIMARY KEY,
  isin         TEXT UNIQUE,
  primary_name TEXT NOT NULL,
  entity_type  TEXT NOT NULL DEFAULT 'equity',
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entities_isin ON entities(isin);

CREATE TABLE IF NOT EXISTS entity_aliases (
  alias_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id  TEXT NOT NULL REFERENCES entities(entity_id),
  alias_kind TEXT NOT NULL,
  alias_norm TEXT NOT NULL,
  alias_raw  TEXT NOT NULL,
  valid_from TEXT NOT NULL DEFAULT '1900-01-01',
  valid_to   TEXT,
  source     TEXT,
  confidence REAL NOT NULL DEFAULT 1.0,
  created_at TEXT NOT NULL,
  UNIQUE (alias_kind, alias_norm, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_alias_lookup ON entity_aliases(alias_kind, alias_norm);
CREATE INDEX IF NOT EXISTS ix_alias_entity ON entity_aliases(entity_id);

CREATE TABLE IF NOT EXISTS identity_events (
  event_id          TEXT PRIMARY KEY,
  event_type        TEXT NOT NULL,
  entity_id         TEXT NOT NULL,
  related_entity_id TEXT,
  effective_date    TEXT NOT NULL,
  details_json      TEXT NOT NULL DEFAULT '{}',
  source_url        TEXT,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_identity_entity ON identity_events(entity_id, effective_date);

CREATE TRIGGER IF NOT EXISTS identity_events_no_update BEFORE UPDATE ON identity_events
BEGIN
  SELECT RAISE(ABORT, 'append-only: UPDATE on identity_events is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS identity_events_no_delete BEFORE DELETE ON identity_events
BEGIN
  SELECT RAISE(ABORT, 'append-only: DELETE on identity_events is forbidden');
END;

-- The review queue is the answer to "what do we do with an unrecognised name".
-- Guessing is not an option; deferring is (CLAUDE.md rule 3).
CREATE TABLE IF NOT EXISTS alias_review_queue (
  queue_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  alias_kind         TEXT NOT NULL,
  alias_norm         TEXT NOT NULL,
  alias_raw          TEXT NOT NULL,
  source_slug        TEXT,
  context_json       TEXT NOT NULL DEFAULT '{}',
  occurrences        INTEGER NOT NULL DEFAULT 1,
  first_seen_at      TEXT NOT NULL,
  last_seen_at       TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'open',
  resolved_entity_id TEXT,
  UNIQUE (alias_kind, alias_norm)
);
CREATE INDEX IF NOT EXISTS ix_queue_status ON alias_review_queue(status, occurrences);
