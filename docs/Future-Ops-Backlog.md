# Future Ops Backlog (not implemented yet)

This file is a reminder/backlog for production ops improvements we agreed to do later.
Nothing here is implemented unless explicitly referenced by code/PRs.

## Goal

Expose a single “ops/health” view for monitoring and connect it to n8n for alerting.

## MCP: season rollover candidates tool

- **New MCP tool**: `get_season_rollover_candidates()`
  - Reads `config/jobs/daily.yaml` tracked leagues.
  - Detects which tracked competitions have the **next season available** in API-Football.
  - Returns actionable output:
    - `league_id`, `name`, `current_season`, `next_season_available`
    - `action_file`: `config/jobs/daily.yaml`
    - `action_yaml_snippet`: exact YAML snippet showing what to change

## Read API: `/ops/health` endpoint

- **Add endpoint**: `GET /ops/health`
- **Single response should include**:
  - `rate_limit_status`
  - `raw_error_summary(60/1440)`
  - `database_stats`
  - `live_loop_status(5)`
  - `daily_fixtures_by_date_status(180)`
  - `backfill_progress_summary`
  - `season_rollover_candidates` (via MCP logic or direct implementation)
  - `coverage_summary_current_season` (optional)

## n8n workflow

- **Cron**: every 5 minutes
- **HTTP Request**: `GET /ops/health`
- **IF conditions (examples)**:
  - `raw_error_summary.err_4xx > 0`
  - `raw_error_summary.err_5xx > 0`
  - `live_loop_status.running == false`
  - `daily_fixtures_by_date_status.running == false`
  - `season_rollover_candidates.found > 0`
- **Actions**:
  - Slack / Telegram / Email alerts


