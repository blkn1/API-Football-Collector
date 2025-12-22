-- Phase 3.1: add explanatory flags to coverage_status to avoid false positives
-- Example: leagues with no scheduled fixtures in a lookback/lookahead window (off-season / winter break)
-- should not be treated as "stale" due to lack of updates.

ALTER TABLE IF EXISTS mart.coverage_status
  ADD COLUMN IF NOT EXISTS flags JSONB;


