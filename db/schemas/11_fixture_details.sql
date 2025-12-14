-- CORE EXTENSION: FIXTURE-LEVEL FEATURES (players/events/statistics/lineups)
-- These tables are designed to be idempotently upserted and linked to core.fixtures.
CREATE SCHEMA IF NOT EXISTS core;

-- Player-level statistics for a match (from GET /fixtures/players)
CREATE TABLE IF NOT EXISTS core.fixture_players (
  fixture_id BIGINT NOT NULL REFERENCES core.fixtures(id) ON DELETE CASCADE,
  team_id BIGINT,
  player_id BIGINT,

  player_name TEXT,
  statistics JSONB,
  update_utc TIMESTAMPTZ,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (fixture_id, team_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_core_fixture_players_fixture
  ON core.fixture_players (fixture_id);
CREATE INDEX IF NOT EXISTS idx_core_fixture_players_player
  ON core.fixture_players (player_id);

-- Team-level match statistics (from GET /fixtures/statistics)
CREATE TABLE IF NOT EXISTS core.fixture_statistics (
  fixture_id BIGINT NOT NULL REFERENCES core.fixtures(id) ON DELETE CASCADE,
  team_id BIGINT,
  statistics JSONB,
  update_utc TIMESTAMPTZ,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (fixture_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_core_fixture_statistics_fixture
  ON core.fixture_statistics (fixture_id);

-- Confirmed lineups (from GET /fixtures/lineups)
CREATE TABLE IF NOT EXISTS core.fixture_lineups (
  fixture_id BIGINT NOT NULL REFERENCES core.fixtures(id) ON DELETE CASCADE,
  team_id BIGINT,

  formation TEXT,
  start_xi JSONB,
  substitutes JSONB,
  coach JSONB,
  colors JSONB,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (fixture_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_core_fixture_lineups_fixture
  ON core.fixture_lineups (fixture_id);

-- Timeline events (from GET /fixtures/events)
-- We create a deterministic "event_key" per fixture to keep upserts idempotent.
CREATE TABLE IF NOT EXISTS core.fixture_events (
  fixture_id BIGINT NOT NULL REFERENCES core.fixtures(id) ON DELETE CASCADE,
  event_key TEXT NOT NULL,

  time_elapsed INTEGER,
  time_extra INTEGER,

  team_id BIGINT,
  player_id BIGINT,
  assist_id BIGINT,

  type TEXT,
  detail TEXT,
  comments TEXT,

  raw JSONB,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (fixture_id, event_key)
);
CREATE INDEX IF NOT EXISTS idx_core_fixture_events_fixture
  ON core.fixture_events (fixture_id);
CREATE INDEX IF NOT EXISTS idx_core_fixture_events_team
  ON core.fixture_events (team_id);
CREATE INDEX IF NOT EXISTS idx_core_fixture_events_player
  ON core.fixture_events (player_id);


