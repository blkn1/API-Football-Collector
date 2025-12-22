-- Add lap tracking to standings refresh cursor
-- Idempotent migration.

ALTER TABLE core.standings_refresh_progress
  ADD COLUMN IF NOT EXISTS lap_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE core.standings_refresh_progress
  ADD COLUMN IF NOT EXISTS last_full_pass_at TIMESTAMPTZ;


