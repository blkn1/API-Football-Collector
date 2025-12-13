"""
SQL query templates for the MCP server.

CRITICAL:
- READ-ONLY only (SELECT / WITH ... SELECT)
- Always parameterize values (%s placeholders). Do NOT string-interpolate user input.
"""

# Coverage status (joins league name for readability)
# Note: `league_filter` is inserted as a fixed safe fragment ("" or "AND c.league_id = %s").
COVERAGE_STATUS = """
    SELECT
      l.name as league_name,
      c.league_id,
      c.season,
      c.endpoint,
      c.count_coverage,
      c.freshness_coverage,
      c.pipeline_coverage,
      c.overall_coverage,
      c.last_update,
      c.lag_minutes,
      c.calculated_at
    FROM mart.coverage_status c
    JOIN core.leagues l ON c.league_id = l.id
    WHERE c.season = %s
    {league_filter}
    ORDER BY c.overall_coverage DESC NULLS LAST, c.calculated_at DESC
"""

COVERAGE_SUMMARY = """
    SELECT
      c.season,
      COUNT(*) AS rows,
      COUNT(DISTINCT c.league_id) AS leagues,
      COUNT(DISTINCT c.endpoint) AS endpoints,
      ROUND(AVG(c.overall_coverage)::numeric, 2) AS avg_overall_coverage,
      MAX(c.calculated_at) AS last_calculated_at
    FROM mart.coverage_status c
    WHERE c.season = %s
    GROUP BY c.season
"""

# Fixtures query (includes team names)
# Note: `filters` is inserted as a fixed safe fragment assembled from known clauses.
FIXTURES_QUERY = """
    SELECT
      f.id,
      f.league_id,
      f.season,
      f.date,
      f.status_short,
      th.name as home_team,
      ta.name as away_team,
      f.goals_home,
      f.goals_away,
      f.updated_at
    FROM core.fixtures f
    JOIN core.teams th ON f.home_team_id = th.id
    JOIN core.teams ta ON f.away_team_id = ta.id
    WHERE 1=1
    {filters}
    ORDER BY f.date DESC
    LIMIT %s
"""

STANDINGS_QUERY = """
    SELECT
      s.league_id,
      s.season,
      s.team_id,
      t.name AS team_name,
      s.rank,
      s.points,
      s.goals_diff,
      s.goals_for,
      s.goals_against,
      s.form,
      s.status,
      s.description,
      s.group_name,
      s.updated_at
    FROM core.standings s
    JOIN core.teams t ON t.id = s.team_id
    WHERE s.league_id = %s
      AND s.season = %s
    ORDER BY s.rank ASC NULLS LAST, t.name ASC
"""

TEAMS_QUERY = """
    SELECT
      t.id,
      t.name,
      t.code,
      t.country,
      t.founded,
      t.national,
      t.logo,
      t.venue_id,
      t.updated_at
    FROM core.teams t
    WHERE 1=1
    {filters}
    ORDER BY t.name ASC
    LIMIT %s
"""

LEAGUE_INFO_QUERY = """
    SELECT
      l.id,
      l.name,
      l.type,
      l.logo,
      l.country_name,
      l.country_code,
      l.country_flag,
      l.updated_at
    FROM core.leagues l
    WHERE l.id = %s
"""

LAST_SYNC_TIME_QUERY = """
    SELECT MAX(fetched_at) AS last_fetched_at
    FROM raw.api_responses
    WHERE endpoint = %s
"""

LAST_QUOTA_HEADERS_QUERY = """
    SELECT
      fetched_at,
      (response_headers->>'x-ratelimit-requests-remaining') AS daily_remaining,
      (response_headers->>'X-RateLimit-Remaining') AS minute_remaining
    FROM raw.api_responses
    WHERE response_headers ? 'x-ratelimit-requests-remaining'
       OR response_headers ? 'X-RateLimit-Remaining'
    ORDER BY fetched_at DESC
    LIMIT 1
"""

DATABASE_STATS_QUERY = """
    SELECT
      (SELECT COUNT(*) FROM raw.api_responses) AS raw_api_responses,
      (SELECT COUNT(*) FROM core.leagues) AS core_leagues,
      (SELECT COUNT(*) FROM core.teams) AS core_teams,
      (SELECT COUNT(*) FROM core.venues) AS core_venues,
      (SELECT COUNT(*) FROM core.fixtures) AS core_fixtures,
      (SELECT COUNT(*) FROM core.fixture_details) AS core_fixture_details,
      (SELECT COUNT(*) FROM core.standings) AS core_standings,
      (SELECT MAX(fetched_at) FROM raw.api_responses) AS raw_last_fetched_at,
      (SELECT MAX(updated_at) FROM core.fixtures) AS core_fixtures_last_updated_at
"""


