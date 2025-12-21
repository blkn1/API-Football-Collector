## MCP Integration (Phase 4)

This project exposes a **READ-ONLY** MCP server so an AI client (e.g. Claude Desktop) can query:
- Coverage status (`mart.coverage_status`)
- Rate limit status (best-effort from `raw.api_responses.response_headers`)
- Core data (`core.fixtures`, `core.standings`, `core.teams`, `core.leagues`)
- Operational status (last sync time, DB stats, best-effort job status from config + logs)

### Files
- `src/mcp/server.py`: MCP stdio server and tool implementations
- `src/mcp/queries.py`: SQL templates (parameterized, read-only)

### Read-only rule
All tools execute **SELECT-only** SQL. No writes, no refreshes, no schema changes.

### Claude Desktop configuration
Add/merge this into your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "api-football": {
      "command": "python",
      "args": [
        "/home/ybc/Desktop/api-football/src/mcp/server.py"
      ],
      "env": {
        "DATABASE_URL": "postgresql://postgres:password@localhost:5432/api_football",
        "COLLECTOR_LOG_FILE": "/home/ybc/Desktop/api-football/logs/collector.jsonl"
      }
    }
  }
}
```

### Available tools
- `get_coverage_status(league_id=None, season=None)` → list per league+endpoint coverage rows
- `get_coverage_summary(season=None)` → aggregated overview for a season
- `get_rate_limit_status()` → quota remaining (best-effort)
- `get_last_sync_time(endpoint)` → last RAW fetch time for an endpoint
- `query_fixtures(league_id=None, date=None, status=None, limit=10)` → fixtures with team names
- `query_standings(league_id, season)` → standings table for league+season
- `query_teams(league_id=None, search=None, limit=20)` → teams (optional search; league filter is best-effort)
- `get_league_info(league_id)` → league metadata
- `get_database_stats()` → record counts + last activity timestamps
- `list_tracked_leagues()` → config-driven tracked leagues (from `config/jobs/daily.yaml`)
- `get_job_status(job_name=None)` → best-effort status from configs + logs

### Example AI prompts
- “What’s the coverage status for league 39?”
- “Show me today’s fixtures for league 78.”
- “What’s my current API quota?”
- “When was the last /standings sync?”


