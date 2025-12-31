from __future__ import annotations

"""
Pydantic schemas for MCP tool IO.

Goal:
- Make MCP tool outputs consistent and schema-validated (OpenAPI-like robustness).
- Keep forward compatibility by allowing extra fields and using enum+fallback patterns.
"""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MCPModel(BaseModel):
    """Base model: allow extra keys for forward-compatibility."""

    model_config = ConfigDict(extra="allow")


class ErrorEnvelope(MCPModel):
    ok: Literal[False] = False
    error: str
    ts_utc: str
    details: Any | None = None


class OkEnvelope(MCPModel):
    ok: Literal[True] = True
    ts_utc: str


# --- Common “enum + fallback” types (accept any string, but document known values) ---

KnownStatusShort = Literal[
    "TBD",
    "NS",
    "1H",
    "HT",
    "2H",
    "ET",
    "BT",
    "P",
    "FT",
    "AET",
    "PEN",
    "SUSP",
    "INT",
    "PST",
    "CANC",
    "ABD",
    "AWD",
    "WO",
]


class FixtureStatusShort(MCPModel):
    """
    Status short code.
    We intentionally accept arbitrary strings, but we keep a documented set of known values.
    """

    value: str = Field(
        ...,
        description="Known values: TBD, NS, 1H, HT, 2H, ET, BT, P, FT, AET, PEN, SUSP, INT, PST, CANC, ABD, AWD, WO (plus forward-compatible unknown strings).",
    )


class RateLimitStatus(OkEnvelope):
    source: str
    daily_remaining: int | None = None
    minute_remaining: int | None = None
    observed_at_utc: str | None = None


class LiveLoopStatus(OkEnvelope):
    window: dict[str, Any]
    running: bool
    requests: int
    last_fetched_at_utc: str | None = None


class DailyFixturesByDateStatus(OkEnvelope):
    window: dict[str, Any]
    running: bool
    requests: int
    global_requests: int
    pages_fetched: int
    max_page: int | None
    results_sum: int
    last_fetched_at_utc: str | None = None


class LastSyncTime(OkEnvelope):
    endpoint: str
    last_fetched_at_utc: str | None = None


class CoverageRow(MCPModel):
    league: str
    league_type: str | None = None
    league_id: int
    season: int
    endpoint: str
    count_coverage: float | None = None
    freshness_coverage: float | None = None
    pipeline_coverage: float | None = None
    overall_coverage: float | None = None
    last_update_utc: str | None = None
    lag_minutes: int | None = None
    calculated_at_utc: str | None = None
    flags: Any | None = None
    # Scope-policy annotations (out-of-scope != missing)
    in_scope: bool | None = None
    scope_reason: str | None = None
    scope_policy_version: int | None = None


class CoverageStatus(OkEnvelope):
    season: int
    coverage: list[CoverageRow]


class CoverageSummaryRow(MCPModel):
    rows: int
    leagues: int
    endpoints: int
    avg_overall_coverage: float | None = None
    last_calculated_at_utc: str | None = None


class CoverageSummary(OkEnvelope):
    season: int
    summary: CoverageSummaryRow | None = None


class ScopeEndpointDecision(MCPModel):
    endpoint: str
    in_scope: bool
    reason: str
    policy_version: int


class ScopePolicyResponse(OkEnvelope):
    league_id: int
    season: int
    league_type: str | None = None
    decisions: list[ScopeEndpointDecision]


class FixtureRow(MCPModel):
    id: int
    league_id: int
    season: int | None = None
    date_utc: str | None = None
    status: str | None = None  # status_short from DB, but keep flexible
    home_team: str | None = None
    away_team: str | None = None
    goals_home: int | None = None
    goals_away: int | None = None
    updated_at_utc: str | None = None


class FixturesQuery(OkEnvelope):
    items: list[FixtureRow]


class StaleLiveFixturesStatus(OkEnvelope):
    threshold_minutes: int
    tracked_only: bool
    scope_source: str
    stale_count: int
    ignored_untracked: int
    fixtures: list[FixtureRow]


class StaleScheduledFixturesStatus(OkEnvelope):
    threshold_minutes: int
    lookback_days: int
    tracked_only: bool
    stale_count: int
    ignored_untracked: int
    fixtures: list[FixtureRow]


class StaleFixtureRow(MCPModel):
    id: int
    league_id: int
    league_name: str | None = None
    season: int | None = None
    status_short: str
    status_long: str | None = None
    date_utc: str | None = None
    updated_at_utc: str | None = None
    hours_since_date_utc: float | None = None
    hours_since_updated: float | None = None
    is_tracked: bool


class StaleFixturesReport(OkEnvelope):
    threshold_hours: int
    safety_lag_hours: int
    league_filter: int | None = None
    stale_count: int
    fixtures: list[StaleFixtureRow]


class AutoFinishStats(OkEnvelope):
    window_hours: int
    league_filter: int | None = None
    total_auto_finished: int
    unique_leagues_affected: int
    is_tracked_league: bool | None = None
    hourly_stats: list[dict[str, Any]]


class StandingsRow(MCPModel):
    league_id: int
    season: int
    team_id: int
    team: str | None = None
    rank: int | None = None
    points: int | None = None
    goals_diff: int | None = None
    goals_for: int | None = None
    goals_against: int | None = None
    form: str | None = None
    status: str | None = None
    description: str | None = None
    group: str | None = None
    updated_at_utc: str | None = None


class StandingsQuery(OkEnvelope):
    items: list[StandingsRow]


class TeamRow(MCPModel):
    id: int
    name: str
    code: str | None = None
    country: str | None = None
    founded: int | None = None
    national: bool | None = None
    logo: str | None = None
    venue_id: int | None = None
    updated_at_utc: str | None = None


class TeamsQuery(OkEnvelope):
    items: list[TeamRow]


class LeagueInfo(MCPModel):
    id: int
    name: str | None = None
    type: str | None = None
    logo: str | None = None
    country_name: str | None = None
    country_code: str | None = None
    country_flag: str | None = None
    updated_at_utc: str | None = None


class LeagueInfoResponse(OkEnvelope):
    league: LeagueInfo | None = None


class DatabaseStats(MCPModel):
    raw_api_responses: int
    core_leagues: int
    core_teams: int
    core_venues: int
    core_fixtures: int
    core_fixture_details: int
    core_injuries: int
    core_fixture_players: int
    core_fixture_events: int
    core_fixture_statistics: int
    core_fixture_lineups: int
    core_standings: int
    core_top_scorers: int
    core_team_statistics: int
    raw_last_fetched_at_utc: str | None = None
    core_fixtures_last_updated_at_utc: str | None = None


class DatabaseStatsResponse(OkEnvelope):
    stats: DatabaseStats | None = None


class InjuryRow(MCPModel):
    league_id: int
    season: int
    team_id: int | None = None
    player_id: int | None = None
    player_name: str | None = None
    team_name: str | None = None
    type: str | None = None
    reason: str | None = None
    severity: str | None = None
    date: str | None = None
    updated_at_utc: str | None = None


class InjuriesQuery(OkEnvelope):
    items: list[InjuryRow]


class FixtureDetailStatus(MCPModel):
    fixture_id: int
    league_id: int
    season: int | None = None
    date_utc: str | None = None
    status_short: str | None = None
    has_players: bool
    has_events: bool
    has_statistics: bool
    has_lineups: bool
    last_players_fetch_utc: str | None = None
    last_events_fetch_utc: str | None = None
    last_statistics_fetch_utc: str | None = None
    last_lineups_fetch_utc: str | None = None


class FixtureDetailStatusResponse(OkEnvelope):
    fixture: FixtureDetailStatus | None = None


class FixturePlayerRow(MCPModel):
    fixture_id: int
    team_id: int | None = None
    player_id: int | None = None
    player_name: str | None = None
    statistics: Any | None = None
    updated_at_utc: str | None = None


class FixturePlayersQuery(OkEnvelope):
    items: list[FixturePlayerRow]


class FixtureEventRow(MCPModel):
    fixture_id: int
    time_elapsed: int | None = None
    time_extra: int | None = None
    team_id: int | None = None
    player_id: int | None = None
    assist_id: int | None = None
    type: str | None = None
    detail: str | None = None
    comments: str | None = None
    updated_at_utc: str | None = None


class FixtureEventsQuery(OkEnvelope):
    items: list[FixtureEventRow]


class FixtureStatisticsRow(MCPModel):
    fixture_id: int
    team_id: int | None = None
    statistics: Any | None = None
    updated_at_utc: str | None = None


class FixtureStatisticsQuery(OkEnvelope):
    items: list[FixtureStatisticsRow]


class FixtureLineupRow(MCPModel):
    fixture_id: int
    team_id: int | None = None
    formation: str | None = None
    start_xi: Any | None = None
    substitutes: Any | None = None
    coach: Any | None = None
    colors: Any | None = None
    updated_at_utc: str | None = None


class FixtureLineupsQuery(OkEnvelope):
    items: list[FixtureLineupRow]


class TrackedLeagueRow(MCPModel):
    id: int
    name: str | None = None


class LeagueOverrideRow(MCPModel):
    source: str
    league_id: int
    season: int | None = None


class ConfiguredLeagueUnionRow(MCPModel):
    league_id: int
    name: str | None = None
    source: str | None = None
    season: int | None = None


class TrackedLeaguesResponse(OkEnvelope):
    tracked_leagues: list[TrackedLeagueRow]
    league_overrides: list[LeagueOverrideRow]
    configured_leagues_union: list[ConfiguredLeagueUnionRow]


class JobStatusRow(MCPModel):
    job_id: str | None = None
    job_name: str | None = None
    enabled: bool | None = None
    endpoint: str | None = None
    type: str | None = None
    interval: Any | None = None
    config_file: str | None = None
    status: str | None = None
    last_event: str | None = None
    last_event_ts_utc: str | None = None
    last_seen_at_utc: str | None = None
    last_seen_source: str | None = None
    last_raw_fetched_at_utc: str | None = None
    last_raw_endpoints: Any | None = None


class JobStatusResponse(OkEnvelope):
    jobs: list[JobStatusRow]
    log_file: str | None = None


class BackfillSummaryRow(MCPModel):
    job_id: str
    total_tasks: int | None = None
    completed_tasks: int | None = None
    pending_tasks: int | None = None
    last_updated_at_utc: str | None = None


class BackfillTaskRow(MCPModel):
    job_id: str
    league_id: int | None = None
    season: int | None = None
    next_page: int | None = None
    completed: bool
    last_error: str | None = None
    last_run_at_utc: str | None = None
    updated_at_utc: str | None = None


class BackfillProgressResponse(OkEnvelope):
    filters: dict[str, Any]
    summaries: list[BackfillSummaryRow]
    tasks: list[BackfillTaskRow]


class StandingsRefreshProgress(MCPModel):
    cursor: int | None = None
    total_pairs: int | None = None
    last_run_at_utc: str | None = None
    last_error: str | None = None
    lap_count: int | None = None
    last_full_pass_at_utc: str | None = None
    updated_at_utc: str | None = None


class StandingsRefreshProgressResponse(OkEnvelope):
    job_id: str
    exists: bool
    progress: StandingsRefreshProgress | None = None


class RawErrorSummaryRow(MCPModel):
    total_requests: int | None = None
    ok_2xx: int | None = None
    err_4xx: int | None = None
    err_5xx: int | None = None
    envelope_errors: int | None = None
    last_fetched_at_utc: str | None = None


class RawErrorsByEndpointRow(MCPModel):
    endpoint: str
    total_requests: int | None = None
    ok_2xx: int | None = None
    non_2xx: int | None = None
    envelope_errors: int | None = None
    last_fetched_at_utc: str | None = None


class RawErrorSummaryResponse(OkEnvelope):
    window: dict[str, Any]
    filters: dict[str, Any]
    summary: RawErrorSummaryRow | None = None
    by_endpoint: list[RawErrorsByEndpointRow]


class RawErrorSampleRow(MCPModel):
    id: int | None = None
    endpoint: str | None = None
    requested_params: Any | None = None
    status_code: int | None = None
    errors: Any | None = None
    results: int | None = None
    fetched_at_utc: str | None = None


class RawErrorSamplesResponse(OkEnvelope):
    window: dict[str, Any]
    filters: dict[str, Any]
    samples: list[RawErrorSampleRow]


class RecentLogErrorRow(MCPModel):
    job_name: str | None = None
    timestamp: str | None = None
    level: str | None = None
    event: str | None = None
    raw: Any | None = None


class RecentLogErrorsResponse(OkEnvelope):
    job_name: str | None = None
    log_file: str | None = None
    errors: list[RecentLogErrorRow]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


