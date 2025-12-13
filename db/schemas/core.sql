-- CORE LAYER (BUSINESS MODEL)
-- Purpose: normalized/queryable tables with FK integrity where appropriate.
--
-- CRITICAL:
-- - Store all timestamps as UTC using TIMESTAMPTZ
-- - Primary keys use API-stable identifiers:
--   - countries: code (API provides ISO code, no numeric id)
--   - timezones: name (API provides timezone string)
--   - standings: (league_id, season, team_id) composite PK (API doesn't provide a standings id)
-- - Use UPSERT in application code to keep writes idempotent.
--
-- UPSERT example (fixtures):
--   INSERT INTO core.fixtures (id, league_id, season, home_team_id, away_team_id, date, status_short, goals_home, goals_away)
--   VALUES (123, 39, 2024, 33, 34, '2024-12-12T20:00:00+00:00', 'FT', 2, 1)
--   ON CONFLICT (id) DO UPDATE SET
--     status_short = EXCLUDED.status_short,
--     goals_home   = EXCLUDED.goals_home,
--     goals_away   = EXCLUDED.goals_away,
--     updated_at   = NOW();

CREATE SCHEMA IF NOT EXISTS core;

-- Shared updated_at trigger helper
CREATE OR REPLACE FUNCTION core.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- a) countries (no dependencies)
CREATE TABLE IF NOT EXISTS core.countries (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  flag TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core_countries_name ON core.countries (name);
CREATE TRIGGER trg_core_countries_updated_at
  BEFORE UPDATE ON core.countries
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- b) timezones (no dependencies)
CREATE TABLE IF NOT EXISTS core.timezones (
  name TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TRIGGER trg_core_timezones_updated_at
  BEFORE UPDATE ON core.timezones
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- c) leagues (no FK, but has country reference fields)
CREATE TABLE IF NOT EXISTS core.leagues (
  id BIGINT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT,
  logo TEXT,

  -- Country reference (no FK by requirement)
  country_name TEXT,
  country_code TEXT,
  country_flag TEXT,

  -- Seasons / coverage metadata from API (nested) stored as JSONB
  seasons JSONB,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core_leagues_country_code ON core.leagues (country_code);
CREATE INDEX IF NOT EXISTS idx_core_leagues_name ON core.leagues (name);
CREATE TRIGGER trg_core_leagues_updated_at
  BEFORE UPDATE ON core.leagues
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- d) venues (no FK)
CREATE TABLE IF NOT EXISTS core.venues (
  id BIGINT PRIMARY KEY,
  name TEXT,
  address TEXT,
  city TEXT,
  country TEXT,
  capacity INTEGER,
  surface TEXT,
  image TEXT,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core_venues_city ON core.venues (city);
CREATE INDEX IF NOT EXISTS idx_core_venues_country ON core.venues (country);
CREATE TRIGGER trg_core_venues_updated_at
  BEFORE UPDATE ON core.venues
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- e) teams (FK: venue_id optional)
CREATE TABLE IF NOT EXISTS core.teams (
  id BIGINT PRIMARY KEY,
  name TEXT NOT NULL,
  code TEXT,
  country TEXT,
  founded INTEGER,
  national BOOLEAN,
  logo TEXT,

  venue_id BIGINT REFERENCES core.venues(id) ON DELETE SET NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core_teams_country ON core.teams (country);
CREATE INDEX IF NOT EXISTS idx_core_teams_venue_id ON core.teams (venue_id);
CREATE TRIGGER trg_core_teams_updated_at
  BEFORE UPDATE ON core.teams
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- f) fixtures (FK: league_id, home_team_id, away_team_id, venue_id)
CREATE TABLE IF NOT EXISTS core.fixtures (
  id BIGINT PRIMARY KEY,

  league_id BIGINT NOT NULL REFERENCES core.leagues(id) ON DELETE RESTRICT,
  season INTEGER,
  round TEXT,

  -- Match metadata (UTC)
  date TIMESTAMPTZ NOT NULL,
  api_timestamp BIGINT,
  referee TEXT,
  timezone TEXT,

  venue_id BIGINT REFERENCES core.venues(id) ON DELETE SET NULL,

  home_team_id BIGINT NOT NULL REFERENCES core.teams(id) ON DELETE RESTRICT,
  away_team_id BIGINT NOT NULL REFERENCES core.teams(id) ON DELETE RESTRICT,

  status_short TEXT,
  status_long TEXT,
  elapsed INTEGER,

  goals_home INTEGER,
  goals_away INTEGER,

  score JSONB,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core_fixtures_date ON core.fixtures (date);
CREATE INDEX IF NOT EXISTS idx_core_fixtures_status ON core.fixtures (status_short);
CREATE INDEX IF NOT EXISTS idx_core_fixtures_league_season ON core.fixtures (league_id, season);
CREATE INDEX IF NOT EXISTS idx_core_fixtures_home_team ON core.fixtures (home_team_id);
CREATE INDEX IF NOT EXISTS idx_core_fixtures_away_team ON core.fixtures (away_team_id);
CREATE TRIGGER trg_core_fixtures_updated_at
  BEFORE UPDATE ON core.fixtures
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- g) fixture_details (FK: fixture_id) - JSONB for nested data
CREATE TABLE IF NOT EXISTS core.fixture_details (
  fixture_id BIGINT PRIMARY KEY REFERENCES core.fixtures(id) ON DELETE CASCADE,
  events JSONB,
  lineups JSONB,
  statistics JSONB,
  players JSONB,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core_fixture_details_events_gin
  ON core.fixture_details USING GIN (events);
CREATE INDEX IF NOT EXISTS idx_core_fixture_details_statistics_gin
  ON core.fixture_details USING GIN (statistics);
CREATE TRIGGER trg_core_fixture_details_updated_at
  BEFORE UPDATE ON core.fixture_details
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- h) players (no FK by requirement, but team relationship fields exist)
CREATE TABLE IF NOT EXISTS core.players (
  id BIGINT PRIMARY KEY,
  name TEXT,
  firstname TEXT,
  lastname TEXT,
  age INTEGER,
  birth_date DATE,
  birth_place TEXT,
  birth_country TEXT,
  nationality TEXT,
  height TEXT,
  weight TEXT,
  injured BOOLEAN,
  photo TEXT,

  -- Relationship fields (no FK constraint by requirement)
  team_id BIGINT,
  league_id BIGINT,
  season INTEGER,
  statistics JSONB,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core_players_team_season ON core.players (team_id, season);
CREATE INDEX IF NOT EXISTS idx_core_players_league_season ON core.players (league_id, season);
CREATE INDEX IF NOT EXISTS idx_core_players_name ON core.players (name);
CREATE TRIGGER trg_core_players_updated_at
  BEFORE UPDATE ON core.players
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();

-- i) standings (FK: league_id, team_id)
CREATE TABLE IF NOT EXISTS core.standings (
  league_id BIGINT NOT NULL REFERENCES core.leagues(id) ON DELETE CASCADE,
  season INTEGER NOT NULL,
  team_id BIGINT NOT NULL REFERENCES core.teams(id) ON DELETE CASCADE,

  rank INTEGER,
  points INTEGER,
  goals_diff INTEGER,
  goals_for INTEGER,
  goals_against INTEGER,
  form TEXT,
  status TEXT,
  description TEXT,
  group_name TEXT,

  all_stats JSONB,
  home_stats JSONB,
  away_stats JSONB,

  updated_api TIMESTAMPTZ,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (league_id, season, team_id)
);

-- Backward-compatible schema evolution (if table existed before adding columns)
ALTER TABLE core.standings
  ADD COLUMN IF NOT EXISTS goals_for INTEGER;
ALTER TABLE core.standings
  ADD COLUMN IF NOT EXISTS goals_against INTEGER;
CREATE INDEX IF NOT EXISTS idx_core_standings_league_season_rank
  ON core.standings (league_id, season, rank);
CREATE INDEX IF NOT EXISTS idx_core_standings_team
  ON core.standings (team_id);
CREATE TRIGGER trg_core_standings_updated_at
  BEFORE UPDATE ON core.standings
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();


