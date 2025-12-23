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
      l.type as league_type,
      c.league_id,
      c.season,
      c.endpoint,
      c.count_coverage,
      c.freshness_coverage,
      c.pipeline_coverage,
      c.overall_coverage,
      c.last_update,
      c.lag_minutes,
      c.calculated_at,
      c.flags
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

# Team fixtures query (includes team IDs + names for FE rendering)
# Note: `filters` is inserted as a fixed safe fragment assembled from known clauses.
TEAM_FIXTURES_QUERY = """
    SELECT
      f.id,
      f.league_id,
      f.season,
      f.date,
      f.status_short,
      f.home_team_id,
      th.name as home_team_name,
      f.away_team_id,
      ta.name as away_team_name,
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

# Head-to-head fixtures (last N meetings). Includes team IDs/names.
H2H_FIXTURES_QUERY = """
    SELECT
      f.id,
      f.league_id,
      f.season,
      f.date,
      f.status_short,
      f.home_team_id,
      th.name as home_team_name,
      f.away_team_id,
      ta.name as away_team_name,
      f.goals_home,
      f.goals_away,
      f.updated_at
    FROM core.fixtures f
    JOIN core.teams th ON f.home_team_id = th.id
    JOIN core.teams ta ON f.away_team_id = ta.id
    WHERE (
      (f.home_team_id = %s AND f.away_team_id = %s)
      OR
      (f.home_team_id = %s AND f.away_team_id = %s)
    )
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

# Job-specific "last sync" evidence (RAW).
# These are used by MCP get_job_status() when collector.jsonl doesn't have events.

# daily_fixtures_by_date (per_tracked_leagues): /fixtures with requested_params.date (and not from/to window backfill)
LAST_SYNC_FIXTURES_DAILY_QUERY = """
    SELECT MAX(fetched_at) AS last_fetched_at
    FROM raw.api_responses
    WHERE endpoint = '/fixtures'
      AND (requested_params ? 'date')
      AND NOT (requested_params ? 'from')
      AND NOT (requested_params ? 'to')
"""

# fixtures_backfill_league_season: /fixtures with from/to window parameters
LAST_SYNC_FIXTURES_BACKFILL_QUERY = """
    SELECT MAX(fetched_at) AS last_fetched_at
    FROM raw.api_responses
    WHERE endpoint = '/fixtures'
      AND (requested_params ? 'from')
      AND (requested_params ? 'to')
"""

# stale_live_refresh: /fixtures with ids (max 20) parameter
LAST_SYNC_FIXTURES_IDS_QUERY = """
    SELECT MAX(fetched_at) AS last_fetched_at
    FROM raw.api_responses
    WHERE endpoint = '/fixtures'
      AND (requested_params ? 'ids')
"""

# Any fixture detail endpoint (players/events/statistics/lineups)
LAST_SYNC_FIXTURE_DETAILS_ANY_QUERY = """
    SELECT MAX(fetched_at) AS last_fetched_at
    FROM raw.api_responses
    WHERE endpoint = ANY(ARRAY['/fixtures/players','/fixtures/events','/fixtures/statistics','/fixtures/lineups'])
"""

STANDINGS_REFRESH_PROGRESS_QUERY = """
    SELECT
      job_id,
      cursor,
      total_pairs,
      last_run_at,
      last_error,
      lap_count,
      last_full_pass_at,
      updated_at
    FROM core.standings_refresh_progress
    WHERE job_id = %s
"""

LIVE_LOOP_ACTIVITY_QUERY = """
    SELECT
      COUNT(*)::int AS requests,
      MAX(fetched_at) AS last_fetched_at
    FROM raw.api_responses
    WHERE endpoint = '/fixtures'
      AND fetched_at >= NOW() - make_interval(mins => %s)
      AND (requested_params->>'live') = 'all'
"""

DAILY_FIXTURES_BY_DATE_ACTIVITY_QUERY = """
    SELECT
      COUNT(*)::int AS requests,
      MAX(fetched_at) AS last_fetched_at
    FROM raw.api_responses
    WHERE endpoint = '/fixtures'
      AND fetched_at >= NOW() - make_interval(mins => %s)
      AND (requested_params ? 'date')
"""

# Extra observability for global_by_date mode (/fixtures?date=...&page=N)
DAILY_FIXTURES_BY_DATE_PAGING_METRICS_QUERY = """
    SELECT
      -- All /fixtures calls that include requested_params.date (includes both per-league and global-by-date)
      COUNT(*)::int AS requests,
      MAX(fetched_at) AS last_fetched_at,

      -- Global-by-date requests: date present, but no league filter
      COUNT(*) FILTER (WHERE NOT (requested_params ? 'league'))::int AS global_requests,
      COUNT(DISTINCT COALESCE(NULLIF(requested_params->>'page', ''), '1'))
        FILTER (WHERE NOT (requested_params ? 'league'))::int AS global_pages_distinct,
      MAX(COALESCE(NULLIF(requested_params->>'page', ''), '1')::int)
        FILTER (WHERE NOT (requested_params ? 'league')) AS global_max_page,
      SUM(COALESCE(results, 0)) FILTER (WHERE NOT (requested_params ? 'league'))::bigint AS global_results_sum
    FROM raw.api_responses
    WHERE endpoint = '/fixtures'
      AND fetched_at >= NOW() - make_interval(mins => %s)
      AND (requested_params ? 'date')
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
      (SELECT COUNT(*) FROM core.injuries) AS core_injuries,
      (SELECT COUNT(*) FROM core.fixture_players) AS core_fixture_players,
      (SELECT COUNT(*) FROM core.fixture_events) AS core_fixture_events,
      (SELECT COUNT(*) FROM core.fixture_statistics) AS core_fixture_statistics,
      (SELECT COUNT(*) FROM core.fixture_lineups) AS core_fixture_lineups,
      (SELECT COUNT(*) FROM core.standings) AS core_standings,
      (SELECT COUNT(*) FROM core.top_scorers) AS core_top_scorers,
      (SELECT COUNT(*) FROM core.team_statistics) AS core_team_statistics,
      (SELECT MAX(fetched_at) FROM raw.api_responses) AS raw_last_fetched_at,
      (SELECT MAX(updated_at) FROM core.fixtures) AS core_fixtures_last_updated_at
"""

# Injuries query (read-only)
# Note: `filters` is inserted as a fixed safe fragment assembled from known clauses.
INJURIES_QUERY = """
    SELECT
      i.league_id,
      i.season,
      i.team_id,
      i.player_id,
      i.player_name,
      i.team_name,
      i.type,
      i.reason,
      i.severity,
      i.date,
      i.updated_at
    FROM core.injuries i
    WHERE 1=1
    {filters}
    ORDER BY i.updated_at DESC NULLS LAST
    LIMIT %s
"""

FIXTURE_DETAIL_STATUS_QUERY = """
    SELECT
      f.id AS fixture_id,
      f.league_id,
      f.season,
      f.date AS fixture_date,
      f.status_short,

      EXISTS (SELECT 1 FROM core.fixture_players p WHERE p.fixture_id = f.id) AS has_players,
      EXISTS (SELECT 1 FROM core.fixture_events e WHERE e.fixture_id = f.id) AS has_events,
      EXISTS (SELECT 1 FROM core.fixture_statistics s WHERE s.fixture_id = f.id) AS has_statistics,
      EXISTS (SELECT 1 FROM core.fixture_lineups l WHERE l.fixture_id = f.id) AS has_lineups,

      (SELECT MAX(r.fetched_at) FROM raw.api_responses r WHERE r.endpoint='/fixtures/players' AND (r.requested_params->>'fixture')::bigint=f.id) AS last_players_fetch,
      (SELECT MAX(r.fetched_at) FROM raw.api_responses r WHERE r.endpoint='/fixtures/events' AND (r.requested_params->>'fixture')::bigint=f.id) AS last_events_fetch,
      (SELECT MAX(r.fetched_at) FROM raw.api_responses r WHERE r.endpoint='/fixtures/statistics' AND (r.requested_params->>'fixture')::bigint=f.id) AS last_statistics_fetch,
      (SELECT MAX(r.fetched_at) FROM raw.api_responses r WHERE r.endpoint='/fixtures/lineups' AND (r.requested_params->>'fixture')::bigint=f.id) AS last_lineups_fetch
    FROM core.fixtures f
    WHERE f.id = %s
"""

FIXTURE_PLAYERS_QUERY = """
    SELECT
      fixture_id,
      team_id,
      player_id,
      player_name,
      statistics,
      updated_at
    FROM core.fixture_players
    WHERE fixture_id = %s
    {team_filter}
    ORDER BY team_id NULLS LAST, player_name NULLS LAST
    LIMIT %s
"""

FIXTURE_EVENTS_QUERY = """
    SELECT
      fixture_id,
      time_elapsed,
      time_extra,
      team_id,
      player_id,
      assist_id,
      type,
      detail,
      comments,
      updated_at
    FROM core.fixture_events
    WHERE fixture_id = %s
    ORDER BY time_elapsed NULLS LAST, time_extra NULLS LAST, updated_at ASC
    LIMIT %s
"""

FIXTURE_STATISTICS_QUERY = """
    SELECT
      fixture_id,
      team_id,
      statistics,
      updated_at
    FROM core.fixture_statistics
    WHERE fixture_id = %s
    ORDER BY team_id NULLS LAST
"""

FIXTURE_LINEUPS_QUERY = """
    SELECT
      fixture_id,
      team_id,
      formation,
      start_xi,
      substitutes,
      coach,
      colors,
      updated_at
    FROM core.fixture_lineups
    WHERE fixture_id = %s
    ORDER BY team_id NULLS LAST
"""

FIXTURE_DETAILS_SNAPSHOT_QUERY = """
    SELECT
      fixture_id,
      events,
      lineups,
      statistics,
      players,
      updated_at
    FROM core.fixture_details
    WHERE fixture_id = %s
"""


# Stale live fixtures: status looks live but updated_at is older than a threshold.
STALE_LIVE_FIXTURES_QUERY = """
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
    JOIN core.teams th ON th.id = f.home_team_id
    JOIN core.teams ta ON ta.id = f.away_team_id
    WHERE f.status_short = ANY(%s)
      AND f.updated_at < NOW() - make_interval(mins => %s)
    ORDER BY f.updated_at ASC
    LIMIT %s
"""


# --- Ops / monitoring helpers ---

BACKFILL_PROGRESS_SUMMARY_QUERY = """
    SELECT
      job_id,
      COUNT(*)::int AS total_tasks,
      SUM(CASE WHEN completed THEN 1 ELSE 0 END)::int AS completed_tasks,
      SUM(CASE WHEN NOT completed THEN 1 ELSE 0 END)::int AS pending_tasks,
      MAX(updated_at) AS last_updated_at
    FROM core.backfill_progress
    WHERE 1=1
      AND (%s::text IS NULL OR job_id = %s::text)
      AND (%s::int IS NULL OR season = %s::int)
    GROUP BY job_id
    ORDER BY job_id ASC
"""

BACKFILL_PROGRESS_LIST_QUERY = """
    SELECT
      job_id,
      league_id,
      season,
      next_page,
      completed,
      last_error,
      last_run_at,
      updated_at
    FROM core.backfill_progress
    WHERE 1=1
      AND (%s::text IS NULL OR job_id = %s::text)
      AND (%s::int IS NULL OR season = %s::int)
      AND (%s::bool IS TRUE OR completed = FALSE)
    ORDER BY
      completed ASC,
      updated_at DESC NULLS LAST,
      league_id ASC,
      season DESC
    LIMIT %s
"""

RAW_ERROR_SUMMARY_QUERY = """
    SELECT
      COUNT(*)::int AS total_requests,
      SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END)::int AS ok_2xx,
      SUM(CASE WHEN status_code BETWEEN 400 AND 499 THEN 1 ELSE 0 END)::int AS err_4xx,
      SUM(CASE WHEN status_code BETWEEN 500 AND 599 THEN 1 ELSE 0 END)::int AS err_5xx,
      -- API-Football envelope "errors" may be [] OR {} depending on the failure shape.
      -- Guard jsonb_array_length to avoid "array length of a non-array".
      SUM(
        CASE
          WHEN errors IS NULL THEN 0
          WHEN jsonb_typeof(errors) = 'array' THEN (CASE WHEN jsonb_array_length(errors) > 0 THEN 1 ELSE 0 END)
          WHEN jsonb_typeof(errors) = 'object' THEN (CASE WHEN errors <> '{}'::jsonb THEN 1 ELSE 0 END)
          ELSE 0
        END
      )::int AS envelope_errors,
      MAX(fetched_at) AS last_fetched_at
    FROM raw.api_responses
    WHERE fetched_at >= NOW() - make_interval(mins => %s)
      AND (%s::text IS NULL OR endpoint = %s::text)
"""

RAW_ERRORS_BY_ENDPOINT_QUERY = """
    SELECT
      endpoint,
      COUNT(*)::int AS total_requests,
      SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END)::int AS ok_2xx,
      SUM(CASE WHEN status_code NOT BETWEEN 200 AND 299 THEN 1 ELSE 0 END)::int AS non_2xx,
      -- Same envelope error guard as summary query.
      SUM(
        CASE
          WHEN errors IS NULL THEN 0
          WHEN jsonb_typeof(errors) = 'array' THEN (CASE WHEN jsonb_array_length(errors) > 0 THEN 1 ELSE 0 END)
          WHEN jsonb_typeof(errors) = 'object' THEN (CASE WHEN errors <> '{}'::jsonb THEN 1 ELSE 0 END)
          ELSE 0
        END
      )::int AS envelope_errors,
      MAX(fetched_at) AS last_fetched_at
    FROM raw.api_responses
    WHERE fetched_at >= NOW() - make_interval(mins => %s)
      AND (%s::text IS NULL OR endpoint = %s::text)
    GROUP BY endpoint
    ORDER BY non_2xx DESC, envelope_errors DESC, total_requests DESC
    LIMIT %s
"""


RAW_ERROR_SAMPLES_QUERY = """
    SELECT
      id,
      endpoint,
      requested_params,
      status_code,
      errors,
      results,
      fetched_at
    FROM raw.api_responses
    WHERE fetched_at >= NOW() - make_interval(mins => %s)
      AND (%s::text IS NULL OR endpoint = %s::text)
      AND (
        (errors IS NOT NULL AND jsonb_typeof(errors) = 'array' AND jsonb_array_length(errors) > 0)
        OR (errors IS NOT NULL AND jsonb_typeof(errors) = 'object' AND errors <> '{}'::jsonb)
      )
    ORDER BY fetched_at DESC
    LIMIT %s
"""


