-- Database validation queries (read-only)
-- Run with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/db_checks.sql

\echo '== RAW: endpoints summary (last 24h) =='
SELECT
  endpoint,
  COUNT(*) AS requests_24h,
  MAX(fetched_at) AS last_fetched_at
FROM raw.api_responses
WHERE fetched_at > NOW() - INTERVAL '24 hours'
GROUP BY endpoint
ORDER BY MAX(fetched_at) DESC;

\echo '== RAW: /players/topscorers requests (last 7d) =='
SELECT
  COUNT(*) AS requests_7d,
  MAX(fetched_at) AS last_fetched_at
FROM raw.api_responses
WHERE endpoint = '/players/topscorers'
  AND fetched_at > NOW() - INTERVAL '7 days';

\echo '== CORE counts =='
SELECT
  (SELECT COUNT(*) FROM core.leagues) AS core_leagues,
  (SELECT COUNT(*) FROM core.teams) AS core_teams,
  (SELECT COUNT(*) FROM core.fixtures) AS core_fixtures,
  (SELECT COUNT(*) FROM core.fixture_events) AS core_fixture_events,
  (SELECT COUNT(*) FROM core.fixture_lineups) AS core_fixture_lineups,
  (SELECT COUNT(*) FROM core.fixture_statistics) AS core_fixture_statistics,
  (SELECT COUNT(*) FROM core.fixture_players) AS core_fixture_players,
  (SELECT COUNT(*) FROM core.standings) AS core_standings,
  (SELECT COUNT(*) FROM core.injuries) AS core_injuries,
  (SELECT COUNT(*) FROM core.top_scorers) AS core_top_scorers,
  (SELECT COUNT(*) FROM core.team_statistics) AS core_team_statistics;

\echo '== CORE top_scorers grouped =='
SELECT league_id, season, COUNT(*) AS rows, MAX(updated_at) AS last_updated_at
FROM core.top_scorers
GROUP BY league_id, season
ORDER BY season DESC, league_id ASC;

\echo '== CORE team_statistics grouped =='
SELECT league_id, season, COUNT(*) AS rows, MAX(updated_at) AS last_updated_at
FROM core.team_statistics
GROUP BY league_id, season
ORDER BY season DESC, league_id ASC;

\echo '== MART coverage: lowest overall (season filtered) =='
-- NOTE: this requires mart.coverage_status to exist and be populated by jobs.
-- Filter season by passing: psql ... -v season=2025
-- If not passed, show latest season present.
WITH chosen AS (
  SELECT COALESCE(NULLIF(:'season','')::int, (SELECT MAX(season) FROM mart.coverage_status)) AS season
)
SELECT
  c.league_id,
  l.name AS league_name,
  c.season,
  c.endpoint,
  c.overall_coverage,
  c.lag_minutes,
  c.calculated_at
FROM mart.coverage_status c
JOIN core.leagues l ON l.id = c.league_id
JOIN chosen x ON x.season = c.season
ORDER BY c.overall_coverage ASC NULLS LAST
LIMIT 30;


