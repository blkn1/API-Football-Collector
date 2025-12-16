-- CORE EVOLUTION: cache /teams bootstrap per (league_id, season)
-- Reason: avoid repeated /teams calls during fixtures/standings backfill windows,
-- which can trigger API per-minute rateLimit errors despite token bucket usage.

CREATE TABLE IF NOT EXISTS core.team_bootstrap_progress (
  league_id BIGINT NOT NULL REFERENCES core.leagues(id) ON DELETE CASCADE,
  season INTEGER NOT NULL,
  completed BOOLEAN NOT NULL DEFAULT FALSE,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (league_id, season)
);

CREATE INDEX IF NOT EXISTS idx_team_bootstrap_progress_completed
  ON core.team_bootstrap_progress (completed);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_team_bootstrap_progress_updated_at'
  ) THEN
    CREATE TRIGGER trg_team_bootstrap_progress_updated_at
      BEFORE UPDATE ON core.team_bootstrap_progress
      FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();
  END IF;
END $$;

