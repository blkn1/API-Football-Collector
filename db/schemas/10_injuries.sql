-- CORE EXTENSION: INJURIES (operational, current-only collection; no historical backfill required)
CREATE SCHEMA IF NOT EXISTS core;

-- Injuries are frequently updated and may not provide a stable numeric ID.
-- Use a deterministic "injury_key" as primary key per league+season.
CREATE TABLE IF NOT EXISTS core.injuries (
  league_id BIGINT NOT NULL,
  season INTEGER NOT NULL,

  injury_key TEXT NOT NULL,

  -- Dimensions (no FK constraints to avoid write failures when upstream entities are missing)
  team_id BIGINT,
  player_id BIGINT,

  -- Facts
  player_name TEXT,
  team_name TEXT,
  type TEXT,
  reason TEXT,
  severity TEXT,
  date DATE,
  timezone TEXT,

  raw JSONB,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (league_id, season, injury_key)
);

CREATE INDEX IF NOT EXISTS idx_core_injuries_league_season_date
  ON core.injuries (league_id, season, date);
CREATE INDEX IF NOT EXISTS idx_core_injuries_team
  ON core.injuries (team_id);
CREATE INDEX IF NOT EXISTS idx_core_injuries_player
  ON core.injuries (player_id);


