-- CORE EXTENSION: BACKFILL PROGRESS (resumeable, quota-safe backfill state)
CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.backfill_progress (
  job_id TEXT NOT NULL,
  league_id BIGINT NOT NULL,
  season INTEGER NOT NULL,

  next_page INTEGER NOT NULL DEFAULT 1,
  completed BOOLEAN NOT NULL DEFAULT FALSE,

  last_error TEXT,
  last_run_at TIMESTAMPTZ,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (job_id, league_id, season)
);

CREATE INDEX IF NOT EXISTS idx_core_backfill_progress_job_completed
  ON core.backfill_progress (job_id, completed);
CREATE INDEX IF NOT EXISTS idx_core_backfill_progress_job_updated_at
  ON core.backfill_progress (job_id, updated_at DESC);


