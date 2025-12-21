-- Top scorers (league+season) from /players/topscorers
-- Purpose: provide historical leaderboards for modeling/analytics.
-- Idempotent: safe to apply multiple times.

CREATE TABLE IF NOT EXISTS core.top_scorers (
  league_id BIGINT NOT NULL REFERENCES core.leagues(id) ON DELETE CASCADE,
  season INTEGER NOT NULL,
  player_id BIGINT NOT NULL,

  rank INTEGER,
  team_id BIGINT,
  team_name TEXT,

  goals INTEGER,
  assists INTEGER,

  -- Store full item payload for forward-compat + feature engineering
  raw JSONB,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (league_id, season, player_id)
);

CREATE INDEX IF NOT EXISTS idx_core_top_scorers_league_season_rank
  ON core.top_scorers (league_id, season, rank);

CREATE INDEX IF NOT EXISTS idx_core_top_scorers_player
  ON core.top_scorers (player_id);

CREATE TRIGGER trg_core_top_scorers_updated_at
  BEFORE UPDATE ON core.top_scorers
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();


