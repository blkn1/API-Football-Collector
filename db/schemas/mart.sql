-- MART LAYER (READ MODELS)
-- Purpose: fast dashboard queries via materialized views.

CREATE SCHEMA IF NOT EXISTS mart;

-- 1) Today's fixtures summary (per league)
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.daily_fixtures_dashboard AS
SELECT
  (NOW() AT TIME ZONE 'UTC')::date AS as_of_date_utc,
  f.league_id,
  l.name AS league_name,
  COUNT(*) AS total_fixtures,
  SUM(CASE WHEN f.status_short IN ('FT', 'AET', 'PEN') THEN 1 ELSE 0 END) AS completed,
  SUM(CASE WHEN f.status_short IN ('1H', '2H', 'HT', 'ET', 'BT', 'P', 'LIVE', 'SUSP', 'INT') THEN 1 ELSE 0 END) AS live,
  SUM(CASE WHEN f.status_short = 'NS' THEN 1 ELSE 0 END) AS not_started,
  MAX(f.updated_at) AS last_updated_at
FROM core.fixtures f
JOIN core.leagues l ON l.id = f.league_id
WHERE f.date >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
  AND f.date <  date_trunc('day', NOW() AT TIME ZONE 'UTC') + INTERVAL '1 day'
GROUP BY f.league_id, l.name;

CREATE INDEX IF NOT EXISTS idx_mart_daily_fixtures_dashboard_league
  ON mart.daily_fixtures_dashboard (league_id);

-- 2) Live score panel (live fixtures list)
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.live_score_panel AS
SELECT
  f.id AS fixture_id,
  f.league_id,
  l.name AS league_name,
  f.season,
  f.round,
  f.date,
  f.status_short,
  f.elapsed,
  f.home_team_id,
  th.name AS home_team_name,
  f.away_team_id,
  ta.name AS away_team_name,
  f.goals_home,
  f.goals_away,
  f.updated_at
FROM core.fixtures f
JOIN core.leagues l ON l.id = f.league_id
JOIN core.teams th ON th.id = f.home_team_id
JOIN core.teams ta ON ta.id = f.away_team_id
WHERE f.status_short IN ('1H', '2H', 'HT', 'ET', 'BT', 'P', 'LIVE', 'SUSP', 'INT')
  AND f.updated_at > NOW() - INTERVAL '10 minutes'
ORDER BY f.date DESC;

CREATE INDEX IF NOT EXISTS idx_mart_live_score_panel_league
  ON mart.live_score_panel (league_id);
CREATE INDEX IF NOT EXISTS idx_mart_live_score_panel_updated_at
  ON mart.live_score_panel (updated_at DESC);

-- 3) Coverage status (foundation scope: fixtures pipeline + freshness; no hard-coded league/season)
-- Phase 3: make this a TABLE so the CoverageCalculator can write per-league metrics.
-- IMPORTANT:
-- Postgres errors on DROP MATERIALIZED VIEW IF EXISTS when an object exists with the same
-- name but different type (e.g. TABLE). Make this idempotent by dropping based on actual type.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_matviews
    WHERE schemaname = 'mart'
      AND matviewname = 'coverage_status'
  ) THEN
    EXECUTE 'DROP MATERIALIZED VIEW mart.coverage_status';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_views
    WHERE schemaname = 'mart'
      AND viewname = 'coverage_status'
  ) THEN
    EXECUTE 'DROP VIEW mart.coverage_status';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'mart'
      AND c.relname = 'coverage_status'
      AND c.relkind = 'r'  -- table
  ) THEN
    -- If table already exists, we keep it (idempotent) and skip drop.
    -- This block is intentionally empty.
    NULL;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS mart.coverage_status (
  league_id BIGINT NOT NULL,
  season INTEGER NOT NULL,
  endpoint TEXT NOT NULL,

  expected_count INTEGER,
  actual_count INTEGER,

  count_coverage NUMERIC(6,2),
  last_update TIMESTAMPTZ,
  lag_minutes INTEGER,
  freshness_coverage NUMERIC(6,2),

  raw_count INTEGER,
  core_count INTEGER,
  pipeline_coverage NUMERIC(6,2),
  overall_coverage NUMERIC(6,2),

  calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (league_id, season, endpoint)
);

CREATE INDEX IF NOT EXISTS idx_mart_coverage_status_season
  ON mart.coverage_status (season);



