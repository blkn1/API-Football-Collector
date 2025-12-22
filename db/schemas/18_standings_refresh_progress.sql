-- Standings refresh progress (cursor-based batching)
-- Purpose: allow daily_standings to run "parça parça" (batch) without reprocessing all leagues every run.
-- Idempotent: safe to apply multiple times.
--
-- How it works:
-- - daily_standings reads tracked (league_id, season) pairs from config/jobs/daily.yaml
-- - If mode.max_leagues_per_run is set, it processes only N pairs starting at cursor
-- - Cursor advances each run and wraps around total_pairs
--
CREATE TABLE IF NOT EXISTS core.standings_refresh_progress (
  job_id TEXT PRIMARY KEY,
  cursor INTEGER NOT NULL DEFAULT 0,
  total_pairs INTEGER,
  last_run_at TIMESTAMPTZ,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_core_standings_refresh_progress_updated_at'
  ) THEN
    CREATE TRIGGER trg_core_standings_refresh_progress_updated_at
      BEFORE UPDATE ON core.standings_refresh_progress
      FOR EACH ROW EXECUTE FUNCTION core.set_updated_at();
  END IF;
END $$;


