-- Team statistics (league+season+team) from /teams/statistics
-- Purpose: season-level team profile for modeling/analytics.
-- Idempotent: safe to apply multiple times.

-- Main table
CREATE TABLE IF NOT EXISTS core.team_statistics (
  league_id BIGINT NOT NULL REFERENCES core.leagues(id) ON DELETE CASCADE,
  season INTEGER NOT NULL,
  team_id BIGINT NOT NULL REFERENCES core.teams(id) ON DELETE CASCADE,

  -- Convenience fields (also present inside raw)
  form TEXT,

  -- Full response payload for feature engineering / forward-compat
  raw JSONB NOT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (league_id, season, team_id)
);

CREATE INDEX IF NOT EXISTS idx_core_team_statistics_league_season
  ON core.team_statistics (league_id, season);

CREATE INDEX IF NOT EXISTS idx_core_team_statistics_team
  ON core.team_statistics (team_id);

CREATE TRIGGER trg_core_team_statistics_updated_at
  BEFORE UPDATE ON core.team_statistics
  FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();


-- Progress table: track refresh cadence per team (distributed across day)
CREATE TABLE IF NOT EXISTS core.team_statistics_progress (
  league_id BIGINT NOT NULL REFERENCES core.leagues(id) ON DELETE CASCADE,
  season INTEGER NOT NULL,
  team_id BIGINT NOT NULL REFERENCES core.teams(id) ON DELETE CASCADE,

  last_fetched_at TIMESTAMPTZ,
  last_error TEXT,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (league_id, season, team_id)
);

CREATE INDEX IF NOT EXISTS idx_core_team_statistics_progress_due
  ON core.team_statistics_progress (last_fetched_at NULLS FIRST, league_id, season);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_core_team_statistics_progress_updated_at'
  ) THEN
    CREATE TRIGGER trg_core_team_statistics_progress_updated_at
      BEFORE UPDATE ON core.team_statistics_progress
      FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();
  END IF;
END $$;


