-- MART EVOLUTION: live_score_panel should be a VIEW (not materialized)
-- Reason: live updates should reflect core.fixtures without needing explicit refresh.

CREATE SCHEMA IF NOT EXISTS mart;

DROP MATERIALIZED VIEW IF EXISTS mart.live_score_panel;

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
