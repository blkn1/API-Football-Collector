-- MART EVOLUTION: live_score_panel should be a VIEW (not materialized)
-- Reason: live updates should reflect core.fixtures without needing explicit refresh.

CREATE SCHEMA IF NOT EXISTS mart;

-- IMPORTANT:
-- Postgres will ERROR on DROP MATERIALIZED VIEW IF EXISTS when an object exists
-- with the same name but different type (e.g. a plain VIEW). Make this migration
-- idempotent by conditionally dropping based on actual type.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_matviews
    WHERE schemaname = 'mart'
      AND matviewname = 'live_score_panel'
  ) THEN
    EXECUTE 'DROP MATERIALIZED VIEW mart.live_score_panel';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_views
    WHERE schemaname = 'mart'
      AND viewname = 'live_score_panel'
  ) THEN
    EXECUTE 'DROP VIEW mart.live_score_panel';
  END IF;
END $$;

CREATE OR REPLACE VIEW mart.live_score_panel AS
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
